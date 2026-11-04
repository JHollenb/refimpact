from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import polars as pl
from pydantic import ValidationError

from refimpact.models import MODEL_ID, Scorer
from refimpact.schema import Comparison, Impact, State

SUPPORTED = {"False Start", "Delay of Game", "Encroachment", "Neutral Zone Infraction"}
UNSAFE_TEXT = re.compile(
    r"declined|offset|no penalty|runoff|run.off|10.second|clock adjust|clock reset", re.I
)


def load_pbp(season: int, path: Path | None = None) -> pl.DataFrame:
    """Read persisted source data; download only when the season is absent."""
    from refimpact.pipeline import ingest_sources
    from refimpact.storage import DEFAULT_DATABASE, Database

    database = Database(DEFAULT_DATABASE)
    ingest_sources(database, season, pbp_path=path, include_schedule=False)
    return database.source_frame("pbp", season)


def second_half_receivers(rows: list[dict]) -> dict[str, str]:
    """Use the explicit input, otherwise the team that kicked the game's first kickoff row.

    A flagged opening kick has a null kickoff_attempt and its re-kick is after 3600 s, so neither
    can identify it. Upstream's first-non-null-defteam rule misreads pregame comment rows.
    """
    result = {}
    for r in rows:
        if r.get("receive_2h_ko") == 1 and r.get("posteam"):
            result[r["game_id"]] = r["posteam"]
        elif r.get("play_type") == "kickoff" and r.get("defteam"):
            result.setdefault(r["game_id"], r["defteam"])
    return result


def state_from_row(r: dict, receiver: str | None) -> State:
    roof = r.get("roof")
    if roof is None:
        raise ValueError("missing roof")
    spread = r.get("spread_line")
    return State(
        season=r["season"],
        home_team=r["home_team"],
        away_team=r["away_team"],
        possession=r["posteam"],
        home_score=r["posteam_score"] if r["posteam"] == r["home_team"] else r["defteam_score"],
        away_score=r["defteam_score"] if r["posteam"] == r["home_team"] else r["posteam_score"],
        qtr=r["qtr"],
        game_seconds_remaining=r["game_seconds_remaining"],
        half_seconds_remaining=r["half_seconds_remaining"],
        down=r["down"],
        ydstogo=r["ydstogo"],
        yardline_100=r["yardline_100"],
        home_timeouts=r["home_timeouts_remaining"],
        away_timeouts=r["away_timeouts_remaining"],
        second_half_receiver=receiver,
        roof=roof,
        neutral_site=r.get("location") == "Neutral",
        home_spread=float(spread)
        if isinstance(spread, (int, float)) and math.isfinite(spread)
        else None,
    )


def reconstruct(r: dict, following: dict | None, receiver: str | None) -> Comparison:
    kind = r.get("penalty_type")
    if kind not in SUPPORTED:
        raise ValueError("unsupported_penalty_type")
    desc = r.get("desc") or ""
    if UNSAFE_TEXT.search(desc) or len(re.findall(r"PENALTY on", desc, re.I)) != 1:
        raise ValueError("multiple_declined_or_clock_adjusted")
    if r.get("play_type") != "no_play" or "No Play" not in desc:
        raise ValueError("not_confirmed_dead_ball")
    if not r.get("penalty_team") or not r.get("penalty_yards"):
        raise ValueError("missing_enforcement")
    before = state_from_row(r, receiver)
    # Last minute can include runoffs that are not consistently structured in PBP.
    if before.half_seconds_remaining <= 60:
        raise ValueError("late_half_clock_requires_manual_comparison")
    offense_penalty = r["penalty_team"] == before.possession
    if r["penalty_team"] not in {before.home_team, before.away_team}:
        raise ValueError("unknown_penalty_team")
    if (kind in {"False Start", "Delay of Game"}) != offense_penalty:
        raise ValueError("unexpected_penalty_team")
    yards = abs(float(r["penalty_yards"]))
    expected_yards = (
        min(5, (100 - before.yardline_100) / 2)
        if offense_penalty
        else min(5, before.yardline_100 / 2)
    )
    if abs(yards - expected_yards) > 0.51:  # recorded half-distance yards may be rounded
        raise ValueError("unexpected_enforcement_distance")
    new_yardline = before.yardline_100 + (yards if offense_penalty else -yards)
    new_togo = before.ydstogo + (yards if offense_penalty else -yards)
    first_down = not offense_penalty and new_togo <= 0
    actual = State.model_validate(
        before.model_dump()
        | {
            "yardline_100": new_yardline,
            "down": 1 if first_down else before.down,
            "ydstogo": min(10, new_yardline) if first_down else new_togo,
        }
    )
    if following is None or following.get("game_id") != r["game_id"]:
        raise ValueError("no_following_state")
    # Check the first following down, including another penalty; never jump over enforcement.
    for key, expected in {
        "posteam": actual.possession,
        "down": actual.down,
        "yardline_100": actual.yardline_100,
        "ydstogo": actual.ydstogo,
    }.items():
        value = following.get(key)
        if value is None or (
            abs(float(value) - expected) > 0.51
            if isinstance(expected, (int, float))
            else value != expected
        ):
            raise ValueError("following_state_does_not_confirm_enforcement")
    next_home = (
        following.get("posteam_score")
        if following.get("posteam") == before.home_team
        else following.get("defteam_score")
    )
    next_away = (
        following.get("posteam_score")
        if following.get("posteam") == before.away_team
        else following.get("defteam_score")
    )
    if next_home != before.home_score or next_away != before.away_score:
        raise ValueError("score_changed_between_states")
    if following.get("qtr") != before.qtr:
        raise ValueError("quarter_boundary_requires_manual_comparison")
    return Comparison(
        game_id=r["game_id"],
        play_id=str(int(r["play_id"])),
        ruling=kind,
        reference_team=before.possession,
        before=before,
        actual=actual,
        alternative=before,
        alternative_label="no flag; same pre-snap state",
        method="dead_ball_enforcement",
        assumptions=[
            "Immediate enforcement effect at the penalty timestamp; no clock runoff.",
            "Alternative keeps the pre-snap state without enforcement; not a foul-free replay.",
            "Estimates consequence of enforcement, not whether the ruling was correct.",
        ],
    )


def derive_week(
    frame: pl.DataFrame,
    scorer: Scorer,
    season: int,
    week: int,
) -> dict:
    from refimpact.penalties import kickoff_start_spots
    from refimpact.tweets import render_tweet

    ordering = ["game_id", "order_sequence" if "order_sequence" in frame.columns else "play_id"]
    # Measured over all the season's rows supplied, not one week's few kickoffs.
    kick_spots = kickoff_start_spots(frame.sort(ordering).to_dicts())
    frame = frame.filter((pl.col("season") == season) & (pl.col("week") == week))
    if frame.is_empty():
        raise ValueError(f"No plays for {season} week {week}")
    frame = frame.sort(ordering)
    rows = frame.to_dicts()
    receivers = second_half_receivers(rows)
    digest = hashlib.sha256(frame.write_json().encode()).hexdigest()
    # Find the next actual down without crossing a game or mutating event.
    following = {}
    next_down = None
    for i in range(len(rows) - 1, -1, -1):
        following[i] = next_down
        r = rows[i]
        if r.get("down") in (1, 2, 3, 4) or r.get("penalty") == 1:
            next_down = r
    reasons = Counter()
    results, review = [], []
    parity = []
    # Check Python inference against published nflfastR pre-snap EP/WP on ordinary downs.
    eligible = [
        r
        for r in rows
        if r.get("play_type") in {"run", "pass"}
        and not r.get("penalty")
        and r.get("qtr", 5) <= 4
        and r.get("ep") is not None
        and r.get("wp") is not None
    ]
    for r in eligible[:: max(1, len(eligible) // 100)][:100]:
        try:
            s = state_from_row(r, receivers.get(r["game_id"]))
        except (ValueError, TypeError, KeyError):
            continue
        p = scorer.predict([s], s.possession)[0]
        vegas = scorer.vegas_wp([s], s.possession)[0]
        parity.append(
            {
                "game_id": r["game_id"],
                "play_id": r["play_id"],
                "ep_error": p.ep - r["ep"],
                "wp_error": p.wp - r["wp"],
                "vegas_wp_error": vegas - r["vegas_wp"]
                if vegas is not None and r.get("vegas_wp") is not None
                else None,
            }
        )
    for i, r in enumerate(rows):
        if r.get("penalty") != 1:
            continue
        try:
            c = reconstruct(r, following[i], receivers.get(r["game_id"]))
            impact = scorer.compare(c)
            text = render_tweet(impact)
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            reason = str(exc) if type(exc) is ValueError else "incomplete_or_invalid_state"
            reasons[reason] += 1
            review.append(
                {
                    "game_id": r["game_id"],
                    "play_id": r["play_id"],
                    "ruling": r.get("penalty_type"),
                    "reason": reason,
                    "description": r.get("desc"),
                }
            )
            continue
        item = impact.model_dump(mode="json") | {"tweet": text}
        results.append(item)
    from refimpact.penalties import build_penalties, coverage
    from refimpact.production import build_production
    from refimpact.progression import build_progression

    penalties = build_penalties(frame, scorer, kick_spots)
    production, drives = build_production(frame, penalties)
    progression = build_progression(frame, penalties, drives)
    summary = {
        "season": season,
        "week": week,
        "plays": len(rows),
        "penalty_rows": len(results) + len(review),
        "scored": len(results),
        "needs_review": len(review),
        "review_reasons": dict(reasons),
        "all_penalty_coverage": coverage(penalties),
        "drives": len(drives),
        "source_sha256": digest,
        "model_id": MODEL_ID,
        "code_sha256": hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest(),
        "parity": {
            "rows": len(parity),
            "max_abs_ep_error": max((abs(p["ep_error"]) for p in parity), default=None),
            "max_abs_wp_error": max((abs(p["wp_error"]) for p in parity), default=None),
            "max_abs_vegas_wp_error": max(
                (abs(p["vegas_wp_error"]) for p in parity if p["vegas_wp_error"] is not None),
                default=None,
            ),
        },
    }
    return {
        "summary": summary,
        "impacts": results,
        "review": review,
        "parity": parity,
        "penalties": penalties,
        "production": production,
        "drives": drives,
        "progression": progression,
    }


def replay(
    frame: pl.DataFrame,
    scorer: Scorer,
    output: Path,
    season: int,
    week: int,
    store=None,
    min_swing: float = 0.0,
) -> dict:
    """Optional standalone audit export; the web pipeline never writes these bundles."""
    bundle_data = derive_week(frame, scorer, season, week)
    summary = bundle_data["summary"]
    results = bundle_data["impacts"]
    parity = bundle_data["parity"]
    digest = summary["source_sha256"]
    output.mkdir(parents=True, exist_ok=True)
    run_id = hashlib.sha256(json.dumps(bundle_data, sort_keys=True).encode()).hexdigest()[:16]
    bundle = output / f"run-{digest[:12]}-{run_id}"
    bundle.mkdir(exist_ok=True)
    for name, value in bundle_data.items():
        if name in {"impacts", "review", "parity", "penalties", "drives"}:
            (bundle / f"{name}.jsonl").write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in value)
            )
        else:
            (bundle / f"{name}.json").write_text(json.dumps(value) + "\n")
    if store is not None:
        if (
            not parity
            or summary["parity"]["max_abs_ep_error"] > 1e-5
            or summary["parity"]["max_abs_wp_error"] > 1e-6
        ):
            raise ValueError(
                f"Published-model parity unresolved; queue unchanged. Reports: {bundle}"
            )
        for item in results:
            impact = Impact.model_validate({k: v for k, v in item.items() if k != "tweet"})
            if abs(impact.wp_swing_pp) >= min_swing:
                store.enqueue(impact, item["tweet"])
    return summary | {"report_directory": str(bundle)}
