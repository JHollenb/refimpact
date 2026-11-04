"""Automatic written-result extraction: omitted actions never become zero-yard runs."""

import pytest

from refimpact.penalties import coverage, evaluate_row


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
        "posteam_score": 3,
        "defteam_score": 17,
        "qtr": 4,
        "half_seconds_remaining": 541,
        "game_seconds_remaining": 541,
        "down": 3,
        "ydstogo": 4,
        "yardline_100": 39,
        "home_timeouts_remaining": 3,
        "away_timeouts_remaining": 3,
        "roof": b.roof,
        "penalty": 1,
        "play_type": "no_play",
        "yards_gained": 0,
        "epa": -0.77,
        "desc": "Run for -2 yards. PENALTY on BUF, Delay of Game, 5 yards - No Play.",
    } | changes


def evaluated(comparison, scorer, source, **following):
    next_row = source | {"yardline_100": 44, "ydstogo": 9} | following
    return evaluate_row(source, next_row, comparison.before.second_half_receiver, scorer)


def branch(event, name):
    return next(b for b in event["decision"]["branches"] if b["id"] == name)


def test_written_erased_third_down_can_benefit_offense_despite_negative_enforcement(
    comparison, scorer
):
    source = row(comparison)
    event = evaluated(comparison, scorer, source)
    n = event["nullified_play"]
    assert event["enforcement_epa"] < 0
    assert n["status"] == "valued" and n["impact_epa"] > 0
    erased = branch(event, "erased_result")
    assert erased["state"]["down"] == 4
    assert erased["state"]["possession"] == "BUF"  # No invented punt.
    assert erased["state"]["yardline_100"] == 41
    assert erased["state"]["ydstogo"] == 6
    assert n["impact_epa"] == pytest.approx(branch(event, "accept")["epa"] - erased["epa"])
    assert n["source"]["text"] == "Run for -2 yards."
    assert n["reference_team"] == "BUF" and n["source"]["play_id"] == "100"


def test_written_fourth_down_failure_changes_possession_without_a_punt(comparison, scorer):
    event = evaluated(comparison, scorer, row(comparison, down=4), down=4)
    state = branch(event, "erased_result")["state"]
    assert state["possession"] == "ARI" and state["down"] == 1
    assert state["yardline_100"] == 59 and state["ydstogo"] == 10
    assert event["nullified_play"]["impact_epa"] > 0


def test_omitted_action_and_penalty_yardage_do_not_create_a_zero_yard_result(comparison, scorer):
    event = evaluated(
        comparison,
        scorer,
        row(
            comparison,
            desc="(9:01) PENALTY on BUF, Delay of Game, 5 yards, enforced at SEA 39 - No Play.",
        ),
    )
    n = event["nullified_play"]
    assert n["status"] == "not_recorded" and not n["recorded"]
    assert n["impact_epa"] is None and n["wp_swing_pp"] is None
    assert not any(b["id"] == "erased_result" for b in event["decision"]["branches"])
    assert "Fresh down" in branch(event, "no_flag")["label"]
    assert event["enforcement_epa"] < 0


@pytest.mark.parametrize(
    "description, down, togo, yardline",
    [
        ("Run for no gain.", 4, 4, 39),
        ("Pass incomplete short right.", 4, 4, 39),
        ("Run for a loss of 3 yards.", 4, 7, 42),
        ("Pass for 20 yards.", 1, 10, 19),
    ],
)
def test_written_result_ignores_structured_no_play_zero(
    comparison, scorer, description, down, togo, yardline
):
    source = row(
        comparison, desc=description + " PENALTY on BUF, Offensive Holding, 10 yards - No Play."
    )
    event = evaluated(comparison, scorer, source, yardline_100=49, ydstogo=14)
    state = branch(event, "erased_result")["state"]
    assert (state["down"], state["ydstogo"], state["yardline_100"]) == (down, togo, yardline)
    assert event["nullified_play"]["status"] == "valued"


def test_reversal_uses_written_corrected_action_not_original(comparison, scorer):
    source = row(
        comparison,
        desc="Run for 20 yards. The ruling was REVERSED. Run for -2 "
        "yards. PENALTY on BUF, Offensive Holding, 10 yards - No "
        "Play.",
    )
    event = evaluated(comparison, scorer, source)
    assert branch(event, "erased_result")["state"]["yardline_100"] == 41
    assert "20 yards" not in event["nullified_play"]["source"]["text"]


def test_scoring_or_ambiguous_result_is_retained_without_an_invented_state(comparison, scorer):
    source = row(
        comparison,
        desc="Pass for 39 yards, TOUCHDOWN. PENALTY on BUF, Offensive Holding, 10 yards - No Play.",
    )
    event = evaluated(comparison, scorer, source)
    n = event["nullified_play"]
    assert n["status"] == "unpriced" and n["recorded"]
    assert "TOUCHDOWN" in n["source"]["text"]
    assert n["impact_epa"] is None


def test_declined_flags_do_not_claim_the_action_was_erased(comparison, scorer):
    source = row(
        comparison,
        play_type="run",
        penalty=0,
        desc="Run for 3 yards. Penalty on BUF, Offensive Holding, declined.",
    )
    event = evaluated(comparison, scorer, source)
    assert event["nullified_play"] is None


def test_coverage_distinguishes_missing_from_valued_erased_results(comparison, scorer):
    events = [
        evaluated(comparison, scorer, row(comparison)),
        evaluated(
            comparison,
            scorer,
            row(comparison, desc="PENALTY on BUF, False Start, 5 yards - No Play."),
        ),
    ]
    assert coverage(events)["nullified_plays"] == {"valued": 1, "not_recorded": 1}
