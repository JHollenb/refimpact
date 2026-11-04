"""Verified migration of local cache/report files into the content-addressed DB archive."""

from __future__ import annotations

import json
from pathlib import Path

from refimpact.pipeline import ingest_sources
from refimpact.storage import Database


def canonical(value):
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, dict):
        return {
            k: str(int(float(v))) if k == "play_id" and v is not None else canonical(v)
            for k, v in value.items()
            if v is not None
        }
    return value


def migrate(database: Database, root: Path, *, prune=False):
    root = root.resolve()
    imports = []
    for path in sorted((root / ".cache").glob("pbp-*.parquet")):
        season = int(path.stem.split("-")[-1])
        result = ingest_sources(
            database, season, pbp_path=path, schedule_path=root / ".cache/games.csv"
        )
        imports.append(result)
    catalog = root / ".cache/web/catalog.json"
    imported_run = None
    if catalog.exists():
        original = json.loads(catalog.read_text())
        pbp = database.source("pbp", original["default_season"])
        schedule = database.source("schedule", 0)
        if not pbp or not schedule:
            raise ValueError("Import the matching raw sources before migrating the catalog")
        imported_run = database.save_catalog(
            original, pbp_source_id=pbp["source_id"], schedule_source_id=schedule["source_id"]
        )
        with database.connect() as db:
            db.execute(
                "UPDATE legacy_review SET play_id=CAST(CAST(play_id AS "
                "INTEGER) AS TEXT) WHERE play_id IS NOT NULL"
            )
        restored = database.read(
            original["default_season"],
            all_weeks=True,
            sections=("calls", "review", "penalties", "drives", "progression"),
        )
        for key in ("games", "calls", "review", "penalties", "drives", "progression"):
            expected, actual = (
                original.get(key, {} if key == "progression" else []),
                restored.get(key),
            )
            # Row ordering is a query concern; business contents must survive exactly.
            if isinstance(expected, list):
                expected = sorted(expected, key=lambda r: json.dumps(canonical(r), sort_keys=True))
                actual = sorted(actual, key=lambda r: json.dumps(canonical(r), sort_keys=True))
            if canonical(expected) != canonical(actual):
                raise ValueError(f"Migration round-trip mismatch in {key}; source files preserved")
    candidates = sorted(
        p
        for p in (root / "reports").rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and p.name != ".gitkeep"
        and p.suffix.lower() not in {".jpg", ".png", ".webp"}
    )
    # Keep the latest standalone arithmetic audit; older evidence remains restorable.
    audits = sorted((root / "reports").glob("penalty-math-audit-*.json"))
    if audits:
        candidates = [p for p in candidates if p != audits[-1]]
    if imported_run:
        candidates += [
            catalog,
            root / ".cache/games.csv",
            *sorted((root / ".cache").glob("pbp-*.parquet")),
        ]
    archived, removed, bytes_removed = 0, 0, 0
    for path in candidates:
        if not path.exists():
            continue
        database.archive_file(path)  # Commits and verifies the compressed copy before unlinking.
        archived += 1
        if prune:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            bytes_removed += size
    if prune:
        for directory in sorted(
            (root / "reports").rglob("*"), key=lambda p: len(p.parts), reverse=True
        ):
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()
    with database.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    return {
        "database": str(database.path),
        "imported_run": imported_run,
        "raw_imports": len(imports),
        "archived_files": archived,
        "removed_files": removed,
        "removed_bytes": bytes_removed,
        "integrity_check": integrity,
    }
