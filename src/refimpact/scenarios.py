"""User-entered what-if states and alternate timelines that hold later plays as played.

An edit replaces the state that followed a play. Its drive then ends as the user declares,
later drives keep their recorded states with shifted scores, and each later snap is re-scored
by the published WP model. This is hindsight accounting, not a forecast of the changed game.

A chain leaves that timeline at a chosen node and continues through states the user authors.
Each node is read by the same models with nothing added, and no chance of the path is computed:
the steps are the user's choices. It ends at its last node ("prediction") or rejoins recorded
plays ("rejoin", hindsight). Scenarios never change source rows and never reach the tweet queue.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, field_validator, model_validator

from refimpact.decisions import opportunity
from refimpact.models import MODEL_ID, Scorer
from refimpact.penalties import after_score, finite, kickoff_start_spots, opponent
from refimpact.replay import second_half_receivers, state_from_row
from refimpact.schema import Record, State
from refimpact.storage import Database

Team = Field(default=None, pattern=r"^[A-Z]{2,3}$")
DRIVE_POINTS = {"td_xp": 7, "td_2pt": 8, "fg": 3, "none": 0}
HELD_NOTE = (
    "Model estimate; ruling correctness not assessed. Later plays held as played: a changed "
    "score would change later play-calling, field position and clock, so the alternate line is "
    "hindsight accounting, not a forecast."
)
NOTE = f"{HELD_NOTE} Win-chance swings are not added together."
CHAIN_NOTE = (
    "Each step is a state you chose, not a forecast of what would occur. A node's win chance is "
    "the same model's reading of that state if the game got there; no chance of reaching it is "
    "computed. Swings between nodes are not added together."
)
OVERTIME = "Overtime is not modeled; end the chain in regulation"
DriveResult = Literal["td_xp", "td_2pt", "fg", "none"]
FORM_FIELDS = (
    "possession",
    "down",
    "ydstogo",
    "yardline_100",
    "qtr",
    "home_score",
    "away_score",
    "home_timeouts",
    "away_timeouts",
)


STATE_RULE_FIELDS = {"possession": "possession", "distance": "ydstogo"}


class ChainError(ValueError):
    """A chain value one field can be blamed for; reported like a request validation error."""

    def __init__(self, loc: tuple, message: str):
        super().__init__(message)
        self.loc = ("chain", *loc)


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
    drive_result: DriveResult | None = None
    # Who the drive result belongs to. Unset means the team with the ball in the entered state,
    # which on a drive's last play is the next team, so the result flags a warning then.
    drive_team: str | None = Team


class ScenarioStep(Record):
    """A full state the user chose after a node, not tied to a recorded play."""

    possession: str = Field(pattern=r"^[A-Z]{2,3}$")
    down: int = Field(ge=1, le=4)
    ydstogo: float = Field(gt=0, le=100)
    yardline_100: float = Field(gt=0, lt=100)
    qtr: int = Field(ge=1)
    quarter_seconds: float = Field(ge=0, le=900)
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    home_timeouts: int = Field(ge=0, le=3)
    away_timeouts: int = Field(ge=0, le=3)
    drive_result: DriveResult | None = None
    # Unset means the team with the ball in this step.
    drive_team: str | None = Team

    @field_validator("qtr")
    @classmethod
    def regulation_only(cls, value: int) -> int:
        if value > 4:
            raise ValueError(OVERTIME)
        return value


class ScenarioChain(Record):
    """Hypothetical steps leaving the alternate timeline at one play, with an explicit ending."""

    from_play_id: str = Field(pattern=r"^[\w.-]{1,64}$")
    steps: list[ScenarioStep] = Field(min_length=1, max_length=24)
    end: Literal["prediction", "rejoin"] = "prediction"
    rejoin_play_id: str | None = Field(default=None, pattern=r"^[\w.-]{1,64}$")

    @model_validator(mode="after")
    def one_ending(self):
        if self.end == "rejoin" and self.rejoin_play_id is None:
            raise ValueError("Rejoining recorded plays needs a play to rejoin at")
        if self.end == "prediction" and self.rejoin_play_id is not None:
            raise ValueError("A prediction ends at its last step; it cannot also rejoin")
        return self


class ScenarioRequest(Record):
    game_id: str = Field(pattern=r"^\d{4}_\d{2}_[A-Z]{2,3}_[A-Z]{2,3}$")
    edits: list[ScenarioEdit] = Field(min_length=1, max_length=12)
    chain: ScenarioChain | None = None

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
        if self.disputed and self.chain is not None:
            raise ValueError(
                "A chain holds your own hypothetical steps, not a disputed call. Save the edit "
                "without the chain to dispute it"
            )
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


def recorded_next(rows: list[dict], index: int, receiver: str | None) -> State | None:
    """The next snap in the same half, if there is one."""
    row = rows[index]
    for later in rows[index + 1 :]:
        if not finite(later.get("qtr")) or first_half(later) != first_half(row):
            return None
        state = row_state(later, receiver)
        if state is not None:
            return state
    return None


def default_state(rows: list[dict], index: int, receiver: str | None) -> State | None:
    """The state recorded after a play: next snap in the same half, else the play's own state."""
    row = rows[index]
    if not finite(row.get("qtr")) or row["qtr"] > 4:
        return None
    return recorded_next(rows, index, receiver) or row_state(row, receiver)


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
                "default_is_pre_play": recorded_next(rows, i, receiver) is None,
            }
        )
    first = plays[0]["default"] if plays else None
    return {
        "game_id": rows[0]["game_id"],
        "home_team": rows[0]["home_team"],
        "away_team": rows[0]["away_team"],
        "context": {
            key: first[key]
            for key in ("season", "roof", "neutral_site", "second_half_receiver", "home_spread")
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
        "home_wp_vegas": scorer.vegas_wp([state], home)[0],
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
        "game_seconds": row.get("game_seconds_remaining"),
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


def form_state(state: State) -> dict:
    """The fields a person enters, in the form's terms."""
    return {key: getattr(state, key) for key in FORM_FIELDS} | {
        "quarter_seconds": quarter_seconds(state)
    }


def score_after(state: State, result: str | None, team: str) -> tuple[int, int]:
    points = DRIVE_POINTS[result] if result else 0
    return (
        state.home_score + (points if team == state.home_team else 0),
        state.away_score + (points if team == state.away_team else 0),
    )


def next_state(state: State, result: str | None, team: str, kick_spot: float | None) -> dict | None:
    """Where the next step's form starts. Nothing here is a forecast; the user can edit it all.

    A drive still going keeps its state. A score hands the opponent the ball at the season's
    measured post-kick spot. No score hands it over at the same spot, with no punt assumed. The
    clock stays put because no elapsed time is modeled.
    """
    if result is None:
        return form_state(state)
    points = DRIVE_POINTS[result]
    if points:
        return form_state(after_score(state, team, points, kick_spot)) if kick_spot else None
    yards = 100 - state.yardline_100
    return form_state(
        changed(
            state,
            possession=opponent(state, state.possession),
            yardline_100=yards,
            down=1,
            ydstogo=min(10, yards),
        )
    )


def hold_row(
    point: dict, row: dict, score: tuple[int, int], shift: tuple[int, int], receiver: str | None
) -> State | None:
    """Put one later row on the shifted scoreboard; returns its state if it has one to score."""
    point["alternate_home_score"] = score[0] + shift[0]
    point["alternate_away_score"] = score[1] + shift[1]
    state = row_state(row, receiver)
    if state is None:
        return None
    return changed(
        state, home_score=state.home_score + shift[0], away_score=state.away_score + shift[1]
    )


def score_held(timeline: list[dict], to_score: dict[int, State], home: str, scorer: Scorer) -> None:
    indexes = list(to_score)
    if indexes:
        values = scorer.predict([to_score[k] for k in indexes], home)
        for k, value in zip(indexes, values, strict=True):
            timeline[k]["alternate_home_wp"] = value.wp
            timeline[k]["alternate_state"] = form_state(to_score[k])


def close_alternate(
    timeline: list[dict], rows: list[dict], scores: list[tuple[int, int]], shift: tuple[int, int]
) -> dict:
    """Carry the alternate line across rows with no state and end it at the decided result."""
    regulation = [i for i, row in enumerate(rows) if finite(row.get("qtr")) and row["qtr"] <= 4]
    last = regulation[-1] if regulation else len(rows) - 1
    overtime = any(finite(row.get("qtr")) and row["qtr"] > 4 for row in rows)
    final_home, final_away = scores[last][0] + shift[0], scores[last][1] + shift[1]
    alternate = {
        "home": final_home,
        "away": final_away,
        "result": scenario_result(final_home, final_away, any(is_terminal(r) for r in rows)),
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
    terminal = next((p for p, row in zip(timeline, rows, strict=True) if is_terminal(row)), None)
    # In an overtime game the last rows stop at regulation; a decided alternate still ends.
    if terminal is not None and (
        terminal["status"] == "modeled" or (overtime and terminal["status"] == "stopped")
    ):
        result = alternate["result"]
        terminal["alternate_home_wp"] = (
            1.0 if result == "home_win" else 0.0 if result == "away_win" else None
        )
        terminal["alternate_home_score"] = final_home
        terminal["alternate_away_score"] = final_away
        terminal["carried"] = False
    return alternate


def chain_node(
    scorer: Scorer,
    state: State,
    previous: dict | None,
    result: str | None,
    team: str,
    kick_spot: float | None,
    **label,
) -> dict:
    """One node: the pinned models' reading of this state, with nothing fitted or added."""
    home = state.home_team
    value = estimate(scorer, state, home)
    vegas, last_vegas = value["home_wp_vegas"], previous and previous["home_wp_vegas"]
    return (
        label
        | value
        | {
            "wp_swing_pp": 100 * (value["home_wp"] - previous["home_wp"]) if previous else None,
            "wp_vegas_swing_pp": 100 * (vegas - last_vegas)
            if vegas is not None and last_vegas is not None
            else None,
            "points_swing": value["home_value"] - previous["home_value"] if previous else None,
            "opportunity": opportunity(scorer, state.possession, {"node": state}, "node", "node")[
                "states"
            ]["node"]
            | {"team": state.possession},
            "drive_result": result,
            "drive_team": team if result else None,
            "score_after": dict(
                zip(("home", "away"), score_after(state, result, team), strict=True)
            ),
            "next_state": next_state(state, result, team, kick_spot),
        }
    )


def clock_text(state: State) -> str:
    seconds = quarter_seconds(state)
    return f"Q{state.qtr} {int(seconds // 60)}:{int(seconds % 60):02d}"


def step_state(source: State, step: ScenarioStep, loc: tuple) -> State:
    if step.qtr in (2, 4) and step.quarter_seconds == 0:
        raise ChainError(
            (*loc, "quarter_seconds"), "No snap can start with no time left in the half"
        )
    values = step.model_dump(exclude={"drive_result", "drive_team"})
    try:
        return apply_changes(source, StateChanges(**values))
    except (ValueError, ValidationError) as exc:
        # The state's own rules name their field in the message.
        field = next((f for key, f in STATE_RULE_FIELDS.items() if key in str(exc)), None)
        raise ChainError((*loc, field) if field else loc, str(exc)) from None


def check_step(previous: State, state: State, floor: tuple[int, int], loc: tuple) -> None:
    """Reject a step the game could not reach from the one before it."""
    if state.game_seconds_remaining > previous.game_seconds_remaining:
        raise ChainError(
            (*loc, "quarter_seconds"),
            f"The clock cannot run backward: {clock_text(state)} comes before "
            f"{clock_text(previous)}",
        )
    if state.qtr < previous.qtr:
        raise ChainError((*loc, "qtr"), f"Q{state.qtr} comes before Q{previous.qtr}")
    for side, name, low in (
        ("home_score", state.home_team, floor[0]),
        ("away_score", state.away_team, floor[1]),
    ):
        if getattr(state, side) < low:
            raise ChainError(
                (*loc, side),
                f"{name}'s score cannot fall below {low}, the previous step plus its drive result",
            )
    # Timeouts come back only at the half.
    if (state.qtr <= 2) == (previous.qtr <= 2):
        for side, name in (
            ("home_timeouts", state.home_team),
            ("away_timeouts", state.away_team),
        ):
            if getattr(state, side) > getattr(previous, side):
                raise ChainError(
                    (*loc, side),
                    f"{name} cannot regain a timeout within a half: it had "
                    f"{getattr(previous, side)}",
                )


def rejoin_options(rows: list[dict], start: int, last: State, receiver: str | None) -> list[dict]:
    """Recorded snaps after the branch that the chain's last clock has not already passed."""
    return [
        {
            "play_id": play_id(row),
            "qtr": row.get("qtr"),
            "time": row.get("time"),
            "possession": row.get("posteam"),
            "description": (row.get("desc") or "")[:70],
        }
        for row in rows[start + 1 :]
        if finite(row.get("qtr"))
        and row["qtr"] <= 4
        and finite(row.get("game_seconds_remaining"))
        and row["game_seconds_remaining"] <= last.game_seconds_remaining
        and row_state(row, receiver) is not None
    ]


def apply_chain(
    result: dict,
    rows: list[dict],
    scores: list[tuple[int, int]],
    chain: ScenarioChain,
    sources: dict[int, tuple[State, str | None, str]],
    last_edit: int,
    receiver: str | None,
    scorer: Scorer,
    kick_spot: float | None,
) -> None:
    """Build the node list and redraw everything after the branch for the chosen ending."""
    timeline, home = result["timeline"], result["home_team"]
    positions = {play_id(row): i for i, row in enumerate(rows)}
    index = positions.get(chain.from_play_id)
    if index is None or index not in sources or index < last_edit:
        raise ChainError(
            ("from_play_id",),
            f"Play {chain.from_play_id} is not a state on the alternate timeline after the last "
            "edit",
        )
    state, drive, team = sources[index]
    point = timeline[index]
    nodes = [
        chain_node(
            scorer, state, None, drive, team, kick_spot, index=0, kind=point["status"], step=None
        )
    ]
    floor = score_after(state, drive, team)
    teams = {result["home_team"], result["away_team"]}
    previous = state
    for k, step in enumerate(chain.steps):
        loc = ("steps", k)
        current = step_state(state, step, loc)
        check_step(previous, current, floor, loc)
        owner = step.drive_team or current.possession
        if owner not in teams:
            raise ChainError((*loc, "drive_team"), f"{owner} is not in this game")
        nodes.append(
            chain_node(
                scorer,
                current,
                nodes[-1],
                step.drive_result,
                owner,
                kick_spot,
                index=k + 1,
                kind="step",
                step=k,
            )
        )
        floor = score_after(current, step.drive_result, owner)
        previous = current

    for later in timeline[index + 1 :]:
        later.update(
            status="branched",
            alternate_home_wp=None,
            alternate_home_score=None,
            alternate_away_score=None,
            alternate_state=None,
        )
        later.pop("carried", None)
    warnings = []
    last = nodes[-1]
    if chain.end == "prediction":
        result["final"]["alternate"] = None
        end = {
            "mode": "prediction",
            "score": last["score_after"],
            "home_wp": last["home_wp"],
            "home_wp_vegas": last["home_wp_vegas"],
            "reads_before_drive_result": last["drive_result"] is not None,
            "label": "Conditional on the steps you chose. No final score is given.",
        }
        result["note"] = CHAIN_NOTE
    else:
        target = positions.get(chain.rejoin_play_id)
        loc = ("rejoin_play_id",)
        if target is None or target <= index:
            raise ChainError(loc, "Rejoin at a recorded play after the chain's start")
        row = rows[target]
        if not finite(row.get("qtr")) or row["qtr"] > 4 or row_state(row, receiver) is None:
            raise ChainError(loc, f"Play {chain.rejoin_play_id} has no regulation state to rejoin")
        if (
            finite(row.get("game_seconds_remaining"))
            and row["game_seconds_remaining"] > previous.game_seconds_remaining
        ):
            raise ChainError(
                loc,
                f"Play {chain.rejoin_play_id} comes before the last step's clock, "
                f"{clock_text(previous)}",
            )
        shift = (floor[0] - scores[target - 1][0], floor[1] - scores[target - 1][1])
        to_score = {}
        for i in range(index + 1, len(rows)):
            if i < target:
                timeline[i].update(
                    status="replaced", alternate_home_score=floor[0], alternate_away_score=floor[1]
                )
            elif finite(rows[i].get("qtr")) and rows[i]["qtr"] > 4:
                timeline[i]["status"] = "stopped"
            else:
                timeline[i]["status"] = "modeled"
                held = hold_row(timeline[i], rows[i], scores[i], shift, receiver)
                if held is not None:
                    to_score[i] = held
        score_held(timeline, to_score, home, scorer)
        result["final"]["alternate"] = close_alternate(timeline, rows, scores, shift) | {
            "mode": "rejoin"
        }
        handed = last["next_state"]
        if handed and handed["possession"] != row.get("posteam"):
            warnings.append(
                f"Your last step hands the ball to {handed['possession']}, but recorded plays "
                f"resume with {row.get('posteam')}."
            )
        end = {
            "mode": "rejoin",
            "rejoin_play_id": chain.rejoin_play_id,
            "score_at_rejoin": {"home": floor[0], "away": floor[1]},
            "label": "Hindsight: recorded plays held from the rejoin on, on a shifted scoreboard.",
        }
        result["note"] = f"{CHAIN_NOTE} {HELD_NOTE}"
    result["chain"] = {
        "from_play_id": chain.from_play_id,
        "end": end,
        "nodes": nodes,
        "kick_spot": kick_spot,
        "rejoin_options": rejoin_options(rows, index, previous, receiver),
        "warnings": warnings,
        "note": CHAIN_NOTE,
    }


def evaluate_scenario(
    rows: list[dict], request: ScenarioRequest, scorer: Scorer, kick_spot: float | None = None
) -> dict:
    rows = ordered(rows)
    home, away = rows[0]["home_team"], rows[0]["away_team"]
    # The API passes the whole season's measured spot; one game's kickoffs are the fallback.
    if kick_spot is None:
        kick_spot = kickoff_start_spots(rows).get(rows[0].get("season"))
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
    sources: dict[int, tuple[State, str | None, str]] = {}
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
            team = edit.drive_team or alternate.possession
            if team not in {home, away}:
                raise ValueError(f"Play {edit.play_id}: {team} is not in this game")
            warnings = []
            if recorded_next(rows, i, receiver) is None:
                warnings.append(
                    "No later snap in this half, so the recorded state is this play's own "
                    "pre-snap state, not what followed it."
                )
            if edit.drive_result and edit.drive_team is None and team != row.get("posteam"):
                warnings.append(
                    f"The drive result went to {team}, who has the ball next, not "
                    f"{row.get('posteam')}, who ran this play. {team}'s real next drive still "
                    "happens. Choose the team if that is not what you meant."
                )
            detail = {
                "play_id": edit.play_id,
                "qtr": row.get("qtr"),
                "time": row.get("time"),
                "description": row.get("desc"),
                "drive_result": edit.drive_result,
                "actual": actual_value,
                "alternate": alternate_value,
                "drive_team": team if edit.drive_result else None,
                "next_state": next_state(alternate, edit.drive_result, team, kick_spot),
                "warnings": warnings,
                "wp_swing_pp": 100 * (alternate_value["home_wp"] - actual_value["home_wp"]),
                "wp_vegas_swing_pp": 100
                * (alternate_value["home_wp_vegas"] - actual_value["home_wp_vegas"])
                if actual_value["home_wp_vegas"] is not None
                and alternate_value["home_wp_vegas"] is not None
                else None,
                "points_swing": alternate_value["home_value"] - actual_value["home_value"],
                "opportunity": opportunity(
                    scorer,
                    alternate.possession,
                    {"recorded": actual, "yours": alternate},
                    "recorded",
                    "yours",
                ),
                # What the real game did next, on the real scoreboard, so separate from the call.
                "aftermath_pp": 100 * (after - actual_value["home_wp"])
                if after is not None and edit.drive_result and not branched
                else None,
            }
            details.append(detail)
            sources[i] = (alternate, edit.drive_result, team)
            branched = True
            point["status"] = "edit"
            # Both lines read the state after the play here, so the chart gap matches the card.
            point["actual_home_wp_before"] = point["actual_home_wp"]
            point["actual_home_wp"] = scorer.predict([state], home)[0].wp
            point["alternate_home_wp"] = alternate_value["home_wp"]
            point["alternate_home_score"] = alternate.home_score
            point["alternate_away_score"] = alternate.away_score
            if edit.drive_result is None:
                stopped = True
                i += 1
                continue
            points = DRIVE_POINTS[edit.drive_result]
            end_home = alternate.home_score + (points if team == home else 0)
            end_away = alternate.away_score + (points if team == away else 0)
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
            state = hold_row(point, row, scores[i], (home_shift, away_shift), receiver)
            if state is not None:
                to_score[i] = state
                sources[i] = (state, None, state.possession)
        i += 1

    score_held(timeline, to_score, home, scorer)

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
    last = regulation[-1] if regulation else len(rows) - 1
    overtime = any(finite(row.get("qtr")) and row["qtr"] > 4 for row in rows)
    alternate_final = None
    if details and details[-1]["drive_result"] is not None:
        alternate_final = close_alternate(timeline, rows, scores, (home_shift, away_shift))
    result = {
        "game_id": request.game_id,
        "home_team": home,
        "away_team": away,
        "model_id": MODEL_ID,
        "edits": details,
        "timeline": timeline,
        "final": {
            "actual": {"home": scores[-1][0], "away": scores[-1][1]},
            # The alternate is regulation-only, so an overtime game needs the matching actual.
            "actual_regulation": {"home": scores[last][0], "away": scores[last][1]}
            if overtime
            else None,
            "alternate": alternate_final,
        },
        "parity": {
            "rows": len(errors),
            "max_abs_wp_error": max(errors) if errors else None,
        },
        "note": NOTE,
    }
    if request.chain:
        apply_chain(
            result, rows, scores, request.chain, sources, sequence[-1], receiver, scorer, kick_spot
        )
    return result


def read_state(state: State, scorer: Scorer) -> dict:
    """A blank what-if: one state's value, with no game or timeline attached."""
    return estimate(scorer, state, state.home_team) | {"model_id": MODEL_ID, "note": NOTE}


def season_kick_spot(database: Database, source_id: str) -> float | None:
    """The measured post-kick start spot for the season behind one play-by-play source."""
    with database.connect() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT game_id, season, play_type, down, posteam, yardline_100 FROM raw_plays "
                "WHERE source_id=? ORDER BY game_id, order_sequence",
                (source_id,),
            )
        ]
    spots = kickoff_start_spots(rows)
    return next(iter(spots.values()), None)


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
        request = {
            "game_id": body.game_id,
            "edits": [e.model_dump() for e in body.edits],
            "chain": body.chain.model_dump() if body.chain else None,
        }
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
            return (
                db.execute("DELETE FROM scenarios WHERE scenario_id=?", (scenario_id,)).rowcount > 0
            )


def disputed_report(saved: list[dict], game_id: str, source_id: str | None) -> dict:
    """Saved disputed calls, valued at the moment. Correctness stays a human input with a source."""
    calls: dict[str, dict] = {}
    for record in sorted(saved, key=lambda r: r["created_at"]):
        # A chain's steps are the user's own hypotheticals, not calls anyone disputes.
        if not record["disputed"] or record.get("request", {}).get("chain"):
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
    return {
        "game_id": game_id,
        "calls": rows,
        "home_sum_at_moment_expected_points": sum(c["points_swing"] for c in rows)
        if rows
        else None,
        "note": "Sum of at-the-moment expected points, each read at its own moment. It is not a "
        "final margin and not added to the score: expected points estimate the next score of "
        "the half, and later plays would have changed. Win-chance swings are not added. Sources "
        "are the people disputing a call, not proof that it was wrong. " + NOTE,
    }
