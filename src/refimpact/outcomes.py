"""Change how a play ended: a chosen result becomes a post-play state, and nothing else moves.

The edited drive stops at that state and later drives stay as recorded, so no score is carried
forward and no later play is re-read. Both branches use the next recorded snap's clock, because the
time a different result would have taken is not recorded.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from refimpact.conversion import Conversion, SeasonForm
from refimpact.models import Scorer
from refimpact.penalties import after_score, finite, opponent
from refimpact.replay import second_half_receivers
from refimpact.scenarios import (
    changed,
    estimate,
    ordered,
    play_id,
    recorded_next,
    row_state,
    totals,
)
from refimpact.schema import Record, State
from refimpact.strength import heat_before

Kind = Literal[
    "gain",
    "first_down",
    "touchdown",
    "incomplete",
    "turnover",
    "defensive_td",
    "safety",
    "no_flag",
    "punt_to",
    "touchback",
    "blocked",
    "muffed",
    "return_td",
    "return_to",
    "onside",
    "fg_good",
    "fg_miss",
    "xp_good",
    "xp_miss",
    "two_good",
    "two_fail",
]

OPTIONS = {
    "gain": {"label": "Gain / loss", "input": "yards"},
    "first_down": {"label": "First down"},
    "touchdown": {"label": "Touchdown"},
    "incomplete": {"label": "Incomplete"},
    "turnover": {"label": "Turnover", "input": "spot"},
    "defensive_td": {"label": "Defensive TD"},
    "safety": {"label": "Safety"},
    "no_flag": {"label": "No flag"},
    "punt_to": {"label": "Punt to", "input": "spot"},
    "touchback": {"label": "Touchback"},
    "blocked": {"label": "Blocked", "input": "spot"},
    "muffed": {"label": "Muffed, kicker recovers", "input": "spot"},
    "return_td": {"label": "Return TD"},
    "return_to": {"label": "Return to", "input": "spot"},
    "onside": {"label": "Onside recovered", "input": "spot"},
    "fg_good": {"label": "Good"},
    "fg_miss": {"label": "Missed"},
    "xp_good": {"label": "Good"},
    "xp_miss": {"label": "No good"},
    "two_good": {"label": "Converted"},
    "two_fail": {"label": "Failed"},
}
MENUS = {
    "scrimmage": [
        "gain",
        "first_down",
        "touchdown",
        "incomplete",
        "turnover",
        "defensive_td",
        "safety",
    ],
    "punt": ["punt_to", "touchback", "blocked", "muffed", "return_td"],
    "kickoff": ["return_to", "touchback", "onside", "return_td"],
    "field_goal": ["fg_good", "fg_miss", "blocked"],
    "extra_point": ["xp_good", "xp_miss"],
    "two_point": ["two_good", "two_fail"],
}
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
# Measured: upstream's overtime rule, ported from helper_add_ep_wp.R at the pinned commit, missed
# published win chance by over 1 pp on 215 of 368 stored rows, so it is not used. The
# expected-points classes match published on 349 of those rows.
OVERTIME = "Win chance isn't modeled in overtime. Expected points and the chances below still are."


class OutcomeRequest(Record):
    game_id: str = Field(pattern=r"^\d{4}_\d{2}_[A-Z]{2,3}_[A-Z]{2,3}$")
    play_id: str = Field(pattern=r"^[\w.-]{1,64}$")
    kind: Kind
    yards: int | None = Field(default=None, ge=-99, le=99)
    spot_team: str | None = Field(default=None, pattern=r"^[A-Z]{2,3}$")
    spot_yard: int | None = Field(default=None, ge=1, le=50)


def play_kind(row: dict) -> str | None:
    """Which results a recorded row can take; None for rows that aren't plays."""
    if row.get("play_deleted") == 1 or not row.get("posteam"):
        return None
    kind = row.get("play_type")
    if row.get("two_point_attempt") == 1 or "TWO-POINT CONVERSION" in (row.get("desc") or ""):
        return "two_point"
    if kind == "kickoff":
        return "kickoff"
    if kind == "punt":
        return "punt"
    if kind == "field_goal":
        return "field_goal"
    if kind == "extra_point":
        return "extra_point"
    if kind in ("run", "pass", "qb_kneel", "qb_spike"):
        return "scrimmage" if row.get("down") in (1, 2, 3, 4) else None
    if kind == "no_play" and row.get("penalty") == 1:
        if row.get("down") in (1, 2, 3, 4):
            return "scrimmage"
        return "kickoff" if " kicks " in (row.get("desc") or "") else None
    return None


def no_flag_branch(event: dict | None) -> dict | None:
    """A flag that took effect, with a priced play-as-written branch."""
    if not event or event.get("disposition") == "declined" or not event.get("decision"):
        return None
    for branch in event["decision"]["branches"]:
        if branch["id"] == "no_flag" and branch.get("state"):
            return branch
    return None


def menu(kind: str, event: dict | None = None) -> list[str]:
    options = list(MENUS[kind])
    if no_flag_branch(event):
        options.append("no_flag")
    return options


def yard_text(state: State) -> str:
    if state.yardline_100 == 50:
        return "50"
    if state.yardline_100 > 50:
        return f"{state.possession} {round(100 - state.yardline_100)}"
    return f"{opponent(state, state.possession)} {round(state.yardline_100)}"


def state_text(state: State) -> str:
    togo = "Goal" if state.ydstogo >= state.yardline_100 else round(state.ydstogo)
    return f"{state.possession} {ORDINAL[state.down]} & {togo}, {yard_text(state)}"


def overtime_state(row: dict, receiver: str | None, home_score: int, away_score: int) -> State:
    """A row past regulation, on its overtime clock. Win chance is never read from this state.

    The expected-points model reads the half clock and not the quarter, so the state stays valid
    by carrying quarter 4.
    """
    spread = row.get("spread_line")
    half = min(max(float(row["half_seconds_remaining"]), 1.0), 900.0)
    togo = float(row["ydstogo"]) if finite(row.get("ydstogo")) and row["ydstogo"] > 0 else 10.0
    yard = float(row["yardline_100"]) if finite(row.get("yardline_100")) else 75.0
    return State(
        season=row["season"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        possession=row["posteam"],
        home_score=home_score,
        away_score=away_score,
        qtr=4,
        game_seconds_remaining=half,
        half_seconds_remaining=half,
        down=int(row["down"]) if finite(row.get("down")) else 1,
        ydstogo=min(togo, yard),
        yardline_100=yard,
        home_timeouts=int(row["home_timeouts_remaining"]),
        away_timeouts=int(row["away_timeouts_remaining"]),
        second_half_receiver=receiver,
        roof=row["roof"],
        neutral_site=row.get("location") == "Neutral",
        home_spread=float(spread) if finite(spread) else None,
    )


def before_scores(scores: list[tuple[int, int]], index: int) -> tuple[int, int]:
    return scores[index - 1] if index else (0, 0)


def base_state(
    rows: list[dict],
    index: int,
    receiver: str | None,
    next_state: State | None,
    own: State | None,
    scores: list[tuple[int, int]],
) -> State:
    """Clock and timeouts both branches share, on the scoreboard before this play."""
    row = rows[index]
    home, away = before_scores(scores, index)
    template = next_state or own
    if template is not None:
        update = {"home_score": home, "away_score": away}
        if not next_state:
            update |= {
                "half_seconds_remaining": max(template.half_seconds_remaining, 1.0),
                "game_seconds_remaining": max(template.game_seconds_remaining, 1.0),
            }
        return changed(template, **update)
    half = max(float(row["half_seconds_remaining"]), 1.0)
    qtr = int(row["qtr"])
    if qtr in (1, 3) and half < 900:
        qtr += 1
    return State(
        season=row["season"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        possession=row["posteam"],
        home_score=home,
        away_score=away,
        qtr=qtr,
        game_seconds_remaining=half + (1800 if qtr <= 2 else 0),
        half_seconds_remaining=half,
        down=1,
        ydstogo=10,
        yardline_100=75,
        home_timeouts=row["home_timeouts_remaining"],
        away_timeouts=row["away_timeouts_remaining"],
        second_half_receiver=receiver,
        roof=row["roof"],
        neutral_site=row.get("location") == "Neutral",
        home_spread=float(row["spread_line"]) if finite(row.get("spread_line")) else None,
    )


def spot_to_goal(request: OutcomeRequest, team: str, default: float) -> float:
    """A written spot as yards to `team`'s opponent's goal."""
    if request.spot_team is None or request.spot_yard is None:
        return default
    return float(100 - request.spot_yard if request.spot_team == team else request.spot_yard)


class Result:
    def __init__(self, state: State, label: str, ended: str | None = None, scored: bool = False):
        self.state, self.label, self.ended, self.scored = state, label, ended, scored


def ball_to(base: State, team: str, yard: float, down: int = 1, togo: float | None = None) -> State:
    if not 0 < yard < 100:
        raise ValueError("That spot is off the field")
    distance = min(10.0, yard) if togo is None else togo
    return changed(base, possession=team, yardline_100=yard, down=down, ydstogo=distance)


def scrimmage_result(
    base: State, pre: State, gain: float, kick_spot: float, safety_spot: float, label: str
) -> Result:
    """Apply a gain from the pre-snap spot, by the same rules as penalties.advance."""
    team, rival = pre.possession, opponent(pre, pre.possession)
    yard = pre.yardline_100 - gain
    if yard <= 0:
        return Result(after_score(base, team, 7, kick_spot), "Touchdown", "Touchdown", True)
    if yard >= 100:
        return safety_result(base, rival, safety_spot)
    if gain >= pre.ydstogo:
        return Result(ball_to(base, team, yard), label)
    if pre.down == 4:
        return Result(ball_to(base, rival, 100 - yard), label, "Turnover on downs")
    return Result(ball_to(base, team, yard, pre.down + 1, pre.ydstogo - gain), label)


def safety_result(base: State, scorer_team: str, spot: float) -> Result:
    key = "home_score" if scorer_team == base.home_team else "away_score"
    scored = changed(base, **{key: getattr(base, key) + 2})
    return Result(ball_to(scored, scorer_team, spot), "Safety", "Safety", True)


def apply_outcome(
    request: OutcomeRequest,
    row: dict,
    base: State,
    pre: State | None,
    *,
    kick_spot: float,
    touchback_spot: float,
    safety_spot: float,
    event: dict | None,
) -> Result:
    team, rival = row["posteam"], row.get("defteam") or opponent(base, row["posteam"])
    kind, ask = play_kind(row), request.kind
    if kind is None or (ask not in MENUS[kind] and ask != "no_flag"):
        raise ValueError(f"{OPTIONS[ask]['label']} isn't a result this play can have")
    if ask == "no_flag":
        branch = no_flag_branch(event)
        if branch is None:
            raise ValueError("This flag has no priced version without it")
        return Result(State.model_validate(branch["state"]), "No flag")
    possess = base.model_copy(update={"possession": team})
    if kind == "scrimmage":
        if pre is None:
            raise ValueError("This play has no pre-snap state")
        if ask == "gain":
            if request.yards is None:
                raise ValueError("Enter the yards gained or lost")
            gain = float(request.yards)
            words = f"{'Gain' if gain >= 0 else 'Loss'} of {abs(request.yards)}"
            return scrimmage_result(base, pre, gain, kick_spot, safety_spot, words)
        if ask == "first_down":
            return scrimmage_result(base, pre, pre.ydstogo, kick_spot, safety_spot, "First down")
        if ask == "incomplete":
            return scrimmage_result(base, pre, 0, kick_spot, safety_spot, "Incomplete")
        if ask == "touchdown":
            return Result(after_score(base, team, 7, kick_spot), "Touchdown", "Touchdown", True)
        if ask == "defensive_td":
            return Result(
                after_score(base, rival, 7, kick_spot), "Defensive TD", "Opp touchdown", True
            )
        if ask == "safety":
            return safety_result(base, rival, safety_spot)
        if ask == "turnover":
            yard = spot_to_goal(request, rival, 100 - pre.yardline_100)
            return Result(ball_to(base, rival, yard), "Turnover", "Turnover")
    if kind == "punt":
        here = pre.yardline_100 if pre else 50.0
        if ask == "punt_to":
            yard = spot_to_goal(request, rival, min(80.0, 100 - here + 40))
            return Result(ball_to(base, rival, yard), "Punt", "Punt")
        if ask == "touchback":
            return Result(ball_to(base, rival, 80.0), "Touchback", "Punt")
        if ask == "blocked":
            yard = spot_to_goal(request, rival, 100 - here)
            return Result(ball_to(base, rival, yard), "Blocked punt", "Blocked punt")
        if ask == "muffed":
            yard = spot_to_goal(request, team, min(99.0, max(1.0, here - 40)))
            return Result(ball_to(base, team, yard), "Muffed, kicking team recovers")
        if ask == "return_td":
            return Result(
                after_score(base, rival, 7, kick_spot), "Return TD", "Opp touchdown", True
            )
    if kind == "kickoff":
        # On a kickoff row the receiver is the listed possession team.
        if ask == "return_to":
            yard = spot_to_goal(request, team, kick_spot)
            return Result(ball_to(possess, team, yard), "Return")
        if ask == "touchback":
            return Result(ball_to(possess, team, touchback_spot), "Touchback")
        if ask == "onside":
            yard = spot_to_goal(request, rival, 50.0)
            return Result(ball_to(possess, rival, yard), "Onside kick recovered")
        if ask == "return_td":
            return Result(
                after_score(possess, team, 7, kick_spot), "Kick return TD", "Touchdown", True
            )
    if kind == "field_goal":
        here = pre.yardline_100 if pre else 25.0
        if ask == "fg_good":
            return Result(
                after_score(possess, team, 3, kick_spot), "Field goal good", "Field goal", True
            )
        if ask == "fg_miss":
            # The opponent takes over at the spot of the kick or its own 20, whichever is better.
            return Result(
                ball_to(base, rival, min(80.0, 100 - (here + 8))),
                "Field goal missed",
                "Missed field goal",
            )
        if ask == "blocked":
            yard = spot_to_goal(request, rival, 100 - (here + 8))
            return Result(ball_to(base, rival, yard), "Field goal blocked", "Blocked kick")
    if kind in ("extra_point", "two_point"):
        points = {"xp_good": 1, "xp_miss": 0, "two_good": 2, "two_fail": 0}[ask]
        return Result(
            after_score(possess, team, points, kick_spot), OPTIONS[ask]["label"], "Try", True
        )
    raise ValueError(f"{OPTIONS[ask]['label']} isn't a result this play can have")


def value_of(scorer: Scorer, state: State, home: str, overtime: bool) -> dict:
    """Win chance and expected points. Overtime has expected points only: see OVERTIME."""
    if not overtime:
        return estimate(scorer, state, home)
    classes = scorer.next_score([state], state.possession)[0]
    ep = (
        7 * classes["td"]
        - 7 * classes["opp_td"]
        + 3 * classes["fg"]
        - 3 * classes["opp_fg"]
        + 2 * classes["safety"]
        - 2 * classes["opp_safety"]
    )
    return {
        "home_wp": None,
        "home_wp_vegas": None,
        "home_ep": ep if state.possession == home else -ep,
    }


def public_value(value: dict) -> dict:
    return {key: value[key] for key in ("home_wp", "home_wp_vegas", "home_ep")}


def evaluate_outcome(
    rows: list[dict],
    request: OutcomeRequest,
    scorer: Scorer,
    fit: Conversion,
    season_form: SeasonForm,
    kick_spot: float,
    event: dict | None = None,
) -> dict:
    rows = ordered(rows)
    positions = {play_id(row): i for i, row in enumerate(rows)}
    index = positions.get(request.play_id)
    if index is None:
        raise ValueError(f"Play {request.play_id} is not in {request.game_id}")
    row = rows[index]
    home = row["home_team"]
    receiver = second_half_receivers(rows).get(row["game_id"])
    scores = totals(rows)
    overtime = finite(row.get("qtr")) and row["qtr"] > 4
    season = row["season"]
    touchback_spot, _ = fit.spot("kickoff_touchback", season)
    safety_spot, safety_rough = fit.spot("safety_kick", season)
    warnings = []
    heat = heat_before(rows, index)
    before_date = row["game_date"]

    if overtime:
        home_score, away_score = before_scores(scores, index)
        pre = overtime_state(row, receiver, home_score, away_score)
        base = pre
        recorded = None
    else:
        pre = row_state(row, receiver)
        recorded = recorded_next(rows, index, receiver)
        if recorded is None and pre is None and play_kind(row) is None:
            raise ValueError("This row isn't a play")
        if recorded is None:
            warnings.append("No later snap in this half, so this reads at the play's own clock.")
        base = base_state(rows, index, receiver, recorded, pre, scores)

    result = apply_outcome(
        request,
        row,
        base,
        pre,
        kick_spot=kick_spot,
        touchback_spot=touchback_spot,
        safety_spot=safety_spot,
        event=event,
    )
    if result.label == "Safety" and safety_rough:
        warnings.append("The spot after the safety kick is approximate.")

    # Recorded next, on the real scoreboard, read by the same model as your version.
    reference = recorded or (pre if pre is not None else base)
    recorded_value = value_of(scorer, reference, home, overtime)
    if overtime:
        warnings.append(OVERTIME)
        yours_value = value_of(scorer, result.state, home, True)
        following = next(
            (r for r in rows[index + 1 :] if finite(r.get("home_wp"))),
            None,
        )
        if following is not None:
            recorded_value["home_wp"] = float(following["home_wp"])
            vegas_next = following.get("vegas_home_wp")
            recorded_value["home_wp_vegas"] = float(vegas_next) if finite(vegas_next) else None
    else:
        yours_value = value_of(scorer, result.state, home, False)
    published = row.get("home_wp")
    vegas = row.get("vegas_home_wp")
    swing = None
    if not overtime:
        swing = {
            "wp_pp": 100 * (yours_value["home_wp"] - recorded_value["home_wp"]),
            "vegas_pp": 100 * (yours_value["home_wp_vegas"] - recorded_value["home_wp_vegas"])
            if yours_value["home_wp_vegas"] is not None
            and recorded_value["home_wp_vegas"] is not None
            else None,
            "points": yours_value["home_ep"]
            + result.state.score_diff(home)
            - recorded_value["home_ep"]
            - reference.score_diff(home),
        }

    first_down = None
    scoring = None
    if not result.scored:
        first_down = fit.first_down(result.state, season_form, before_date, heat)
        scoring = fit.scoring(result.state, scorer, season_form, before_date, heat)

    return {
        "game_id": request.game_id,
        "play_id": request.play_id,
        "drive_id": f"{row['game_id']}:{int(row['fixed_drive'])}"
        if finite(row.get("fixed_drive"))
        else None,
        "outcome": {
            "kind": request.kind,
            "label": result.label,
            "drive_result": result.ended,
            "scored": result.scored,
        },
        "before": {
            "home_wp": float(published) if finite(published) else None,
            "home_wp_vegas": float(vegas) if finite(vegas) else None,
        },
        "recorded": {
            "text": state_text(reference),
            "score": {"home": reference.home_score, "away": reference.away_score},
            **public_value(recorded_value),
        },
        "yours": {
            "text": state_text(result.state),
            "score": {"home": result.state.home_score, "away": result.state.away_score},
            **public_value(yours_value),
        },
        "swing": swing,
        "first_down": first_down,
        "scoring": scoring,
        "warnings": warnings,
        "note": "Same clock on both sides. Your version stops this drive; later drives stay as "
        "recorded.",
    }
