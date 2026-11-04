"""Erased scores, punts, kickoffs and fumbles: priced from the written result, or left unpriced."""

import json
from pathlib import Path

import pytest

from refimpact.models import spread_features
from refimpact.penalties import coverage, evaluate_row, kickoff_start_spots
from refimpact.replay import second_half_receivers, state_from_row
from refimpact.schema import State

SPOT = 70.0


def row(comparison, **changes):
    b = comparison.before
    return {
        "game_id": comparison.game_id,
        "play_id": 100,
        "week": 1,
        "fixed_drive": 1,
        "season": b.season,
        "home_team": b.home_team,
        "away_team": b.away_team,
        "posteam": b.possession,
        "defteam": b.away_team if b.possession == b.home_team else b.home_team,
        "posteam_score": 3,
        "defteam_score": 17,
        "qtr": 3,
        "time": "09:00",
        "half_seconds_remaining": 1440,
        "game_seconds_remaining": 1440,
        "down": 2,
        "ydstogo": 5,
        "yardline_100": 30,
        "home_timeouts_remaining": 3,
        "away_timeouts_remaining": 3,
        "roof": b.roof,
        "penalty": 1,
        "play_type": "no_play",
        "yards_gained": 0,
        "epa": -1.0,
    } | changes


def evaluated(comparison, scorer, source, kick_spot=SPOT, **following):
    next_row = source | {
        "down": 2,
        "ydstogo": 15,
        "yardline_100": 40,
        "half_seconds_remaining": 1420,
        "game_seconds_remaining": 1420,
    }
    return evaluate_row(
        source,
        next_row | following,
        comparison.before.second_half_receiver,
        scorer,
        kick_spot=kick_spot,
    )


def branch(event, name):
    return next(b for b in event["decision"]["branches"] if b["id"] == name)


TD = "Pass to 1-A for 30 yards, TOUCHDOWN NULLIFIED by Penalty. "
HOLD = "PENALTY on BUF-2-B, Offensive Holding, 10 yards, enforced at ARI 30 - No Play."


def test_erased_touchdown_is_points_plus_the_opponent_after_the_kick(comparison, scorer):
    event = evaluated(comparison, scorer, row(comparison, desc=TD + HOLD))
    alt = branch(event, "no_flag")
    state = alt["base_state"]
    assert state["possession"] == "ARI"
    assert state["yardline_100"] == SPOT and state["down"] == 1
    assert state["home_score"] - 3 == 7  # BUF is home here: 3 + 7 on the board.
    # Coherent with the actual branch: 7 minus the opponent's expected points after the kick.
    actual = branch(event, "accept")
    before_ep = actual["ep"] - actual["epa"]
    assert alt["epa"] == pytest.approx(7 + alt["ep"] - before_ep)
    assert alt["ep"] < 0  # The opponent's expected points, in BUF's frame.
    assert event["enforcement_epa"] < 0 and event["enforcement_wp_swing_pp"] < 0
    assert (
        "Approximate" in event["approximate"] and "extra point assumed good" in event["approximate"]
    )
    assert event["nullified_play"]["status"] == "valued"
    assert event["unpriced_reason"] is None


def test_no_measured_spot_leaves_the_touchdown_unpriced(comparison, scorer):
    event = evaluated(comparison, scorer, row(comparison, desc=TD + HOLD), kick_spot=None)
    assert event["enforcement_epa"] is None
    assert any("measured post-kick" in issue for issue in event["issues"])
    assert event["unpriced_reason"] == "no_measured_kick_spot"


def test_erased_defensive_touchdown_has_the_opposite_sign(comparison, scorer):
    text = (
        "Pass INTERCEPTED by 9-C at ARI 10. 9-C for 90 yards, TOUCHDOWN NULLIFIED by Penalty. "
        "PENALTY on ARI-5-D, Defensive Holding, 5 yards, enforced at ARI 30 - No Play."
    )
    event = evaluated(
        comparison, scorer, row(comparison, desc=text), down=1, ydstogo=10, yardline_100=25
    )
    state = branch(event, "no_flag")["base_state"]
    assert state["away_score"] == 17 + 7 and state["possession"] == "BUF"
    assert state["yardline_100"] == SPOT
    assert event["enforcement_epa"] > 0 and event["enforcement_wp_swing_pp"] > 0


def test_touchdown_that_stands_is_not_given_a_made_up_enforcement(comparison, scorer):
    source = row(
        comparison,
        play_type="pass",
        desc="Pass to 1-A for 30 yards, TOUCHDOWN. PENALTY on ARI-5-D, Roughing the Passer, "
        "15 yards, enforced between downs.",
    )
    event = evaluated(comparison, scorer, source)
    assert event["enforcement_epa"] is None
    assert event["unpriced_reason"] == "score_stands_enforced_after"


@pytest.mark.parametrize(
    "text, possession, yardline",
    [
        ("9-P punts 40 yards to ARI 30, Center-1-L. 2-R to ARI 45 for 15 yards (3-T).", "ARI", 55),
        ("9-P punts 40 yards to ARI 30, Center-1-L, fair catch by 2-R.", "ARI", 70),
        ("9-P punts 50 yards to end zone, Center-1-L, Touchback.", "ARI", 80),
        ("9-P punts 40 yards to ARI 30, Center-1-L, out of bounds.", "ARI", 70),
        ("9-P punts 40 yards to ARI 30, Center-1-L. 2-R ran ob at BUF 45 for 25 yards.", "ARI", 45),
    ],
)
def test_written_punt_results(comparison, scorer, text, possession, yardline):
    source = row(
        comparison,
        down=4,
        ydstogo=5,
        yardline_100=70,
        play_type="punt",
        desc=text + " PENALTY on ARI-5-D, Offensive Holding, 10 yards, enforced at ARI 20.",
    )
    event = evaluated(
        comparison,
        scorer,
        source,
        posteam="ARI",
        defteam="BUF",
        down=1,
        ydstogo=10,
        yardline_100=80,
        posteam_score=17,
        defteam_score=3,
    )
    state = branch(event, "no_flag")["base_state"]
    assert (state["possession"], state["down"], state["yardline_100"]) == (possession, 1, yardline)
    assert event["enforcement_epa"] is not None


def test_muffed_punt_stays_unpriced(comparison, scorer):
    source = row(
        comparison,
        down=4,
        play_type="punt",
        desc="9-P punts 40 yards to ARI 30. 2-R MUFFS catch, recovered by BUF-3-T at ARI 30. "
        "PENALTY on ARI-5-D, Offensive Holding, 10 yards, enforced at ARI 20.",
    )
    event = evaluated(comparison, scorer, source)
    assert event["enforcement_epa"] is None
    assert event["unpriced_reason"] == "kick_result_ambiguous"


def kick(comparison, text, **changes):
    return row(
        comparison,
        posteam="ARI",
        defteam="BUF",
        posteam_score=17,
        defteam_score=3,
        down=None,
        ydstogo=None,
        yardline_100=35,
        play_type="kickoff",
        desc=text,
        **changes,
    )


def next_snap(source, **changes):
    return (
        source
        | {
            "down": 1,
            "ydstogo": 10,
            "yardline_100": 85,
            "half_seconds_remaining": 1435,
            "game_seconds_remaining": 1435,
            "play_type": "run",
            "desc": "next",
        }
        | changes
    )


def test_flagged_kickoff_compares_the_written_return_with_the_next_snap(comparison, scorer):
    source = kick(
        comparison,
        "9-K kicks 64 yards from BUF 35 to ARI 1. 2-R to ARI 30 for 29 yards (3-T). "
        "PENALTY on ARI-5-D, Offensive Holding, 10 yards, enforced at ARI 15.",
    )
    event = evaluate_row(
        source, next_snap(source), comparison.before.second_half_receiver, scorer, kick_spot=SPOT
    )
    alt = branch(event, "no_flag")["base_state"]
    assert alt["possession"] == "ARI" and alt["yardline_100"] == 70 and alt["down"] == 1
    assert branch(event, "accept")["base_state"]["yardline_100"] == 85
    assert event["reference_team"] == "ARI"
    assert event["enforcement_epa"] < 0
    assert event["home_epa_impact"] == pytest.approx(-event["enforcement_epa"])  # BUF is home.
    assert event["enforcement_baseline"] == "written_kick_result"


def test_erased_return_touchdown_is_oriented_to_the_receiving_team(comparison, scorer):
    # 2026_05_PHI_JAX:39. The no-flag branch hands the ball to the kicker, so its possession
    # must not set the home sign.
    source = kick(
        comparison,
        "9-K kicks 55 yards from BUF 35 to ARI 10. 2-R for 90 yards, TOUCHDOWN NULLIFIED by "
        "Penalty. PENALTY on ARI-5-D, Illegal Formation, 5 yards, enforced at ARI 30.",
    )
    event = evaluate_row(
        source, next_snap(source), comparison.before.second_half_receiver, scorer, kick_spot=SPOT
    )
    assert event["reference_team"] == "ARI"
    alt = branch(event, "no_flag")["base_state"]
    assert alt["possession"] == "BUF" and alt["away_score"] == 17 + 7
    assert event["enforcement_epa"] < 0 and event["enforcement_wp_swing_pp"] < 0
    # BUF is home: a touchdown taken from ARI helps BUF.
    assert event["home_epa_impact"] > 0 and event["home_wp_swing_pp"] > 0
    assert event["opportunity"]["downs_gained"] is None


def test_kick_rule_placement_has_no_foul_free_result(comparison, scorer):
    source = kick(
        comparison,
        "9-K kicks 36 yards from BUF 35 to ARI 29, short of landing zone. PENALTY on BUF-9-K, "
        "Kickoff Short of Landing Zone, placed at ARI 40.",
    )
    event = evaluate_row(
        source, next_snap(source), comparison.before.second_half_receiver, scorer, kick_spot=SPOT
    )
    assert event["enforcement_epa"] is None
    assert event["unpriced_reason"] == "kick_rule_placement"


def test_touchback_to_the_written_spot(comparison, scorer):
    source = kick(
        comparison,
        "9-K kicks 65 yards from BUF 35 to end zone, Touchback to the ARI 35. PENALTY on BUF-3-T, "
        "Illegal Formation, 5 yards, enforced at ARI 35.",
    )
    event = evaluate_row(
        source,
        next_snap(source, yardline_100=60),
        comparison.before.second_half_receiver,
        scorer,
        kick_spot=SPOT,
    )
    assert branch(event, "no_flag")["base_state"]["yardline_100"] == 65


@pytest.mark.parametrize(
    "text, possession, yardline, down",
    [
        ("sacked at BUF 40 for -10 yards. FUMBLES, RECOVERED by ARI-5-D at BUF 38.", "ARI", 38, 1),
        ("Run to ARI 25 for 5 yards. FUMBLES, recovered by BUF-6-E at ARI 28.", "BUF", 28, 3),
        ("Run to ARI 25 for 5 yards. FUMBLES, and recovers at ARI 26.", "BUF", 26, 3),
        (
            "Run to ARI 25 for 5 yards. FUMBLES, RECOVERED by ARI-5-D at ARI 25. "
            "5-D to ARI 40 for 15 yards.",
            "ARI",
            60,
            1,
        ),
    ],
)
def test_single_written_fumble_recovery(comparison, scorer, text, possession, yardline, down):
    source = row(
        comparison,
        play_type="pass",
        down=2,
        ydstogo=8,
        desc=text + " PENALTY on BUF-2-B, Offensive Holding, 10 yards, enforced at ARI 30.",
    )
    event = evaluated(comparison, scorer, source)
    state = branch(event, "no_flag")["base_state"]
    assert (state["possession"], state["yardline_100"], state["down"]) == (
        possession,
        yardline,
        down,
    )
    assert event["approximate"].startswith("Approximate")


def test_two_fumbles_stay_unpriced(comparison, scorer):
    source = row(
        comparison,
        play_type="run",
        desc="Run for 5 yards. FUMBLES, RECOVERED by ARI-5-D at ARI 25. FUMBLES, RECOVERED by "
        "BUF-1-A at ARI 20. PENALTY on BUF-2-B, Offensive Holding, 10 yards, enforced at ARI 30.",
    )
    event = evaluated(comparison, scorer, source)
    assert event["enforcement_epa"] is None
    assert event["unpriced_reason"] == "turnover_sequence_ambiguous"


def test_erased_and_missed_field_goals(comparison, scorer):
    good = row(
        comparison,
        down=4,
        desc="9-K 48 yard field goal is GOOD, NULLIFIED by Penalty, Center-1-L, Holder-2-H. "
        "PENALTY on ARI-5-D, Defensive Offside, 5 yards, enforced at ARI 30 - No Play.",
    )
    event = evaluated(comparison, scorer, good, ydstogo=1, yardline_100=25, down=4)
    state = branch(event, "no_flag")["base_state"]
    assert state["home_score"] == 3 + 3 and state["possession"] == "ARI"
    assert state["yardline_100"] == SPOT
    assert "counted as 3" in event["approximate"]
    miss = good | {
        "desc": "9-K 48 yard field goal is No Good, Wide Right, Center-1-L, Holder-2-H. "
        "PENALTY on ARI-5-D, Defensive Offside, 5 yards, enforced at ARI 30 - No Play."
    }
    event = evaluated(comparison, scorer, miss, ydstogo=1, yardline_100=25, down=4)
    state = branch(event, "no_flag")["base_state"]
    assert state["possession"] == "ARI" and state["yardline_100"] == 62
    assert event["enforcement_epa"] > 0


def test_opportunity_panel_on_a_defensive_flag_inside_the_five(comparison, scorer):
    source = row(
        comparison,
        down=3,
        ydstogo=3,
        yardline_100=3,
        play_type="no_play",
        desc="Pass incomplete. PENALTY on ARI-5-D, Defensive Holding, 2 yards, enforced at ARI 3 "
        "- No Play.",
    )
    event = evaluated(comparison, scorer, source, down=1, ydstogo=1, yardline_100=1)
    o = event["opportunity"]
    assert o["team"] == "BUF" and o["baseline"] == "before" and o["result"] == "actual"
    assert o["states"]["before"]["downs_available"] == 2
    assert o["states"]["no_flag"]["downs_available"] == 1
    assert o["states"]["actual"]["downs_available"] == 4
    assert o["downs_gained"] == 2
    assert o["td_change_pp"] > 0 and o["fg_change_pp"] < 0
    assert "flip field goals into touchdowns" in o["note"]
    # An offensive foul that replays the down adds no downs.
    hold = row(
        comparison,
        down=3,
        ydstogo=5,
        desc="Pass incomplete. PENALTY on BUF-2-B, Offensive Holding, 10 yards, enforced at ARI "
        "30 - No Play.",
    )
    assert evaluated(comparison, scorer, hold, down=3)["opportunity"]["downs_gained"] == 0


def test_first_down_reset_gains_one_two_three_downs_by_the_down_it_came_from(comparison, scorer):
    for down, gained in ((1, 0), (2, 1), (3, 2), (4, 3)):
        source = row(
            comparison,
            down=down,
            ydstogo=3,
            yardline_100=3,
            desc="Pass incomplete. PENALTY on ARI-5-D, Defensive Holding, 2 yards, enforced at "
            "ARI 3 - No Play.",
        )
        event = evaluated(comparison, scorer, source, down=1, ydstogo=1, yardline_100=1)
        assert event["opportunity"]["downs_gained"] == gained


def test_kick_spot_is_measured_per_season_and_ignores_kicker_recoveries():
    rows = [
        {"game_id": "a", "season": 2026, "play_type": "kickoff", "posteam": "X", "defteam": "Y"},
        {"game_id": "a", "season": 2026, "play_type": "run", "down": 1, "posteam": "X",
         "yardline_100": 70.0},
        {"game_id": "a", "season": 2026, "play_type": "kickoff", "posteam": "Y", "defteam": "X"},
        {"game_id": "a", "season": 2026, "play_type": "run", "down": 1, "posteam": "Y",
         "yardline_100": 74.0},
        # An onside kick the kicking team recovered does not count.
        {"game_id": "a", "season": 2026, "play_type": "kickoff", "posteam": "X", "defteam": "Y"},
        {"game_id": "a", "season": 2026, "play_type": "run", "down": 1, "posteam": "Y",
         "yardline_100": 45.0},
        {"game_id": "b", "season": 2025, "play_type": "kickoff", "posteam": "Z", "defteam": "W"},
        {"game_id": "b", "season": 2025, "play_type": "run", "down": 1, "posteam": "Z",
         "yardline_100": 60.0},
    ]  # fmt: skip
    assert kickoff_start_spots(rows) == {2026: 72.0, 2025: 60.0}


def test_coverage_reports_unpriced_reasons(comparison, scorer):
    stands = row(
        comparison,
        play_type="pass",
        desc="Pass for 30 yards, TOUCHDOWN. PENALTY on ARI-5-D, Roughing the Passer, 15 yards, "
        "enforced between downs.",
    )
    valued = row(comparison, desc=TD + HOLD)
    result = coverage(
        [evaluated(comparison, scorer, stands), evaluated(comparison, scorer, valued)]
    )
    assert result["valued_enforcement"] == 1 and result["unpriced"] == 1
    assert result["unpriced_by_reason"] == {"score_stands_enforced_after": 1}


def test_spread_features_follow_the_upstream_order(comparison):
    state = State.model_validate(comparison.before.model_dump() | {"home_spread": 3.5})
    vector = spread_features(state)
    assert len(vector) == 12
    sign = 1 if state.possession == state.home_team else -1
    assert vector[1] == pytest.approx(
        sign * 3.5 * 2.718281828 ** (-4 * (3600 - state.game_seconds_remaining) / 3600), rel=1e-6
    )


def test_vegas_model_matches_published_values(scorer):
    # Rows from 2026 Week 8 CHI at SEA: published vegas_wp for ordinary snaps.
    rows = json.loads(Path("tests/fixtures/chi_sea_2026_w08.json").read_text())
    rows.sort(key=lambda r: r["order_sequence"])
    receiver = second_half_receivers(rows)["2026_08_CHI_SEA"]
    checked = 0
    for r in rows:
        if r["play_type"] not in {"run", "pass"} or r["qtr"] > 4 or not r["ydstogo"]:
            continue
        state = state_from_row(r, receiver)
        assert scorer.vegas_wp([state], state.possession)[0] == pytest.approx(
            r["vegas_wp"], abs=1e-6
        )
        checked += 1
    assert checked > 100
