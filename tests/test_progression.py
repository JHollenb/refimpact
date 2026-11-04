import polars as pl
import pytest

from refimpact.production import build_production
from refimpact.progression import build_progression


def rows():
    common = dict(
        game_id="2026_08_CHI_SEA",
        season=2026,
        week=8,
        home_team="SEA",
        away_team="CHI",
        qtr=4,
        time="09:01",
        down=3,
        ydstogo=4,
        yardline_100=39,
        fixed_drive=1,
        fixed_drive_result="Punt",
        play_type="run",
        posteam="SEA",
        penalty=0,
        desc="Run for 3 yards.",
        play_type_nfl="RUSH",
    )
    return [
        common | dict(play_id=1, epa=1.0, home_wp=0.4, home_wp_post=0.5),
        common
        | dict(
            play_id=2,
            play_type="no_play",
            epa=-0.5,
            penalty=1,
            desc="PENALTY on SEA, False Start, 5 yards.",
            home_wp=0.5,
            home_wp_post=0.45,
        ),
        common | dict(play_id=3, play_type="punt", epa=-1.0, home_wp=0.45, home_wp_post=0.42),
        common
        | dict(play_id=4, posteam="CHI", fixed_drive=2, epa=2.0, home_wp=0.42, home_wp_post=0.3),
    ]


def events():
    return [
        {
            "id": "2026_08_CHI_SEA:2",
            "drive_id": "2026_08_CHI_SEA:1",
            "reference_team": "SEA",
            "home_epa_impact": -0.6,
            "enforcement_epa": -0.6,
        }
    ]


def build(source=None, flags=None):
    source = source if source is not None else rows()
    flags = flags if flags is not None else events()
    frame = pl.DataFrame(source)
    production, drives = build_production(frame, flags)
    games = build_progression(frame, flags, drives)
    return games["2026_08_CHI_SEA"], flags, drives, production


def test_observed_epa_and_wp_remain_separate_from_ruling_comparisons():
    game, flags, drives, production = build()
    assert game["timeline"][-1]["cumulative_offense_epa"] == {"SEA": 0.5, "CHI": 2.0}
    assert game["penalty_benefit"]["home_net_epa"] == -0.6
    assert game["penalty_benefit"]["home_on_offense_epa"] == -0.6
    assert production["2026_08_CHI_SEA"]["SEA"]["offensive_epa"] == 0.5  # Never -.1.
    assert flags[0]["progression"]["home_wp_before"] == 0.5
    assert flags[0]["progression"]["home_wp_after"] == 0.45
    assert flags[0]["progression"]["home_wp_change_pp"] == pytest.approx(-5)
    assert game["latest_home_wp"] == 0.3
    assert game["home_wp_change_from_start_pp"] == pytest.approx(-10)
    assert drives[0]["wp_before"] == 0.4 and drives[0]["wp_after"] == 0.42
    assert drives[0]["wp_change_pp"] == pytest.approx(2)
    assert drives[1]["wp_before"] == pytest.approx(0.58)
    assert drives[1]["wp_after"] == pytest.approx(0.7)
    assert drives[1]["wp_change_pp"] == pytest.approx(12)
    assert drives[0]["cumulative_team_drive_epa"] == -0.5
    assert drives[0]["timeline"][1]["wp_change_pp"] == pytest.approx(-5)


def test_cumulative_win_chance_uses_endpoints_not_a_sum_across_missing_intervals():
    source = rows()
    source[1]["home_wp"] = 0.6  # Unattributed source gap between plays.
    game, _, _, _ = build(source)
    summed_changes = sum(p["home_wp_change_pp"] for p in game["timeline"])
    assert summed_changes != pytest.approx(game["home_wp_change_from_start_pp"])
    assert game["home_wp_change_from_start_pp"] == pytest.approx(-10)


def test_missing_wp_endpoint_and_epa_are_disclosed_not_zero():
    source = rows()
    source[0]["epa"] = None
    source[2]["home_wp_post"] = None
    game, _, drives, _ = build(source)
    assert game["timeline"][0]["cumulative_offense_epa"]["SEA"] is None
    assert game["timeline"][-1]["missing_offense_epa_rows"]["SEA"] == 1
    assert drives[0]["wp_after"] is None and drives[0]["wp_change_pp"] is None


def test_other_possession_penalty_benefit_keeps_fixed_home_sign():
    flags = events() + [
        dict(
            events()[0],
            id="2026_08_CHI_SEA:4",
            drive_id="2026_08_CHI_SEA:2",
            reference_team="CHI",
            home_epa_impact=1.5,
            enforcement_epa=-1.5,
        )
    ]
    game, _, _, _ = build(flags=flags)
    assert game["penalty_benefit"]["home_net_epa"] == pytest.approx(0.9)
    assert game["penalty_benefit"]["home_on_defense_epa"] == 1.5


def test_final_record_is_an_endpoint_not_an_extra_play_swing():
    source = rows()
    source.append(
        dict(
            source[-1],
            play_id=5,
            play_type=None,
            desc="END GAME",
            play_type_nfl="END_GAME",
            epa=None,
            home_wp=0,
            home_wp_post=None,
        )
    )
    game, _, drives, _ = build(source)
    assert game["latest_home_wp"] == 0
    assert game["timeline"][-1]["home_wp_change_pp"] is None
    assert drives[-1]["wp_after"] == 1


def test_finished_tie_is_not_displayed_as_a_fifty_percent_chance_to_win():
    source = rows()
    source.append(
        dict(
            source[-1],
            play_id=5,
            play_type=None,
            desc="END GAME",
            play_type_nfl="END_GAME",
            epa=None,
            home_wp=0.5,
            home_wp_post=None,
            total_home_score=20,
            total_away_score=20,
        )
    )
    game, _, drives, _ = build(source)
    assert game["latest_home_wp"] is None and drives[-1]["wp_after"] is None


def test_game_start_marker_does_not_move_drive_start_before_opening_kickoff():
    source = rows()
    source.insert(
        0,
        dict(
            source[0],
            play_id=0,
            play_type=None,
            play_type_nfl="GAME_START",
            desc="GAME",
            epa=None,
            home_wp=0.2,
            home_wp_post=None,
        ),
    )
    game, _, drives, _ = build(source)
    assert game["initial_home_wp"] == 0.2
    assert drives[0]["wp_before"] == 0.4
    assert drives[0]["wp_change_pp"] == pytest.approx(2)


def test_heat_describes_the_game_so_far_and_feeds_nothing():
    rows = [
        {"game_id": "2026_01_ARI_BUF", "season": 2026, "week": 1, "play_id": 1, "order_sequence": 1,
         "home_team": "BUF", "away_team": "ARI", "posteam": "BUF", "play_type": "pass", "qtr": 1,
         "down": 1, "epa": 2.0, "yards_gained": 25.0, "total_home_score": 0, "total_away_score": 0},
        {"game_id": "2026_01_ARI_BUF", "season": 2026, "week": 1, "play_id": 2, "order_sequence": 2,
         "home_team": "BUF", "away_team": "ARI", "posteam": "BUF", "play_type": "run", "qtr": 1,
         "down": 1, "epa": 1.0, "yards_gained": 5.0, "total_home_score": 7, "total_away_score": 0},
        {"game_id": "2026_01_ARI_BUF", "season": 2026, "week": 1, "play_id": 3, "order_sequence": 3,
         "home_team": "BUF", "away_team": "ARI", "posteam": "ARI", "play_type": "run", "qtr": 1,
         "down": 1, "epa": -0.5, "yards_gained": 12.0, "total_home_score": 7,
         "total_away_score": 0},
        {"game_id": "2026_01_ARI_BUF", "season": 2026, "week": 1, "play_id": 4, "order_sequence": 4,
         "home_team": "BUF", "away_team": "ARI", "posteam": "BUF", "play_type": "pass", "qtr": 1,
         "down": 1, "epa": 3.0, "yards_gained": 8.0, "total_home_score": 10,
         "total_away_score": 0},
    ]  # fmt: skip
    frame = pl.DataFrame(rows, infer_schema_length=None)
    timeline = build_progression(frame, [], [])["2026_01_ARI_BUF"]["timeline"]
    heat = timeline[-1]["heat"]
    assert heat["epa_last_5"] == {"BUF": 6.0, "ARI": -0.5}
    assert heat["big_plays_last_10"] == {"BUF": 1, "ARI": 1}
    assert heat["scoring_run"] == {"team": "BUF", "points": 10}
    assert timeline[0]["cumulative_home_penalty_benefit_epa"] is None
    assert "not a forecast" in build_progression(frame, [], [])["2026_01_ARI_BUF"]["heat_note"]
