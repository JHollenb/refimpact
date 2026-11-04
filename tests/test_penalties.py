import pytest

from refimpact.penalties import (
    HURRY_DELAY,
    RUNOUT_DELAY,
    coverage,
    evaluate_row,
    headline_delay,
    is_flagged,
    parse_fouls,
    penalty_restart,
    runoff_info,
)
from refimpact.schema import State


def source_row(comparison, **changes):
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
        "posteam_score": 3,
        "defteam_score": 17,
        "qtr": 2,
        "half_seconds_remaining": 160,
        "game_seconds_remaining": 1960,
        "down": 3,
        "ydstogo": 5,
        "yardline_100": 60,
        "home_timeouts_remaining": 3,
        "away_timeouts_remaining": 3,
        "roof": b.roof,
        "penalty": 0,
        "play_type": "pass",
        "yards_gained": 20,
        "epa": 1.5,
        "desc": "Pass to BUF 40 for 20 yards. Penalty on ARI-1-Player, "
        "Defensive Pass Interference, declined.",
    } | changes


def test_declined_penalty_not_lost_when_indicator_is_zero(comparison, scorer):
    r = source_row(comparison)
    following = r | {
        "down": 1,
        "ydstogo": 10,
        "yardline_100": 40,
        "game_seconds_remaining": 1940,
        "half_seconds_remaining": 140,
    }
    assert is_flagged(r)
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    assert result["disposition"] == "declined"
    assert result["enforcement_epa"] == 0
    assert result["choice_edge_scenarios"] is not None
    accepted = [b for b in result["decision"]["branches"] if b["id"].startswith("accept")]
    assert accepted and all(b["evidence"] == "scenario" for b in accepted)
    assert result["choice_edge_epa"] is None  # Catch location is not an observed foul spot.


def test_offensive_pi_declined_has_a_point_acceptance_alternative(comparison, scorer):
    r = source_row(
        comparison,
        desc="Pass for 20 yards. Penalty on BUF-1-Player, Offensive Pass Interference, declined.",
    )
    following = r | {"down": 1, "ydstogo": 10, "yardline_100": 40}
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    assert result["choice_edge_epa"] is not None
    accepted = next(b for b in result["decision"]["branches"] if b["id"] == "accept")
    assert accepted["state"]["yardline_100"] == 70 and accepted["state"]["down"] == 3


def test_multiple_fouls_preserve_dispositions_and_are_not_double_counted(comparison):
    r = source_row(
        comparison,
        desc="Run for 3 yards. Penalty on BUF-1-P, Offensive Holding, offsetting. "
        "Penalty on ARI-2-P, Unnecessary Roughness, offsetting.",
    )
    fouls, _, _ = parse_fouls(r)
    assert len(fouls) == 2 and {f["disposition"] for f in fouls} == {"offsetting"}


def test_reversed_play_uses_latest_description(comparison):
    r = source_row(
        comparison,
        desc="Pass for 20 yards. Penalty on ARI, Defensive Pass Interference, declined. "
        "The play was REVERSED. Pass incomplete. Penalty on ARI, "
        "Defensive Pass Interference, 10 yards.",
    )
    fouls, prefix, revised = parse_fouls(r)
    assert revised and len(fouls) == 1 and fouls[0]["disposition"] == "accepted"
    assert "incomplete" in prefix


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("10-second runoff.", 10),
        ("10 second runoff avoided.", 0),
        ("10-second runoff was declined.", 0),
        ("No runoff reported.", 0),
    ],
)
def test_reported_runoffs_and_waivers(text, seconds):
    assert runoff_info(text)["applied_seconds"] == seconds


def test_postsnap_following_timestamp_does_not_charge_runoff_twice(comparison, scorer):
    r = source_row(
        comparison,
        qtr=4,
        half_seconds_remaining=21,
        game_seconds_remaining=21,
        penalty=1,
        penalty_type="Intentional Grounding",
        penalty_team="BUF",
        penalty_yards=12,
        desc="Pass incomplete. PENALTY on BUF, Intentional Grounding, 12 yards. 10-second runoff.",
    )
    following = r | {
        "yardline_100": 72,
        "down": 4,
        "ydstogo": 17,
        "half_seconds_remaining": 7,
        "game_seconds_remaining": 7,
    }
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    accepted = next(b for b in result["decision"]["branches"] if b["id"] == "accept")
    assert accepted["state"]["game_seconds_remaining"] == 7
    assert accepted["base_state"]["game_seconds_remaining"] == 17


def test_q4_penalty_stops_clock_inside_last_five_minutes(comparison):
    before = State.model_validate(
        comparison.before.model_dump()
        | {"qtr": 4, "game_seconds_remaining": 250, "half_seconds_remaining": 250}
    )
    assert penalty_restart(before, "Pass for 20 yards.", False, False, 0) == "snap"
    assert penalty_restart(before, "False start", True, True, 10) == "ready"


def test_replay_only_runoff_keeps_corrected_spot_fixed(comparison, scorer):
    r = source_row(
        comparison,
        qtr=2,
        half_seconds_remaining=115,
        game_seconds_remaining=1915,
        desc="Run for 8 yards. The Replay Official reviewed the first "
        "down ruling, and the play was REVERSED. Run for 6 "
        "yards. 10-second runoff.",
    )
    following = r | {
        "down": 1,
        "ydstogo": 10,
        "yardline_100": 54,
        "half_seconds_remaining": 95,
        "game_seconds_remaining": 1895,
    }
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    branches = {b["id"]: b for b in result["decision"]["branches"]}
    assert result["enforcement_epa"] is not None
    assert branches["accept"]["state"]["half_seconds_remaining"] == 95
    assert branches["no_flag"]["state"]["half_seconds_remaining"] == 105
    assert (
        branches["accept"]["state"]["yardline_100"] == branches["no_flag"]["state"]["yardline_100"]
    )


def test_q4_clock_sensitivity_includes_last_five_minutes(comparison, scorer):
    r = source_row(comparison, qtr=4, half_seconds_remaining=250, game_seconds_remaining=250)
    following = r | {
        "down": 1,
        "ydstogo": 10,
        "yardline_100": 40,
        "half_seconds_remaining": 240,
        "game_seconds_remaining": 240,
    }
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    assert result["decision"]["timing_scenarios"]


def late_row(comparison, seconds, **changes):
    return source_row(
        comparison,
        qtr=4,
        half_seconds_remaining=seconds,
        game_seconds_remaining=seconds,
        penalty=1,
        down=2,
        ydstogo=8,
        desc="Pass to BUF 50 for 10 yards. PENALTY on ARI-1-P, Roughing the Passer, 15 yards, "
        "enforced at BUF 50.",
        **changes,
    )


def after(r, seconds):
    return r | {
        "down": 1,
        "ydstogo": 10,
        "yardline_100": 35,
        "half_seconds_remaining": seconds,
        "game_seconds_remaining": seconds,
    }


NO_TIMEOUTS = {"home_timeouts_remaining": 0, "away_timeouts_remaining": 0}


def test_inside_two_minutes_headline_is_restart_aware_and_reports_a_range(comparison, scorer):
    r = late_row(comparison, 110, **NO_TIMEOUTS)  # BUF trails with the ball, out of timeouts.
    result = evaluate_row(r, after(r, 104), comparison.before.second_half_receiver, scorer)
    assert result["enforcement_wp_basis"] == "restart_aware"
    assert result["enforcement_wp_restarts"] == {
        "actual": "snap",
        "alternative": "running",
        "delay_seconds": 10,
    }
    common = next(b for b in result["decision"]["branches"] if b["id"] == "no_flag")
    assert result["enforcement_wp_common_clock_pp"] == common["actual_minus_branch_wp_pp"]
    case = next(
        c
        for c in result["decision"]["timing_comparisons"]
        if c["alternative_branch"] == "no_flag" and c["next_snap_delay_seconds"] == 10
    )
    assert result["enforcement_wp_swing_pp"] == case["actual_minus_branch_wp_pp"]
    low, high = result["enforcement_wp_range"]["min"], result["enforcement_wp_range"]["max"]
    assert low <= result["enforcement_wp_swing_pp"] <= high
    assert low <= result["enforcement_wp_common_clock_pp"] <= high
    # Expected points stay at the common clock; only the win-chance headline moves.
    assert result["enforcement_epa"] == common["actual_minus_branch_epa"]


def test_a_trailing_team_waiting_out_the_half_is_not_a_headline(comparison, scorer):
    # 2024_18_CHI_GB:4163: a 20 s wait with 15 s left read as +29 pp for the fouling offense.
    r = late_row(comparison, 14, **NO_TIMEOUTS)  # BUF trails 3-17.
    result = evaluate_row(r, after(r, 8), comparison.before.second_half_receiver, scorer)
    assert result["enforcement_wp_basis"] == "common_clock"
    assert result["enforcement_wp_swing_pp"] == result["enforcement_wp_common_clock_pp"]
    assert any(
        c["alternative_phase"] != "scrimmage" for c in result["decision"]["timing_comparisons"]
    )
    assert result["enforcement_wp_range"] is not None


def test_a_leading_team_running_out_the_half_is_the_clock_effect(comparison, scorer):
    r = late_row(comparison, 22, posteam_score=17, defteam_score=3, **NO_TIMEOUTS)  # BUF leads.
    result = evaluate_row(r, after(r, 15), comparison.before.second_half_receiver, scorer)
    assert result["enforcement_wp_basis"] == "restart_aware"
    assert result["enforcement_wp_restarts"]["delay_seconds"] == 20
    case = next(
        c
        for c in result["decision"]["timing_comparisons"]
        if c["alternative_branch"] == "no_flag" and c["next_snap_delay_seconds"] == 20
    )
    assert case["alternative_phase"] == "regulation_end" and case["alternative_wp"] == 1.0
    assert result["enforcement_wp_swing_pp"] == case["actual_minus_branch_wp_pp"] < 0


def test_a_trailing_team_with_a_timeout_is_not_charged_a_wait(comparison, scorer):
    # 2026_01_BUF_HOU:4287: HOU trailed by 5 with 2 timeouts and snapped again at :39 after the
    # flag. A declared 20 s wait read 12.82 pp against the 1.49 pp at the same clock.
    r = late_row(
        comparison,
        44,
        posteam_score=31,
        defteam_score=36,
        home_timeouts_remaining=2,
        away_timeouts_remaining=2,
    )
    result = evaluate_row(r, after(r, 39), comparison.before.second_half_receiver, scorer)
    assert result["enforcement_wp_basis"] == "common_clock"
    assert result["enforcement_wp_restarts"] is None
    assert result["enforcement_wp_swing_pp"] == result["enforcement_wp_common_clock_pp"]
    assert result["enforcement_wp_range"]["max"] > result["enforcement_wp_range"]["min"]


@pytest.mark.parametrize(
    ("home_score", "away_score", "home_timeouts", "away_timeouts", "delay"),
    [
        (10, 17, 2, 3, 0),  # Home has the ball, trails, has a timeout.
        (10, 17, 0, 3, HURRY_DELAY),  # Home has the ball, trails, has none.
        (10, 10, 0, 3, HURRY_DELAY),  # Tied: the offense needs time.
        (17, 10, 0, 1, 0),  # Home leads: away needs time and has a timeout.
        (17, 10, 3, 0, RUNOUT_DELAY),  # Home leads: away needs time and has none.
    ],
)
def test_headline_delay_follows_the_team_that_needs_time(
    home_score, away_score, home_timeouts, away_timeouts, delay
):
    state = {
        "possession": "BUF",
        "home_team": "BUF",
        "home_score": home_score,
        "away_score": away_score,
        "home_timeouts": home_timeouts,
        "away_timeouts": away_timeouts,
    }
    assert headline_delay(state) == delay


def test_the_away_offense_is_read_from_its_own_timeouts():
    state = {
        "possession": "ARI",
        "home_team": "BUF",
        "home_score": 17,
        "away_score": 10,
        "home_timeouts": 3,
        "away_timeouts": 0,
    }
    assert headline_delay(state) == HURRY_DELAY
    assert headline_delay(state | {"home_score": 3, "home_timeouts": 0}) == RUNOUT_DELAY


def test_outside_two_minutes_keeps_the_common_clock_headline(comparison, scorer):
    r = late_row(comparison, 165)
    result = evaluate_row(r, after(r, 150), comparison.before.second_half_receiver, scorer)
    assert result["enforcement_wp_basis"] == "common_clock"
    assert result["enforcement_wp_range"] is None


def test_unknown_restart_stays_a_range_not_a_headline(comparison, scorer):
    r = source_row(
        comparison,
        qtr=4,
        half_seconds_remaining=100,
        game_seconds_remaining=100,
        penalty=1,
        play_type="no_play",
        desc="PENALTY on ARI-1-P, Encroachment, 5 yards, enforced at BUF 40 - No Play.",
    )
    following = r | {"yardline_100": 55, "ydstogo": 5}
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    assert result["enforcement_wp_basis"] == "common_clock"
    assert result["enforcement_wp_range"] is not None


def test_coverage_does_not_count_declined_zeros_as_measured(comparison, scorer):
    declined = source_row(comparison)
    following = declined | {"down": 1, "ydstogo": 10, "yardline_100": 40}
    unknown = source_row(comparison, desc="Pass for 12 yards. The penalty was assisted by replay.")
    events = [
        evaluate_row(declined, following, comparison.before.second_half_receiver, scorer),
        evaluate_row(unknown, None, comparison.before.second_half_receiver, scorer),
    ]
    assert events[0]["enforcement_epa"] == 0
    result = coverage(events)
    assert result["took_effect"] == 1 and result["valued_enforcement"] == 0
    assert result["unpriced"] == 1
    assert result["unpriced_by_reason"] == {"foul_not_identified": 1}


def test_unclassified_penalty_mention_is_retained(comparison):
    assert is_flagged(
        source_row(comparison, desc="Pass for 12 yards. The penalty was assisted by replay.")
    )


def test_joint_flags_do_not_invent_a_single_team_accept_decline_choice(comparison, scorer):
    r = source_row(
        comparison,
        desc="Run for 3 yards. Penalty on BUF-1-P, Offensive Holding, "
        "offsetting. Penalty on ARI-2-P, Unnecessary Roughness, "
        "offsetting.",
    )
    following = r | {"yards_gained": 0}
    result = evaluate_row(r, following, comparison.before.second_half_receiver, scorer)
    assert result["choice_edge_epa"] is None
    assert result["choice_edge_scenarios"] is None
