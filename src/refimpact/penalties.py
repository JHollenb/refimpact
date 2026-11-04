"""All reported flags: dispositions, decision alternatives, and clock evidence.

Missing spots stay missing. Conditional DPI spot/clock cases are scenarios, never
silently assigned probabilities or included as observed enforcement effects.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import polars as pl

from refimpact.decisions import (
    Clock,
    DecisionBranch,
    DecisionRequest,
    evaluate_decision,
    opportunity,
)
from refimpact.models import Scorer
from refimpact.replay import second_half_receivers, state_from_row
from refimpact.schema import State

FLAG = re.compile(r"penalty on ([A-Z]{2,3})(?:-[^,]+)?,\s*([^,.]+)", re.I)
RUNOFF = re.compile(r"10[ -]second (?:runoff|run.off)", re.I)
DEAD = {"False Start", "Encroachment", "Neutral Zone Infraction", "Delay of Game"}
PREVIOUS = {
    "Defensive Holding": (5, True, False),
    "Illegal Contact": (5, True, False),
    "Defensive Offside": (5, False, False),
    "Defensive Too Many Men on Field": (5, False, False),
    "Offensive Pass Interference": (10, False, False),
    "Illegal Formation": (5, False, False),
    "Illegal Shift": (5, False, False),
    "Illegal Motion": (5, False, False),
    "Ineligible Downfield Pass": (5, False, False),
    "Offensive Offside": (5, False, False),
}
# Kick-rule placements: the feed has no foul-free result to compare with.
KICK_PLACEMENT = {"Kickoff Out of Bounds", "Kickoff Short of Landing Zone", "Illegal Touch Kick"}
# A spot with no team is midfield ("to 50"). Group 1 is the team, group 2 the yard.
RETURN_END = re.compile(
    r"\b(?:to|ran ob at|pushed ob at|out of bounds at)\s+(?:([A-Z]{2,3}) )?(\d{1,2}) "
    r"for (?:-?\d+ yards?|no gain)"
)
RECOVERY = re.compile(r"(?i:recovered by) ([A-Z]{2,3})-\S+ at (?:([A-Z]{2,3}) )?(\d{1,2})")
SELF_RECOVERY = re.compile(r"and recovers at (?:([A-Z]{2,3}) )?(\d{1,2})")
# Inside two minutes a next-snap wait is often the point of the flag. A declared scenario length,
# used only when the feed and Rule 4 give both branches a known restart.
LATE_SECONDS = 120
HURRY_DELAY = 10
RUNOUT_DELAY = 20
CLOCK_RUNS = {"running", "ready"}
OUT_OF_BOUNDS = re.compile(r"ball out of bounds at (?:([A-Z]{2,3}) )?(\d{1,2})")
FUMBLE_SPOT = re.compile(
    r"(?:\b(?:to|ran ob at|pushed ob at|out of bounds at)\s+|sacked at )"
    r"(?:([A-Z]{2,3}) )?(\d{1,2})\b"
)
# What a description holds when nothing happened: a clock, a formation, a reported eligible.
NO_ACTION = re.compile(
    r"\s*(?:\(\d*:\d+\)\s*)?(?:(?:\d+-\S+(?: and \d+-\S+)* reported in as eligible\."
    r"|\((?:Shotgun|No Huddle|No Huddle, Shotgun|Run formation|Pass formation|Punt formation"
    r"|Field Goal formation)\))\s*)*"
)
# A try that was played and written, then erased.
TRY_RESULTS = {
    "extra point is GOOD": 1,
    "extra point is No Good": 0,
    "extra point is Blocked": 0,
    "ATTEMPT SUCCEEDS": 2,
    "ATTEMPT FAILS": 0,
}


def finite(value) -> bool:
    return isinstance(value, (float, int)) and math.isfinite(value)


def is_flagged(row: dict) -> bool:
    text = row.get("desc") or ""
    return bool(
        row.get("penalty") == 1
        or FLAG.search(text)
        or re.search(r"\bpenalt(?:y|ies)\b", text, re.I)
        or RUNOFF.search(text)
        or re.search(r"penalty.*(?:picked up|rescinded|withdrawn|no foul)", text, re.I)
    )


def parse_fouls(row: dict) -> tuple[list[dict], str, bool]:
    original = row.get("desc") or ""
    revised = "REVERSED" in original
    text = re.split(r"REVERSED[.:]", original)[-1] if revised else original
    matches = list(FLAG.finditer(text))
    fouls = []
    for i, match in enumerate(matches):
        segment = text[
            match.start() : matches[i + 1].start() if i + 1 < len(matches) else len(text)
        ]
        kind = match.group(2).strip()
        disposition = (
            "declined"
            if re.search(r"\bdeclined\b", segment, re.I)
            else "offsetting"
            if re.search(r"\boffsetting\b", segment, re.I)
            else "rescinded"
            if re.search(r"picked up|rescinded|withdrawn|no foul", segment, re.I)
            else "accepted"
        )
        yards = re.search(r"\b(\d+(?:\.\d+)?) yards?\b", segment, re.I)
        fouls.append(
            {
                "team": match.group(1).upper(),
                "type": kind,
                "disposition": disposition,
                "reported_yards": float(yards.group(1)) if yards else None,
                "source_text": segment,
            }
        )
    if not fouls:
        fouls = [
            {
                "team": row.get("penalty_team"),
                "type": row.get("penalty_type")
                or ("Clock administration" if RUNOFF.search(original) else "Unclassified flag"),
                "disposition": "accepted" if row.get("penalty") == 1 else "unknown",
                "reported_yards": row.get("penalty_yards"),
                "source_text": original,
            }
        ]
    prefix = text[: matches[0].start()] if matches else text
    return fouls, prefix, revised


def runoff_info(text: str) -> dict:
    mentioned = bool(RUNOFF.search(text))
    avoided = mentioned and bool(
        re.search(
            r"run[ -]?off\s+(?:was\s+)?(?:avoided|declined|waived|not applied)|no\s+10[ -]second",
            text,
            re.I,
        )
    )
    return {
        "mentioned": mentioned,
        "applied_seconds": 10 if mentioned and not avoided else 0,
        "avoided": avoided,
        "evidence": "reported in play description" if mentioned else "not reported",
    }


def changed(s: State, **changes) -> State:
    return State.model_validate(s.model_dump() | changes)


def advance(
    before: State,
    yardline: float,
    *,
    consumed: bool,
    automatic_first: bool = False,
    possession: str | None = None,
) -> State:
    if not 0 < yardline < 100:
        raise ValueError("Scoring/end-zone alternative needs a scoring-state valuation")
    gain = before.yardline_100 - yardline
    first = automatic_first or gain >= before.ydstogo
    down = 1 if first else before.down + int(consumed)
    togo = min(10, yardline) if first else before.ydstogo - gain
    if possession or down == 5:
        other = possession or (
            before.away_team if before.possession == before.home_team else before.home_team
        )
        return changed(
            before,
            possession=other,
            yardline_100=100 - yardline,
            down=1,
            ydstogo=min(10, 100 - yardline),
        )
    return changed(before, yardline_100=yardline, down=down, ydstogo=togo)


def opponent(s: State, team: str) -> str:
    return s.away_team if team == s.home_team else s.home_team


def yard_from(defense: str, team: str | None, yard: str) -> float:
    """A written spot as distance to `defense`'s goal line."""
    return float(yard) if team in {defense, None} else 100 - float(yard)


def after_score(before: State, team: str, points: int, kick_spot: float | None) -> State:
    """The state a score hands over: points on the board, opponent ball after the kick."""
    if kick_spot is None:
        raise ValueError("Scoring alternative needs a measured post-kick start spot")
    key = "home_score" if team == before.home_team else "away_score"
    return changed(
        before,
        **{key: getattr(before, key) + points},
        possession=opponent(before, team),
        yardline_100=kick_spot,
        down=1,
        ydstogo=min(10, kick_spot),
    )


def touchdown_team(before: State, prefix: str) -> str:
    if len(re.findall(r"FUMBLES|INTERCEPTED|MUFFS|BLOCKED", prefix)) > 1 or "BLOCKED" in prefix:
        raise ValueError("Touchdown after a block or several turnovers is ambiguous; not guessed")
    recovered = re.findall(r"(?i:recovered by) ([A-Z]{2,3})-", prefix)
    if recovered:
        return recovered[-1]
    if re.search(r"INTERCEPTED|punts", prefix):
        return opponent(before, before.possession)
    return before.possession


def single_recovery(text: str, default_team: str) -> tuple[str, tuple] | None:
    """Who ended up with one loose ball, and where, read from the text after a kick.

    None when nothing came loose. Several loose balls or a lateral are not guessed. A return after
    the recovery ends the play.
    """
    if not re.search(r"FUMBLES|MUFFS", text):
        return None
    recoveries, own = RECOVERY.findall(text), SELF_RECOVERY.findall(text)
    if (
        len(re.findall(r"FUMBLES|MUFFS", text)) != 1
        or len(recoveries) + len(own) != 1
        or "Lateral" in text
    ):
        raise ValueError("Loose ball is not a single unambiguous recovery; not guessed")
    team = recoveries[0][0] if recoveries else default_team
    spot = recoveries[0][1:] if recoveries else own[0]
    returned = RETURN_END.findall(
        text[(RECOVERY.search(text) or SELF_RECOVERY.search(text)).end() :]
    )
    return team, returned[-1] if returned else spot


def blocked_field_goal(before: State, prefix: str) -> State:
    """The team recovering a block keeps it; a kicking team short of the line turns it over."""
    other = opponent(before, before.possession)
    recoveries = RECOVERY.findall(prefix)
    if len(recoveries) != 1 or "Lateral" in prefix or "FUMBLES" in prefix:
        raise ValueError("Blocked or unusual field goal result is not guessed")
    returned = RETURN_END.findall(prefix[RECOVERY.search(prefix).end() :])
    team, spot = recoveries[0][0], returned[-1] if returned else recoveries[0][1:]
    yard = yard_from(other, spot[0] or None, spot[1])
    if team == other:
        return advance(before, yard, consumed=True, possession=other)
    return advance(before, yard, consumed=True)


def punt_result(before: State, prefix: str) -> State:
    if re.search(r"BLOCKED|Lateral", prefix):
        raise ValueError("Punt with a block or lateral is ambiguous; not guessed")
    other = opponent(before, before.possession)
    tail = prefix[re.search(r"\bpunts\b", prefix).start() :]
    loose = single_recovery(tail, other)
    if loose:
        team, spot = loose
        yard = yard_from(other, spot[0] or None, spot[1])
        return (
            advance(before, yard, consumed=True, possession=other)
            if team == other
            else advance(before, yard, consumed=True, automatic_first=True)
        )
    if "Touchback" in tail:
        return advance(before, 20, consumed=True, possession=other)
    returned = RETURN_END.findall(tail)
    if len(returned) > 1:
        raise ValueError("Punt return endpoint is ambiguous; not guessed")
    spot = returned[0] if returned else None
    if spot is None:
        target = re.search(r"punts -?\d+ yards? to (?:([A-Z]{2,3}) )?(\d{1,2})\b", tail)
        spot = target.groups() if target else None
    if spot is None:
        raise ValueError("Punt result is not recoverable from the description")
    yard = yard_from(other, spot[0] or None, spot[1])
    return advance(before, yard, consumed=True, possession=other)


def fumble_result(before: State, prefix: str) -> State:
    recoveries = RECOVERY.findall(prefix)
    own = SELF_RECOVERY.findall(prefix)
    other = opponent(before, before.possession)
    out = OUT_OF_BOUNDS.search(prefix)
    if (
        out
        and not recoveries
        and not own
        and len(re.findall(r"FUMBLES", prefix)) == 1
        and not re.search(r"Lateral|INTERCEPT|MUFFS", prefix)
    ):
        # Rule 8-7: backward keeps the inbounds spot, forward returns to the spot of the fumble.
        # Whichever is farther from the goal is the one that applies.
        carried = FUMBLE_SPOT.findall(prefix[: prefix.index("FUMBLES")])
        if not carried:
            raise ValueError("Fumble spot is not written; not guessed")
        return advance(
            before,
            max(
                yard_from(other, carried[-1][0] or None, carried[-1][1]),
                yard_from(other, out.group(1), out.group(2)),
            ),
            consumed=True,
        )
    if (
        len(re.findall(r"FUMBLES", prefix)) != 1
        or len(recoveries) + len(own) != 1
        or len(re.findall(r"INTERCEPTED", prefix)) > 1
        or re.search(r"Lateral|MUFFS", prefix)
    ):
        raise ValueError("Fumble recovery is not a single unambiguous sequence; not guessed")
    team = recoveries[0][0] if recoveries else before.possession
    spot = recoveries[0][1:] if recoveries else own[0]
    # A return after the recovery ends the play; a gain before the fumble does not.
    tail = prefix[(RECOVERY.search(prefix) or SELF_RECOVERY.search(prefix)).end() :]
    returned = RETURN_END.findall(tail)
    if len(returned) > 1:
        raise ValueError("Fumble return endpoint is ambiguous; not guessed")
    if returned:
        spot = returned[0]
    yard = yard_from(other, spot[0] or None, spot[1])
    if team != before.possession:
        return advance(before, yard, consumed=True, possession=other)
    # Getting back an interception is a new series; a fumble the offense kept is not.
    return advance(before, yard, consumed=True, automatic_first="INTERCEPTED" in prefix)


def play_result(before: State, row: dict, prefix: str, kick_spot: float | None = None) -> State:
    if re.search(r"SAFETY", prefix):
        raise ValueError("Safety alternative needs a free-kick spot; not guessed")
    if re.search(r"TOUCHDOWN", prefix):
        return after_score(before, touchdown_team(before, prefix), 7, kick_spot)
    if re.search(r"\bfield goal is\b", prefix):
        if re.search(r"field goal is GOOD", prefix):
            return after_score(before, before.possession, 3, kick_spot)
        if re.search(r"field goal is No Good", prefix):
            # Rule 11-4: spot of the kick, eight yards behind the line, or the 20 if farther out.
            return advance(
                before,
                max(20.0, before.yardline_100 + 8),
                consumed=True,
                possession=opponent(before, before.possession),
            )
        return blocked_field_goal(before, prefix)
    if re.search(r"\bpunts\b", prefix):
        return punt_result(before, prefix)
    if re.search(r"\bkicks\b", prefix):
        raise ValueError("Free-kick alternative from a scrimmage row is not guessed")
    if re.search(r"FUMBLES", prefix):
        return fumble_result(before, prefix)
    if re.search(r"INTERCEPT", prefix, re.I):
        if len(re.findall(r"INTERCEPTED", prefix, re.I)) != 1 or re.search(
            r"lateral", prefix, re.I
        ):
            raise ValueError("Ambiguous interception/return sequence; not guessed")
        other = before.away_team if before.possession == before.home_team else before.home_team
        # Interception touchbacks are at the receiving team's 20 (Rule 11-6-3),
        # not the kickoff touchback spot. No-play structured yards are irrelevant.
        if re.search(r"\bTouchback\b", prefix, re.I):
            return advance(before, 20, consumed=True, possession=other)
        # Require an explicit return endpoint, not just the interception location.
        tail = re.split(r"INTERCEPTED", prefix, flags=re.I)[-1]
        endpoints = RETURN_END.findall(tail)
        endpoint = endpoints[0] if len(endpoints) == 1 else None
        if endpoint and endpoint[0] in {before.home_team, before.away_team, ""}:
            original_coordinate = yard_from(other, endpoint[0] or None, endpoint[1])
            return advance(before, original_coordinate, consumed=True, possession=other)
        raise ValueError("Interception return endpoint is not recoverable from the description")
    if re.search(r"pass incomplete|spiked the ball", prefix, re.I):
        return advance(before, before.yardline_100, consumed=True)
    prefix = re.sub(r"for a loss of (\d+) yards?", r"for -\1 yards", prefix, flags=re.I)
    gains = re.findall(r"for (-?\d+) yards?\b", prefix, re.I)
    ended = RETURN_END.findall(prefix)
    if len(gains) > 1 and "Lateral" in prefix and len(ended) == len(gains):
        # The last written spot is where the carry ended, however it got there.
        other = opponent(before, before.possession)
        return advance(before, yard_from(other, ended[-1][0] or None, ended[-1][1]), consumed=True)
    if len(gains) > 1:
        raise ValueError("Multiple written gains need an unambiguous net result; not guessed")
    if gains:
        gain = float(gains[-1])
    elif re.search(r"no gain", prefix, re.I):
        gain = 0.0
    elif row.get("play_type") in {"run", "pass", "qb_kneel", "qb_spike"} and finite(
        row.get("yards_gained")
    ):
        gain = row["yards_gained"]
    else:
        raise ValueError("The unpenalized play result is not recoverable from the source")
    return advance(before, before.yardline_100 - gain, consumed=True)


def noflag_restart(prefix: str, before: State, dead: bool) -> str:
    if re.search(r"injur|challeng|replay|REVERSED", prefix, re.I):
        return "unknown"
    if dead:
        return "unknown"
    if re.search(r"INTERCEPT|TOUCHDOWN|field goal is|\bpunts\b", prefix, re.I):
        return "snap"  # A score or change of possession stops the game clock.
    if re.search(r"FUMBLES", prefix):
        return "unknown"  # Depends on who recovered and where the ball went.
    if re.search(r"incomplete", prefix, re.I):
        return "snap"
    if re.search(r"(?:ran|pushed|runs) ob|out of bounds", prefix, re.I):
        return (
            "snap"
            if (
                (before.qtr == 2 and before.half_seconds_remaining <= 120)
                or (before.qtr == 4 and before.half_seconds_remaining <= 300)
            )
            else "ready"
        )
    return "running"


def penalty_restart(before: State, prefix: str, dead: bool, offense: bool, runoff: int) -> str:
    if runoff:
        return "ready"
    if (
        (before.qtr == 2 and before.half_seconds_remaining <= 120)
        or (before.qtr == 4 and before.half_seconds_remaining <= 300)
        or (before.qtr == 4 and dead and offense)
    ):
        return "snap"
    return noflag_restart(prefix, before, dead)


def acceptance(before: State, foul: dict, prefix: str, actual: State | None) -> State:
    kind = foul["type"]
    offense = foul["team"] == before.possession
    if foul["team"] not in {before.home_team, before.away_team}:
        raise ValueError("Unknown fouling team")
    if kind in DEAD:
        yards = min(5, ((100 - before.yardline_100) if offense else before.yardline_100) / 2)
        return advance(before, before.yardline_100 + (yards if offense else -yards), consumed=False)
    if kind in PREVIOUS:
        if kind == "Defensive Holding" and not re.search(r"pass|sacked", prefix, re.I):
            # On a run the basic spot is the dead-ball spot (Rule 14-4-2).
            # The foul's phase is not recoverable reliably from a run description.
            raise ValueError("Defensive holding on a run needs enforcement-phase/spot evidence")
        yards, auto, loss = PREVIOUS[kind]
        yards = min(yards, ((100 - before.yardline_100) if offense else before.yardline_100) / 2)
        return advance(
            before,
            before.yardline_100 + (yards if offense else -yards),
            consumed=loss,
            automatic_first=auto and not offense,
        )
    # A declined holding/personal-foul penalty can depend on an unreported foul spot.
    # Actual accepted enforcement can instead be read from the following recorded state.
    if actual is not None:
        return actual
    raise ValueError("Acceptance requires the foul/enforcement spot; it is not reported")


def make_branch(
    id: str,
    label: str,
    state: State,
    *,
    restart="unknown",
    runoff=0,
    timeout_team=None,
    evidence="reconstructed",
    assumptions=None,
) -> DecisionBranch:
    return DecisionBranch(
        id=id,
        label=label,
        state=state,
        clock=Clock(
            restart=restart,
            runoff_seconds=runoff,
            timeout_team=timeout_team,
            evidence="Runoff from description; restart follows Rule 4 or is explicitly unknown.",
        ),
        evidence=evidence,
        assumptions=assumptions
        or [
            "Hold the described football action fixed; no claim about a foul-free play.",
            "Post-snap spatial comparisons use the next recorded "
            "down's clock for both alternatives; "
            "this is a common evaluation horizon, not an observed play-end timestamp.",
            "Hold timeout budgets at the pre-state unless an explicit linked runoff-avoidance "
            "record identifies the timeout intervention; unrelated "
            "timeouts are not attributed to the flag.",
        ],
    )


def nullified_record(row: dict, prefix: str, disposition: str) -> dict | None:
    """Keep written erased action separately from no-play statistical placeholders."""
    if disposition in {"declined", "rescinded"} or not (
        row.get("play_type") == "no_play" or re.search(r"\bNo Play\b", row.get("desc") or "", re.I)
    ):
        return None
    # Inspect only action text before the flag. Penalty yardage is not play yardage.
    written = bool(
        re.search(
            r"\b(?:for (?:-?\d+ yards?|no gain|a loss of \d+)|pass incomplete|"
            r"sacked|INTERCEPTED|FUMBLES?|TOUCHDOWN|SAFETY|punts|field goal|spiked)\b",
            prefix,
            re.I,
        )
    )
    return {
        "status": "unpriced" if written else "not_recorded",
        "recorded": written,
        "source": {
            "dataset": "nflverse play-by-play",
            "game_id": row["game_id"],
            "play_id": str(int(row["play_id"])),
            "field": "desc",
            "text": prefix.strip(),
        },
        "reason": (
            "Written action found; a comparison needs recoverable branch states."
            if written
            else "The feed does not describe the erased action. No-play statistical zeros "
            "are not treated as a zero-yard result."
        ),
        "impact_epa": None,
        "wp_swing_pp": None,
        "reference_team": row.get("posteam"),
        "alternative_branch": None,
        "interpretation": "Actual ruling minus keeping the written erased result. Conditional "
        "on that result standing; the text does not establish "
        "whistle timing or ruling correctness.",
        "clock_basis": "Both spatial alternatives use a common evaluation clock. The feed "
        "does not establish elapsed time for action removed from the official record.",
    }


def written_erased_branch(
    base: State, prefix: str, kick_spot: float | None = None
) -> DecisionBranch:
    # Disable the usual structured-yards fallback. nflverse commonly sets those
    # fields to zero precisely because the physical action was nullified.
    written = re.sub(r"for a loss of (\d+) yards?", r"for -\1 yards", prefix, flags=re.I)
    result = play_result(base, {"play_type": "no_play"}, written, kick_spot)
    return make_branch(
        "erased_result",
        "IF the written nullified action counted",
        result,
        evidence="reconstructed",
        restart=noflag_restart(prefix, base, False),
        assumptions=[
            "Automatically parsed from the written action before the "
            "flag, not from no-play yardage fields.",
            "Hold the written physical result fixed. It is not proof "
            "that players ignored a whistle "
            "or that the ruling was wrong.",
            "Use the same evaluation clock as the actual branch; "
            "unreported action duration is not invented.",
            "Fourth down is consumed only if it was the down being played; a failed third down "
            "creates fourth down, not an assumed punt or possession change.",
        ],
    )


def score_stands(prefix: str, description: str) -> bool:
    """A written score that counted: enforcement moved to the try or kickoff, not the play."""
    return (
        bool(re.search(r"TOUCHDOWN|field goal is GOOD", prefix))
        and "NULLIFIED" not in prefix
        and "No Play" not in description
    )


def approximation(prefix: str, kick_spot: float | None) -> str | None:
    """Plain statement of what a scoring or kick alternative assumes beyond the written text."""
    if re.search(r"TOUCHDOWN", prefix):
        return (
            f"Approximate: erased touchdown counted as 7 with the extra point assumed good, then "
            f"the opponent's ball {kick_spot:g} yards from goal (season mean after kickoffs), "
            "at the same clock."
        )
    if re.search(r"field goal is GOOD", prefix):
        return (
            f"Approximate: erased field goal counted as 3, then the opponent's ball {kick_spot:g} "
            "yards from goal (season mean after kickoffs), at the same clock."
        )
    if re.search(r"field goal is No Good", prefix):
        return "Approximate: missed kick gives the opponent the spot of the kick or its own 20."
    if "BLOCKED" in prefix:
        return "Approximate: possession and spot read from the written recovery of the block."
    if re.search(r"FUMBLES|MUFFS", prefix):
        return "Approximate: possession and spot read from the written recovery and return."
    if "Lateral" in prefix:
        return "Approximate: the carry ends at the last written spot."
    return None


def no_action_written(row: dict, prefix: str) -> bool:
    """A no-play row with nothing written before the flag: the foul came with no snap to erase."""
    return (
        row.get("play_type") == "no_play"
        and "No Play" in (row.get("desc") or "")
        and NO_ACTION.fullmatch(prefix) is not None
    )


def unpriced_reason(row: dict, event: dict, prefix: str) -> str | None:
    """Why a flag that took effect has no enforcement value; None if it has one or was declined."""
    if event["enforcement_epa"] is not None or event["disposition"] in {"declined", "rescinded"}:
        return None
    text = row.get("desc") or ""
    if not finite(row.get("qtr")) or row["qtr"] > 4:
        return "overtime_not_modeled"
    # Text that mentions a penalty but names no foul, whatever else the play was.
    if any(f["team"] is None or f["disposition"] == "unknown" for f in event["fouls"]):
        return "foul_not_identified"
    if any("measured post-kick" in issue for issue in event["issues"]):
        return "no_measured_kick_spot"
    if re.search(r"TWO-POINT CONVERSION|extra point", text) or (
        "(Kick formation)" in text and row.get("down") is None
    ):
        return "try_not_modeled"
    if any(f["type"] in KICK_PLACEMENT for f in event["fouls"]):
        return "kick_rule_placement"
    if score_stands(prefix, text):
        return "score_stands_enforced_after"
    if re.search(r"SAFETY", text):
        return "safety"
    if row.get("play_type") == "kickoff" or re.search(r"\bkicks\b", prefix):
        return "kickoff_result_not_recoverable"
    if re.search(r"\bpunts\b|field goal is", prefix):
        return "kick_result_ambiguous"
    if re.search(r"FUMBLES|INTERCEPT|TOUCHDOWN", prefix):
        return "turnover_sequence_ambiguous"
    if not re.search(r"\bfor (?:-?\d|no gain|a loss)|incomplete|sacked|kneel|spike", prefix, re.I):
        return "no_written_action"
    return "other"


def sensible_wait(case: dict, decision: dict) -> bool:
    """Running out the half is a real outcome only for the team ahead, who wants it.

    A team that is tied or behind would hurry or spike rather than wait out the declared delay,
    so that scenario's 0% says nothing about the flag.
    """
    branches = {b["id"]: b for b in decision["branches"]}
    for phase, name in (
        (case["actual_phase"], decision["actual_branch"]),
        (case["alternative_phase"], case["alternative_branch"]),
    ):
        state = branches[name]["base_state"]
        margin = state["home_score"] - state["away_score"]
        ahead = margin > 0 if state["possession"] == state["home_team"] else margin < 0
        if phase != "scrimmage" and not ahead:
            return False
    return True


def headline_delay(state: dict) -> int:
    """Seconds lost before the next snap, set by the team that needs time.

    That team is the offense when tied or behind, otherwise the defense. With a timeout left it
    can stop the clock, so nothing is charged; the spent timeout itself is not priced.
    """
    margin = state["home_score"] - state["away_score"]
    home_ball = state["possession"] == state["home_team"]
    offense_needs_time = margin <= 0 if home_ball else margin >= 0
    home_needs_time = home_ball == offense_needs_time
    if state["home_timeouts" if home_needs_time else "away_timeouts"] > 0:
        return 0
    return HURRY_DELAY if offense_needs_time else RUNOUT_DELAY


def restart_aware_case(cases: list[dict], decision: dict) -> dict | None:
    """The known-restart comparison at the delay every running branch needs, if there is one."""
    known = [c for c in cases if not c["conditional_clock_assumption"]]
    if not known:
        return None
    branches = {b["id"]: b for b in decision["branches"]}
    needed = set()
    if known[0]["actual_restart"] in CLOCK_RUNS:
        needed.add(headline_delay(branches[decision["actual_branch"]]["base_state"]))
    if known[0]["alternative_restart"] in CLOCK_RUNS:
        needed.add(headline_delay(branches[known[0]["alternative_branch"]]["base_state"]))
    if len(needed) != 1:
        return None
    delay = needed.pop()
    return next(
        (c for c in known if c["next_snap_delay_seconds"] == delay and sensible_wait(c, decision)),
        None,
    )


def late_clock(event: dict, decision: dict, half_seconds: float) -> None:
    """Inside 2:00 report a range; lead with restart-aware only when the clock would really run."""
    event.update(
        enforcement_wp_common_clock_pp=event["enforcement_wp_swing_pp"],
        enforcement_wp_vegas_common_clock_pp=event["enforcement_wp_vegas_pp"],
        enforcement_wp_basis="common_clock",
        enforcement_wp_range=None,
    )
    if half_seconds > LATE_SECONDS or event["enforcement_wp_swing_pp"] is None:
        return
    cases = [
        c
        for c in decision["timing_comparisons"]
        if c["alternative_branch"] == "no_flag" and c["actual_minus_branch_wp_pp"] is not None
    ]
    sampled = [c["actual_minus_branch_wp_pp"] for c in cases if sensible_wait(c, decision)]
    values = [event["enforcement_wp_swing_pp"], *sampled]
    event["enforcement_wp_range"] = {
        "min": min(values),
        "max": max(values),
        "meaning": "Across next-snap waits of 0, 10 and 20 seconds under each restart rule, "
        "leaving out a team that is tied or behind waiting out the half; sampled scenarios, "
        "not a confidence interval.",
    }
    known = restart_aware_case(cases, decision)
    if known:
        event["enforcement_wp_swing_pp"] = known["actual_minus_branch_wp_pp"]
        event["enforcement_wp_vegas_pp"] = known["actual_minus_branch_wp_vegas_pp"]
        event["enforcement_wp_basis"] = "restart_aware"
        event["enforcement_wp_restarts"] = {
            "actual": known["actual_restart"],
            "alternative": known["alternative_restart"],
            "delay_seconds": known["next_snap_delay_seconds"],
        }


def finish_decision(event: dict, decision: dict, before: State) -> None:
    """Copy the no-flag comparison onto the event and orient its win-chance swing to home."""
    event["decision"] = decision
    nf = next((b for b in decision["branches"] if b["id"] == "no_flag"), None)
    if nf:
        event["enforcement_epa"] = nf["actual_minus_branch_epa"]
        event["enforcement_wp_swing_pp"] = nf["actual_minus_branch_wp_pp"]
        event["enforcement_wp_vegas_pp"] = nf["actual_minus_branch_wp_vegas_pp"]
        late_clock(event, decision, before.half_seconds_remaining)
        sign = 1 if decision["reference_team"] == before.home_team else -1
        event["home_epa_impact"] = event["enforcement_epa"] * sign
        for side, value in (
            ("home_wp_swing_pp", event["enforcement_wp_swing_pp"]),
            ("home_wp_vegas_swing_pp", event["enforcement_wp_vegas_pp"]),
        ):
            event[side] = value * sign if value is not None else None


def kick_return_state(next_state: State, prefix: str, receiver: str, kick_spot) -> State:
    """The written kickoff result, as the first snap of whichever team kept the ball."""
    if re.search(r"kicks onside|BLOCKED", prefix):
        raise ValueError("Kickoff with an onside kick or block is ambiguous; not guessed")
    if "TOUCHDOWN" in prefix:
        if re.search(r"FUMBLES|MUFFS|Lateral", prefix):
            raise ValueError("Return touchdown after a loose ball is ambiguous; not guessed")
        return after_score(next_state, receiver, 7, kick_spot)
    kicker = opponent(next_state, receiver)
    tail = prefix[re.search(r"\bkicks\b", prefix).start() :]
    loose = single_recovery(tail, receiver)
    if loose:
        team, spot = loose
        yards = yard_from(opponent(next_state, team), spot[0] or None, spot[1])
        return changed(
            next_state, possession=team, yardline_100=yards, down=1, ydstogo=min(10, yards)
        )
    touchback = re.search(r"Touchback to the ([A-Z]{2,3}) (\d{1,2})", tail)
    returned = RETURN_END.findall(tail)
    if touchback:
        spot = touchback.groups()
    elif returned:
        spot = returned[-1]
    else:
        raise ValueError("Kickoff result is not recoverable from the description")
    yards = yard_from(kicker, spot[0] or None, spot[1])
    return changed(
        next_state, possession=receiver, yardline_100=yards, down=1, ydstogo=min(10, yards)
    )


def evaluate_kickoff(
    row: dict, event: dict, fouls: list[dict], prefix: str, following, receiver, scorer, kick_spot
) -> None:
    """A kickoff row has no scrimmage pre-state, so compare the two first snaps it can lead to.

    The next recorded snap is the actual branch and supplies the clock and timeouts for both.
    """
    if not following or following.get("game_id") != row["game_id"]:
        raise ValueError("No following scrimmage state for this kickoff")
    placement = any(f["type"] in KICK_PLACEMENT for f in fouls)
    if placement and (
        len(fouls) > 1
        or re.search(r"kicks onside|Illegal Touch", row.get("desc") or "")
        or not re.search(r"placed at", row.get("desc") or "")
    ):
        raise ValueError("Kick-rule placement has no foul-free result in the feed")
    if score_stands(prefix, row.get("desc") or ""):
        raise ValueError("Return touchdown stands; enforcement falls on the try or next kick")
    actual = state_from_row(following, receiver)
    key = {row["posteam"]: "posteam_score", row["defteam"]: "defteam_score"}
    if (actual.home_score, actual.away_score) != (
        row[key[actual.home_team]],
        row[key[actual.away_team]],
    ) or (actual.qtr <= 2) != (row["qtr"] <= 2):
        raise ValueError("Score or half changed before the next snap; not compared")
    note = approximation(prefix, kick_spot)
    if placement:
        # The kick never stayed in play, so the only foul-free reference is a typical kick.
        if kick_spot is None:
            raise ValueError("Kick placement needs a measured post-kick start spot")
        noflag = changed(
            actual,
            possession=row["posteam"],
            yardline_100=kick_spot,
            down=1,
            ydstogo=min(10, kick_spot),
        )
        note = (
            f"Approximate: the placement against a kick kept in play, read as the season mean "
            f"start, {kick_spot:g} yards from goal. The feed has no foul-free result for this kick."
        )
    else:
        noflag = kick_return_state(actual, prefix, row["posteam"], kick_spot)
    actual_id = "accept" if len(fouls) == 1 else "as_called"
    assumptions = [
        "Compares the next recorded snap with the written kick result, both at that snap's clock "
        "and timeouts. Rows without a scrimmage state have no earlier value to subtract.",
        *([note] if note else []),
    ]
    branches = [
        make_branch(
            "no_flag",
            "No flag; keep the written kick result",
            noflag,
            restart="snap",
            assumptions=assumptions,
        ),
        make_branch(
            actual_id,
            "Observed snap after enforcement",
            actual,
            restart="snap",
            evidence="observed",
            assumptions=assumptions,
        ),
    ]
    decision = evaluate_decision(
        DecisionRequest(
            reference_team=row["posteam"],
            before=noflag,
            actual_branch=actual_id,
            branches=branches,
            timing_delays=[0],
        ),
        scorer,
    )
    event.update(
        before=noflag.model_dump(),
        enforcement_baseline="season_mean_kick" if placement else "written_kick_result",
        approximate=note,
        valuation_status="valued",
    )
    finish_decision(event, decision, noflag)
    event["opportunity"] = opportunity(
        scorer, row["posteam"], {"no_flag": noflag, "actual": actual}, "no_flag", "actual"
    )


def evaluate_row(
    row: dict,
    following: dict | None,
    receiver: str | None,
    scorer: Scorer,
    clock_context: list[dict] | None = None,
    kick_spot: float | None = None,
    try_rate: dict[str, float] | None = None,
) -> dict:
    _, prefix, _ = parse_fouls(row)
    event = value_row(row, following, receiver, scorer, clock_context, kick_spot, try_rate)
    event["unpriced_reason"] = unpriced_reason(row, event, prefix)
    return event


def value_row(
    row: dict,
    following: dict | None,
    receiver: str | None,
    scorer: Scorer,
    clock_context: list[dict] | None,
    kick_spot: float | None,
    try_rate: dict[str, float] | None = None,
) -> dict:
    fouls, prefix, revised = parse_fouls(row)
    disposition = (
        fouls[0]["disposition"] if len({f["disposition"] for f in fouls}) == 1 else "mixed"
    )
    game_id, play_id = row["game_id"], str(int(row["play_id"]))
    runoff = runoff_info(row.get("desc") or "")
    event = {
        "id": f"{game_id}:{play_id}",
        "game_id": game_id,
        "play_id": play_id,
        "season": row["season"],
        "week": row["week"],
        "drive_id": f"{game_id}:{int(row['fixed_drive'])}"
        if finite(row.get("fixed_drive"))
        else None,
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "reference_team": row.get("posteam"),
        "qtr": row.get("qtr"),
        "time": row.get("time"),
        "description": row.get("desc"),
        "fouls": fouls,
        "disposition": disposition,
        "source_revised": revised,
        "runoff": runoff,
        "observed_play_epa": row.get("epa") if finite(row.get("epa")) else None,
        "enforcement_epa": None,
        "enforcement_baseline": None,
        "enforcement_wp_swing_pp": None,
        "enforcement_wp_vegas_pp": None,
        "enforcement_wp_common_clock_pp": None,
        "enforcement_wp_vegas_common_clock_pp": None,
        "enforcement_wp_basis": None,
        "enforcement_wp_range": None,
        "enforcement_wp_restarts": None,
        "home_epa_impact": None,
        "home_wp_swing_pp": None,
        "home_wp_vegas_swing_pp": None,
        "approximate": None,
        "opportunity": None,
        "valuation_status": "unpriced",
        "decision": None,
        "choice_edge_epa": None,
        "choice_edge_wp_pp": None,
        "choice_edge_wp_scenarios": None,
        "choice_edge_scenarios": None,
        "nullified_play": nullified_record(row, prefix, disposition),
        "issues": [],
        "linked_clock_events": [],
    }
    if disposition in {"declined", "rescinded"} and not runoff["applied_seconds"]:
        # At the common clock instant, a wholly declined flag does not alter the
        # observed play. This says nothing about accepting it or a later clock delay.
        event.update(
            enforcement_epa=0.0,
            enforcement_wp_swing_pp=0.0,
            home_epa_impact=0.0,
            home_wp_swing_pp=0.0,
            home_wp_vegas_swing_pp=0.0,
            enforcement_wp_vegas_pp=0.0,
            valuation_status="partial",
        )
    if (
        row.get("play_type") == "kickoff"
        and finite(row.get("qtr"))
        and row["qtr"] <= 4
        and disposition not in {"declined", "rescinded"}
    ):
        try:
            evaluate_kickoff(row, event, fouls, prefix, following, receiver, scorer, kick_spot)
        except (ValueError, TypeError, KeyError) as exc:
            event["issues"].append(str(exc))
        return event
    if written_try(prefix) is not None and disposition == "accepted":
        try:
            evaluate_try(row, event, prefix, clock_context, receiver, scorer, kick_spot, try_rate)
        except (ValueError, TypeError, KeyError) as exc:
            event["issues"].append(str(exc))
        return event
    try:
        before = state_from_row(row, receiver)
    except (ValueError, TypeError, KeyError):
        event["issues"].append(
            "Pre-state is not an applicable regulation scrimmage "
            "state; source play EPA is retained."
        )
        return event
    event["before"] = before.model_dump()
    if score_stands(prefix, row.get("desc") or "") and disposition not in {"declined", "rescinded"}:
        event["issues"].append(
            "The written score stands and enforcement falls on the try or kickoff; "
            "that effect is not valued."
        )
        return event
    timeout_team = None
    for admin in clock_context or []:
        if (
            admin.get("game_id") == game_id
            and admin.get("timeout") == 1
            and admin.get("timeout_team") in {before.home_team, before.away_team}
            and (runoff["avoided"] or runoff_info(admin.get("desc") or "")["avoided"])
            and disposition == "accepted"
        ):
            key = "home_timeouts" if admin["timeout_team"] == before.home_team else "away_timeouts"
            # Admin-row timeout counts are post-charge. Verify exactly one was spent.
            remaining = admin.get(key.replace("timeouts", "timeouts_remaining"))
            if finite(remaining) and getattr(before, key) - remaining == 1:
                timeout_team = admin["timeout_team"]
                runoff.update(
                    mentioned=True,
                    avoided=True,
                    applied_seconds=0,
                    evidence="Runoff avoidance linked to adjacent charged-timeout record",
                )
                event["linked_clock_events"].append(
                    {
                        "play_id": str(int(admin["play_id"])),
                        "timeout_team": timeout_team,
                        "description": admin.get("desc"),
                    }
                )
                break
    dead_ball_foul = (
        len(fouls) == 1 and fouls[0]["type"] in DEAD and "No Play" in (row.get("desc") or "")
    )
    no_action = not dead_ball_foul and no_action_written(row, prefix)
    dead = dead_ball_foul or no_action
    event["enforcement_baseline"] = "fresh_down" if dead else "described_play_result"
    observed = None
    if following and following.get("game_id") == game_id:
        try:
            next_state = state_from_row(following, receiver)
            # A safety enforced on the offense is on the next snap's board: two for the defense.
            enforced_safety = (
                2 * (before.possession == before.away_team),
                2 * (before.possession == before.home_team),
            )
            added = (
                next_state.home_score - before.home_score,
                next_state.away_score - before.away_score,
            )
            if (
                added == (0, 0)
                or (
                    added == enforced_safety
                    and "enforced in End Zone, SAFETY" in (row.get("desc") or "")
                )
            ) and (next_state.qtr <= 2) == (before.qtr <= 2):
                observed = changed(
                    next_state,
                    home_timeouts=before.home_timeouts,
                    away_timeouts=before.away_timeouts,
                )
        except (ValueError, TypeError, KeyError):
            pass
    # For pre-snap flags the administration starts at the reported timestamp. A reported
    # runoff is applied once below, even when the next snap's clock already reflects it.
    anchor = before if dead else (observed or before)
    if not dead and observed is not None and runoff["applied_seconds"]:
        # The following timestamp already includes the deduction. Add it back for the
        # pre-deduction anchor, then apply it exactly once in the clock intervention.
        restored = observed.half_seconds_remaining + runoff["applied_seconds"]
        if restored <= before.half_seconds_remaining:
            anchor = changed(
                observed,
                half_seconds_remaining=restored,
                game_seconds_remaining=observed.game_seconds_remaining + runoff["applied_seconds"],
            )
        else:
            event["issues"].append(
                "Reported runoff conflicts with the adjacent clocks; no timestamp is invented "
                "and no second deduction is charged."
            )
            return event
    base = changed(
        before,
        qtr=anchor.qtr,
        game_seconds_remaining=anchor.game_seconds_remaining,
        half_seconds_remaining=anchor.half_seconds_remaining,
    )
    branches = []
    noflag = None
    try:
        if fouls[0]["type"] == "Clock administration" and runoff["applied_seconds"] and observed:
            # The counterfactual removes only the runoff, not the replay correction.
            # This also handles corrected fumbles whose endpoint is in the next state.
            noflag = changed(
                observed,
                qtr=base.qtr,
                game_seconds_remaining=base.game_seconds_remaining,
                half_seconds_remaining=base.half_seconds_remaining,
            )
        else:
            noflag = base if dead else play_result(base, row, prefix, kick_spot)
        restart = noflag_restart(prefix, before, dead)
        branches.append(
            make_branch(
                "no_flag",
                "Fresh down without yardage enforcement"
                if dead
                else "No flag; keep the described play result",
                noflag,
                restart=restart,
            )
        )
        branches.append(
            make_branch(
                "decline",
                "Decline yardage; down remains unplayed"
                if dead
                else "Decline enforcement; keep the play result",
                noflag,
                restart=penalty_restart(
                    before, prefix, dead, fouls[0]["team"] == before.possession, 0
                ),
            )
        )
    except ValueError as exc:
        event["issues"].append(str(exc))
    actual_id = "decline" if disposition in {"declined", "rescinded"} else "accept"
    accepted = None
    if len(fouls) == 1:
        try:
            # Prefer observed accepted geometry. For dead-ball rows retain the source time,
            # then explicitly subtract only the reported runoff.
            if fouls[0]["type"] == "Clock administration" and runoff["applied_seconds"]:
                if noflag is None:
                    raise ValueError("Clock administration needs the corrected play state")
                accepted = noflag
            elif fouls[0]["disposition"] == "accepted" and observed is not None:
                accepted = changed(
                    observed,
                    qtr=base.qtr,
                    game_seconds_remaining=base.game_seconds_remaining,
                    half_seconds_remaining=base.half_seconds_remaining,
                )
            else:
                accepted = acceptance(base, fouls[0], prefix, None)
            branches.append(
                make_branch(
                    "accept",
                    "Accept penalty enforcement",
                    accepted,
                    runoff=runoff["applied_seconds"],
                    timeout_team=timeout_team,
                    restart="snap"
                    if timeout_team
                    else penalty_restart(
                        before,
                        prefix,
                        dead,
                        fouls[0]["team"] == before.possession,
                        runoff["applied_seconds"],
                    ),
                )
            )
            if timeout_team:
                branches[-1].assumptions.append(
                    "An adjacent source row explicitly records a charged timeout avoiding runoff. "
                    "The actual branch spends it once; the no-enforcement alternative retains it. "
                    "This does not assert that the coach would never otherwise choose a timeout."
                )
        except ValueError as exc:
            event["issues"].append(str(exc))
            if (
                fouls[0]["type"] == "Defensive Pass Interference"
                and fouls[0]["team"] != before.possession
            ):
                # Sample explicitly labeled spots. Never equate catch yardage with the foul spot.
                gains = sorted({2.0, 5.0, 10.0, 20.0, before.yardline_100 - 1})
                for gain in gains:
                    if 1 < gain < before.yardline_100:
                        s = advance(
                            base, before.yardline_100 - gain, consumed=False, automatic_first=True
                        )
                        branches.append(
                            make_branch(
                                f"accept_spot_{int(gain)}",
                                f"Accept DPI IF foul was {gain:g} yards downfield",
                                s,
                                evidence="scenario",
                                assumptions=[
                                    "DPI foul spot is missing. This is a sampled legal-spot "
                                    "scenario, not the observed spot.",
                                    "A caught ball's final location is not assumed to be the "
                                    "interference spot.",
                                ],
                            )
                        )
                goal_spot = 1 if base.yardline_100 >= 2 else base.yardline_100 / 2
                branches.append(
                    make_branch(
                        "accept_endzone",
                        "Accept DPI IF interference was in the end zone",
                        advance(base, goal_spot, consumed=False, automatic_first=True),
                        evidence="scenario",
                        assumptions=[
                            "Conditional end-zone DPI spot under Rule 8-5: ball at the one, "
                            "or half the distance when the previous spot is inside the two."
                        ],
                    )
                )
    elif disposition not in {"declined", "rescinded"}:
        actual_id = "as_called"
        if observed:
            branches.append(
                make_branch(
                    "as_called",
                    "Observed combined/offsetting enforcement",
                    changed(
                        observed,
                        qtr=base.qtr,
                        half_seconds_remaining=base.half_seconds_remaining,
                        game_seconds_remaining=base.game_seconds_remaining,
                    ),
                    runoff=runoff["applied_seconds"],
                    timeout_team=timeout_team,
                    restart="snap"
                    if timeout_team
                    else penalty_restart(before, prefix, dead, False, runoff["applied_seconds"]),
                    evidence="observed",
                )
            )
        event["issues"].append(
            "Multiple fouls are valued jointly; individual "
            "acceptance options need joint enforcement evidence."
        )
    if (
        accepted is not None
        and runoff["applied_seconds"]
        and len(fouls) == 1
        and fouls[0]["team"] == before.possession
        and before.half_seconds_remaining <= 120
        and not re.search(r"replay|review|REVERSED", row.get("desc") or "", re.I)
    ):
        # These are legal-choice sensitivities, never claims about an observed waiver.
        branches.append(
            make_branch(
                "accept_waive_runoff",
                "Accept yardage IF defense waives foul-related runoff",
                accepted,
                restart="snap",
                evidence="scenario",
                assumptions=[
                    "Conditional on defense waiving this foul-related runoff "
                    "under Rule 4-7; not a replay-review runoff."
                ],
            )
        )
        key = "home_timeouts" if before.possession == before.home_team else "away_timeouts"
        if getattr(accepted, key) > 0:
            branches.append(
                DecisionBranch(
                    id="accept_timeout",
                    label="Accept yardage IF offense spends a timeout to avoid runoff",
                    state=accepted,
                    clock=Clock(
                        restart="snap",
                        timeout_team=before.possession,
                        evidence="Conditional Rule 4-7 timeout alternative; one timeout "
                        "is consumed.",
                    ),
                    evidence="scenario",
                    assumptions=[
                        "This timeout was available in the pre-state; spending "
                        "it is a scenario, not an observed timeout."
                    ],
                )
            )
    nullified = event["nullified_play"]
    if nullified and nullified["recorded"]:
        try:
            branches.append(written_erased_branch(base, prefix, kick_spot))
        except ValueError as exc:
            nullified["reason"] = str(exc)
    ids = {b.id for b in branches}
    if actual_id not in ids and actual_id == "decline" and observed:
        branches.append(
            make_branch(
                "decline", "Observed play with enforcement declined", observed, evidence="observed"
            )
        )
        ids.add("decline")
    if actual_id not in ids or len(branches) < 2:
        event["issues"].append(
            "Insufficient branch states for a numerical comparison; not assumed zero."
        )
        return event
    try:
        decision = evaluate_decision(
            DecisionRequest(
                reference_team=before.possession,
                before=before,
                actual_branch=actual_id,
                branches=branches,
                timing_delays=[0, 10, 20]
                if before.half_seconds_remaining <= (300 if before.qtr == 4 else 120)
                else [0],
            ),
            scorer,
        )
    except ValueError as exc:
        event["issues"].append(str(exc))
        return event
    finish_decision(event, decision, before)
    event["approximate"] = None if dead else approximation(prefix, kick_spot)
    if no_action:
        event["approximate"] = (
            "Approximate: nothing is written before the flag on this no-play row, so the "
            "foul-free state is the same down, not yet played."
        )
    elif "enforced in End Zone, SAFETY" in (row.get("desc") or ""):
        event["approximate"] = (
            "Approximate: the ruling is the next recorded snap, after the safety and its free kick."
        )
    if event["approximate"]:
        for branch in decision["branches"]:
            if branch["id"] in {"no_flag", "decline", "erased_result"}:
                branch["assumptions"] = [*branch["assumptions"], event["approximate"]]
    erased = next((b for b in decision["branches"] if b["id"] == "erased_result"), None)
    if nullified and erased:
        nullified.update(
            status="valued",
            reason="Automatically reconstructed from the written play description.",
            impact_epa=erased["actual_minus_branch_epa"],
            wp_swing_pp=erased["actual_minus_branch_wp_pp"],
            alternative_branch="erased_result",
        )
    nf = next((b for b in decision["branches"] if b["id"] == "no_flag"), None)
    actual_state = next(b.state for b in branches if b.id == actual_id)
    states = {"before": before, "actual": actual_state}
    if noflag is not None:
        states["no_flag"] = noflag
    event["opportunity"] = opportunity(scorer, before.possession, states, "before", "actual")
    alternatives = [
        b
        for b in decision["branches"]
        if len(fouls) == 1
        and (b["id"].startswith("accept") if actual_id == "decline" else b["id"] == "decline")
    ]
    if len(alternatives) == 1 and alternatives[0]["evidence"] != "scenario":
        event["choice_edge_epa"] = alternatives[0]["actual_minus_branch_epa"]
        event["choice_edge_wp_pp"] = alternatives[0]["actual_minus_branch_wp_pp"]
    elif alternatives:
        values = [b["actual_minus_branch_epa"] for b in alternatives]
        event["choice_edge_scenarios"] = {
            "min": min(values),
            "max": max(values),
            "meaning": "Range across sampled assumptions, not a confidence "
            "interval or exhaustive bound",
        }
    wp_values = [
        b["actual_minus_branch_wp_pp"]
        for b in alternatives
        if b["actual_minus_branch_wp_pp"] is not None
    ]
    if event["choice_edge_scenarios"] is not None and wp_values:
        event["choice_edge_wp_scenarios"] = {
            "min": min(wp_values),
            "max": max(wp_values),
            "meaning": "Sampled assumption range in percentage points; not a confidence interval",
        }
    event["valuation_status"] = (
        "scenario"
        if any(b.evidence == "scenario" for b in branches)
        else "valued"
        if nf
        else "partial"
    )
    return event


def written_try(prefix: str) -> int | None:
    """Points a written try would have scored, or None if this is no written try."""
    return next((points for text, points in TRY_RESULTS.items() if text in prefix), None)


def try_rates(rows: list[dict]) -> dict[int, dict[str, float]]:
    """Measured per season: how often a played try converts, by kind. Distance is not separated."""
    counts = defaultdict(lambda: {"xp": [0, 0], "two": [0, 0]})
    for row in rows:
        for kind, column, made in (
            ("xp", "extra_point_result", "good"),
            ("two", "two_point_conv_result", "success"),
        ):
            if row.get(column):
                counts[row["season"]][kind][0] += row[column] == made
                counts[row["season"]][kind][1] += 1
    return {
        season: {kind: made / tried for kind, (made, tried) in kinds.items() if tried}
        for season, kinds in counts.items()
    }


def evaluate_try(
    row: dict,
    event: dict,
    prefix: str,
    later: list[dict] | None,
    receiver: str | None,
    scorer: Scorer,
    kick_spot: float | None,
    try_rate: dict[str, float] | None,
) -> None:
    """A written try erased for a re-try: its points against the re-try at the season's rate.

    A try has no scrimmage state, so both sides are read after the next kick: the opponent's ball
    at the season's mean spot, with the points on the board, at the row's clock.
    """
    if not finite(row.get("qtr")) or row["qtr"] > 4 or "No Play" not in (row.get("desc") or ""):
        raise ValueError("Only a written try erased for a re-try in regulation is valued")
    if kick_spot is None:
        raise ValueError("A try's value needs a measured post-kick start spot")
    again = next(
        (
            r
            for r in later or []
            if re.search(r"TWO-POINT|extra point|Kick formation", r.get("desc") or "")
        ),
        None,
    )
    if again is None:
        raise ValueError("No re-try is recorded after this try")
    kind = "two" if "TWO-POINT" in again["desc"] else "xp"
    rate = (try_rate or {}).get(kind)
    if rate is None:
        raise ValueError("No measured conversion rate for this try")
    team = row["posteam"]
    scored = written_try(prefix)
    after = state_from_row(
        row
        | {
            "posteam": row["defteam"],
            "defteam": team,
            "posteam_score": row["defteam_score"],
            "defteam_score": row["posteam_score"],
            "down": 1,
            "ydstogo": min(10, kick_spot),
            "yardline_100": kick_spot,
        },
        receiver,
    )
    key = "home_score" if team == after.home_team else "away_score"
    worth = {"xp": 1, "two": 2}[kind]

    def board(points: int) -> State:
        return changed(after, **{key: getattr(after, key) + points})

    written, made, missed = board(scored), board(worth), board(0)
    values = scorer.predict([written, made, missed], team)
    vegas = scorer.vegas_wp([written, made, missed], team)
    margin = [
        s.score_diff(team) + v.ep for s, v in zip((written, made, missed), values, strict=True)
    ]
    event["enforcement_epa"] = rate * margin[1] + (1 - rate) * margin[2] - margin[0]
    event["enforcement_wp_swing_pp"] = 100 * (
        rate * values[1].wp + (1 - rate) * values[2].wp - values[0].wp
    )
    event["enforcement_wp_vegas_pp"] = (
        100 * (rate * vegas[1] + (1 - rate) * vegas[2] - vegas[0]) if None not in vegas else None
    )
    sign = 1 if team == after.home_team else -1
    event.update(
        before=written.model_dump(),
        enforcement_baseline="written_try_result",
        enforcement_wp_common_clock_pp=event["enforcement_wp_swing_pp"],
        enforcement_wp_vegas_common_clock_pp=event["enforcement_wp_vegas_pp"],
        enforcement_wp_basis="common_clock",
        home_epa_impact=event["enforcement_epa"] * sign,
        home_wp_swing_pp=event["enforcement_wp_swing_pp"] * sign,
        home_wp_vegas_swing_pp=event["enforcement_wp_vegas_pp"] * sign
        if event["enforcement_wp_vegas_pp"] is not None
        else None,
        valuation_status="valued",
        approximate=f"Approximate: the written try counted as {scored} against the re-try at the "
        f"season's {'two-point' if kind == 'two' else 'extra-point'} rate ({rate:.0%}), with no "
        f"adjustment for distance, each read after the next kick {kick_spot:g} yards from goal "
        "at the same clock.",
    )
    if event.get("nullified_play"):
        event["nullified_play"].update(
            status="valued",
            recorded=True,
            reason="The written try is compared with the re-try that followed it.",
            impact_epa=event["enforcement_epa"],
            wp_swing_pp=event["enforcement_wp_swing_pp"],
            reference_team=team,
        )


def kickoff_start_spots(rows: list[dict]) -> dict[int, float]:
    """Measured: whole-yard mean distance to goal on the receiver's first snap after a kickoff.

    `rows` must be in game order. Kicks the kicking team recovered do not count, since the
    alternatives this feeds hand the ball to the receiving team.
    """
    seen = defaultdict(list)
    pending = None
    for row in rows:
        if row.get("play_type") == "kickoff":
            pending = row
        elif pending is not None and row.get("game_id") != pending.get("game_id"):
            pending = None
        elif pending is not None and row.get("down") in (1, 2, 3, 4):
            if (
                row.get("down") == 1
                and row.get("posteam") == pending.get("posteam")
                and finite(row.get("yardline_100"))
            ):
                seen[row["season"]].append(row["yardline_100"])
            pending = None
    return {season: float(round(sum(v) / len(v))) for season, v in seen.items()}


def build_penalties(
    frame: pl.DataFrame,
    scorer: Scorer,
    kick_spots: dict[int, float] | None = None,
    rates: dict[int, dict[str, float]] | None = None,
) -> list[dict]:
    ordering = ["game_id", "order_sequence" if "order_sequence" in frame.columns else "play_id"]
    rows = frame.sort(ordering).to_dicts()
    receivers = second_half_receivers(rows)
    if kick_spots is None:
        kick_spots = kickoff_start_spots(rows)
    if rates is None:
        rates = try_rates(rows)
    following = {}
    next_index = None
    for i in range(len(rows) - 1, -1, -1):
        following[i] = next_index
        if rows[i].get("down") in (1, 2, 3, 4):
            next_index = i
    return [
        evaluate_row(
            r,
            rows[following[i]] if following[i] is not None else None,
            receivers.get(r["game_id"]),
            scorer,
            clock_context=rows[i + 1 : following[i]] if following[i] is not None else [],
            kick_spot=kick_spots.get(r["season"]),
            try_rate=rates.get(r["season"]),
        )
        for i, r in enumerate(rows)
        if is_flagged(r)
    ]


def took_effect(event: dict) -> bool:
    """A declined or picked-up flag changes nothing, so its exact zero is not a measurement."""
    return event.get("disposition") not in {"declined", "rescinded"}


def coverage(events: list[dict]) -> dict:
    effective = [e for e in events if took_effect(e)]
    return {
        "events": len(events),
        "fouls": sum(len(e["fouls"]) for e in events),
        "dispositions": dict(Counter(e["disposition"] for e in events)),
        "took_effect": len(effective),
        "valued_enforcement": sum(e["enforcement_epa"] is not None for e in effective),
        "unpriced": sum(e["enforcement_epa"] is None for e in effective),
        "unpriced_by_reason": dict(
            Counter(e["unpriced_reason"] for e in effective if e.get("unpriced_reason"))
        ),
        "valued_choices": sum(e["choice_edge_epa"] is not None for e in events),
        "scenario_choices": sum(e["choice_edge_scenarios"] is not None for e in events),
        "reported_runoffs": sum(e["runoff"]["applied_seconds"] > 0 for e in events),
        "nullified_plays": dict(
            Counter(e["nullified_play"]["status"] for e in events if e.get("nullified_play"))
        ),
    }
