import pytest
from pydantic import ValidationError

from refimpact.decisions import Clock, DecisionBranch, DecisionRequest, evaluate_decision
from refimpact.schema import State


def late_state(comparison, seconds=100, qtr=4):
    return State.model_validate(
        comparison.before.model_dump()
        | {
            "qtr": qtr,
            "half_seconds_remaining": seconds,
            "game_seconds_remaining": seconds + (1800 if qtr == 2 else 0),
            "down": 3,
            "ydstogo": 5,
            "yardline_100": 60,
            "home_timeouts": 1,
        }
    )


def branch(id, state, **clock):
    return DecisionBranch(
        id=id,
        label=id,
        state=state,
        clock=Clock(**clock),
        assumptions=["Explicit test alternative"],
    )


def test_twenty_yard_catch_vs_accepting_dpi(comparison, scorer):
    before = late_state(comparison)
    catch = State.model_validate(
        before.model_dump() | {"down": 1, "ydstogo": 10, "yardline_100": 40}
    )
    accepted = State.model_validate(catch.model_dump() | {"yardline_100": 45})
    result = evaluate_decision(
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="decline",
            branches=[
                branch("decline", catch, restart="snap"),
                branch("accept", accepted, restart="snap"),
                branch("no_flag", catch, restart="running"),
            ],
        ),
        scorer,
    )
    values = {b["id"]: b for b in result["branches"]}
    assert values["no_flag"]["actual_minus_branch_epa"] == 0
    assert values["accept"]["actual_minus_branch_epa"] == pytest.approx(
        values["decline"]["epa"] - values["accept"]["epa"]
    )
    assert values["accept"]["actual_minus_branch_epa"] != 0


def test_clock_stoppage_has_value_at_declared_next_snap_horizon(comparison, scorer):
    before = late_state(comparison)
    result = evaluate_decision(
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="stopped",
            branches=[
                branch("stopped", before, restart="snap"),
                branch("running", before, restart="running"),
            ],
            timing_delays=[20],
        ),
        scorer,
    )
    cases = {b["id"]: b for b in result["timing_scenarios"]}
    assert cases["stopped"]["state"]["game_seconds_remaining"] == 100
    assert cases["running"]["state"]["game_seconds_remaining"] == 80
    assert cases["stopped"]["wp"] != cases["running"]["wp"]


def test_ten_second_runoff_applied_once_and_timeout_costs_a_timeout(comparison, scorer):
    before = late_state(comparison, 16)
    result = evaluate_decision(
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="runoff",
            branches=[
                branch("runoff", before, runoff_seconds=10, restart="ready"),
                branch("timeout", before, timeout_team=before.home_team, restart="snap"),
            ],
            timing_delays=[0],
        ),
        scorer,
    )
    values = {b["id"]: b for b in result["branches"]}
    assert values["runoff"]["state"]["game_seconds_remaining"] == 6
    assert values["runoff"]["clock_seconds_consumed"] == 10
    assert values["timeout"]["state"]["home_timeouts"] == 0
    assert values["timeout"]["state"]["game_seconds_remaining"] == 16


@pytest.mark.parametrize("qtr,phase", [(2, "half_end"), (4, "regulation_end")])
def test_runoff_can_expire_half_without_feeding_invalid_state_to_model(
    comparison, scorer, qtr, phase
):
    before = late_state(comparison, 3, qtr)
    result = evaluate_decision(
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="runoff",
            branches=[branch("runoff", before, runoff_seconds=10), branch("no_runoff", before)],
            timing_delays=[0],
        ),
        scorer,
    )
    actual = result["branches"][0]
    assert actual["state"] is None and actual["phase"] == phase
    assert actual["ep"] == 0
    if qtr == 2:
        assert actual["wp"] is None


def test_no_timeout_cannot_avoid_runoff(comparison):
    before = late_state(comparison)
    before.home_timeouts = 0
    with pytest.raises(ValidationError, match="none remain"):
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="a",
            branches=[branch("a", before), branch("b", before, timeout_team=before.home_team)],
        )


def test_unknown_clock_restart_produces_both_scenarios(comparison, scorer):
    before = late_state(comparison)
    result = evaluate_decision(
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="a",
            branches=[branch("a", before), branch("b", before)],
            timing_delays=[10],
        ),
        scorer,
    )
    assert {c["restart"] for c in result["timing_scenarios"]} == {"running", "snap"}
    assert all(c["conditional_clock_assumption"] for c in result["timing_scenarios"])
