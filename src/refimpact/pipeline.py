"""Explicit import, offline transformation and durable background-job lifecycle."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from refimpact.catalog import PBP_URL, SCHEDULE_URL, build_catalog
from refimpact.models import MODEL_ID, Scorer
from refimpact.storage import Database, now

# Only numerical/aggregation code affects transformation identity.
MATH_FILES = (
    "models.py",
    "schema.py",
    "replay.py",
    "penalties.py",
    "decisions.py",
    "production.py",
    "progression.py",
    "catalog.py",
    "analysis.py",
    "storage.py",
    "pipeline.py",
)
PIPELINE_VERSION = "sqlite-normalized-v1"


def code_digest():
    root = Path(__file__).parent
    return hashlib.sha256(b"".join((root / name).read_bytes() for name in MATH_FILES)).hexdigest()


def ingest_sources(
    database: Database,
    season: int | None,
    *,
    pbp_path: Path | None = None,
    schedule_path: Path | None = None,
    download=False,
    include_schedule=True,
    as_of: date | None = None,
):
    """Existing source heads win unless a path or explicit download is supplied."""

    def imported(kind, source_season, path, url):
        stored = database.source(kind, source_season)
        if stored and path is None and not download:
            return stored
        if path:
            content, origin = Path(path).read_bytes(), str(Path(path).resolve())
        else:
            response = httpx.get(url, timeout=90, follow_redirects=True)
            response.raise_for_status()
            content, origin = response.content, url
        frame = (
            pl.read_parquet(io.BytesIO(content))
            if kind == "pbp"
            else pl.read_csv(content, infer_schema_length=10000)
        )
        if kind == "pbp":
            if frame.is_empty() or set(frame["season"].unique().to_list()) != {source_season}:
                raise ValueError("Play-by-play source does not match requested season")
            if frame.select(pl.struct(["game_id", "play_id"]).n_unique()).item() != frame.height:
                raise ValueError("Duplicate game/play identities in source")
        return database.ingest(kind, source_season, frame, origin, content)

    schedule = imported("schedule", 0, schedule_path, SCHEDULE_URL) if include_schedule else None
    if season is None:
        frame = database.source_frame("schedule", 0)
        begun = frame.filter(pl.col("gameday") <= (as_of or date.today()).isoformat())
        if begun.is_empty():
            raise ValueError("No started games in stored schedule")
        season = begun["season"].max()
    pbp = imported("pbp", season, pbp_path, PBP_URL.format(season=season))
    return {"season": season, "play_by_play": pbp, "schedule": schedule}


def request_transform(database: Database, season: int, as_of: date):
    database.recover_abandoned()
    pbp, schedule = database.source("pbp", season), database.source("schedule", 0)
    if not pbp or not schedule:
        raise ValueError(
            "Source data is missing. Import it with refimpact "
            "refresh; this endpoint never downloads."
        )
    code = code_digest()
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                PIPELINE_VERSION,
                pbp["source_id"],
                schedule["source_id"],
                season,
                as_of.isoformat(),
                MODEL_ID,
                code,
            ]
        ).encode()
    ).hexdigest()
    run_id = fingerprint[:32]
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT run_id,status FROM runs WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if existing:
            # A completed identical request reuses the existing tables, with no writes.
            if existing["status"] != "failed":
                return database.run(existing["run_id"]), False
            try:
                db.execute(
                    "UPDATE runs SET "
                    "status='queued',error=NULL,finished_at=NULL,worker_pid="
                    "? WHERE run_id=?",
                    (os.getpid(), run_id),
                )
            except sqlite3.IntegrityError:
                raise RuntimeError(
                    "Another transformation is queued/running; retry after it finishes."
                ) from None
        else:
            try:
                db.execute(
                    """INSERT INTO runs(run_id,fingerprint,season,as_of,model_id,code_sha256,
                           pbp_source_id,schedule_source_id,status,created_at,worker_pid)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        fingerprint,
                        season,
                        as_of.isoformat(),
                        MODEL_ID,
                        code,
                        pbp["source_id"],
                        schedule["source_id"],
                        "queued",
                        now(),
                        os.getpid(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise RuntimeError(
                    "Another transformation is queued/running; retry after it finishes."
                ) from None
    return database.run(run_id), True


def execute_transform(database: Database, run_id: str, models: Path):
    run = database.run(run_id)
    if run["status"] == "completed":
        return run
    try:
        with database.connect() as db:
            # Claim once across threads/processes. Stale runs are failed at startup only explicitly.
            cursor = db.execute(
                "UPDATE runs SET status='running' WHERE run_id=? AND status='queued'", (run_id,)
            )
            if cursor.rowcount != 1:
                return database.run(run_id)
        frame = database.source_frame("pbp", run["season"], run["pbp_source_id"])
        schedule = database.source_frame("schedule", 0, run["schedule_source_id"])
        data = build_catalog(
            frame, schedule, Scorer(models), run["season"], date.fromisoformat(run["as_of"])
        )
        data["sources"] = {
            name: {
                "source_id": source["source_id"],
                "origin": source["origin"],
                "sha256": source["sha256"],
                "imported_at": source["imported_at"],
            }
            for name, source in [
                ("play_by_play", database.source("pbp", run["season"], run["pbp_source_id"])),
                ("schedule", database.source("schedule", 0, run["schedule_source_id"])),
            ]
        }
        database.save_catalog(data, run_id)
    except BaseException as exc:
        with database.connect() as db:
            db.execute(
                "UPDATE runs SET status='failed',finished_at=?,error=? WHERE run_id=?",
                (now(), str(exc), run_id),
            )
        raise
    return database.run(run_id)
