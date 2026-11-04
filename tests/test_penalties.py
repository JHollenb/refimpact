import pytest

from refimpact.penalties import evaluate_row, is_flagged, parse_fouls, penalty_restart, runoff_info
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
