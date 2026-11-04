import copy
from datetime import date

import pytest
from fastapi.testclient import TestClient

from refimpact.api import create_app
from refimpact.catalog import select_current
from refimpact.storage import Database


@pytest.fixture
def snapshot(tmp_path, impact):
    c = impact.comparison
    call = impact.model_dump(mode="json") | {
        "tweet": "preview",
        "season": c.before.season,
        "week": int(c.game_id.split("_")[1]),
        "id": f"{c.game_id}:{c.play_id}",
    }
    game = {
        "game_id": c.game_id,
        "season": c.before.season,
        "week": 1,
        "gameday": "2024-09-08",
        "home_team": c.before.home_team,
        "away_team": c.before.away_team,
        "home_score": 34,
        "away_score": 28,
        "status": "final",
        "pbp_complete": True,
        "scored_calls": 1,
        "penalty_rows": 2,
        "review_calls": 1,
        "net_home_epa": -0.5,
        "largest_call_swing_pp": 3,
    }
    future = game | {
        "game_id": "2024_02_SEA_SF",
        "week": 2,
        "gameday": "2024-09-15",
        "status": "scheduled",
        "scored_calls": 0,
        "penalty_rows": 0,
        "review_calls": 0,
        "net_home_epa": None,
        "home_score": None,
        "away_score": None,
        "pbp_complete": False,
    }
    data = {
        "generated_at": "2024-09-10T12:00:00Z",
        "as_of": "2024-09-10",
        "default_season": 2024,
        "default_week": 1,
        "model_id": "test",
        "mode": "postgame",
        "sources": {},
        "games": [game, future],
        "calls": [call],
        "review": [{"game_id": c.game_id, "reason": "unsupported_penalty_type"}],
        "weeks": [{"season": 2024, "week": 1}, {"season": 2024, "week": 2}],
    }
    path = tmp_path / "data.sqlite"
    Database(path).save_catalog(data)
    return path, data


@pytest.fixture
def client(snapshot):
    with TestClient(create_app(snapshot[0])) as client:
        yield client


def test_dashboard_assets_and_documentation(client):
    assert client.get("/").status_code == 200
    assert "A flag changes" in client.get("/").text
    for url in ["/assets/app.js", "/assets/style.css", "/docs", "/openapi.json"]:
        assert client.get(url).status_code == 200
    assert client.get("/api/v1/methodology").json()["correctness_assessed"] is False


def test_current_week_games_calls_and_details(client, snapshot):
    result = client.get("/api/v1/games").json()
    assert result["week"] == 1 and len(result["games"]) == 1
    result = client.get("/api/v1/calls?min_swing=2").json()
    assert result["total"] == 1
    assert client.get("/api/v1/calls?min_swing=4").json()["total"] == 0
    assert client.get("/api/v1/calls?team=SEA").json()["total"] == 0
    game_id = snapshot[1]["games"][0]["game_id"]
    detail = client.get(f"/api/v1/games/{game_id}").json()
    assert len(detail["calls"]) == 1 and len(detail["review"]) == 1


def test_upcoming_results_are_missing_not_zero(client):
    game = client.get("/api/v1/games?season=2024&week=2").json()["games"][0]
    assert game["net_home_epa"] is None
    result = client.get("/api/v1/analysis?season=2024&week=2").json()
    assert result["games"] == 0 and result["pearson_r_margin"] is None


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/games?week=0",
        "/api/v1/games?season=1900",
        "/api/v1/calls?limit=1000",
        "/api/v1/calls?offset=-1",
        "/api/v1/calls?min_swing=-1",
        "/api/v1/calls?team=../../",
    ],
)
def test_query_contracts(client, url):
    assert client.get(url).status_code == 422


def test_missing_week_or_game_returns_404(client):
    assert client.get("/api/v1/games?week=22").status_code == 404
    assert client.get("/api/v1/games/no-such-game").status_code == 404


def test_live_snapshot_replacement(client, snapshot):
    path, data = snapshot
    changed = copy.deepcopy(data)
    changed["generated_at"] = "2024-09-10T14:00:00Z"
    Database(path).save_catalog(changed)
    assert client.get("/api/v1/status").json()["generated_at"] == changed["generated_at"]


def test_comparison_is_persisted_without_queuing(client, comparison, scorer):
    result = client.post("/api/v1/compare", json=comparison.model_dump(mode="json"))
    assert result.status_code == 200
    assert result.json()["epa_impact"] < 0
    assert "outbox_id" not in result.json()
    saved = client.get("/api/v1/calculations/" + result.json()["calculation_id"])
    assert saved.status_code == 200
    assert saved.json()["result"]["epa_impact"] == result.json()["epa_impact"]
    assert client.post("/api/v1/compare", json={"ruling": "incomplete"}).status_code == 422


def test_csv_and_missing_snapshot(client, tmp_path):
    csv = client.get("/api/v1/export.csv")
    assert csv.status_code == 200 and "net_home_epa" in csv.text
    assert len(csv.text.splitlines()) == 2
    with TestClient(create_app(tmp_path / "absent.json")) as empty:
        assert empty.get("/api/v1/status").json()["status"] == "no_data"
        assert empty.get("/api/v1/games").status_code == 503


def test_tuesday_selects_previous_started_slate_not_upcoming_week(snapshot):
    games = snapshot[1]["games"]
    assert select_current(games, date(2024, 9, 10)) == (2024, 1)
    assert select_current(games, date(2024, 9, 15)) == (2024, 2)


def test_expanded_ledger_api_contracts(client, snapshot):
    path, data = snapshot
    game = data["games"][0]
    game["production"] = {"BUF": {"team": "BUF", "offensive_epa": 2.5}}
    game["net_home_production_epa_per_snap"] = 0.2
    game["net_home_drive_epa"] = 3.0
    event = {
        "id": "flag",
        "game_id": game["game_id"],
        "season": 2024,
        "week": 1,
        "fouls": [{"disposition": "declined"}],
        "drive_id": "drive",
        "decision": None,
    }
    drive = {
        "id": "drive",
        "game_id": game["game_id"],
        "season": 2024,
        "week": 1,
        "timeline": [{"play_id": "100", "epa": 2.5}],
    }
    data.update(penalties=[event], drives=[drive])
    Database(path).save_catalog(data)
    assert client.get("/api/v1/penalties?disposition=declined").json()["penalties"] == [event]
    assert client.get("/api/v1/penalties?disposition=accepted").json()["total"] == 0
    assert client.get("/api/v1/penalties?disposition=invalid").status_code == 422
    assert client.get("/api/v1/penalties?limit=1001").status_code == 422
    assert client.get("/api/v1/production").json()["games"][0]["teams"] == game["production"]
    assert "timeline" not in client.get("/api/v1/drives").json()["drives"][0]
    detail = client.get("/api/v1/drives/drive").json()
    assert detail["drive"]["timeline"] == drive["timeline"] and detail["penalties"] == [event]
    assert client.get("/api/v1/drives/absent").status_code == 404
    for metric, value in [("production_epa", 0.2), ("drive_epa", 3.0)]:
        result = client.get(f"/api/v1/analysis?metric={metric}").json()
        assert result["metric"] == metric and result["points"][0]["metric_value"] == value
    assert client.get("/api/v1/analysis?metric=invalid").status_code == 422


def test_decision_api_models_clock_and_validates_input(client, comparison, scorer):
    state = comparison.before.model_dump(mode="json")
    payload = {
        "before": state,
        "reference_team": state["possession"],
        "actual_branch": "a",
        "branches": [
            {
                "id": "a",
                "label": "Runoff",
                "state": state,
                "clock": {"runoff_seconds": 10, "restart": "ready"},
                "assumptions": ["Explicit scenario"],
            },
            {
                "id": "b",
                "label": "No runoff",
                "state": state,
                "clock": {"restart": "snap"},
                "assumptions": ["Explicit scenario"],
            },
        ],
        "timing_delays": [0],
    }
    response = client.post("/api/v1/decisions", json=payload)
    assert response.status_code == 200
    assert response.json()["branches"][0]["clock_seconds_consumed"] == 10
    assert "outbox_id" not in response.json()
    payload["actual_branch"] = "missing"
    assert client.post("/api/v1/decisions", json=payload).status_code == 422


def test_automatic_nullified_result_filter_and_missing_values(client, snapshot):
    path, data = snapshot
    game = data["games"][0]
    data["penalties"] = [
        {
            "id": str(i),
            "game_id": game["game_id"],
            "season": 2024,
            "week": 1,
            "fouls": [{"disposition": "accepted"}],
            "nullified_play": {"status": status, "impact_epa": 1.4 if status == "valued" else None},
        }
        for i, status in enumerate(["valued", "not_recorded", "unpriced"])
    ]
    Database(path).save_catalog(data)
    result = client.get("/api/v1/penalties?nullified_status=not_recorded").json()
    assert result["total"] == 1 and result["penalties"][0]["nullified_play"]["impact_epa"] is None
    assert result["nullified_coverage"] == {"not_recorded": 1}
    assert client.get("/api/v1/penalties").json()["nullified_coverage"] == {
        "valued": 1,
        "not_recorded": 1,
        "unpriced": 1,
    }
    assert client.get("/api/v1/penalties?nullified_status=manual_review").status_code == 422


def test_game_progression_endpoint_keeps_probability_and_penalty_ledger_separate(client, snapshot):
    path, data = snapshot
    game_id = data["games"][0]["game_id"]
    evidence = {
        "game_id": game_id,
        "drives": [],
        "initial_home_wp": 0.5,
        "latest_home_wp": 0.65,
        "home_wp_change_from_start_pp": 15,
        "penalty_benefit": {"home_net_epa": 8.47},
        "timeline": [
            {"play_id": "100", "home_wp_before": 0.6, "home_wp_after": 0.65, "home_wp_change_pp": 5}
        ],
    }
    data["progression"] = {game_id: evidence}
    Database(path).save_catalog(data)
    assert client.get(f"/api/v1/games/{game_id}/progression").json()["progression"] == evidence
    assert client.get(f"/api/v1/games/{game_id}").json()["progression"] == evidence
    assert client.get("/api/v1/games/not-present/progression").status_code == 404
    future_id = data["games"][1]["game_id"]
    assert client.get(f"/api/v1/games/{future_id}/progression").json()["progression"] is None
    assert client.get("/assets/progression.js").status_code == 200
