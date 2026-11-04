"""What-if states and alternate timelines, checked against recorded 2026 Week 8 CHI at SEA."""

import copy
import io
import json
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from refimpact.api import create_app
from refimpact.models import features
from refimpact.scenarios import (
    ChainError,
    ScenarioRequest,
    ScenarioSave,
    disputed_report,
    evaluate_scenario,
    read_state,
    scenario_plays,
)
from refimpact.schema import State
from refimpact.storage import Database

GAME = "2026_08_CHI_SEA"
OPI = {"play_id": "2895", "changes": {"down": 1, "ydstogo": 1, "yardline_100": 1}}
STOP = {"play_id": "3410", "changes": {"down": 4, "ydstogo": 6, "yardline_100": 41}}


@pytest.fixture(scope="module")
def rows():
    return json.loads(Path("tests/fixtures/chi_sea_2026_w08.json").read_text())


def run(rows, scorer, *edits):
    return evaluate_scenario(rows, ScenarioRequest(game_id=GAME, edits=list(edits)), scorer)


def point(result, play_id):
    return next(p for p in result["timeline"] if p["play_id"] == play_id)


def test_model_reproduces_the_published_line(rows, scorer):
    result = run(rows, scorer, OPI)
    assert result["parity"]["rows"] > 100
    assert result["parity"]["max_abs_wp_error"] < 1e-6


def test_edit_without_score_change_keeps_every_later_win_chance(rows, scorer):
    # Drive 1 punted in reality, so "no score" shifts nothing and later rows must not move.
    result = run(rows, scorer, {"play_id": "108", "drive_result": "none"})
    modeled = [p for p in result["timeline"] if p["status"] == "modeled" and not p.get("carried")]
    assert len(modeled) > 100
    assert result["final"]["alternate"] == {"home": 17, "away": 34, "result": "away_win"}
    for p in modeled:
        if p["actual_home_wp"] is not None and p["description"] != "END GAME":
            assert p["alternate_home_wp"] == pytest.approx(p["actual_home_wp"], abs=1e-6)


def test_moment_compares_the_entered_state_with_the_next_recorded_state(rows, scorer):
    result = run(rows, scorer, OPI)
    edit = result["edits"][0]
    assert edit["actual"]["state"]["down"] == 2 and edit["actual"]["state"]["ydstogo"] == 18
    assert edit["actual"]["home_wp"] == pytest.approx(0.211, abs=0.001)
    assert edit["alternate"]["home_wp"] == pytest.approx(0.296, abs=0.001)
    assert edit["wp_swing_pp"] == pytest.approx(8.4, abs=0.1)
    assert edit["points_swing"] == pytest.approx(2.9, abs=0.05)
    # No drive result means no timeline beyond the edited moment.
    assert result["final"]["alternate"] is None
    assert point(result, "3045")["alternate_home_wp"] is None


def test_timeline_replaces_the_drive_and_holds_later_plays(rows, scorer):
    result = run(rows, scorer, OPI | {"drive_result": "td_xp"})
    assert point(result, "3024")["status"] == "replaced"
    assert point(result, "3024")["alternate_home_wp"] is None
    # SEA 9 + 7 now, plus the 8 they really scored later: 24-20 at the delay of game.
    later = point(result, "3443")
    assert (later["alternate_home_score"], later["alternate_away_score"]) == (24, 20)
    assert later["alternate_home_wp"] == pytest.approx(0.680, abs=0.001)
    assert later["actual_home_wp"] == pytest.approx(0.269, abs=0.001)
    assert result["final"]["alternate"] == {"home": 24, "away": 34, "result": "away_win"}
    assert result["final"]["actual"] == {"home": 17, "away": 34}
    # The blocked kick is what the game did next, kept apart from the call.
    assert result["edits"][0]["aftermath_pp"] == pytest.approx(-12.8, abs=0.1)


def test_stop_removes_the_points_the_replaced_drive_really_scored(rows, scorer):
    result = run(rows, scorer, STOP | {"drive_result": "none"})
    assert result["edits"][0]["wp_swing_pp"] == pytest.approx(4.1, abs=0.1)
    assert result["final"]["alternate"] == {"home": 17, "away": 27, "result": "away_win"}


def test_edits_stack_on_the_shifted_scoreboard(rows, scorer):
    result = run(rows, scorer, STOP | {"drive_result": "none"}, OPI | {"drive_result": "td_xp"})
    assert [e["play_id"] for e in result["edits"]] == ["2895", "3410"]
    assert result["edits"][1]["actual"]["state"]["home_score"] == 24
    assert result["edits"][1]["alternate"]["home_wp"] == pytest.approx(0.727, abs=0.001)
    assert result["final"]["alternate"] == {"home": 24, "away": 27, "result": "away_win"}


def test_entered_scores_and_clock_are_applied(rows, scorer):
    edit = {
        "play_id": "2895",
        "changes": {"qtr": 3, "quarter_seconds": 14, "home_score": 16, "home_timeouts": 1},
    }
    state = run(rows, scorer, edit)["edits"][0]["alternate"]["state"]
    assert state["qtr"] == 3 and state["quarter_seconds"] == 14
    assert state["half_seconds_remaining"] == 914 and state["game_seconds_remaining"] == 914
    assert state["home_score"] == 16 and state["home_timeouts"] == 1


def test_flipping_the_winner_ends_the_line_at_the_result(rows, scorer):
    result = run(rows, scorer, OPI | {"changes": {"home_score": 40}, "drive_result": "none"})
    assert result["final"]["alternate"]["result"] == "home_win"
    assert point(result, "4319")["alternate_home_wp"] == 1.0
    assert point(result, "4319")["actual_home_wp"] == 0.0


def test_tie_is_not_given_a_win_chance(rows, scorer):
    edit = {"play_id": "108", "changes": {"home_score": 17}, "drive_result": "none"}
    result = run(rows, scorer, edit)
    assert result["final"]["alternate"] == {"home": 34, "away": 34, "result": "tie_ot_not_modeled"}
    assert point(result, "4319")["alternate_home_wp"] is None


def test_overtime_rows_are_not_held_or_scored(rows, scorer):
    overtime = copy.deepcopy(rows)
    end = next(r for r in overtime if r["desc"] == "END GAME")
    overtime.remove(end)
    snap = next(r for r in overtime if str(r["play_id"]) == "4224") | {
        "play_id": "5000",
        "order_sequence": 5000,
        "qtr": 5,
        "total_home_score": 41,
    }
    overtime += [
        snap,
        end | {"play_id": "5001", "order_sequence": 5001, "qtr": 5, "total_home_score": 41},
    ]
    result = run(overtime, scorer, OPI | {"drive_result": "none"})
    assert point(result, "5000")["status"] == "stopped"
    assert point(result, "5000")["alternate_home_wp"] is None
    assert result["final"]["alternate"]["home"] == 9 + 8
    assert result["final"]["actual"]["home"] == 41


@pytest.mark.parametrize(
    "edits,message",
    [
        ([{"play_id": "999999"}], "not in"),
        ([{"play_id": "2936"}], "no editable"),
        ([OPI, STOP | {"drive_result": "none"}], "needs a drive result"),
        (
            [OPI | {"drive_result": "td_xp"}, {"play_id": "2986", "drive_result": "none"}],
            "inside the drive",
        ),
        ([{"play_id": "2895", "changes": {"qtr": 1}}], "precedes"),
        ([{"play_id": "2895", "changes": {"ydstogo": 30}}], "distance to gain"),
        ([{"play_id": "2895", "changes": {"possession": "GB"}}], "possession"),
    ],
)
def test_impossible_scenarios_are_rejected(rows, scorer, edits, message):
    with pytest.raises(ValueError, match=message):
        run(rows, scorer, *edits)


def test_request_contracts():
    with pytest.raises(ValidationError, match="once per scenario"):
        ScenarioRequest(game_id=GAME, edits=[OPI, OPI])
    with pytest.raises(ValidationError):
        ScenarioRequest(game_id=GAME, edits=[])
    with pytest.raises(ValidationError):
        ScenarioRequest(game_id=GAME, edits=[{"play_id": "1", "changes": {"down": 5}}])
    with pytest.raises(ValidationError, match="source link"):
        ScenarioSave(game_id=GAME, edits=[OPI], label="OPI", disputed=True)


def test_source_rows_are_not_changed(rows, scorer):
    original = copy.deepcopy(rows)
    run(rows, scorer, OPI | {"drive_result": "td_xp"}, STOP | {"drive_result": "none"})
    assert rows == original


def test_every_play_with_a_state_is_prefilled_from_what_came_next(rows):
    plays = scenario_plays(rows)
    assert plays["home_team"] == "SEA" and plays["context"]["second_half_receiver"] == "CHI"
    by_id = {p["play_id"]: p for p in plays["plays"]}
    assert "2936" not in by_id
    flag = by_id["3410"]
    assert flag["flagged"] and flag["default"]["down"] == 3 and flag["default"]["ydstogo"] == 9
    no_call = by_id["108"]
    assert not no_call["flagged"] and no_call["default"]["down"] == 4
    # Prefilled states never cross halftime.
    assert by_id["2028"]["default"]["qtr"] == 2


def test_chart_at_the_edit_compares_the_same_moment_as_the_card(rows, scorer):
    # The chart used to read the pre-snap line against the post-play alternate: 7.08 vs 8.44 pp.
    result = run(rows, scorer, OPI)
    edit, mark = result["edits"][0], point(result, "2895")
    assert mark["status"] == "edit"
    assert 100 * (mark["alternate_home_wp"] - mark["actual_home_wp"]) == pytest.approx(
        edit["wp_swing_pp"], abs=1e-6
    )
    assert mark["actual_home_wp_before"] != mark["actual_home_wp"]


def test_vegas_model_is_shown_beside_the_ledger_model(rows, scorer):
    edit = run(rows, scorer, OPI)["edits"][0]
    assert edit["actual"]["home_wp_vegas"] != edit["actual"]["home_wp"]
    assert edit["wp_vegas_swing_pp"] == pytest.approx(
        100 * (edit["alternate"]["home_wp_vegas"] - edit["actual"]["home_wp_vegas"])
    )


def test_opportunity_counts_downs_in_hand_and_next_score_chances(rows, scorer):
    edit = run(rows, scorer, OPI)["edits"][0]
    o = edit["opportunity"]
    assert o["team"] == "SEA" and o["states"]["recorded"]["down"] == 2
    assert o["states"]["yours"]["downs_available"] == 4
    assert o["downs_gained"] == 1
    assert o["states"]["yours"]["td"] > o["states"]["recorded"]["td"]
    assert o["fg_change_pp"] < 0
    state = o["states"]["yours"]
    assert state["td"] + state["fg"] + state["no_score"] + state["other"] == pytest.approx(1)
    assert "plays" not in str(o).lower()


def test_drive_result_on_a_final_play_warns_and_can_name_the_team(rows, scorer):
    # On a punt the recorded next state belongs to the team that received it (2026_08_CLE_PIT:239).
    source = sorted(rows, key=lambda r: r["order_sequence"])
    final = next(
        r
        for i, r in enumerate(source[:-1])
        if r["play_type"] == "punt"
        and r["qtr"] <= 2
        and source[i + 1]["posteam"] not in {None, r["posteam"]}
    )
    edit = {"play_id": str(final["play_id"]), "drive_result": "td_xp"}
    unset = run(rows, scorer, edit)["edits"][0]
    assert unset["drive_team"] != final["posteam"]
    assert any("has the ball next" in w for w in unset["warnings"])
    named = run(rows, scorer, edit | {"drive_team": final["posteam"]})["edits"][0]
    assert named["drive_team"] == final["posteam"] and not named["warnings"]
    side = "home" if final["posteam"] == "SEA" else "away"
    assert (
        named["alternate_drive_end_score"][side] == named["alternate"]["state"][f"{side}_score"] + 7
    )
    with pytest.raises(ValueError, match="not in this game"):
        run(rows, scorer, edit | {"drive_team": "GB"})


def test_play_with_no_later_snap_says_the_fields_are_pre_play(rows, scorer):
    plays = scenario_plays(rows)["plays"]
    last_half = [p for p in plays if p["qtr"] == 4][-1]
    assert last_half["default_is_pre_play"]
    assert not next(p for p in plays if p["play_id"] == "2895")["default_is_pre_play"]
    warnings = run(rows, scorer, {"play_id": last_half["play_id"]})["edits"][0]["warnings"]
    assert any("own pre-snap state" in w for w in warnings)


def test_decided_alternate_ends_at_its_result_in_an_overtime_game(rows, scorer):
    overtime = copy.deepcopy(rows)
    end = next(r for r in overtime if r["desc"] == "END GAME")
    overtime.remove(end)
    snap = next(r for r in overtime if str(r["play_id"]) == "4224") | {
        "play_id": "5000",
        "order_sequence": 5000,
        "qtr": 5,
        "total_home_score": 41,
    }
    overtime += [
        snap,
        end | {"play_id": "5001", "order_sequence": 5001, "qtr": 5, "total_home_score": 41},
    ]
    result = run(overtime, scorer, OPI | {"changes": {"home_score": 40}, "drive_result": "none"})
    assert result["final"]["alternate"]["result"] == "home_win"
    assert point(result, "5001")["alternate_home_wp"] == 1.0
    assert result["final"]["actual_regulation"] == {"home": 17, "away": 34}
    assert result["final"]["actual"]["home"] == 41


def test_blank_state_reads_without_a_game(scorer):
    state = State(
        season=2026, home_team="SEA", away_team="CHI", possession="SEA", home_score=0,
        away_score=0, qtr=1, game_seconds_remaining=3600, half_seconds_remaining=1800, down=1,
        ydstogo=10, yardline_100=75, home_timeouts=3, away_timeouts=3,
        second_half_receiver="CHI", roof="outdoors",
    )  # fmt: skip
    result = read_state(state, scorer)
    assert 0 < result["home_wp"] < 1 and "Model estimate" in result["note"]


def test_dispute_report_marks_values_that_depend_on_earlier_edits(rows, scorer):
    result = run(rows, scorer, OPI | {"drive_result": "td_xp"}, STOP | {"drive_result": "none"})
    record = {
        "scenario_id": "a", "label": "both", "kind": "flag", "source_url": "https://x.test/a",
        "assumptions": [], "disputed": True, "created_at": "1", "pbp_source_id": "s",
        "result": result,
    }  # fmt: skip
    calls = disputed_report([record], GAME, "s")["calls"]
    assert [c["stacked"] for c in calls] == [False, True]


def test_dispute_report_sums_expected_points_without_claiming_a_margin_and_counts_plays_once():
    def saved(when, play, points, disputed=True):
        return {
            "scenario_id": when, "label": play, "kind": "flag", "source_url": "https://x.test/a",
            "assumptions": [], "disputed": disputed, "created_at": when, "pbp_source_id": "s",
            "result": {
                "final": {"actual": {"home": 17, "away": 34}},
                "edits": [{
                    "play_id": play, "qtr": 3, "time": "00:14", "wp_swing_pp": 8.4,
                    "points_swing": points, "aftermath_pp": None,
                }],
            },
        }  # fmt: skip

    records = [saved("3", "2895", 3.0), saved("2", "2895", 9.0), saved("1", "108", 4.0)]
    records.append(saved("4", "683", 50.0, disputed=False))
    report = disputed_report(records, GAME, "s")
    assert [c["play_id"] for c in report["calls"]] == ["108", "2895"]
    assert report["home_sum_at_moment_expected_points"] == 7.0
    assert not any("margin" in key for key in report)
    assert "not a final margin" in report["note"]
    assert not report["calls"][0]["stale"]
    assert not report["calls"][0]["stacked"]
    assert disputed_report(records, GAME, "newer")["calls"][0]["stale"]
    assert disputed_report([], GAME, "s")["calls"] == []


@pytest.fixture
def stored(tmp_path, rows):
    db = Database(tmp_path / "data.sqlite")
    frame = pl.DataFrame(rows, infer_schema_length=None)
    content = io.BytesIO()
    frame.write_parquet(content)
    pbp = db.ingest("pbp", 2026, frame, "fixture", content.getvalue())
    game = {
        "game_id": GAME, "season": 2026, "week": 8, "gameday": "2026-11-02", "gametime": "20:15",
        "home_team": "SEA", "away_team": "CHI", "home_score": 17, "away_score": 34,
        "status": "final", "pbp_complete": True, "scored_calls": 0, "penalty_rows": 0,
        "review_calls": 0, "valued_penalties": 0, "net_home_epa": 0, "production": {},
        "penalty_coverage": {},
    }  # fmt: skip
    data = {
        "generated_at": "2026-11-03T12:00:00Z", "as_of": "2026-11-03", "default_season": 2026,
        "default_week": 8, "model_id": "test", "mode": "postgame", "sources": {},
        "games": [game], "calls": [], "review": [], "weeks": [{"season": 2026, "week": 8}],
    }  # fmt: skip
    db.save_catalog(data, pbp_source_id=pbp["source_id"])
    return tmp_path / "data.sqlite"


@pytest.fixture
def local(stored, scorer):
    with TestClient(create_app(stored), client=("127.0.0.1", 50000)) as client:
        yield client


SAVE = {
    "game_id": GAME,
    "edits": [OPI | {"drive_result": "td_xp"}],
    "label": "Smith-Njigba OPI",
    "kind": "flag",
    "disputed": True,
    "source_url": "https://example.com/report",
    "assumptions": ["Catch stands at the 1."],
}


def test_api_prefills_and_evaluates_without_saving(local):
    plays = local.get("/api/v1/scenarios/plays", params={"game_id": GAME}).json()
    assert any(p["play_id"] == "2895" for p in plays["plays"])
    result = local.post("/api/v1/scenarios/evaluate", json={"game_id": GAME, "edits": [OPI]}).json()
    assert result["edits"][0]["wp_swing_pp"] == pytest.approx(8.4, abs=0.1)
    assert "ruling correctness not assessed" in result["note"]
    assert local.get("/api/v1/scenarios", params={"game_id": GAME}).json()["scenarios"] == []


def test_api_errors_are_readable(local):
    bad = {"game_id": GAME, "edits": [{"play_id": "2895", "changes": {"ydstogo": 30}}]}
    response = local.post("/api/v1/scenarios/evaluate", json=bad)
    assert response.status_code == 422 and "distance to gain" in response.json()["detail"]
    missing = {"game_id": "2026_08_GB_DET", "edits": [{"play_id": "1"}]}
    assert local.post("/api/v1/scenarios/evaluate", json=missing).status_code == 404
    assert local.get("/api/v1/scenarios/plays", params={"game_id": "x"}).status_code == 422
    unsourced = SAVE | {"source_url": None}
    assert local.post("/api/v1/scenarios", json=unsourced).status_code == 422


def test_api_save_list_report_delete(local):
    saved = local.post("/api/v1/scenarios", json=SAVE)
    assert saved.status_code == 201
    scenario_id = saved.json()["scenario_id"]
    # Saved lists change without a new transform, so they must not be served from the ETag cache.
    assert "etag" not in local.get("/api/v1/scenarios", params={"game_id": GAME}).headers
    assert local.get(f"/api/v1/scenarios/{scenario_id}").json()["label"] == "Smith-Njigba OPI"
    report = local.get("/api/v1/scenarios/disputes", params={"game_id": GAME}).json()
    assert report["calls"][0]["source_url"] == SAVE["source_url"]
    assert report["home_sum_at_moment_expected_points"] == pytest.approx(2.9, abs=0.1)
    assert local.delete(f"/api/v1/scenarios/{scenario_id}").status_code == 204
    assert local.get(f"/api/v1/scenarios/{scenario_id}").status_code == 404
    assert local.delete(f"/api/v1/scenarios/{scenario_id}").status_code == 404


def test_lan_clients_can_compute_but_not_write(stored, local):
    scenario_id = local.post("/api/v1/scenarios", json=SAVE).json()["scenario_id"]
    with TestClient(create_app(stored), client=("192.168.1.20", 50000)) as lan:
        evaluated = lan.post("/api/v1/scenarios/evaluate", json={"game_id": GAME, "edits": [OPI]})
        assert evaluated.status_code == 200
        assert lan.post("/api/v1/scenarios", json=SAVE).status_code == 403
        assert lan.delete(f"/api/v1/scenarios/{scenario_id}").status_code == 403
    assert len(local.get("/api/v1/scenarios", params={"game_id": GAME}).json()["scenarios"]) == 1


def test_saving_never_touches_source_rows_or_the_tweet_queue(stored, local):
    def read():
        with Database(stored).connect() as db:
            rows = db.execute("SELECT count(*) FROM raw_plays").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return rows, {table["name"] for table in tables}

    before, _ = read()
    local.post("/api/v1/scenarios", json=SAVE)
    after, tables = read()
    assert after == before and "scenarios" in tables
    assert not any("outbox" in name or name == "posts" for name in tables)


# A chain: the disputed OPI stands as a touchdown, then steps the user authors.
KICK_SPOT = 69.0
TD_EDIT = OPI | {"drive_result": "td_xp"}
AFTER_KICK = {
    "possession": "CHI", "down": 1, "ydstogo": 10, "yardline_100": KICK_SPOT, "qtr": 4,
    "quarter_seconds": 900, "home_score": 16, "away_score": 20, "home_timeouts": 3,
    "away_timeouts": 3, "drive_result": "none",
}  # fmt: skip
SEA_BALL = AFTER_KICK | {
    "possession": "SEA", "yardline_100": 65, "quarter_seconds": 750, "drive_result": None,
}  # fmt: skip


def chained(rows, scorer, steps, edits=(TD_EDIT,), from_play="2895", **end):
    request = ScenarioRequest(
        game_id=GAME,
        edits=list(edits),
        chain={"from_play_id": from_play, "steps": steps} | end,
    )
    return evaluate_scenario(rows, request, scorer, KICK_SPOT)


def state_of(node, **changes):
    fields = {k: v for k, v in node["state"].items() if k != "quarter_seconds"}
    return State(**fields | changes)


def test_edit_prefills_the_next_step_from_the_alternate_after_the_kick(rows, scorer):
    # Which team, what score and where: the opponent, on the shifted board, at the kick spot.
    edit = evaluate_scenario(
        rows, ScenarioRequest(game_id=GAME, edits=[TD_EDIT]), scorer, KICK_SPOT
    )["edits"][0]
    filled = edit["next_state"]
    assert filled["possession"] == "CHI"
    assert (filled["home_score"], filled["away_score"]) == (16, 20)
    assert (filled["down"], filled["ydstogo"], filled["yardline_100"]) == (1, 10, KICK_SPOT)
    assert filled["qtr"] == edit["alternate"]["state"]["qtr"]
    assert filled["quarter_seconds"] == edit["alternate"]["state"]["quarter_seconds"]
    # The board is the alternate's, not the recorded row's.
    assert edit["actual"]["state"]["home_score"] == 9


def test_later_alternate_points_offer_their_own_shifted_state(rows, scorer):
    result = run(rows, scorer, TD_EDIT)
    held = point(result, "3410")
    assert held["status"] == "modeled"
    # The touchdown moves the board at that later snap and nothing else about it.
    assert held["alternate_state"]["home_score"] == held["actual_home_score"] + 7
    assert held["alternate_state"]["away_score"] == held["actual_away_score"]
    assert held["alternate_state"]["possession"] == held["possession"]


def test_field_goal_and_no_score_prefills(rows, scorer):
    filled = chained(rows, scorer, [AFTER_KICK | {"drive_result": "fg"}])
    after = filled["chain"]["nodes"][-1]["next_state"]
    assert (after["possession"], after["away_score"], after["yardline_100"]) == ("SEA", 23, 69)
    filled = chained(rows, scorer, [AFTER_KICK])
    after = filled["chain"]["nodes"][-1]["next_state"]
    # No punt is assumed: the other team's ball at the same spot, on an unchanged board.
    assert (after["possession"], after["yardline_100"], after["away_score"]) == ("SEA", 31, 20)
    assert (after["down"], after["ydstogo"]) == (1, 10)
    open_drive = chained(rows, scorer, [AFTER_KICK | {"drive_result": None}])
    after = open_drive["chain"]["nodes"][-1]["next_state"]
    assert (after["possession"], after["yardline_100"]) == ("CHI", 69)


def test_zero_change_branch_reproduces_the_source_node(rows, scorer):
    # From an edit with no drive result, and from a held later snap.
    moment = run(rows, scorer, OPI)["edits"][0]
    same = moment["next_state"] | {"drive_result": None}
    nodes = chained(rows, scorer, [same], edits=(OPI,))["chain"]["nodes"]
    for key in ("home_wp", "home_wp_vegas", "home_ep", "home_value", "opportunity"):
        assert nodes[1][key] == nodes[0][key], key
    assert nodes[0]["home_wp"] == moment["alternate"]["home_wp"]
    assert nodes[1]["wp_swing_pp"] == 0 and nodes[1]["points_swing"] == 0

    held = point(run(rows, scorer, TD_EDIT), "3410")
    nodes = chained(rows, scorer, [held["alternate_state"]], from_play="3410")["chain"]["nodes"]
    assert nodes[0]["home_wp"] == nodes[1]["home_wp"] == held["alternate_home_wp"]
    assert nodes[0]["home_wp_vegas"] == nodes[1]["home_wp_vegas"]
    assert nodes[0]["kind"] == "modeled"


def test_fixed_chain_matches_direct_model_calls(rows, scorer):
    result = chained(rows, scorer, [AFTER_KICK, SEA_BALL])
    nodes = result["chain"]["nodes"]
    assert [n["state"]["possession"] for n in nodes] == ["SEA", "CHI", "SEA"]
    assert [n["kind"] for n in nodes] == ["edit", "step", "step"]
    assert nodes[0]["score_after"] == {"home": 16, "away": 20}
    assert nodes[0]["next_state"]["possession"] == "CHI"
    context = state_of(nodes[0])
    direct = [
        context.model_copy(update=changes)
        for changes in (
            {},
            {"possession": "CHI", "down": 1, "ydstogo": 10.0, "yardline_100": 69.0,
             "home_score": 16},
            {"possession": "SEA", "down": 1, "ydstogo": 10.0, "yardline_100": 65.0,
             "home_score": 16, "game_seconds_remaining": 750.0, "half_seconds_remaining": 750.0},
        )
    ]  # fmt: skip
    for node, state in zip(nodes, direct, strict=True):
        assert node["home_wp"] == scorer.predict([state], "SEA")[0].wp
        assert node["home_ep"] == scorer.predict([state], "SEA")[0].ep
        assert node["home_wp_vegas"] == scorer.vegas_wp([state], "SEA")[0]
        chance = scorer.next_score([state], state.possession)[0]
        assert node["opportunity"]["td"] == chance["td"]
        assert node["opportunity"]["fg"] == chance["fg"]
        assert node["opportunity"]["team"] == state.possession
    assert nodes[1]["opportunity"]["downs_available"] == 4
    assert nodes[1]["wp_swing_pp"] == 100 * (nodes[1]["home_wp"] - nodes[0]["home_wp"])
    assert nodes[0]["wp_swing_pp"] is None


def test_popping_back_and_continuing_replaces_only_the_tail(rows, scorer):
    first = chained(rows, scorer, [AFTER_KICK, SEA_BALL])["chain"]["nodes"]
    other = SEA_BALL | {"yardline_100": 40, "quarter_seconds": 500}
    second = chained(rows, scorer, [AFTER_KICK, other])["chain"]["nodes"]
    assert len(second) == len(first)
    assert second[:2] == first[:2]
    assert second[2]["state"]["yardline_100"] == 40
    assert second[2]["home_wp"] != first[2]["home_wp"]
    # Popped to the first step: the path ends there, with nothing kept from the old tail.
    popped = chained(rows, scorer, [AFTER_KICK])["chain"]["nodes"]
    assert popped == first[:2]


def test_prediction_ends_at_the_last_node_with_no_final_score(rows, scorer):
    result = chained(rows, scorer, [AFTER_KICK, SEA_BALL])
    assert result["final"]["alternate"] is None
    end = result["chain"]["end"]
    assert end["mode"] == "prediction"
    assert end["score"] == {"home": 16, "away": 20}
    assert end["home_wp"] == result["chain"]["nodes"][-1]["home_wp"]
    assert "Conditional" in end["label"] and not end["reads_before_drive_result"]
    assert "not a forecast" in result["note"]
    assert "held as played" not in result["note"]
    assert not any(k in end for k in ("probability", "path_probability"))
    later = result["timeline"][-1]
    assert later["status"] == "branched" and later["alternate_home_wp"] is None
    assert later["actual_home_wp"] is not None
    # Recorded plays after the branch stay drawn, with nothing alternate on them.
    assert all(
        p["alternate_home_wp"] is None and p["alternate_state"] is None
        for p in result["timeline"]
        if p["status"] == "branched"
    )
    # A drive result on the last node moves the score but not the reading, and says so.
    last = chained(rows, scorer, [AFTER_KICK | {"drive_result": "td_xp"}])["chain"]["end"]
    assert last["score"] == {"home": 16, "away": 27} and last["reads_before_drive_result"]


def test_rejoin_holds_recorded_plays_labeled_hindsight(rows, scorer):
    result = chained(rows, scorer, [AFTER_KICK, SEA_BALL], end="rejoin", rejoin_play_id="3410")
    assert result["chain"]["end"]["mode"] == "rejoin"
    assert "Hindsight" in result["chain"]["end"]["label"]
    assert result["final"]["alternate"]["mode"] == "rejoin"
    assert "hindsight" in result["note"]
    # The steps replace the recorded plays up to the rejoin, including SEA's real score there.
    held = point(result, "3410")
    assert held["status"] == "modeled"
    assert (held["alternate_home_score"], held["alternate_away_score"]) == (16, 20)
    assert point(result, "3273")["status"] == "replaced"
    assert point(result, "3273")["alternate_home_wp"] is None
    assert result["final"]["alternate"]["home"] == 16 and result["final"]["alternate"]["away"] == 34
    rejoined = held["alternate_state"]
    assert rejoined["possession"] == held["possession"] and rejoined["home_score"] == 16
    # A score in the chain carries into everything held after it.
    scored = chained(
        rows, scorer, [AFTER_KICK | {"drive_result": "fg"}], end="rejoin", rejoin_play_id="3410"
    )
    assert scored["final"]["alternate"]["away"] == 37
    assert scored["chain"]["end"]["score_at_rejoin"] == {"home": 16, "away": 23}
    assert "resume with CHI" in result["chain"]["warnings"][0]
    agrees = chained(rows, scorer, [AFTER_KICK | {"drive_result": None}], end="rejoin",
                     rejoin_play_id="3410")  # fmt: skip
    assert not agrees["chain"]["warnings"]


def test_a_chain_cannot_mix_its_endings():
    base = {"from_play_id": "2895", "steps": [AFTER_KICK]}
    with pytest.raises(ValidationError, match="cannot also rejoin"):
        ScenarioRequest(game_id=GAME, edits=[TD_EDIT], chain=base | {"rejoin_play_id": "3410"})
    with pytest.raises(ValidationError, match="needs a play to rejoin"):
        ScenarioRequest(game_id=GAME, edits=[TD_EDIT], chain=base | {"end": "rejoin"})


@pytest.mark.parametrize(
    ("steps", "field", "message"),
    [
        ([AFTER_KICK, SEA_BALL | {"quarter_seconds": 800}, AFTER_KICK | {"quarter_seconds": 850}],
         ("steps", 2, "quarter_seconds"), "cannot run backward"),
        ([AFTER_KICK | {"qtr": 3, "quarter_seconds": 100}],
         ("steps", 0, "quarter_seconds"), "cannot run backward"),
        ([AFTER_KICK | {"quarter_seconds": 0}], ("steps", 0, "quarter_seconds"), "no time left"),
        ([AFTER_KICK | {"home_score": 9}], ("steps", 0, "home_score"), "cannot fall below 16"),
        ([AFTER_KICK | {"drive_result": "td_xp"}, SEA_BALL],
         ("steps", 1, "away_score"), "cannot fall below 27"),
        ([AFTER_KICK, SEA_BALL | {"home_timeouts": 2},
          SEA_BALL | {"home_timeouts": 3}], ("steps", 2, "home_timeouts"), "regain a timeout"),
        ([AFTER_KICK | {"ydstogo": 80}], ("steps", 0, "ydstogo"), "distance to gain"),
        ([AFTER_KICK | {"possession": "GB"}], ("steps", 0, "possession"), "possession"),
        ([AFTER_KICK | {"drive_team": "GB", "drive_result": "fg"}],
         ("steps", 0, "drive_team"), "not in this game"),
    ],
)  # fmt: skip
def test_impossible_steps_name_the_field(rows, scorer, steps, field, message):
    with pytest.raises(ChainError, match=message) as caught:
        chained(rows, scorer, steps)
    assert caught.value.loc == ("chain", *field)


def test_overtime_step_is_rejected_with_a_clear_message():
    with pytest.raises(ValidationError, match="Overtime is not modeled") as caught:
        ScenarioRequest(
            game_id=GAME,
            edits=[TD_EDIT],
            chain={"from_play_id": "2895", "steps": [AFTER_KICK | {"qtr": 5}]},
        )
    assert caught.value.errors()[0]["loc"][-1] == "qtr"


@pytest.mark.parametrize("start", ["2148", "9999"])
def test_chain_must_start_on_the_alternate_timeline_after_the_last_edit(rows, scorer, start):
    with pytest.raises(ChainError, match="not a state on the alternate timeline"):
        chained(rows, scorer, [AFTER_KICK], from_play=start)


def test_rejoin_play_must_follow_the_chain(rows, scorer):
    with pytest.raises(ChainError, match="after the chain's start") as caught:
        chained(rows, scorer, [AFTER_KICK], end="rejoin", rejoin_play_id="2148")
    assert caught.value.loc == ("chain", "rejoin_play_id")
    with pytest.raises(ChainError, match="comes before the last step"):
        chained(
            rows,
            scorer,
            [AFTER_KICK | {"quarter_seconds": 10}],
            end="rejoin",
            rejoin_play_id="3410",
        )


def first_half_play(rows):
    return next(
        p for p in scenario_plays(rows)["plays"] if p["qtr"] == 1 and p["default"]["qtr"] == 1
    )


def test_second_half_receiver_on_hypothetical_second_half_states(rows, scorer):
    play = first_half_play(rows)
    edit = {"play_id": play["play_id"], "changes": {}}
    source = play["default"]
    receiver = source["second_half_receiver"]
    other = "CHI" if receiver == "SEA" else "SEA"
    fields = {k: source[k] for k in (
        "down", "ydstogo", "yardline_100", "home_score", "away_score", "home_timeouts",
        "away_timeouts",
    )}  # fmt: skip
    q2 = fields | {"possession": receiver, "qtr": 2, "quarter_seconds": 300}
    # The receiver's first-half bonus is on in Q2 and gone after the half; timeouts come back.
    q3 = fields | {"possession": receiver, "qtr": 3, "quarter_seconds": 900, "home_timeouts": 3,
                   "away_timeouts": 3}  # fmt: skip
    nodes = chained(rows, scorer, [q2, q3], edits=(edit,), from_play=play["play_id"])["chain"][
        "nodes"
    ]
    second = state_of(nodes[2])
    assert second.second_half_receiver == receiver
    first = state_of(nodes[1])
    assert first.second_half_receiver == receiver
    assert features(first)[1][0] == 1 and features(second)[1][0] == 0
    assert features(first.model_copy(update={"possession": other}))[1][0] == 0
    assert nodes[2]["home_wp"] == scorer.predict([second], first.home_team)[0].wp


def test_chain_api_reports_a_field_error_and_round_trips(local):
    chain = {"from_play_id": "2895", "steps": [AFTER_KICK, SEA_BALL]}
    body = {"game_id": GAME, "edits": [TD_EDIT]}
    bad = local.post(
        "/api/v1/scenarios/evaluate",
        json=body | {"chain": chain | {"steps": [AFTER_KICK | {"home_score": 9}]}},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["loc"] == ["body", "chain", "steps", 0, "home_score"]
    assert "cannot fall below" in bad.json()["detail"][0]["msg"]
    extra = local.post(
        "/api/v1/scenarios/evaluate",
        json=body | {"chain": chain | {"steps": [AFTER_KICK | {"qtr": 5}]}},
    )
    assert "Overtime is not modeled" in extra.json()["detail"][0]["msg"]

    evaluated = local.post("/api/v1/scenarios/evaluate", json=body | {"chain": chain})
    assert evaluated.status_code == 200
    # Without the kick spot supplied, the API measures one from the stored source.
    assert evaluated.json()["chain"]["kick_spot"] is not None

    saved = local.post("/api/v1/scenarios", json=body | {"chain": chain, "label": "Stop then ball"})
    assert saved.status_code == 201
    record = local.get(f"/api/v1/scenarios/{saved.json()['scenario_id']}").json()
    assert record["request"]["chain"]["steps"][1]["possession"] == "SEA"
    assert record["result"]["chain"]["nodes"][2]["state"]["possession"] == "SEA"
    again = local.post(
        "/api/v1/scenarios/evaluate",
        json={"game_id": GAME, "edits": record["request"]["edits"],
              "chain": record["request"]["chain"]},
    ).json()  # fmt: skip
    assert again["chain"]["nodes"] == record["result"]["chain"]["nodes"]


def test_old_saved_scenarios_still_load_and_rerun(stored, local):
    saved = local.post("/api/v1/scenarios", json=SAVE).json()
    # A record from before chains: its request has no chain key.
    old = {"game_id": GAME, "edits": saved["request"]["edits"]}
    with Database(stored).connect() as db:
        db.execute(
            "UPDATE scenarios SET request=? WHERE scenario_id=?",
            (json.dumps(old), saved["scenario_id"]),
        )
    record = local.get(f"/api/v1/scenarios/{saved['scenario_id']}").json()
    assert "chain" not in record["request"]
    again = local.post("/api/v1/scenarios/evaluate", json=record["request"])
    assert again.status_code == 200
    assert "chain" not in again.json()
    report = local.get("/api/v1/scenarios/disputes", params={"game_id": GAME}).json()
    assert len(report["calls"]) == 1


def test_chains_stay_out_of_the_disputes_report(local):
    chain = {"from_play_id": "2895", "steps": [AFTER_KICK]}
    refused = local.post("/api/v1/scenarios", json=SAVE | {"chain": chain})
    assert refused.status_code == 422
    assert "not a disputed call" in refused.text
    assert (
        local.post(
            "/api/v1/scenarios", json=SAVE | {"chain": chain, "disputed": False, "label": "Chain"}
        ).status_code
        == 201
    )
    report = local.get("/api/v1/scenarios/disputes", params={"game_id": GAME}).json()
    assert report["calls"] == []
    # A record marked disputed some other way still does not reach it.
    assert (
        disputed_report(
            [{"disputed": True, "request": {"chain": chain}, "created_at": "1"}], GAME, "s"
        )["calls"]
        == []
    )


def test_lan_clients_can_evaluate_a_chain_but_not_save_it(stored, local):
    chain = {"from_play_id": "2895", "steps": [AFTER_KICK]}
    body = {"game_id": GAME, "edits": [TD_EDIT], "chain": chain}
    with TestClient(create_app(stored), client=("192.168.1.20", 50000)) as lan:
        assert lan.post("/api/v1/scenarios/evaluate", json=body).status_code == 200
        assert lan.post("/api/v1/scenarios", json=body | {"label": "x"}).status_code == 403


def test_saving_a_chain_never_touches_source_rows_or_the_tweet_queue(stored, local):
    def read():
        with Database(stored).connect() as db:
            count = db.execute("SELECT count(*) FROM raw_plays").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return count, {table["name"] for table in tables}

    before, _ = read()
    chain = {"from_play_id": "2895", "steps": [AFTER_KICK]}
    assert (
        local.post(
            "/api/v1/scenarios",
            json={"game_id": GAME, "edits": [TD_EDIT], "chain": chain, "label": "x"},
        ).status_code
        == 201
    )
    after, tables = read()
    assert after == before
    assert not any("outbox" in name or name == "posts" for name in tables)
