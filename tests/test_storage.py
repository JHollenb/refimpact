"""Persistence, lineage, offline/idempotent generation and atomic publication."""

import copy
import io
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from refimpact.api import create_app
from refimpact.pipeline import execute_transform, ingest_sources, request_transform
from refimpact.storage import Database


@pytest.fixture
def stored(tmp_path):
    db = Database(tmp_path / "data.sqlite")
    gid = "2024_01_ARI_BUF"
    frame = pl.DataFrame(
        {
            "game_id": [gid, gid],
            "play_id": [1.0, 2.0],
            "season": [2024, 2024],
            "week": [1, 1],
            "desc": ["flag", "next down"],
            "epa": [-0.5, 0.2],
            "game_date": ["2024-09-08"] * 2,
        }
    )
    content = io.BytesIO()
    frame.write_parquet(content)
    pbp = db.ingest("pbp", 2024, frame, "fixture", content.getvalue())
    schedule_frame = pl.DataFrame(
        {"game_id": [gid], "season": [2024], "week": [1], "gameday": ["2024-09-08"]}
    )
    schedule = db.ingest(
        "schedule", 0, schedule_frame, "fixture", schedule_frame.write_csv().encode()
    )
    game = {
        "game_id": gid,
        "season": 2024,
        "week": 1,
        "gameday": "2024-09-08",
        "gametime": "13:00",
        "home_team": "BUF",
        "away_team": "ARI",
        "home_score": 20,
        "away_score": 17,
        "status": "final",
        "pbp_complete": True,
        "scored_calls": 0,
        "penalty_rows": 1,
        "review_calls": 0,
        "valued_penalties": 1,
        "net_home_epa": -0.5,
        "production": {},
        "penalty_coverage": {},
    }
    event = {
        "id": gid + ":1",
        "game_id": gid,
        "play_id": "1",
        "season": 2024,
        "week": 1,
        "drive_id": None,
        "fouls": [{"team": "BUF", "type": "False Start", "disposition": "accepted"}],
        "decision": None,
        "home_epa_impact": -0.5,
        "enforcement_epa": -0.5,
    }
    data = {
        "generated_at": "2024-09-09T00:00:00Z",
        "as_of": "2024-09-09",
        "default_season": 2024,
        "default_week": 1,
        "model_id": "fixture",
        "mode": "postgame",
        "sources": {},
        "games": [game],
        "weeks": [{"season": 2024, "week": 1}],
        "calls": [],
        "review": [],
        "penalties": [event],
        "drives": [],
        "progression": {},
    }
    return db, frame, pbp, schedule, data


def test_source_import_is_deduplicated_typed_and_offline(stored, monkeypatch):
    db, frame, pbp, _, _ = stored
    assert db.source_frame("pbp", 2024).equals(frame)

    def forbidden(*args, **kwargs):
        pytest.fail("Cached refresh attempted a network request")

    monkeypatch.setattr("httpx.get", forbidden)
    sources = ingest_sources(db, 2024)
    assert sources["play_by_play"]["source_id"] == pbp["source_id"]
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM raw_plays").fetchone()[0] == 2
        assert c.execute("SELECT play_id FROM raw_plays ORDER BY play_id").fetchone()[0] == "1"


def test_source_revisions_keep_previous_rows_and_lineage(stored):
    db, frame, pbp, _, _ = stored
    revised = frame.with_columns(pl.lit(0.9).alias("epa"))
    content = io.BytesIO()
    revised.write_parquet(content)
    other = db.ingest("pbp", 2024, revised, "revision", content.getvalue())
    assert other["source_id"] != pbp["source_id"]
    assert db.source_frame("pbp", 2024, pbp["source_id"]).equals(frame)
    assert db.source_frame("pbp", 2024).equals(revised)


def test_offline_job_is_idempotent_and_pins_sources(stored, monkeypatch):
    db, _, pbp, schedule, data = stored
    monkeypatch.setattr("refimpact.pipeline.Scorer", lambda *_: None)
    monkeypatch.setattr("refimpact.pipeline.build_catalog", lambda *args: copy.deepcopy(data))
    monkeypatch.setattr(
        "httpx.get", lambda *args, **kwargs: pytest.fail("Transformation used network")
    )
    run, new = request_transform(db, 2024, date(2024, 9, 9))
    same, created = request_transform(db, 2024, date(2024, 9, 9))
    assert new and not created and same["run_id"] == run["run_id"]
    with pytest.raises(RuntimeError, match="Another"):
        request_transform(db, 2024, date(2024, 9, 10))
    completed = execute_transform(db, run["run_id"], Path("unused"))
    assert completed["status"] == "completed"
    assert completed["pbp_source_id"] == pbp["source_id"]
    assert completed["schedule_source_id"] == schedule["source_id"]
    again, created = request_transform(db, 2024, date(2024, 9, 9))
    assert not created and again["status"] == "completed"
    assert db.read()["games"][0]["net_home_epa"] == -0.5
    with db.connect() as c:
        joined = c.execute("""SELECT raw.epa,p.enforcement_epa FROM penalties p
            JOIN runs r USING(run_id) JOIN raw_plays raw ON raw.source_id=r.pbp_source_id
            AND raw.game_id=p.game_id AND raw.play_id=p.play_id""").fetchall()
        assert tuple(joined[0]) == (-0.5, -0.5)
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_publish_preserves_previous_run_and_rolls_back_rows(stored, monkeypatch):
    db, _, _, _, data = stored
    previous = db.save_catalog(data)
    monkeypatch.setattr("refimpact.pipeline.Scorer", lambda *_: None)
    monkeypatch.setattr("refimpact.pipeline.build_catalog", lambda *args: copy.deepcopy(data))
    run, _ = request_transform(db, 2024, date(2024, 9, 10))
    insert = db._insert

    def broken(connection, table, records):
        if table == "penalties":
            raise ValueError("injected write failure")
        return insert(connection, table, records)

    monkeypatch.setattr(db, "_insert", broken)
    with pytest.raises(ValueError, match="injected"):
        execute_transform(db, run["run_id"], Path("unused"))
    assert db.run(run["run_id"])["status"] == "failed"
    assert db.read()["run_id"] == previous
    with db.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM games WHERE run_id=?", (run["run_id"],)).fetchone()[0]
            == 0
        )


def test_transform_api_persists_tables_and_does_not_fetch(stored, monkeypatch):
    db, _, _, _, data = stored
    monkeypatch.setattr("refimpact.pipeline.Scorer", lambda *_: None)
    monkeypatch.setattr("refimpact.pipeline.build_catalog", lambda *args: copy.deepcopy(data))
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: pytest.fail("API attempted download"))
    with TestClient(create_app(db.path)) as client:
        result = client.post("/api/v1/transforms", json={"season": 2024, "as_of": "2024-09-09"})
        assert result.status_code == 202
        rid = result.json()["run_id"]
        assert client.get(f"/api/v1/transforms/{rid}").json()["status"] == "completed"
        assert (
            client.post(
                "/api/v1/transforms", json={"season": 2024, "as_of": "2024-09-09"}
            ).status_code
            == 200
        )
        rows = client.get("/api/v1/data/raw_plays?game_id=2024_01_ARI_BUF").json()["rows"]
        assert len(rows) == 2 and rows[0]["play_id"] == "1"
        assert client.get("/api/v1/data/schema").json()["tables"]["raw_plays"]["indexes"]
        assert client.get("/api/v1/data/artifacts").status_code == 404
        assert client.get("/api/v1/data/raw_plays?limit=1001").status_code == 422
        assert client.post("/api/v1/transforms", json={"season": 1900}).status_code == 422
        assert client.post("/api/v1/transforms", json={"season": 2025}).status_code == 422
        assert client.get("/api/v1/transforms/missing").status_code == 404
        response = client.get("/api/v1/games")
        assert (
            client.get(
                "/api/v1/games", headers={"If-None-Match": response.headers["etag"]}
            ).status_code
            == 304
        )
        assert "EPA_T" in client.get("/api/v1/math").text


def test_archive_verifies_content_and_deduplicates(stored, tmp_path):
    import zlib

    db, *_ = stored
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text('{"evidence":1}')
    b.write_bytes(a.read_bytes())
    sha = db.archive_file(a)
    assert db.archive_file(b) == sha
    with db.connect() as c:
        row = c.execute("SELECT compressed FROM artifacts WHERE sha256=?", (sha,)).fetchone()
        assert zlib.decompress(row[0]) == a.read_bytes()
        assert c.execute("SELECT count(*) FROM archived_files").fetchone()[0] == 2


def test_schema_evolution_from_null_keeps_numeric_types(stored):
    db, *_, data = stored
    data["games"][0]["new_metric"] = None
    db.save_catalog(data)
    updated = copy.deepcopy(data)
    updated["generated_at"] = "later"
    updated["games"][0]["new_metric"] = 1.25
    db.save_catalog(updated)
    assert db.read()["games"][0]["new_metric"] == 1.25


def test_stale_worker_can_be_retried_without_expiring_live_jobs(stored, monkeypatch):
    import os

    db, *_ = stored
    run, _ = request_transform(db, 2024, date(2024, 9, 9))
    db.recover_abandoned()
    assert db.run(run["run_id"])["status"] == "queued"
    with db.connect() as c:
        c.execute("UPDATE runs SET worker_pid=? WHERE run_id=?", (99999999, run["run_id"]))
    real_kill = os.kill

    def dead(pid, sig):
        if pid == 99999999:
            raise ProcessLookupError()
        return real_kill(pid, sig)

    monkeypatch.setattr("refimpact.storage.os.kill", dead)
    db.recover_abandoned()
    assert db.run(run["run_id"])["status"] == "failed"
    same, created = request_transform(db, 2024, date(2024, 9, 9))
    assert created and same["run_id"] == run["run_id"] and same["status"] == "queued"


def test_statistics_are_materialized_and_revision_cache_invalidates(stored, monkeypatch):
    db, *_, data = stored
    db.save_catalog(data)

    def forbidden(*args, **kwargs):
        pytest.fail("Page read recomputed bootstrap statistics")

    monkeypatch.setattr("refimpact.analysis.relationships", forbidden)
    with TestClient(create_app(db.path)) as client:
        response = client.get("/api/v1/analysis?season=2024")
        assert response.status_code == 200 and response.json()["games"] == 1
        previous = client.get("/api/v1/games")
        monkeypatch.undo()
        updated = copy.deepcopy(data)
        updated["generated_at"] = "new"
        updated["games"][0]["home_score"] = 21
        db.save_catalog(updated)
        result = client.get("/api/v1/games", headers={"If-None-Match": previous.headers["etag"]})
        assert result.status_code == 200 and result.json()["games"][0]["home_score"] == 21
        assert result.headers["etag"] != previous.headers["etag"]
