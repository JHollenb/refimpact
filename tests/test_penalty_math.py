"""Independent state/outcome counterexamples and accounting identities."""

import pytest
from test_decisions import branch, late_state
from test_penalties import source_row

from refimpact.decisions import DecisionRequest, evaluate_decision
from refimpact.penalties import acceptance, advance, build_penalties, evaluate_row, play_result


def test_erased_interception_touchback_is_not_zero_or_a_kickoff(comparison, scorer):
    r = source_row(
        comparison,
        penalty=1,
        play_type="no_play",
        yards_gained=0,
        desc="Pass INTERCEPTED by defender at ARI -2. Touchback. "
        "PENALTY on ARI, Defensive Holding, 5 yards - No Play.",
    )
    next_row = r | {"down": 1, "ydstogo": 10, "yardline_100": 55}
    e = evaluate_row(r, next_row, comparison.before.second_half_receiver, scorer)
    v = {b["id"]: b for b in e["decision"]["branches"]}
    nf = v["no_flag"]["state"]
    assert (nf["possession"], nf["down"], nf["ydstogo"], nf["yardline_100"]) == ("ARI", 1, 10, 80)
    assert v["no_flag"]["restart"] == "snap"
    assert e["enforcement_epa"] > 0 and e["enforcement_wp_swing_pp"] > 0
    assert e["nullified_play"]["status"] == "valued"
    assert e["nullified_play"]["impact_epa"] == e["enforcement_epa"]


def test_interception_return_uses_final_spot_not_pick_location(comparison):
    s = play_result(
        comparison.before, {}, "Pass INTERCEPTED at ARI 10. Return to BUF 40 for 50 yards."
    )
    assert s.possession == "ARI" and s.yardline_100 == 40
    with pytest.raises(ValueError, match="endpoint"):
        play_result(comparison.before, {}, "Pass INTERCEPTED at ARI 10.")


def test_ambiguous_yards_do_not_pick_last_number(comparison):
    with pytest.raises(ValueError, match="Multiple"):
        play_result(comparison.before, {}, "Run for 10 yards. Lateral for 5 yards.")


def test_failed_fourth_down_uses_opponents_field_and_fixed_reference(comparison, scorer):
    before = late_state(comparison).model_copy(update={"down": 4, "yardline_100": 40})
    failed = advance(before, 42, consumed=True)
    accepted = advance(before, 35, consumed=False, automatic_first=True)
    assert (failed.possession, failed.down, failed.yardline_100) == ("ARI", 1, 58)
    request = dict(
        before=before,
        actual_branch="accept",
        timing_delays=[0],
        branches=[branch("accept", accepted), branch("no_flag", failed)],
    )
    own = evaluate_decision(DecisionRequest(reference_team="BUF", **request), scorer)
    opp = evaluate_decision(DecisionRequest(reference_team="ARI", **request), scorer)
    a, n = own["branches"]
    assert a["epa"] - n["epa"] > 0
    assert n["actual_minus_branch_wp_pp"] == pytest.approx(a["wpa_pp"] - n["wpa_pp"])
    assert n["actual_minus_branch_wp_pp"] == pytest.approx(
        -opp["branches"][1]["actual_minus_branch_wp_pp"]
    )
    assert n["actual_minus_branch_epa"] == pytest.approx(
        -opp["branches"][1]["actual_minus_branch_epa"]
    )
    assert n["ep"] == pytest.approx(-scorer.predict([failed], failed.possession)[0].ep)


def test_wp_is_situation_specific_even_with_identical_yardage(comparison, scorer):
    results = []
    for seconds, diff in [(1600, -14), (50, -3), (50, 14)]:
        qtr = 3 if seconds > 900 else 4
        before = late_state(comparison, seconds, qtr).model_copy(
            update={"away_score": 20, "home_score": 20 - diff}
        )
        accepted = advance(before, before.yardline_100 + 5, consumed=False)
        result = evaluate_decision(
            DecisionRequest(
                reference_team=before.possession,
                before=before,
                actual_branch="accept",
                timing_delays=[0],
                branches=[branch("accept", accepted), branch("no_flag", before)],
            ),
            scorer,
        )
        results.append(result["branches"][1]["actual_minus_branch_wp_pp"])
    assert len({round(x, 3) for x in results}) == 3


def test_clock_comparisons_pair_same_opportunity_and_reconcile_wpa(comparison, scorer):
    before = late_state(comparison, 16)
    result = evaluate_decision(
        DecisionRequest(
            reference_team=before.possession,
            before=before,
            actual_branch="stopped",
            timing_delays=[20],
            branches=[
                branch("stopped", before, restart="snap"),
                branch("no_flag", before, restart="running"),
            ],
        ),
        scorer,
    )
    c = result["timing_comparisons"][0]
    assert c["actual_wp"] == result["branches"][0]["wp"]
    assert c["alternative_wp"] == 0  # BUF is trailing when the running clock expires.
    assert c["actual_minus_branch_wp_pp"] == pytest.approx(
        c["actual_wpa_pp"] - c["alternative_wpa_pp"]
    )
    assert c["additional_clock_wp_pp"] == c["actual_minus_branch_wp_pp"]


def test_inconsistent_runoff_clocks_are_unknown_not_double_charged(comparison, scorer):
    r = source_row(
        comparison,
        qtr=4,
        half_seconds_remaining=21,
        game_seconds_remaining=21,
        penalty=1,
        desc="Pass incomplete. PENALTY on BUF, Intentional Grounding, 12 yards. 10-second runoff.",
    )
    e = evaluate_row(
        r,
        r | {"down": 4, "half_seconds_remaining": 17, "game_seconds_remaining": 17},
        comparison.before.second_half_receiver,
        scorer,
    )
    assert e["enforcement_epa"] is None and e["decision"] is None
    assert any("conflicts" in issue for issue in e["issues"])


def test_run_holding_not_automatically_previous_spot(comparison):
    with pytest.raises(ValueError, match="phase/spot"):
        acceptance(
            comparison.before,
            {"type": "Defensive Holding", "team": "ARI"},
            "Run for 20 yards.",
            None,
        )


def test_dpi_inside_two_uses_half_distance_endzone_scenario(comparison, scorer):
    r = source_row(
        comparison,
        yardline_100=1,
        ydstogo=1,
        yards_gained=0,
        desc="Pass incomplete. Penalty on ARI, Defensive Pass Interference, declined.",
    )
    e = evaluate_row(r, r | {"down": 4}, comparison.before.second_half_receiver, scorer)
    alternatives = [b for b in e["decision"]["branches"] if b["id"].startswith("accept")]
    assert len(alternatives) == 1
    assert alternatives[0]["state"]["yardline_100"] == 0.5
    assert alternatives[0]["state"]["down"] == 1
    assert e["choice_edge_epa"] is None  # The spot is still conditional.


def test_replay_runoff_keeps_corrected_fumble_state(comparison, scorer):
    r = source_row(
        comparison,
        qtr=4,
        half_seconds_remaining=110,
        game_seconds_remaining=110,
        desc="Pass for 2 yards. FUMBLES, ball out of bounds. 10-second runoff.",
    )
    e = evaluate_row(
        r,
        r
        | {
            "down": 4,
            "ydstogo": 3,
            "yardline_100": 58,
            "half_seconds_remaining": 90,
            "game_seconds_remaining": 90,
        },
        comparison.before.second_half_receiver,
        scorer,
    )
    v = {b["id"]: b for b in e["decision"]["branches"]}
    assert v["no_flag"]["state"]["yardline_100"] == v["accept"]["state"]["yardline_100"] == 58
    assert v["no_flag"]["state"]["game_seconds_remaining"] == 100
    assert v["accept"]["state"]["game_seconds_remaining"] == 90


def test_explicit_adjacent_timeout_avoiding_runoff_is_spent_once(comparison, scorer):
    r = source_row(
        comparison,
        qtr=4,
        half_seconds_remaining=20,
        game_seconds_remaining=20,
        penalty=1,
        play_type="no_play",
        desc="PENALTY on BUF, False Start, 5 yards - No Play.",
    )
    admin = r | {
        "play_id": 101,
        "down": None,
        "timeout": 1,
        "timeout_team": "BUF",
        "home_timeouts_remaining": 2,
        "desc": "Timeout by BUF. 10-second runoff avoided.",
    }
    following = r | {
        "play_id": 102,
        "yardline_100": 65,
        "ydstogo": 10,
        "home_timeouts_remaining": 2,
    }
    e = evaluate_row(r, following, comparison.before.second_half_receiver, scorer, [admin])
    v = {b["id"]: b for b in e["decision"]["branches"]}
    assert v["accept"]["state"]["home_timeouts"] == 2
    assert v["no_flag"]["state"]["home_timeouts"] == 3
    assert v["accept"]["runoff_seconds"] == 0
    assert v["accept"]["state"]["game_seconds_remaining"] == 20
    assert e["runoff"]["avoided"] and e["linked_clock_events"][0]["play_id"] == "101"
    ordinary = admin | {"desc": "Timeout by BUF."}
    without = evaluate_row(r, following, comparison.before.second_half_receiver, scorer, [ordinary])
    assert (
        next(b for b in without["decision"]["branches"] if b["id"] == "accept")["timeout_team"]
        is None
    )
    import polars as pl

    events = build_penalties(
        pl.DataFrame([r, admin, following | {"desc": "Pass incomplete.", "penalty": 0}]), scorer
    )
    parent = next(e for e in events if e["play_id"] == "100")
    assert parent["linked_clock_events"][0]["play_id"] == "101"
    assert (
        next(b for b in parent["decision"]["branches"] if b["id"] == "accept")["state"][
            "home_timeouts"
        ]
        == 2
    )
