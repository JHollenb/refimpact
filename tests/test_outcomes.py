import json
import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from refimpact.conversion import FORM_COLUMNS, Conversion, SeasonForm, Table, form_frame
from refimpact.gameview import game_view, week_card
from refimpact.outcomes import (
    OutcomeRequest,
    apply_outcome,
    evaluate_outcome,
    menu,
    play_kind,
)
from refimpact.scenarios import ordered, play_id
from refimpact.schema import State
from refimpact.strength import FormTable, Heat, form_values, heat_before, togo_bucket, unit_frame

GAME = "2026_08_CHI_SEA"


@pytest.fixture(scope="module")
def rows():
    return ordered(json.loads(Path("tests/fixtures/chi_sea_2026_w08.json").read_text()))


@pytest.fixture(scope="module")
def fit():
    return Conversion()


def full_rows(rows):
    """The fixture leaves out columns the team tables read; fill them with plain values."""
    defaults = {
        "epa": 0.1,
        "wp": 0.5,
        "success": 1.0,
        "yards_gained": 5.0,
        "play_deleted": 0.0,
        "qb_dropback": 0.0,
        "rush": 1.0,
        "sack": 0.0,
        "qb_hit": 0.0,
        "third_down_converted": 0.0,
        "third_down_failed": 0.0,
        "fourth_down_converted": 0.0,
        "fourth_down_failed": 0.0,
        "cpoe": None,
        "fixed_drive_result": "Punt",
    }
    return [{**defaults, **row} for row in rows]


@pytest.fixture(scope="module")
def season_form(rows, fit):
    # An earlier copy of the game, so the fixture game has a prior game in its window.
    earlier = [{**r, "game_id": "2026_07_CHI_SEA", "game_date": "2026-10-20"} for r in rows]
    return SeasonForm(form_frame(full_rows(earlier)), fit)


def state(**changes) -> State:
    values = {
        "season": 2026,
        "home_team": "SEA",
        "away_team": "CHI",
        "possession": "SEA",
        "home_score": 7,
        "away_score": 3,
        "qtr": 3,
        "game_seconds_remaining": 1500,
        "half_seconds_remaining": 1500,
        "down": 3,
        "ydstogo": 5,
        "yardline_100": 40,
        "home_timeouts": 3,
        "away_timeouts": 2,
        "second_half_receiver": "CHI",
        "roof": "outdoors",
    }
    return State(**(values | changes))


def apply(kind, row=None, pre=None, **request):
    row = row or {"posteam": "SEA", "defteam": "CHI", "down": 3, "play_type": "run"}
    pre = pre if pre is not None else state()
    return apply_outcome(
        OutcomeRequest(game_id=GAME, play_id="1", kind=kind, **request),
        row,
        state(),
        pre,
        kick_spot=70,
        touchback_spot=65,
        safety_spot=57,
        event=None,
    )


def test_every_play_type_is_editable_and_non_plays_are_not(rows):
    kinds = [play_kind(r) for r in rows]
    assert {"scrimmage", "kickoff", "punt", "field_goal", "extra_point"} <= set(kinds)
    for row, kind in zip(rows, kinds, strict=True):
        if row.get("play_type") in {"run", "pass", "punt", "field_goal", "kickoff", "extra_point"}:
            assert kind is not None, row["desc"]
        if row.get("play_type") is None or row.get("play_type_nfl") in {"TIMEOUT", "END_QUARTER"}:
            assert kind is None, row["desc"]


def test_changing_any_result_of_any_play_reads(rows, scorer, fit, season_form):
    plays = [r for r in rows if play_kind(r)]
    assert len(plays) > 150
    for row in plays:
        for kind in menu(play_kind(row)):
            result = evaluate_outcome(
                rows,
                OutcomeRequest(
                    game_id=GAME,
                    play_id=play_id(row),
                    kind=kind,
                    yards=3 if kind == "gain" else None,
                ),
                scorer,
                fit,
                season_form,
                70.0,
            )
            assert 0 <= result["yours"]["home_wp"] <= 1, (play_id(row), kind)
            assert result["outcome"]["scored"] == (result["first_down"] is None)
            if result["first_down"]:
                assert 0 < result["first_down"]["chance"] < 1


def test_gain_rules():
    short = apply("gain", yards=3).state
    assert (short.possession, short.down, short.ydstogo, short.yardline_100) == ("SEA", 4, 2, 37)
    first = apply("gain", yards=7).state
    assert (first.down, first.ydstogo, first.yardline_100) == (1, 10, 33)
    assert apply("first_down").state.yardline_100 == 35
    assert apply("gain", yards=40).ended == "Touchdown"
    # A loss never becomes a first down, even with the same yards to go.
    loss = apply("gain", yards=-4).state
    assert (loss.down, loss.ydstogo, loss.yardline_100) == (4, 9, 44)


def test_touchdown_puts_points_on_the_board_and_gives_the_ball_after_the_kick():
    result = apply("touchdown")
    assert result.scored
    assert (result.state.home_score, result.state.possession) == (14, "CHI")
    assert result.state.yardline_100 == 70


def test_failed_fourth_down_and_turnovers_change_possession():
    pre = state(down=4, ydstogo=2, yardline_100=40)
    failed = apply("incomplete", pre=pre)
    assert failed.ended == "Turnover on downs"
    assert (failed.state.possession, failed.state.yardline_100) == ("CHI", 60)
    turnover = apply("turnover", spot_team="CHI", spot_yard=25)
    assert (turnover.state.possession, turnover.state.yardline_100) == ("CHI", 75)
    returned = apply("defensive_td")
    assert (returned.state.away_score, returned.state.possession) == (10, "SEA")


def test_safety_scores_two_for_the_defense_and_gives_it_the_ball():
    deep = state(yardline_100=95, ydstogo=10, down=1)
    result = apply("gain", pre=deep, yards=-6)
    assert result.label == "Safety"
    assert (result.state.away_score, result.state.possession) == (5, "CHI")
    assert result.state.yardline_100 == 57


def test_kick_results():
    punt_row = {"posteam": "SEA", "defteam": "CHI", "down": 4, "play_type": "punt"}
    touchback = apply("touchback", row=punt_row).state
    assert (touchback.possession, touchback.yardline_100) == ("CHI", 80)
    kick_row = {"posteam": "SEA", "defteam": "CHI", "play_type": "kickoff"}
    assert apply("touchback", row=kick_row).state.yardline_100 == 65
    onside = apply("onside", row=kick_row, spot_team="CHI", spot_yard=45).state
    assert (onside.possession, onside.yardline_100) == ("CHI", 55)
    kick = {"posteam": "SEA", "defteam": "CHI", "down": 4, "play_type": "field_goal"}
    assert apply("fg_good", row=kick).state.home_score == 10
    # Missed from the 40: the kick is from the 48, and the other team takes it there.
    assert apply("fg_miss", row=kick).state.yardline_100 == 52
    # Missed from the 10: the 18 is inside its 20, so it takes the ball at its own 20.
    close = state(down=4, ydstogo=2, yardline_100=10)
    assert apply("fg_miss", row=kick, pre=close).state.yardline_100 == 80
    try_row = {"posteam": "SEA", "defteam": "CHI", "play_type": "extra_point"}
    assert apply("xp_good", row=try_row).state.home_score == 8
    assert apply("xp_miss", row=try_row).state.home_score == 7


def test_a_result_the_play_cannot_have_is_refused():
    with pytest.raises(ValueError, match="isn't a result"):
        apply("fg_good")
    with pytest.raises(ValueError, match="Enter the yards"):
        apply("gain")


def test_no_flag_is_offered_only_for_a_priced_flag_that_took_effect():
    branch = {"id": "no_flag", "state": state().model_dump()}
    event = {"disposition": "accepted", "decision": {"branches": [branch]}}
    assert "no_flag" in menu("scrimmage", event)
    assert "no_flag" not in menu("scrimmage", {**event, "disposition": "declined"})
    assert "no_flag" not in menu("scrimmage", {"disposition": "accepted", "decision": None})


def test_editing_a_play_leaves_the_rest_of_the_game_alone(rows, scorer, fit, season_form):
    before = json.dumps(rows, sort_keys=True)
    evaluate_outcome(
        rows,
        OutcomeRequest(game_id=GAME, play_id="2895", kind="touchdown"),
        scorer,
        fit,
        season_form,
        70.0,
    )
    assert json.dumps(rows, sort_keys=True) == before


def test_scoreboard_before_a_play_comes_from_the_rows(rows, scorer, fit, season_form):
    result = evaluate_outcome(
        rows,
        OutcomeRequest(game_id=GAME, play_id="2895", kind="touchdown"),
        scorer,
        fit,
        season_form,
        70.0,
    )
    assert result["yours"]["score"] == {"home": 16, "away": 20}
    assert result["recorded"]["score"] == {"home": 9, "away": 20}
    assert result["outcome"]["drive_result"] == "Touchdown"
    assert result["first_down"] is None and result["scoring"] is None


def test_overtime_reads_expected_points_and_chances_but_no_win_chance(
    rows, scorer, fit, season_form
):
    # The fixture game ended in regulation; put its last scrimmage play in overtime.
    index = next(
        i for i, r in reversed(list(enumerate(rows))) if r.get("play_type") in ("run", "pass")
    )
    changed = [dict(r) for r in rows]
    changed[index] |= {"qtr": 5.0, "half_seconds_remaining": 300.0, "game_seconds_remaining": 300.0}
    result = evaluate_outcome(
        changed,
        OutcomeRequest(game_id=GAME, play_id=play_id(changed[index]), kind="first_down"),
        scorer,
        fit,
        season_form,
        70.0,
    )
    assert result["yours"]["home_wp"] is None and result["swing"] is None
    assert result["yours"]["home_ep"] is not None
    assert result["first_down"]["chance"] > 0
    assert any("overtime" in w for w in result["warnings"])
    scored = evaluate_outcome(
        changed,
        OutcomeRequest(game_id=GAME, play_id=play_id(changed[index]), kind="touchdown"),
        scorer,
        fit,
        season_form,
        70.0,
    )
    assert scored["outcome"]["scored"] and scored["yours"]["home_wp"] is None


def test_table_lookup_matches_the_stored_shape():
    p = np.arange(4 * 14 * 6 * 2, dtype=float)
    table = Table({"p": p.tolist(), "n": p.tolist(), "shape": [4, 14, 6, 2]})
    for down, togo, yard, stratum, cell in [
        (1, 10, 75, 0, (0, 9, 4, 0)),
        (3, 1, 5, 1, (2, 0, 0, 1)),
        (4, 25, 99, 1, (3, 13, 5, 1)),
    ]:
        assert table.read(down, togo, yard, stratum)[0] == p.reshape(4, 14, 6, 2)[cell]


def test_togo_buckets():
    assert [togo_bucket(v) for v in (1, 10, 11, 13, 14, 20, 21, 40)] == [
        0,
        9,
        10,
        10,
        11,
        12,
        13,
        13,
    ]


def test_listed_changes_add_up_to_the_chance(fit):
    terms = {"intercept": 0.01, "offense": 0.2, "defense": -0.3, "own": 0.05, "opp": 0.02}
    chance, steps = fit.adjusted("series", 0.6, terms)
    total = steps["league"] + steps["offense"] + steps["defense"] + steps["momentum"]
    assert total == pytest.approx(100 * chance)


def test_stored_fit_records_what_it_applied(fit):
    assert fit.applied("series") and fit.applied("drive")
    assert not fit.applied("go")
    assert fit.models["series"]["beta"][1] > 0 > fit.models["series"]["beta"][2]
    assert "momentum" in fit.models["series"]["names"]
    assert fit.momentum["verdict"].startswith("reads as game-level form")


def test_unapplied_terms_fall_back_to_the_league_rate(fit, season_form):
    # Fourth-down team terms did not hold up on later seasons, so the table stands alone.
    result = fit.first_down(state(down=4, ydstogo=2), season_form, "2026-11-02", Heat())
    assert result["kind"] == "go" and not result["applied"]
    assert result["chance"] == result["league"]
    assert result["factors"] == [] and 0 < result["go_rate"] < 1


def test_first_down_lists_offense_defense_and_momentum(fit, season_form):
    result = fit.first_down(state(), season_form, "2026-11-02", Heat())
    assert [f["key"] for f in result["factors"]] == ["offense", "defense", "momentum"]
    assert "Measured size" in result["momentum_line"]
    assert result["snap"] < result["chance"]
    assert result["n"] > 200 and result["range"] is None


def test_thin_cells_carry_a_range(fit, season_form):
    # Third and 30 from the 2 cannot happen often enough for a thick cell.
    thin = state(down=2, ydstogo=19, yardline_100=19)
    result = fit.first_down(thin, season_form, "2026-11-02", Heat())
    if result["n"] < 200:
        assert result["range"][0] < result["league"] < result["range"][1]


def test_scoring_measures_are_distinct_and_named(fit, scorer, season_form):
    result = fit.scoring(state(), scorer, season_form, "2026-11-02", Heat())
    keys = [m["key"] for m in result["measures"]]
    assert keys == ["next_td", "next_fg", "drive_league", "drive_matchup", "ep", "own_form"]
    for measure in result["measures"]:
        assert measure["note"]


def test_window_excludes_the_game_itself_and_uses_at_most_four_games():
    def frame(day, epa):
        return {
            "season": 2026,
            "game_id": f"g{day}",
            "game_date": f"2026-10-{day:02d}",
            "posteam": "SEA",
            "defteam": "CHI",
            "qtr": 1,
            "down": 1,
            "play_type": "run",
            "epa": epa,
            "wp": 0.5,
            "play_deleted": 0.0,
            "yards_gained": 3.0,
            "success": 1.0,
            "third_down_converted": 0.0,
            "third_down_failed": 0.0,
            "fourth_down_converted": 0.0,
            "fourth_down_failed": 0.0,
            "qb_dropback": 0.0,
            "rush": 1.0,
            "cpoe": None,
            "sack": 0.0,
            "qb_hit": 0.0,
        }

    data = [frame(day, float(day)) for day in (1, 2, 3, 4, 5, 6)]
    table = FormTable(unit_frame(pl.DataFrame(data, infer_schema_length=None)))
    sums, games = table.window(2026, "SEA", "offense", "2026-10-06")
    assert games == ["g2", "g3", "g4", "g5"]
    assert sums["epa"] == 14
    prior = form_values(table, 2026, "SEA", "2026-10-01", k=100, r={"O": 0.5}, prior={"O": 0.2})
    assert prior["values"]["O"] == pytest.approx(0.1)
    assert prior["counts"]["offense"]["games"] == 0


def test_garbage_time_rows_are_left_out_of_team_form():
    row = {
        "season": 2026,
        "game_id": "g",
        "game_date": "2026-10-01",
        "posteam": "SEA",
        "defteam": "CHI",
        "down": 1,
        "play_type": "run",
        "epa": 1.0,
        "play_deleted": 0.0,
        "yards_gained": 3.0,
        "success": 1.0,
        "third_down_converted": 0.0,
        "third_down_failed": 0.0,
        "fourth_down_converted": 0.0,
        "fourth_down_failed": 0.0,
        "qb_dropback": 0.0,
        "rush": 1.0,
        "cpoe": None,
        "sack": 0.0,
        "qb_hit": 0.0,
    }
    data = [
        {**row, "qtr": 1, "wp": 0.99},
        {**row, "qtr": 3, "wp": 0.99},
        {**row, "qtr": 3, "wp": 0.5},
    ]
    frame = pl.DataFrame(data, infer_schema_length=None)
    offense = unit_frame(frame).filter(pl.col("unit") == "offense")
    assert offense["plays"][0] == 2
    assert (
        unit_frame(frame, garbage_filter=False).filter(pl.col("unit") == "offense")["plays"][0] == 3
    )


def test_heat_counts_only_the_last_five_snaps_before_a_play():
    row = {"down": 1, "play_type": "run", "posteam": "SEA", "epa": 1.0}
    rows = [row] * 7 + [{**row, "posteam": "CHI", "epa": -2.0}, {"down": None, "play_type": None}]
    assert heat_before(rows, 3).value("SEA") == 3
    assert heat_before(rows, 7).value("SEA") == 5
    assert heat_before(rows, 8).value("CHI") == -2
    heat = heat_before(rows, 0)
    assert heat.value("SEA") == 0 and heat.value(None) == 0


def test_form_columns_exist_in_the_stored_source():
    assert {"game_date", "fixed_drive_result", "epa", "wp"} <= set(FORM_COLUMNS)


def game_record():
    return {
        "game_id": GAME,
        "season": 2026,
        "week": 8,
        "gameday": "2026-11-02",
        "home_team": "SEA",
        "away_team": "CHI",
        "home_score": 17,
        "away_score": 34,
        "status": "final",
        "pbp_available": True,
        "net_home_epa": 13.7,
    }


def test_play_changes_run_to_the_next_shown_row(rows):
    view = game_view(game_record(), rows, [], [])
    plays = [p for d in view["drives"] for p in d["plays"]]
    assert len(plays) > 150
    for current, following in zip(plays, plays[1:], strict=False):
        if current["home_wp"] is not None and following["home_wp"] is not None:
            assert current["home_wp_after"] == following["home_wp"]
    known = [p for p in plays if p["delta_pp"] is not None]
    assert sum(p["delta_pp"] for p in known) == pytest.approx(
        100 * (known[-1]["home_wp_after"] - known[0]["home_wp"]), abs=1e-6
    )


def test_every_shown_play_has_a_menu_and_kickoffs_stay_with_their_drive(rows):
    view = game_view(game_record(), rows, [], [])
    assert view["drives"][0]["plays"][0]["kind"] == "kickoff"
    assert all(p["menu"] for d in view["drives"] for p in d["plays"])
    assert not math.isnan(view["chart"][0]["wp"])


def event(swing, disposition="accepted"):
    return {
        "id": f"e{swing}",
        "play_id": "1",
        "qtr": 4,
        "time": "12:42",
        "fouls": [{"type": "Defensive Holding", "team": "CHI"}],
        "disposition": disposition,
        "home_wp_swing_pp": swing,
    }


def test_week_card_gives_the_biggest_swing_and_each_teams_added_swings():
    events = [event(14.6), event(-8.4), event(10.1), event(1.0), event(30.0, "declined")]
    card = week_card(game_record(), events)
    assert card["biggest"]["team"] == "SEA" and card["biggest"]["pp"] == 14.6
    assert card["big_calls"] == {"total": 3, "by_team": {"SEA": 2, "CHI": 1}}
    totals = card["swing_totals"]
    assert totals["SEA"]["gained"] == pytest.approx(25.7) and totals["SEA"]["lost"] == 8.4
    assert totals["CHI"] == {"gained": totals["SEA"]["lost"], "lost": totals["SEA"]["gained"]}


def test_week_card_credits_the_away_team_when_the_swing_is_negative():
    card = week_card(game_record(), [event(-9.0)])
    assert card["biggest"]["team"] == "CHI" and card["biggest"]["pp"] == 9.0


def test_week_card_without_flags_invents_nothing():
    card = week_card({**game_record(), "status": "scheduled", "home_score": None}, [])
    assert card["biggest"] is None and card["big_calls"]["total"] == 0
