"""User-entered what-if states and alternate timelines that hold later plays as played.

An edit replaces the state that followed a play. Its drive then ends as the user declares,
later drives keep their recorded states with shifted scores, and each later snap is re-scored
by the published WP model. This is hindsight accounting, not a forecast of the changed game.
Scenarios never change source rows and never reach the tweet queue.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, model_validator

from refimpact.models import MODEL_ID, Scorer
from refimpact.penalties import finite
from refimpact.replay import second_half_receivers, state_from_row
from refimpact.schema import Record, State
from refimpact.storage import Database

Team = Field(default=None, pattern=r"^[A-Z]{2,3}$")
DRIVE_POINTS = {"td_xp": 7, "td_2pt": 8, "fg": 3, "none": 0}
NOTE = (
    "Model estimate; ruling correctness not assessed. Later plays held as played: a changed "
    "score would change later play-calling, field position and clock, so the alternate line is "
    "hindsight accounting, not a forecast. Win-chance swings are not added together."
)


class StateChanges(Record):
    """Only the fields the user changed; everything else keeps the prefilled state."""

    possession: str | None = Team
    down: int | None = Field(default=None, ge=1, le=4)
    ydstogo: float | None = Field(default=None, gt=0, le=100)
    yardline_100: float | None = Field(default=None, gt=0, lt=100)
    qtr: int | None = Field(default=None, ge=1, le=4)
    quarter_seconds: float | None = Field(default=None, ge=0, le=900)
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    home_timeouts: int | None = Field(default=None, ge=0, le=3)
    away_timeouts: int | None = Field(default=None, ge=0, le=3)


class ScenarioEdit(Record):
    play_id: str = Field(pattern=r"^[\w.-]{1,64}$")
    changes: StateChanges = Field(default_factory=StateChanges)
    drive_result: Literal["td_xp", "td_2pt", "fg", "none"] | None = None


class ScenarioRequest(Record):
    game_id: str = Field(pattern=r"^\d{4}_\d{2}_[A-Z]{2,3}_[A-Z]{2,3}$")
    edits: list[ScenarioEdit] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_plays(self):
        plays = [edit.play_id for edit in self.edits]
        if len(set(plays)) != len(plays):
            raise ValueError("Each play can be edited once per scenario")
        return self


class ScenarioSave(ScenarioRequest):
    label: str = Field(min_length=1, max_length=120)
    kind: Literal["flag", "no_call", "what_if"] = "what_if"
    disputed: bool = False
    source_url: HttpUrl | None = None
    assumptions: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def disputed_needs_source(self):
        if self.disputed and self.source_url is None:
            raise ValueError("A disputed call needs a source link")
        return self


def error_text(exc: ValidationError) -> str:
    return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())


def changed(state: State, **changes) -> State:
    try:
        return State.model_validate(state.model_dump() | changes)
    except ValidationError as exc:
        raise ValueError(error_text(exc)) from None


def ordered(rows: list[dict]) -> list[dict]:
    key = "order_sequence" if all(finite(r.get("order_sequence")) for r in rows) else "play_id"
    return sorted(rows, key=lambda r: float(r[key]))


def play_id(row: dict) -> str:
    return str(int(float(row["play_id"])))


def row_state(row: dict, receiver: str | None) -> State | None:
    """The pre-snap state of a regulation scrimmage row; other rows have no valid state."""
    try:
        return state_from_row(row, receiver)
    except (ValueError, TypeError, KeyError):
        return None


def first_half(row: dict) -> bool:
    return row.get("qtr", 5) <= 2


def default_state(rows: list[dict], index: int, receiver: str | None) -> State | None:
    """The state recorded after a play: next snap in the same half, else the play's own state."""
    row = rows[index]
    if not finite(row.get("qtr")) or row["qtr"] > 4:
        return None
    for later in rows[index + 1 :]:
        if not finite(later.get("qtr")) or first_half(later) != first_half(row):
            break
        state = row_state(later, receiver)
        if state is not None:
            return state
    return row_state(row, receiver)


def editable(row: dict) -> bool:
    return row.get("play_type") is not None and finite(row.get("qtr")) and row["qtr"] <= 4


def quarter_seconds(state: State) -> float:
    return state.half_seconds_remaining - (900 if state.qtr in (1, 3) else 0)


def apply_changes(state: State, changes: StateChanges) -> State:
    values = changes.model_dump(exclude_none=True)
    qtr = values.pop("qtr", None)
    seconds = values.pop("quarter_seconds", None)
    if qtr is not None or seconds is not None:
        qtr = qtr if qtr is not None else state.qtr
        seconds = seconds if seconds is not None else quarter_seconds(state)
        half = seconds + (900 if qtr in (1, 3) else 0)
        values |= {
            "qtr": qtr,
            "half_seconds_remaining": half,
            "game_seconds_remaining": half + (1800 if qtr <= 2 else 0),
        }
    return changed(state, **values)


def scenario_plays(rows: list[dict]) -> dict:
    rows = ordered(rows)
    receiver = second_half_receivers(rows).get(rows[0]["game_id"])
    plays = []
    for i, row in enumerate(rows):
        state = default_state(rows, i, receiver) if editable(row) else None
        if state is None:
            continue
        plays.append(
            {
                "play_id": play_id(row),
                "qtr": row["qtr"],
                "time": row.get("time"),
                "possession": row.get("posteam"),
                "down": row.get("down"),
                "ydstogo": row.get("ydstogo"),
                "yardline_100": row.get("yardline_100"),
                "play_type": row.get("play_type"),
                "description": row.get("desc"),
                "flagged": row.get("penalty") == 1 or "penalty" in (row.get("desc") or "").lower(),
                "default": state.model_dump() | {"quarter_seconds": quarter_seconds(state)},
            }
        )
    first = plays[0]["default"] if plays else None
    return {
        "game_id": rows[0]["game_id"],
        "home_team": rows[0]["home_team"],
        "away_team": rows[0]["away_team"],
        "context": {
            key: first[key]
            for key in ("season", "roof", "neutral_site", "second_half_receiver")
        }
        if first
        else None,
        "plays": plays,
    }


def estimate(scorer: Scorer, state: State, home: str) -> dict:
    value = scorer.predict([state], home)[0]
    return {
        "state": state.model_dump() | {"quarter_seconds": quarter_seconds(state)},
        "home_wp": value.wp,
        "home_ep": value.ep,
        "home_value": state.score_diff(home) + value.ep,
    }


def published_wp(row: dict) -> float | None:
    value = row.get("home_wp")
    return float(value) if finite(value) and 0 <= value <= 1 else None


def is_terminal(row: dict) -> bool:
    return row.get("play_type_nfl") == "END_GAME" or (row.get("desc") or "").strip() == "END GAME"


def totals(rows: list[dict]) -> list[tuple[int, int]]:
    """Post-play cumulative scores, carrying the last known value over rows without one."""
    home, away, result = 0, 0, []
    for row in rows:
        if finite(row.get("total_home_score")):
            home = int(row["total_home_score"])
        if finite(row.get("total_away_score")):
            away = int(row["total_away_score"])
        result.append((home, away))
    return result


def drive_end(rows: list[dict], index: int) -> int:
    drive = rows[index].get("fixed_drive")
    end = index
    while drive is not None and end + 1 < len(rows) and rows[end + 1].get("fixed_drive") == drive:
        end += 1
    return end


def scenario_result(home: int, away: int, final: bool) -> str | None:
    if not final:
        return None
    return "home_win" if home > away else "away_win" if away > home else "tie_ot_not_modeled"


def timeline_point(rows: list[dict], scores: list[tuple[int, int]], i: int, status: str) -> dict:
    row = rows[i]
    return {
        "play_id": play_id(row),
        "qtr": row.get("qtr"),
        "time": row.get("time"),
        "possession": row.get("posteam"),
        "description": row.get("desc"),
        "actual_home_wp": published_wp(row),
        "actual_home_score": scores[i][0],
        "actual_away_score": scores[i][1],
        "alternate_home_wp": None,
        "alternate_home_score": None,
        "alternate_away_score": None,
        "status": status,
    }


def evaluate_scenario(rows: list[dict], request: ScenarioRequest, scorer: Scorer) -> dict:
    rows = ordered(rows)
    home, away = rows[0]["home_team"], rows[0]["away_team"]
    receiver = second_half_receivers(rows).get(rows[0]["game_id"])
    positions = {play_id(row): i for i, row in enumerate(rows)}
    edits = {}
    for edit in request.edits:
        index = positions.get(edit.play_id)
        if index is None:
            raise ValueError(f"Play {edit.play_id} is not in {request.game_id}")
        if not editable(rows[index]):
            raise ValueError(f"Play {edit.play_id} has no editable game state")
        edits[index] = edit
    sequence = sorted(edits)
    if any(edits[i].drive_result is None for i in sequence[:-1]):
        raise ValueError("Every edit before the last needs a drive result")
    scores = totals(rows)

    home_shift, away_shift = 0, 0
    timeline, details = [], []
    to_score: dict[int, State] = {}
    branched, stopped = False, False
    i = 0
    while i < len(rows):
        row = rows[i]
        point = timeline_point(
            rows, scores, i, "actual" if not branched else "stopped" if stopped else "modeled"
        )
        timeline.append(point)
        if stopped:
            i += 1
            continue
        if i in edits:
            edit = edits[i]
            state = default_state(rows, i, receiver)
            if state is None:
                raise ValueError(f"Play {edit.play_id} has no state to edit")
            actual = changed(
                state,
                home_score=state.home_score + home_shift,
                away_score=state.away_score + away_shift,
            )
            alternate = apply_changes(actual, edit.changes)
            if (
                finite(row.get("game_seconds_remaining"))
                and alternate.game_seconds_remaining > row["game_seconds_remaining"]
            ):
                raise ValueError(f"Play {edit.play_id}: the entered clock precedes that play")
            actual_value = estimate(scorer, actual, home)
            alternate_value = estimate(scorer, alternate, home)
            end = drive_end(rows, i) if edit.drive_result else i
            after = published_wp(rows[end + 1]) if end + 1 < len(rows) else None
            detail = {
                "play_id": edit.play_id,
                "qtr": row.get("qtr"),
                "time": row.get("time"),
                "description": row.get("desc"),
                "drive_result": edit.drive_result,
                "actual": actual_value,
                "alternate": alternate_value,
                "wp_swing_pp": 100 * (alternate_value["home_wp"] - actual_value["home_wp"]),
                "points_swing": alternate_value["home_value"] - actual_value["home_value"],
                # What the real game did next, on the real scoreboard, so separate from the call.
                "aftermath_pp": 100 * (after - actual_value["home_wp"])
                if after is not None and edit.drive_result and not branched
                else None,
            }
            details.append(detail)
            branched = True
            point["status"] = "edit"
            point["alternate_home_wp"] = alternate_value["home_wp"]
            point["alternate_home_score"] = alternate.home_score
            point["alternate_away_score"] = alternate.away_score
            if edit.drive_result is None:
                stopped = True
                i += 1
                continue
            points = DRIVE_POINTS[edit.drive_result]
            end_home = alternate.home_score + (points if alternate.possession == home else 0)
            end_away = alternate.away_score + (points if alternate.possession == away else 0)
            home_shift, away_shift = end_home - scores[end][0], end_away - scores[end][1]
            detail["alternate_drive_end_score"] = {"home": end_home, "away": end_away}
            for j in range(i + 1, end + 1):
                if j in edits:
                    raise ValueError(
                        f"Play {edits[j].play_id} is inside the drive replaced by "
                        f"play {edit.play_id}"
                    )
                timeline.append(
                    timeline_point(rows, scores, j, "replaced")
                    | {"alternate_home_score": end_home, "alternate_away_score": end_away}
                )
            i = end + 1
            continue
        if branched:
            if finite(row.get("qtr")) and row["qtr"] > 4:
                # Regulation scores were shifted; overtime plays cannot be re-scored or held.
                stopped = True
                point["status"] = "stopped"
                i += 1
                continue
            point["alternate_home_score"] = scores[i][0] + home_shift
            point["alternate_away_score"] = scores[i][1] + away_shift
            state = row_state(row, receiver)
            if state is not None:
                to_score[i] = changed(
                    state,
                    home_score=state.home_score + home_shift,
                    away_score=state.away_score + away_shift,
                )
        i += 1

    by_play = {p["play_id"]: p for p in timeline}
    indexes = list(to_score)
    if indexes:
        values = scorer.predict([to_score[k] for k in indexes], home)
        for k, value in zip(indexes, values, strict=True):
            by_play[play_id(rows[k])]["alternate_home_wp"] = value.wp

    parity_rows = {i: row_state(row, receiver) for i, row in enumerate(rows)}
    parity_rows = {
        i: s for i, s in parity_rows.items() if s is not None and published_wp(rows[i]) is not None
    }
    errors = (
        [
            abs(value.wp - published_wp(rows[i]))
            for i, value in zip(
                parity_rows,
                scorer.predict(list(parity_rows.values()), home),
                strict=True,
            )
        ]
        if parity_rows
        else []
    )

    regulation = [i for i, row in enumerate(rows) if finite(row.get("qtr")) and row["qtr"] <= 4]
    final_row = any(is_terminal(row) for row in rows)
    last = regulation[-1] if regulation else len(rows) - 1
    alternate_final = None
    if details and details[-1]["drive_result"] is not None:
        final_home, final_away = scores[last][0] + home_shift, scores[last][1] + away_shift
        alternate_final = {
            "home": final_home,
            "away": final_away,
            "result": scenario_result(final_home, final_away, final_row),
        }
        carried = None
        for point in timeline:
            if point["status"] == "modeled":
                if point["alternate_home_wp"] is None:
                    point["alternate_home_wp"] = carried
                    point["carried"] = True
                carried = point["alternate_home_wp"]
            elif point["status"] == "edit":
                carried = point["alternate_home_wp"]
        terminal = next(
            (p for p, row in zip(timeline, rows, strict=True) if is_terminal(row)), None
        )
        if terminal is not None and terminal["status"] == "modeled":
            result = alternate_final["result"]
            terminal["alternate_home_wp"] = (
                1.0 if result == "home_win" else 0.0 if result == "away_win" else None
            )
            terminal["carried"] = False
    return {
        "game_id": request.game_id,
        "home_team": home,
        "away_team": away,
        "model_id": MODEL_ID,
        "edits": details,
        "timeline": timeline,
        "final": {
            "actual": {"home": scores[-1][0], "away": scores[-1][1]},
            "alternate": alternate_final,
        },
        "parity": {
            "rows": len(errors),
            "max_abs_wp_error": max(errors) if errors else None,
        },
        "note": NOTE,
    }


def read_state(state: State, scorer: Scorer) -> dict:
    """A blank what-if: one state's value, with no game or timeline attached."""
    return estimate(scorer, state, state.home_team) | {"model_id": MODEL_ID, "note": NOTE}


def game_rows(database: Database, game_id: str) -> tuple[list[dict], str]:
    """One game's recorded rows from the source version behind the active transformed run."""
    with database.connect() as db:
        active = db.execute(
            "SELECT r.pbp_source_id FROM active_seasons a JOIN runs r USING(run_id) "
            "WHERE a.season=?",
            (int(game_id[:4]),),
        ).fetchone()
        if not active or not active["pbp_source_id"]:
            raise LookupError("No play-by-play is stored for this season")
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM raw_plays WHERE source_id=? AND game_id=?",
                (active["pbp_source_id"], game_id),
            )
        ]
    if not rows:
        raise LookupError("No plays are stored for this game")
    return rows, active["pbp_source_id"]


class ScenarioStore:
    """Saved scenarios; separate from raw_plays and transformed results, which stay read-only."""

    def __init__(self, database: Database):
        self.database = database
        with database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scenarios(
                    scenario_id TEXT PRIMARY KEY, game_id TEXT NOT NULL, label TEXT NOT NULL,
                    kind TEXT NOT NULL, disputed INTEGER NOT NULL, source_url TEXT,
                    assumptions TEXT NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL,
                    pbp_source_id TEXT NOT NULL, model_id TEXT NOT NULL, code_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS scenarios_game ON scenarios(game_id,created_at);
            """)

    def save(self, body: ScenarioSave, result: dict, source_id: str, code: str) -> dict:
        record = {
            "scenario_id": secrets.token_hex(8),
            "game_id": body.game_id,
            "label": body.label,
            "kind": body.kind,
            "disputed": body.disputed,
            "source_url": str(body.source_url) if body.source_url else None,
            "assumptions": body.assumptions,
            "created_at": datetime.now(UTC).isoformat(),
        }
        request = {"game_id": body.game_id, "edits": [e.model_dump() for e in body.edits]}
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO scenarios VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["scenario_id"],
                    body.game_id,
                    body.label,
                    body.kind,
                    int(body.disputed),
                    record["source_url"],
                    json.dumps(body.assumptions),
                    json.dumps(request, allow_nan=False),
                    json.dumps(result, allow_nan=False),
                    source_id,
                    result["model_id"],
                    code,
                    record["created_at"],
                ),
            )
        return record | {"request": request, "result": result, "pbp_source_id": source_id}

    @staticmethod
    def decode(row) -> dict:
        record = dict(row)
        record["disputed"] = bool(record["disputed"])
        for key in ("assumptions", "request", "result"):
            record[key] = json.loads(record[key])
        return record

    def list(self, game_id: str) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM scenarios WHERE game_id=? ORDER BY created_at DESC", (game_id,)
            ).fetchall()
        return [self.decode(row) for row in rows]

    def get(self, scenario_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)
            ).fetchone()
        return self.decode(row) if row else None

    def delete(self, scenario_id: str) -> bool:
        with self.database.connect() as db:
            return db.execute(
                "DELETE FROM scenarios WHERE scenario_id=?", (scenario_id,)
            ).rowcount > 0


def disputed_report(saved: list[dict], game_id: str, source_id: str | None) -> dict:
    """Saved disputed calls, valued at the moment. Correctness stays a human input with a source."""
    calls: dict[str, dict] = {}
    for record in sorted(saved, key=lambda r: r["created_at"]):
        if not record["disputed"]:
            continue
        for position, edit in enumerate(record["result"]["edits"]):
            # A later entry for the same play replaces an earlier one rather than double-counting.
            calls[edit["play_id"]] = {
                "scenario_id": record["scenario_id"],
                "label": record["label"],
                "kind": record["kind"],
                "source_url": record["source_url"],
                "assumptions": record["assumptions"],
                "play_id": edit["play_id"],
                "qtr": edit["qtr"],
                "time": edit["time"],
                "wp_swing_pp": edit["wp_swing_pp"],
                "points_swing": edit["points_swing"],
                "aftermath_pp": edit["aftermath_pp"],
                "stale": record["pbp_source_id"] != source_id,
                # A stacked edit is valued on the scoreboard the earlier edits produced.
                "stacked": position > 0,
            }
    rows = sorted(calls.values(), key=lambda c: float(c["play_id"]))
    latest = next((r["result"] for r in saved), None)
    actual = latest["final"]["actual"] if latest else None
    total = sum(c["points_swing"] for c in rows)
    margin = actual["home"] - actual["away"] if actual else None
    return {
        "game_id": game_id,
        "calls": rows,
        "home_points_if_all_wrong": total if rows else None,
        "actual_home_margin": margin,
        "home_margin_if_all_wrong": margin + total if rows and margin is not None else None,
        "note": "Range runs from none of these calls being wrong (actual margin) to all of them "
        "being wrong. Point values are at-the-moment estimates added as an approximation; "
        "win-chance swings are not added. Sources are the people disputing a call, not proof "
        "that it was wrong. " + NOTE,
    }
