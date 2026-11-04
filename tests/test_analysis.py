import pytest

from refimpact.analysis import relationships


def game(number, epa, margin, **changes):
    return {
        "game_id": f"2026_08_GAME_{number}",
        "week": 8,
        "home_team": "SEA",
        "away_team": "SF",
        "home_score": 30 + margin,
        "away_score": 30,
        "status": "final",
        "pbp_complete": True,
        "net_home_epa": epa,
        "scored_calls": 2,
        "penalty_rows": 10,
    } | changes


def test_one_observation_per_game_and_correct_win_direction():
    result = relationships([game(1, -1, -7), game(2, 1, 10), game(3, 2, 21)])
    assert result["games"] == 3  # Never mirror these into six team observations.
    assert result["benefited_team_wins"] == 3
    assert result["pearson_r_margin"] > 0.99
    assert result["bootstrap_95_interval"] is not None


def test_coverage_denominator_is_flags_that_took_effect():
    took_effect = {"penalty_coverage": {"took_effect": 4}}
    result = relationships([game(1, 1, 7, **took_effect), game(2, 2, 14, **took_effect)])
    assert result["penalty_rows"] == 20  # Declined flags stay reported ...
    assert result["flags_took_effect"] == 8  # ... but have no effect to price.
    assert result["scored_calls"] == 4


def test_missing_evidence_never_zero_and_future_scores_not_analyzed():
    result = relationships(
        [
            game(1, 1, 7),
            game(2, None, -2, scored_calls=0),
            game(3, 1, 14, pbp_complete=False),
            game(4, None, 0, status="scheduled"),
        ]
    )
    assert result["games"] == 1
    assert result["excluded_final_games"] == 2
    assert result["not_final_games"] == 1
    assert result["pearson_r_margin"] is None
    assert result["bootstrap_95_interval"] is None


def test_ties_and_zero_benefit_do_not_create_fake_wins():
    result = relationships([game(1, 1, 0), game(2, 0, 7), game(3, -1, 7)])
    assert result["games"] == 3
    assert result["decisive_games_with_nonzero_impact"] == 1
    assert result["benefited_team_win_rate"] == 0


def test_empty_and_constant_series_remain_undefined():
    assert relationships([])["pearson_r_margin"] is None
    result = relationships([game(i, 0, i) for i in range(5)])
    assert result["pearson_r_margin"] is None
    assert result["point_biserial_r_home_win"] is None


def test_negative_relationship_is_preserved():
    result = relationships([game(i, i - 2, 10 - i * 5) for i in range(5)])
    assert result["pearson_r_margin"] == pytest.approx(-1)
    assert result["benefited_team_win_rate"] == 0
