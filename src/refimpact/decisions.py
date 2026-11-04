"""Explicit accept/decline/no-flag branches and clock interventions.

The published EP/WP models have no clock-running feature. Clock status is valued by
evaluating declared next-snap delays, never by inventing an extra learned feature.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from refimpact.models import MODEL_ID, Scorer
from refimpact.schema import Record, State


class Clock(Record):
    restart: Literal["running", "ready", "snap", "unknown"] = "unknown"
    runoff_seconds: float = Field(default=0, ge=0, le=10)
    timeout_team: str | None = None
    evidence: str = "Unreported clock restart; timing scenarios are conditional."


class DecisionBranch(Record):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    label: str = Field(min_length=1, max_length=160)
    state: State
    clock: Clock = Field(default_factory=Clock)
    evidence: Literal["observed", "reconstructed", "scenario", "manual"] = "manual"
    assumptions: list[str] = Field(min_length=1)


class DecisionRequest(Record):
    reference_team: str
    before: State
    actual_branch: str
    branches: list[DecisionBranch] = Field(min_length=2, max_length=30)
    timing_delays: list[int] = Field(default_factory=lambda: [0, 10, 20], max_length=5)

    @model_validator(mode="after")
    def consistent(self):
        ids = [b.id for b in self.branches]
        if len(set(ids)) != len(ids) or self.actual_branch not in ids:
            raise ValueError("Branch IDs must be unique and include actual_branch")
        if self.reference_team not in {self.before.home_team, self.before.away_team}:
            raise ValueError("reference_team is not a game team")
        if any(delay < 0 or delay > 40 for delay in self.timing_delays):
            raise ValueError("Timing delays must be between 0 and 40 seconds")
        for branch in self.branches:
            s = branch.state
            for key in (
                "season",
                "home_team",
                "away_team",
                "roof",
                "neutral_site",
                "second_half_receiver",
            ):
                if getattr(s, key) != getattr(self.before, key):
                    raise ValueError(f"Branch disagrees on {key}")
            if (s.qtr <= 2) != (self.before.qtr <= 2):
                raise ValueError("Cross-half branches need a different EP horizon")
            if s.game_seconds_remaining > self.before.game_seconds_remaining:
                raise ValueError("A branch cannot precede the starting state")
            if branch.clock.timeout_team not in {None, s.home_team, s.away_team}:
                raise ValueError("Unknown timeout team")
            if branch.clock.timeout_team:
                key = (
                    "home_timeouts" if branch.clock.timeout_team == s.home_team else "away_timeouts"
                )
                if getattr(s, key) == 0:
                    raise ValueError("Cannot spend a timeout when none remain")
                if branch.clock.runoff_seconds:
                    raise ValueError("A timeout used to avoid runoff cannot also apply that runoff")
        return self


def clock_state(
    state: State, seconds: float, timeout_team: str | None = None
) -> tuple[State | None, str]:
    changes = {}
    if timeout_team:
        key = "home_timeouts" if timeout_team == state.home_team else "away_timeouts"
        if getattr(state, key) <= 0:
            raise ValueError("No timeout available")
        changes[key] = getattr(state, key) - 1
    if seconds >= state.half_seconds_remaining:
        return None, "half_end" if state.qtr <= 2 else "regulation_end"
    changes.update(
        game_seconds_remaining=state.game_seconds_remaining - seconds,
        half_seconds_remaining=state.half_seconds_remaining - seconds,
    )
    # A delay may cross a quarter but never silently cross a half.
    half = changes["half_seconds_remaining"]
    changes["qtr"] = (1 if half > 900 else 2) if state.qtr <= 2 else (3 if half > 900 else 4)
    return State.model_validate(state.model_dump() | changes), "scrimmage"


def value_branch(
    scorer: Scorer,
    before: State,
    team: str,
    branch: DecisionBranch,
    delay: float = 0,
    assumed_restart: str | None = None,
) -> dict:
    restart = assumed_restart or branch.clock.restart
    if restart == "unknown" and delay:
        raise ValueError("Unknown clock restart requires an explicit timing scenario")
    elapsed = branch.clock.runoff_seconds + (delay if restart in {"running", "ready"} else 0)
    effective, phase = clock_state(branch.state, elapsed, branch.clock.timeout_team)
    pre = scorer.predict([before], team)[0]
    points = branch.state.score_diff(team) - before.score_diff(team)
    if effective is None:
        ep = 0.0
        # A regulation-end tie proceeds to overtime, whose WP is not modeled here.
        wp = (
            float(branch.state.score_diff(team) > 0)
            if phase == "regulation_end" and branch.state.score_diff(team) != 0
            else None
        )
    else:
        prediction = scorer.predict([effective], team)[0]
        ep, wp = prediction.ep, prediction.wp
    baseline = scorer.predict([branch.state], team)[0]
    return {
        "id": branch.id,
        "label": branch.label,
        "evidence": branch.evidence,
        "state": effective.model_dump() if effective else None,
        "base_state": branch.state.model_dump(),
        "phase": phase,
        "ep": ep,
        "wp": wp,
        "wp_before": pre.wp,
        "wpa_pp": 100 * (wp - pre.wp) if wp is not None else None,
        "epa": points + ep - pre.ep,
        "clock_epa_component": ep - baseline.ep,
        "clock_wp_component_pp": 100 * (wp - baseline.wp) if wp is not None else None,
        "runoff_seconds": branch.clock.runoff_seconds,
        "clock_seconds_consumed": min(elapsed, branch.state.half_seconds_remaining),
        "restart": restart,
        "timeout_team": branch.clock.timeout_team,
        "clock_evidence": branch.clock.evidence,
        "assumptions": branch.assumptions,
    }


def evaluate_decision(request: DecisionRequest, scorer: Scorer) -> dict:
    values = [
        value_branch(scorer, request.before, request.reference_team, branch)
        for branch in request.branches
    ]
    actual = next(b for b in values if b["id"] == request.actual_branch)
    for b in values:
        b["actual_minus_branch_epa"] = actual["epa"] - b["epa"]
        b["actual_minus_branch_wp_pp"] = (
            100 * (actual["wp"] - b["wp"])
            if actual["wp"] is not None and b["wp"] is not None
            else None
        )
    timing = []
    for delay in sorted(set(request.timing_delays) - {0}):
        for branch in request.branches:
            # Preserve both possibilities if the feed does not state when the clock restarts.
            restarts = (
                ["running", "snap"] if branch.clock.restart == "unknown" else [branch.clock.restart]
            )
            for restart in restarts:
                v = value_branch(
                    scorer, request.before, request.reference_team, branch, delay, restart
                )
                timing.append(
                    v
                    | {
                        "next_snap_delay_seconds": delay,
                        "conditional_clock_assumption": branch.clock.restart == "unknown",
                    }
                )
    comparisons = []
    for delay in sorted(set(request.timing_delays) - {0}):
        cases = [v for v in timing if v["next_snap_delay_seconds"] == delay]
        actual_cases = [v for v in cases if v["id"] == request.actual_branch]
        for a in actual_cases:
            for b in cases:
                if b["id"] == request.actual_branch:
                    continue
                base = next(v for v in values if v["id"] == b["id"])
                swing = (
                    100 * (a["wp"] - b["wp"])
                    if a["wp"] is not None and b["wp"] is not None
                    else None
                )
                comparisons.append(
                    {
                        "alternative_branch": b["id"],
                        "alternative_label": b["label"],
                        "next_snap_delay_seconds": delay,
                        "actual_restart": a["restart"],
                        "alternative_restart": b["restart"],
                        "conditional_clock_assumption": a["conditional_clock_assumption"]
                        or b["conditional_clock_assumption"],
                        "actual_wp": a["wp"],
                        "alternative_wp": b["wp"],
                        "actual_wpa_pp": a["wpa_pp"],
                        "alternative_wpa_pp": b["wpa_pp"],
                        "actual_minus_branch_epa": a["epa"] - b["epa"],
                        "actual_minus_branch_wp_pp": swing,
                        "additional_clock_epa": a["epa"]
                        - b["epa"]
                        - base["actual_minus_branch_epa"],
                        "additional_clock_wp_pp": swing - base["actual_minus_branch_wp_pp"]
                        if swing is not None and base["actual_minus_branch_wp_pp"] is not None
                        else None,
                    }
                )
    return {
        "reference_team": request.reference_team,
        "wp_before": actual["wp_before"],
        "model_id": MODEL_ID,
        "actual_branch": request.actual_branch,
        "branches": values,
        "timing_scenarios": timing,
        "timing_comparisons": comparisons,
        "timing_note": "Delays are sensitivity scenarios, not probabilities or "
        "observed time saved. "
        "Ready/running scenarios consume the declared game-clock delay; snap scenarios "
        "do not. The native model has no clock-status feature.",
    }
