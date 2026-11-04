"""All reported flags: dispositions, decision alternatives, and clock evidence.

Missing spots stay missing. Conditional DPI spot/clock cases are scenarios, never
silently assigned probabilities or included as observed enforcement effects.
"""

from __future__ import annotations

import math
import re
from collections import Counter

import polars as pl

from refimpact.decisions import Clock, DecisionBranch, DecisionRequest, evaluate_decision
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


def play_result(before: State, row: dict, prefix: str) -> State:
    if re.search(r"TOUCHDOWN|SAFETY|FUMBLE|field goal|punts|kicks", prefix, re.I):
        raise ValueError(
            "Scoring, turnover or kicking alternative needs additional outcome evidence"
        )
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
        endpoints = re.findall(r"\bto ([A-Z]{2,3}) (\d+) for (?:-?\d+ yards?|no gain)", tail)
        endpoint = endpoints[0] if len(endpoints) == 1 else None
        if endpoint and endpoint[0] in {before.home_team, before.away_team}:
            yard = float(endpoint[1])
            original_coordinate = yard if endpoint[0] == other else 100 - yard
            return advance(before, original_coordinate, consumed=True, possession=other)
        raise ValueError("Interception return endpoint is not recoverable from the description")
    if re.search(r"pass incomplete", prefix, re.I):
        return advance(before, before.yardline_100, consumed=True)
    prefix = re.sub(r"for a loss of (\d+) yards?", r"for -\1 yards", prefix, flags=re.I)
    gains = re.findall(r"for (-?\d+) yards?\b", prefix, re.I)
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
    if re.search(r"INTERCEPT", prefix, re.I):
        return "snap"  # A change of possession stops the game clock.
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
            r"sacked|INTERCEPTED|FUMBLES?|TOUCHDOWN|SAFETY|punts|field goal)\b",
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


def written_erased_branch(base: State, prefix: str) -> DecisionBranch:
    # Disable the usual structured-yards fallback. nflverse commonly sets those
    # fields to zero precisely because the physical action was nullified.
    written = re.sub(r"for a loss of (\d+) yards?", r"for -\1 yards", prefix, flags=re.I)
    if len(re.findall(r"for -?\d+ yards?\b", written, re.I)) > 1:
        raise ValueError("Multiple written gains need an unambiguous net result; not guessed")
    result = play_result(base, {"play_type": "no_play"}, written)
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


def evaluate_row(
    row: dict,
    following: dict | None,
    receiver: str | None,
    scorer: Scorer,
    clock_context: list[dict] | None = None,
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
        "home_epa_impact": None,
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
            valuation_status="partial",
        )
    try:
        before = state_from_row(row, receiver)
    except (ValueError, TypeError, KeyError):
        event["issues"].append(
            "Pre-state is not an applicable regulation scrimmage "
            "state; source play EPA is retained."
        )
        return event
    event["before"] = before.model_dump()
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
    dead = len(fouls) == 1 and fouls[0]["type"] in DEAD and "No Play" in (row.get("desc") or "")
    event["enforcement_baseline"] = "fresh_down" if dead else "described_play_result"
    observed = None
    if following and following.get("game_id") == game_id:
        try:
            next_state = state_from_row(following, receiver)
            if (
                next_state.home_score == before.home_score
                and next_state.away_score == before.away_score
                and (next_state.qtr <= 2) == (before.qtr <= 2)
            ):
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
            noflag = base if dead else play_result(base, row, prefix)
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
            branches.append(written_erased_branch(base, prefix))
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
    event["decision"] = decision
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
    if nf:
        event["enforcement_epa"] = nf["actual_minus_branch_epa"]
        event["enforcement_wp_swing_pp"] = nf["actual_minus_branch_wp_pp"]
        event["home_epa_impact"] = event["enforcement_epa"] * (
            1 if before.possession == before.home_team else -1
        )
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


def build_penalties(frame: pl.DataFrame, scorer: Scorer) -> list[dict]:
    ordering = ["game_id", "order_sequence" if "order_sequence" in frame.columns else "play_id"]
    rows = frame.sort(ordering).to_dicts()
    receivers = second_half_receivers(rows)
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
        )
        for i, r in enumerate(rows)
        if is_flagged(r)
    ]


def coverage(events: list[dict]) -> dict:
    return {
        "events": len(events),
        "fouls": sum(len(e["fouls"]) for e in events),
        "dispositions": dict(Counter(e["disposition"] for e in events)),
        "valued_enforcement": sum(e["enforcement_epa"] is not None for e in events),
        "valued_choices": sum(e["choice_edge_epa"] is not None for e in events),
        "scenario_choices": sum(e["choice_edge_scenarios"] is not None for e in events),
        "reported_runoffs": sum(e["runoff"]["applied_seconds"] > 0 for e in events),
        "nullified_plays": dict(
            Counter(e["nullified_play"]["status"] for e in events if e.get("nullified_play"))
        ),
    }
