import copy

import pytest
from pydantic import ValidationError

from refimpact.models import features
from refimpact.schema import Comparison, State
from refimpact.tweets import render_tweet


def test_published_nflfastr_predictions(scorer, published):
    for r in published:
        state = State.model_validate(r["state"])
        actual = scorer.predict([state], state.possession)[0]
        assert actual.ep == pytest.approx(r["ep"], abs=1e-5), (r["game_id"], r["play_id"])
        assert actual.wp == pytest.approx(r["wp"], abs=1e-6), (r["game_id"], r["play_id"])


def test_epa_and_probability_are_separate(scorer, comparison):
    result = scorer.compare(comparison)
    assert result.alternative_epa == 0
    assert result.epa_impact == result.actual_epa
    assert result.epa_impact < 0
    assert result.wp_swing_pp == pytest.approx(100 * (result.actual.wp - result.alternative.wp))


def test_reference_team_sign_and_identity(scorer, comparison):
    other = (
        comparison.before.away_team
        if comparison.reference_team == comparison.before.home_team
        else comparison.before.home_team
    )
    original = scorer.compare(comparison)
    reverse = scorer.compare(
        Comparison.model_validate(comparison.model_dump() | {"reference_team": other})
    )
    assert reverse.epa_impact == pytest.approx(-original.epa_impact)
    assert reverse.wp_swing_pp == pytest.approx(-original.wp_swing_pp)
    identical = scorer.compare(
        Comparison.model_validate(
            comparison.model_dump() | {"alternative": comparison.actual.model_dump()}
        )
    )
    assert identical.epa_impact == 0
    assert identical.wp_swing_pp == 0


def test_possession_reversal_uses_fixed_team(scorer, comparison):
    state = comparison.before
    other = state.away_team if state.possession == state.home_team else state.home_team
    estimates = scorer.predict([state], state.possession), scorer.predict([state], other)
    assert estimates[0][0].ep == -estimates[1][0].ep
    assert estimates[0][0].wp + estimates[1][0].wp == pytest.approx(1)


@pytest.mark.parametrize(
    "changes",
    [
        {"qtr": 5},
        {"down": 0},
        {"yardline_100": 0},
        {"ydstogo": float("nan")},
        {"half_seconds_remaining": 0},
        {"home_timeouts": 4},
        {"possession": "ZZZ"},
        {"game_seconds_remaining": 3000},
    ],
)
def test_invalid_state_rejected(comparison, changes):
    with pytest.raises(ValidationError):
        State.model_validate(comparison.before.model_dump() | changes)


def test_wrong_game_and_cross_half_rejected(comparison):
    data = comparison.model_dump()
    data["game_id"] = "2025_01_SEA_SF"
    with pytest.raises(ValidationError):
        Comparison.model_validate(data)
    data = copy.deepcopy(comparison.model_dump())
    data["actual"].update(qtr=3, game_seconds_remaining=1500, half_seconds_remaining=1500)
    with pytest.raises(ValidationError):
        Comparison.model_validate(data)


def test_neutral_site_and_retractable_features(comparison):
    state = State.model_validate(
        comparison.before.model_dump()
        | {"possession": comparison.before.home_team, "neutral_site": True, "roof": "closed"}
    )
    ep, wp = features(state)
    assert ep[2:6] == [0, 1, 0, 0]
    assert wp[1] == 1


def test_tweet_units_and_budget(impact):
    text = render_tweet(impact)
    assert "-3.0 pp" in text
    assert "-0.50 points" in text
    assert "vs no flag" in text
    assert text.isascii() and len(text) <= 280
    assert "correctness not assessed" in text


def test_unicode_tweets_require_ascii_labels(impact):
    impact.comparison.alternative_label = "no flag \u2014 incompletion"
    with pytest.raises(ValueError, match="280"):
        render_tweet(impact)


@pytest.mark.parametrize(
    "quarter,half,clock",
    [
        (1, 1800, "15:00"),
        (1, 900, "0:00"),
        (2, 900, "15:00"),
        (3, 1800, "15:00"),
        (3, 900, "0:00"),
        (4, 900, "15:00"),
    ],
)
def test_clock_at_quarter_boundaries(impact, quarter, half, clock):
    impact.comparison.before.qtr = quarter
    impact.comparison.before.half_seconds_remaining = half
    assert f"Q{quarter} {clock}" in render_tweet(impact)
