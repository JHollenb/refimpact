import pytest

from refimpact.replay import reconstruct, second_half_receivers, state_from_row


def row(comparison, **changes):
    b = comparison.before
    data = {
        "game_id": comparison.game_id,
        "play_id": 100,
        "season": b.season,
        "home_team": b.home_team,
        "away_team": b.away_team,
        "posteam": b.possession,
        "posteam_score": b.home_score if b.possession == b.home_team else b.away_score,
        "defteam_score": b.away_score if b.possession == b.home_team else b.home_score,
        "qtr": b.qtr,
        "half_seconds_remaining": b.half_seconds_remaining,
        "game_seconds_remaining": b.game_seconds_remaining,
        "down": 2,
        "ydstogo": 7,
        "yardline_100": 55,
        "home_timeouts_remaining": b.home_timeouts,
        "away_timeouts_remaining": b.away_timeouts,
        "roof": b.roof,
        "penalty_type": "False Start",
        "penalty_team": b.possession,
        "penalty_yards": 5,
        "play_type": "no_play",
        "desc": "PENALTY on BUF, False Start, 5 yards, No Play.",
    }
    return data | changes


def test_reconstructs_false_start(comparison):
    r = row(comparison)
    following = r | {"yardline_100": 60, "ydstogo": 12}
    c = reconstruct(r, following, comparison.before.second_half_receiver)
    assert c.actual.down == 2 and c.actual.ydstogo == 12
    assert c.actual.yardline_100 == 60
    assert c.alternative == c.before


def test_defensive_penalty_can_earn_first_down(comparison):
    other = (
        comparison.before.away_team
        if comparison.before.possession == comparison.before.home_team
        else comparison.before.home_team
    )
    r = row(comparison, penalty_type="Encroachment", penalty_team=other, ydstogo=3)
    following = r | {"yardline_100": 50, "down": 1, "ydstogo": 10}
    c = reconstruct(r, following, comparison.before.second_half_receiver)
    assert c.actual.down == 1 and c.actual.ydstogo == 10


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"penalty_type": "Defensive Pass Interference"}, "unsupported"),
        ({"desc": "PENALTY on BUF, declined No Play"}, "declined"),
        ({"desc": "PENALTY on BUF; PENALTY on SEA; No Play"}, "multiple"),
        ({"play_type": "pass"}, "dead_ball"),
        ({"penalty_yards": 10}, "distance"),
    ],
)
def test_ambiguous_cases_retained_for_review(comparison, changes, reason):
    r = row(comparison, **changes)
    with pytest.raises(ValueError, match=reason):
        reconstruct(r, r, comparison.before.second_half_receiver)


def test_mismatched_following_state_rejected(comparison):
    r = row(comparison)
    with pytest.raises(ValueError, match="confirm_enforcement"):
        reconstruct(r, r, comparison.before.second_half_receiver)


def test_pre_snap_scores_not_post_play_cumulative_scores(comparison):
    r = row(comparison, total_home_score=99, total_away_score=98)
    s = state_from_row(r, comparison.before.second_half_receiver)
    assert s.home_score == comparison.before.home_score
    assert s.away_score == comparison.before.away_score


def test_receiving_team_inference_does_not_use_future_plays():
    result = second_half_receivers(
        [
            {
                "game_id": "g",
                "qtr": 1,
                "game_seconds_remaining": 3600,
                "play_type": "kickoff",
                "kickoff_attempt": 1,
                "defteam": "SEA",
                "posteam": "SF",
            },
        ]
    )
    assert result == {"g": "SEA"}


def test_flagged_opening_kickoff_keeps_the_second_half_receiver():
    # 2026_05_PHI_JAX: the nullified return has a null kickoff_attempt, the re-kick is at 3587 s.
    rows = [
        {"game_id": "g", "qtr": 1, "game_seconds_remaining": 3600, "play_type": None},
        {
            "game_id": "g",
            "qtr": 1,
            "game_seconds_remaining": 3600,
            "play_type": "kickoff",
            "kickoff_attempt": None,
            "defteam": "PHI",
            "posteam": "JAX",
        },
        {
            "game_id": "g",
            "qtr": 1,
            "game_seconds_remaining": 3587,
            "play_type": "kickoff",
            "kickoff_attempt": 1,
            "defteam": "PHI",
            "posteam": "JAX",
        },
    ]
    assert second_half_receivers(rows) == {"g": "PHI"}


def test_pregame_comment_rows_do_not_pick_the_receiver():
    # 2024_05_DAL_PIT: a weather comment names DAL as defteam before PIT kicks to DAL.
    rows = [
        {"game_id": "g", "qtr": 1, "play_type": None, "defteam": "DAL", "posteam": "PIT"},
        {"game_id": "g", "qtr": 1, "play_type": "kickoff", "defteam": "PIT", "posteam": "DAL"},
    ]
    assert second_half_receivers(rows) == {"g": "PIT"}
