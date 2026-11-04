"""Assemble transformed game results in memory; persistence belongs to storage."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime

import polars as pl

from refimpact.models import MODEL_ID, Scorer
from refimpact.penalties import coverage, took_effect
from refimpact.replay import derive_week

SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
LARGE_SWING_PP = 5
PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"
)


def swing_summary(events: list[dict]) -> dict:
    """The per-flag win-chance view: largest swings first, in pp at the moment of each flag."""
    priced = [e for e in events if took_effect(e) and e["home_wp_swing_pp"] is not None]
    priced.sort(key=lambda e: abs(e["home_wp_swing_pp"]), reverse=True)
    return {
        "swings_over_5pp": sum(abs(e["home_wp_swing_pp"]) >= LARGE_SWING_PP for e in priced),
        "top_swings": [
            {
                "id": e["id"],
                "qtr": e["qtr"],
                "time": e["time"],
                "ruling": " + ".join(f["type"] for f in e["fouls"]),
                "home_wp_swing_pp": e["home_wp_swing_pp"],
                "home_wp_vegas_swing_pp": e["home_wp_vegas_swing_pp"],
                "basis": e["enforcement_wp_basis"],
                "delay_seconds": (e["enforcement_wp_restarts"] or {}).get("delay_seconds"),
                "approximate": bool(e["approximate"]),
            }
            for e in priced[:3]
        ],
    }


def select_current(games: list[dict], as_of: date) -> tuple[int, int]:
    eligible = [g for g in games if g["gameday"] <= as_of.isoformat()]
    if not eligible:
        first = min(games, key=lambda g: g["gameday"])
        return first["season"], first["week"]
    latest = max(eligible, key=lambda g: (g["gameday"], g["season"], g["week"]))
    return latest["season"], latest["week"]


def build_catalog(
    frame: pl.DataFrame, schedule: pl.DataFrame, scorer: Scorer, season: int, as_of: date
) -> dict:
    """Pure transformation: no network access or report/snapshot files."""
    schedule = schedule.filter(
        (pl.col("season") == season) & pl.col("game_type").is_in(["REG", "WC", "DIV", "CON", "SB"])
    )
    if schedule.is_empty():
        raise ValueError(f"No schedule for {season}")
    progression = {}
    # An explicit historical as-of date must not leak subsequent games into the sample.
    frame = frame.filter(
        (pl.col("season") == season) & (pl.col("game_date").cast(pl.String) <= as_of.isoformat())
    )
    source_digest = hashlib.sha256(frame.write_json().encode()).hexdigest()
    all_calls, all_review, all_penalties, all_drives, summaries, production = [], [], [], [], {}, {}
    for week in sorted(frame["week"].unique().to_list()):
        bundle = derive_week(frame, scorer, season, week)
        all_calls.extend(bundle["impacts"])
        all_review.extend(bundle["review"])
        all_penalties.extend(bundle["penalties"])
        all_drives.extend(bundle["drives"])
        production.update(bundle["production"])
        progression.update(bundle["progression"])
        summaries[week] = bundle["summary"]
    calls_by_game, review_by_game = defaultdict(list), defaultdict(list)
    penalties_by_game = defaultdict(list)
    for event in all_penalties:
        penalties_by_game[event["game_id"]].append(event)
    for call in all_calls:
        c = call["comparison"]
        call["id"] = f"{c['game_id']}:{c['play_id']}"
        call["season"], call["week"] = season, int(c["game_id"].split("_")[1])
        call["home_epa_impact"] = call["epa_impact"] * (
            1 if c["reference_team"] == c["before"]["home_team"] else -1
        )
        call["benefited_team"] = (
            c["reference_team"]
            if call["epa_impact"] >= 0
            else (
                c["before"]["away_team"]
                if c["reference_team"] == c["before"]["home_team"]
                else c["before"]["home_team"]
            )
        )
        calls_by_game[c["game_id"]].append(call)
    for row in all_review:
        review_by_game[row["game_id"]].append(row)
    available_ids = set(frame["game_id"].unique().to_list())
    ended = set(frame.filter(pl.col("play_type_nfl") == "END_GAME")["game_id"].unique().to_list())
    games = []
    for row in schedule.sort(["gameday", "gametime", "game_id"]).to_dicts():
        game_id = row["game_id"]
        calls = calls_by_game[game_id]
        events = penalties_by_game[game_id]
        measured = [e["home_epa_impact"] for e in events if e["home_epa_impact"] is not None]
        effective = [e for e in events if took_effect(e)]
        team_production = production.get(game_id, {})
        home_production = team_production.get(row["home_team"], {})
        away_production = team_production.get(row["away_team"], {})
        net_production = (
            home_production["offensive_epa_per_snap"] - away_production["offensive_epa_per_snap"]
            if home_production.get("offensive_epa_per_snap") is not None
            and away_production.get("offensive_epa_per_snap") is not None
            else None
        )
        net_drives = (
            home_production["drive_epa"] - away_production["drive_epa"]
            if home_production.get("drive_epa") is not None
            and away_production.get("drive_epa") is not None
            else None
        )
        final = (
            row["gameday"] <= as_of.isoformat()
            and row["home_score"] is not None
            and row["away_score"] is not None
        )
        game = {
            key: row.get(key)
            for key in [
                "game_id",
                "season",
                "week",
                "gameday",
                "gametime",
                "game_type",
                "away_team",
                "home_team",
                "stadium",
            ]
        }
        game.update(
            {
                "home_score": row["home_score"] if final else None,
                "away_score": row["away_score"] if final else None,
                "status": "final"
                if final
                else ("awaiting_result" if row["gameday"] <= as_of.isoformat() else "scheduled"),
                "pbp_available": game_id in available_ids,
                "pbp_complete": game_id in ended,
                "scored_calls": len(calls),
                "valued_penalties": sum(e["enforcement_epa"] is not None for e in effective),
                "unpriced_penalties": sum(e["enforcement_epa"] is None for e in effective),
                "declined_penalties": len(events) - len(effective),
                **swing_summary(events),
                "penalty_coverage": coverage(events),
                "production": team_production,
                "penalty_benefit": progression.get(game_id, {}).get("penalty_benefit"),
                "win_probability": {
                    key: progression.get(game_id, {}).get(key)
                    for key in [
                        "initial_home_wp",
                        "latest_home_wp",
                        "home_wp_change_from_start_pp",
                    ]
                },
                "net_home_production_epa_per_snap": net_production,
                "net_home_drive_epa": net_drives,
                "review_calls": len(review_by_game[game_id]),
                "penalty_rows": len(events),
                "net_home_epa": sum(measured) if measured else None,
                "largest_call_swing_pp": max(
                    (
                        abs(e["enforcement_wp_swing_pp"])
                        for e in events
                        if e["enforcement_wp_swing_pp"] is not None
                    ),
                    default=None,
                ),
            }
        )
        games.append(game)
    if not games:
        raise ValueError("Schedule contains no supported games")
    default_season, default_week = select_current(games, as_of)
    weeks = []
    for week in sorted({g["week"] for g in games}):
        slate = [g for g in games if g["week"] == week]
        summary = summaries.get(week)
        weeks.append(
            {
                "season": season,
                "week": week,
                "start": min(g["gameday"] for g in slate),
                "end": max(g["gameday"] for g in slate),
                "games": len(slate),
                "final_games": sum(g["status"] == "final" for g in slate),
                "scored_calls": sum(g["scored_calls"] for g in slate),
                "valued_penalties": sum(g["valued_penalties"] for g in slate),
                "penalty_coverage": coverage([e for e in all_penalties if e["week"] == week]),
                "penalty_rows": sum(g["penalty_rows"] for g in slate),
                "review_calls": sum(g["review_calls"] for g in slate),
                "parity": summary["parity"] if summary else None,
            }
        )
    snapshot = {
        "schema_version": 4,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "default_season": default_season,
        "default_week": default_week,
        "model_id": MODEL_ID,
        "mode": "postgame",
        "games": games,
        "weeks": weeks,
        "calls": all_calls,
        "review": all_review,
        "penalties": all_penalties,
        "drives": all_drives,
        "progression": progression,
        "coverage": coverage(all_penalties),
        "sources": {
            "schedule": {
                "url": SCHEDULE_URL,
                "sha256": hashlib.sha256(schedule.write_json().encode()).hexdigest(),
            },
            "play_by_play": {
                "url": PBP_URL.format(season=season),
                "sha256": source_digest,
                "hash_scope": "as-of-filtered dataframe serialized as JSON",
            },
        },
        "week_summaries": {str(k): v for k, v in summaries.items()},
    }
    return snapshot
