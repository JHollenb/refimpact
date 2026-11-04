"""SQLite source-of-truth: immutable imports/runs, normalized results, indexed reads."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

DEFAULT_DATABASE = Path(".cache/refimpact.sqlite")
RESULT_TABLES = {
    "games": ("run_id", "game_id"),
    "team_production": ("run_id", "game_id", "team"),
    "play_metrics": ("run_id", "game_id", "play_id"),
    "game_progression": ("run_id", "game_id"),
    "drives": ("run_id", "id"),
    "drive_plays": ("run_id", "drive_id", "position"),
    "penalties": ("run_id", "id"),
    "penalty_fouls": ("run_id", "penalty_id", "position"),
    "penalty_decisions": ("run_id", "penalty_id"),
    "penalty_branches": ("run_id", "penalty_id", "id"),
    "penalty_timing": ("run_id", "penalty_id", "position"),
    "penalty_timing_comparisons": ("run_id", "penalty_id", "position"),
    "legacy_calls": ("run_id", "id"),
    "legacy_review": ("run_id", "position"),
    "analysis_results": ("run_id", "scope_week", "metric"),
    "analysis_points": ("run_id", "scope_week", "metric", "game_id"),
}
DATA_TABLES = ("raw_plays", "raw_games", *RESULT_TABLES, "calculations")


def now():
    return datetime.now(UTC).isoformat()


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def encode(value):
    return json.dumps(clean(value), separators=(",", ":"), allow_nan=False)


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


class Database:
    def __init__(self, path: Path = DEFAULT_DATABASE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calculations(
                    calculation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, game_id TEXT, play_id TEXT,
                    model_id TEXT NOT NULL, code_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    request_json TEXT NOT NULL, result_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS calculations_game ON calculations(game_id,play_id);
                CREATE TABLE IF NOT EXISTS artifacts(
                    sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, compressed BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS archived_files(
                    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
                    archived_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                    source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, season INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, origin TEXT NOT NULL, imported_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL, schema_json TEXT NOT NULL,
                    artifact_sha256 TEXT REFERENCES artifacts(sha256), UNIQUE(kind,season,sha256));
                CREATE TABLE IF NOT EXISTS source_heads(
                    kind TEXT, season INTEGER,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    checked_at TEXT NOT NULL, PRIMARY KEY(kind,season));
                CREATE TABLE IF NOT EXISTS raw_plays(
                    source_id TEXT REFERENCES sources(source_id), game_id TEXT, play_id TEXT,
                    PRIMARY KEY(source_id,game_id,play_id));
                CREATE TABLE IF NOT EXISTS raw_games(
                    source_id TEXT REFERENCES sources(source_id), game_id TEXT,
                    PRIMARY KEY(source_id,game_id));
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                    season INTEGER NOT NULL, as_of TEXT NOT NULL, model_id TEXT,
                    code_sha256 TEXT, pbp_source_id TEXT REFERENCES sources(source_id),
                    schedule_source_id TEXT REFERENCES sources(source_id),
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
                    created_at TEXT NOT NULL, finished_at TEXT, error TEXT, metadata TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_generation_writer ON runs((1))
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS active_seasons(
                    season INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id));
                CREATE TABLE IF NOT EXISTS boolean_columns(
                    table_name TEXT, column_name TEXT, PRIMARY KEY(table_name,column_name));
                CREATE TABLE IF NOT EXISTS json_columns(
                    table_name TEXT, column_name TEXT, PRIMARY KEY(table_name,column_name));
            """)
            for table, keys in RESULT_TABLES.items():
                fields = [
                    f"{quoted(k)} {'INTEGER' if k in {'position', 'scope_week'} else 'TEXT'}"
                    for k in keys
                ]
                common = {
                    "games": {
                        "season": "INTEGER",
                        "week": "INTEGER",
                        "gameday": "TEXT",
                        "gametime": "TEXT",
                    },
                    "play_metrics": {"position": "INTEGER"},
                    "drives": {"game_id": "TEXT", "season": "INTEGER", "week": "INTEGER"},
                    "drive_plays": {"game_id": "TEXT", "play_id": "TEXT"},
                    "penalties": {
                        "game_id": "TEXT",
                        "season": "INTEGER",
                        "week": "INTEGER",
                        "drive_id": "TEXT",
                        "position": "INTEGER",
                    },
                    "penalty_branches": {"position": "INTEGER"},
                    "legacy_calls": {
                        "game_id": "TEXT",
                        "play_id": "TEXT",
                        "season": "INTEGER",
                        "week": "INTEGER",
                    },
                    "legacy_review": {"game_id": "TEXT", "play_id": "TEXT"},
                }.get(table, {})
                fields += [f"{quoted(k)} {t}" for k, t in common.items() if k not in keys]
                if table != "games" and ("game_id" in keys or "game_id" in common):
                    fields += ["FOREIGN KEY(run_id,game_id) REFERENCES games(run_id,game_id)"]
                if "penalty_id" in keys:
                    fields += ["FOREIGN KEY(run_id,penalty_id) REFERENCES penalties(run_id,id)"]
                if table == "drive_plays":
                    fields += ["FOREIGN KEY(run_id,drive_id) REFERENCES drives(run_id,id)"]
                # Every result is tied to one run; game/penalty joins retain explicit stable keys.
                fields += [
                    "PRIMARY KEY(" + ",".join(map(quoted, keys)) + ")",
                    "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE",
                ]
                db.execute(f"CREATE TABLE IF NOT EXISTS {quoted(table)}({','.join(fields)})")
            if "worker_pid" not in {r["name"] for r in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN worker_pid INTEGER")
            # Upgrade prototype JSON booleans to SQL-friendly 0/1 without changing meaning.
            for item in list(db.execute("SELECT table_name,column_name FROM json_columns")):
                table, column = item
                sample = db.execute(
                    f"SELECT {quoted(column)} FROM {quoted(table)} "
                    f"WHERE {quoted(column)} IS NOT NULL LIMIT 1"
                ).fetchone()
                if sample and sample[0] in ("true", "false"):
                    db.execute(
                        f"UPDATE {quoted(table)} SET {quoted(column)}=CASE {quoted(column)} "
                        f"WHEN 'true' THEN 1 WHEN 'false' THEN 0 ELSE {quoted(column)} END"
                    )
                    db.execute("INSERT OR IGNORE INTO boolean_columns VALUES(?,?)", (table, column))
                    db.execute(
                        "DELETE FROM json_columns WHERE table_name=? AND column_name=?",
                        (table, column),
                    )
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _insert(self, db, table, records):
        if not records:
            return
        records = [clean(r) for r in records]
        info = list(db.execute(f"PRAGMA table_info({quoted(table)})"))
        existing = {r["name"] for r in info}
        keys = [r["name"] for r in info if r["pk"]]
        if any(any(row.get(k) is None for k in keys) for row in records):
            raise ValueError(f"Missing primary key in {table}")
        columns = list(dict.fromkeys(k for row in records for k in row))
        nested = set()
        for key in columns:
            values = [row.get(key) for row in records if row.get(key) is not None]
            if any(isinstance(v, bool) for v in values):
                db.execute("INSERT OR IGNORE INTO boolean_columns VALUES(?,?)", (table, key))
            if any(isinstance(v, (list, dict)) for v in values):
                nested.add(key)
                db.execute("INSERT OR IGNORE INTO json_columns VALUES(?,?)", (table, key))
            if key not in existing:
                kind = (
                    "TEXT"
                    if key in nested
                    else "BLOB"
                    if not values
                    else "INTEGER"
                    if values and all(isinstance(v, (bool, int)) for v in values)
                    else "REAL"
                    if values and all(isinstance(v, (int, float)) for v in values)
                    else "TEXT"
                )
                db.execute(f"ALTER TABLE {quoted(table)} ADD COLUMN {quoted(key)} {kind}")
        nested |= {
            r[0]
            for r in db.execute("SELECT column_name FROM json_columns WHERE table_name=?", (table,))
        }
        sql = (
            f"INSERT INTO {quoted(table)}({','.join(map(quoted, columns))}) "
            f"VALUES({','.join('?' for _ in columns)})"
        )
        db.executemany(
            sql,
            [
                tuple(
                    encode(row.get(k)) if k in nested and row.get(k) is not None else row.get(k)
                    for k in columns
                )
                for row in records
            ],
        )

    def _rows(self, db, table, where="", args=(), *, order="", limit=None, drop=()):
        nested = {
            r[0]
            for r in db.execute("SELECT column_name FROM json_columns WHERE table_name=?", (table,))
        }
        booleans = {
            r[0]
            for r in db.execute(
                "SELECT column_name FROM boolean_columns WHERE table_name=?", (table,)
            )
        }
        rows = db.execute(
            f"SELECT * FROM {quoted(table)}"
            + (" WHERE " + where if where else "")
            + (" ORDER BY " + order if order else "")
            + (" LIMIT ? OFFSET ?" if limit else ""),
            (*args, *limit) if limit else args,
        )
        return [
            {
                k: bool(int(v))
                if k in booleans and v is not None
                else json.loads(v)
                if k in nested and v is not None
                else v
                for k, v in dict(row).items()
                if k not in drop
            }
            for row in rows
        ]

    def put_artifact(self, content: bytes, db=None):
        digest = hashlib.sha256(content).hexdigest()
        if db is None:
            with self.connect() as connection:
                return self.put_artifact(content, connection)
        db.execute(
            "INSERT OR IGNORE INTO artifacts VALUES(?,?,?)",
            (digest, len(content), zlib.compress(content, 9)),
        )
        return digest

    def archive_file(self, path: Path):
        """Verified DB copy, suitable for removing a redundant generated/cache file."""
        content = path.read_bytes()
        with self.connect() as db:
            digest = self.put_artifact(content, db)
            restored = zlib.decompress(
                db.execute("SELECT compressed FROM artifacts WHERE sha256=?", (digest,)).fetchone()[
                    0
                ]
            )
            if restored != content:
                raise ValueError(f"Archive verification failed: {path}")
            db.execute(
                "INSERT OR REPLACE INTO archived_files VALUES(?,?,?)",
                (str(path.resolve()), digest, now()),
            )
        return digest

    def source(self, kind, season, source_id=None):
        with self.connect() as db:
            row = (
                db.execute(
                    "SELECT s.* FROM sources s JOIN source_heads h "
                    "USING(source_id) WHERE h.kind=? AND h.season=?",
                    (kind, season),
                ).fetchone()
                if source_id is None
                else db.execute(
                    "SELECT * FROM sources WHERE source_id=? AND kind=? AND season=?",
                    (source_id, kind, season),
                ).fetchone()
            )
        return dict(row) if row else None

    def ingest(self, kind, season, frame: pl.DataFrame, origin: str, content: bytes):
        digest = hashlib.sha256(content).hexdigest()
        source_id = hashlib.sha256(f"{kind}:{season}:{digest}".encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not found:
                artifact = self.put_artifact(content, db)
                db.execute(
                    "INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        kind,
                        season,
                        digest,
                        origin,
                        now(),
                        frame.height,
                        encode({n: str(t) for n, t in frame.schema.items()}),
                        artifact,
                    ),
                )
                records = frame.to_dicts()
                for row in records:
                    row["source_id"] = source_id
                    if kind == "pbp":
                        row["play_id"] = str(int(row["play_id"]))
                self._insert(db, "raw_plays" if kind == "pbp" else "raw_games", records)
            db.execute(
                "INSERT OR REPLACE INTO source_heads VALUES(?,?,?,?)",
                (kind, season, source_id, now()),
            )
        return self.source(kind, season, source_id)

    def source_frame(self, kind, season, source_id=None):
        source = self.source(kind, season, source_id)
        if not source:
            raise ValueError(
                f"No stored {kind} source for {season}; import it with refimpact refresh"
            )
        schema = json.loads(source["schema_json"])
        with self.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    f"SELECT {','.join(map(quoted, schema))} "
                    f"FROM {'raw_plays' if kind == 'pbp' else 'raw_games'} "
                    "WHERE source_id=? ORDER BY rowid",
                    (source["source_id"],),
                )
            ]
        types = {name: getattr(pl, dtype) for name, dtype in schema.items()}
        return pl.DataFrame(rows, infer_schema_length=None).cast(types)

    def recover_abandoned(self):
        """Fail pending jobs only when their local owner process is known to be gone."""
        with self.connect() as db:
            for row in db.execute(
                "SELECT run_id,worker_pid FROM runs WHERE status IN ('queued','running')"
            ):
                if not row["worker_pid"]:
                    continue
                try:
                    os.kill(row["worker_pid"], 0)
                except ProcessLookupError:
                    db.execute(
                        "UPDATE runs SET status='failed',finished_at=?,error=? WHERE run_id=?",
                        (
                            now(),
                            "Worker process exited before publication; request this "
                            "transformation again.",
                            row["run_id"],
                        ),
                    )
                except PermissionError:
                    pass

    def run(self, run_id):
        with self.connect() as db:
            r = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not r:
            return None
        result = dict(r)
        result["metadata"] = json.loads(result["metadata"]) if result["metadata"] else None
        return result

    def save_catalog(self, data, run_id=None, *, pbp_source_id=None, schedule_source_id=None):
        """Publish all result tables and the active pointer in one transaction."""
        from refimpact.analysis import relationships

        run_id = run_id or hashlib.sha256(encode(data).encode()).hexdigest()[:32]
        records = {table: [] for table in RESULT_TABLES}

        def add(table, value):
            records[table].append({"run_id": run_id, **value})

        for game in data["games"]:
            g = dict(game)
            for team, metrics in g.pop("production", {}).items():
                add("team_production", {"game_id": g["game_id"], **metrics, "team": team})
            add("games", g)
        for game_id, source in data.get("progression", {}).items():
            p = dict(source)
            for position, point in enumerate(p.pop("timeline", [])):
                point = {k: v for k, v in point.items() if k != "description"}
                add("play_metrics", {"game_id": game_id, "position": position, **point})
            p.pop("drives", None)  # Projected from the drives table on read.
            add("game_progression", {"game_id": game_id, **p})
        for source in data.get("drives", []):
            d = dict(source)
            for position, point in enumerate(d.pop("timeline", [])):
                point = {k: v for k, v in point.items() if k != "description"}
                add(
                    "drive_plays",
                    {"game_id": d["game_id"], "drive_id": d["id"], "position": position, **point},
                )
            add("drives", d)
        for position, source in enumerate(data.get("penalties", [])):
            e = dict(source)
            for i, f in enumerate(e.pop("fouls", [])):
                add("penalty_fouls", {"penalty_id": e["id"], "position": i, **f})
            decision = e.pop("decision", None)
            if decision:
                decision = dict(decision)
                for key, table in [
                    ("branches", "penalty_branches"),
                    ("timing_scenarios", "penalty_timing"),
                    ("timing_comparisons", "penalty_timing_comparisons"),
                ]:
                    for i, b in enumerate(decision.pop(key, [])):
                        add(table, {"penalty_id": e["id"], "position": i, **b})
                add("penalty_decisions", {"penalty_id": e["id"], **decision})
            add("penalties", {"position": position, **e})
        for c in data.get("calls", []):
            add(
                "legacy_calls",
                {"game_id": c["comparison"]["game_id"], "play_id": c["comparison"]["play_id"], **c},
            )
        for i, r in enumerate(data.get("review", [])):
            r = dict(r)
            if r.get("play_id") is not None:
                r["play_id"] = str(int(float(r["play_id"])))
            add("legacy_review", {"position": i, **r})
        for scope in [None, *sorted({g["week"] for g in data["games"]})]:
            games = [g for g in data["games"] if scope is None or g["week"] == scope]
            for metric in ("penalty_epa", "production_epa", "drive_epa"):
                result = relationships(games, metric)
                for point in result.pop("points"):
                    add("analysis_points", {"scope_week": scope or 0, "metric": metric, **point})
                add("analysis_results", {"scope_week": scope or 0, **result})
        metadata = {
            k: v
            for k, v in data.items()
            if k not in {"games", "calls", "review", "penalties", "drives", "progression"}
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing and existing[0] == "completed":
                return run_id
            if not existing:
                db.execute(
                    "INSERT INTO "
                    "runs(run_id,fingerprint,season,as_of,model_id,status,cr"
                    "eated_at,pbp_source_id,schedule_source_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        run_id,
                        data["default_season"],
                        data["as_of"],
                        data["model_id"],
                        "running",
                        now(),
                        pbp_source_id,
                        schedule_source_id,
                    ),
                )
            for table, values in records.items():
                self._insert(db, table, values)
                columns = {r["name"] for r in db.execute(f"PRAGMA table_info({quoted(table)})")}
                for name, keys in [
                    ("game", ["run_id", "game_id"]),
                    ("scope", ["run_id", "season", "week"]),
                    ("penalty", ["run_id", "penalty_id"]),
                    ("drive", ["run_id", "drive_id"]),
                ]:
                    if set(keys) <= columns:
                        db.execute(
                            f"CREATE INDEX IF NOT EXISTS {table}_{name} "
                            f"ON {quoted(table)}({','.join(map(quoted, keys))})"
                        )
            db.execute(
                "UPDATE runs SET "
                "status='completed',finished_at=?,metadata=?,error=NULL "
                "WHERE run_id=?",
                (now(), encode(metadata), run_id),
            )
            db.execute(
                "INSERT OR REPLACE INTO active_seasons VALUES(?,?)",
                (data["default_season"], run_id),
            )
        return run_id

    def _active(self, db, season=None):
        row = db.execute(
            "SELECT r.* FROM active_seasons a JOIN runs r USING(run_id)"
            + (
                " WHERE a.season=?"
                if season is not None
                else " ORDER BY r.finished_at DESC LIMIT 1"
            ),
            (season,) if season is not None else (),
        ).fetchone()
        if not row:
            raise LookupError("No transformed data for this season. Run refimpact refresh first.")
        return dict(row)

    def read(
        self,
        season=None,
        week=None,
        *,
        all_weeks=False,
        sections=(),
        game_id=None,
        drive_id=None,
        penalty_id=None,
        detail=True,
    ):
        """One consistent read transaction; never load unrelated seasons or games."""
        with self.connect() as db:
            db.execute("BEGIN")
            if game_id is None:
                candidate = penalty_id or drive_id
                if candidate and ":" in candidate:
                    game_id = candidate.split(":", 1)[0]
            run = self._active(db, season)
            rid = run["run_id"]
            data = json.loads(run["metadata"])
            data["run_id"] = rid
            season = season if season is not None else data["default_season"]
            week = week if week is not None else (None if all_weeks else data["default_week"])

            def rows(table, extra="", args=(), order=""):
                return self._rows(
                    db,
                    table,
                    "run_id=?" + (" AND " + extra if extra else ""),
                    (rid, *args),
                    order=order,
                    drop=("run_id",),
                )

            games = rows(
                "games",
                "season=?"
                + (" AND week=?" if week is not None else "")
                + (" AND game_id=?" if game_id else ""),
                (season, *([week] if week is not None else []), *([game_id] if game_id else [])),
                "gameday,gametime,game_id",
            )
            ids = [g["game_id"] for g in games]
            clause = "game_id IN (" + ",".join("?" for _ in ids) + ")" if ids else "0"
            production = rows("team_production", clause, ids)
            for g in games:
                g["production"] = {
                    p["team"]: {k: v for k, v in p.items() if k != "game_id"}
                    for p in production
                    if p["game_id"] == g["game_id"]
                }
            data["games"] = games
            for section, table in [
                ("calls", "legacy_calls"),
                ("review", "legacy_review"),
                ("drives", "drives"),
                ("penalties", "penalties"),
            ]:
                if section not in sections:
                    continue
                extra, args = clause, list(ids)
                if drive_id:
                    extra += " AND " + ("id" if section == "drives" else "drive_id") + "=?"
                    args.append(drive_id)
                if penalty_id and section == "penalties":
                    extra += " AND id=?"
                    args.append(penalty_id)
                values = rows(table, extra, args)
                if section == "calls":
                    values = [
                        {k: v for k, v in c.items() if k not in {"game_id", "play_id"}}
                        for c in values
                    ]
                if section == "review":
                    values = [{k: v for k, v in c.items() if k != "position"} for c in values]
                if section == "penalties":
                    values.sort(key=lambda e: e.pop("position"))
                    for e in values:
                        e["fouls"] = [
                            {k: v for k, v in f.items() if k not in {"penalty_id", "position"}}
                            for f in rows("penalty_fouls", "penalty_id=?", (e["id"],), "position")
                        ]
                        e["decision"] = None
                        if detail:
                            decisions = rows("penalty_decisions", "penalty_id=?", (e["id"],))
                            if decisions:
                                e["decision"] = {
                                    k: v for k, v in decisions[0].items() if k != "penalty_id"
                                }
                                for name, t in [
                                    ("branches", "penalty_branches"),
                                    ("timing_scenarios", "penalty_timing"),
                                    ("timing_comparisons", "penalty_timing_comparisons"),
                                ]:
                                    e["decision"][name] = [
                                        {
                                            k: v
                                            for k, v in b.items()
                                            if k not in {"penalty_id", "position"}
                                        }
                                        for b in rows(t, "penalty_id=?", (e["id"],), "position")
                                    ]
                if section == "drives" and detail:
                    for d in values:
                        d["timeline"] = [
                            {
                                k: v
                                for k, v in p.items()
                                if k not in {"game_id", "drive_id", "position"}
                            }
                            for p in rows("drive_plays", "drive_id=?", (d["id"],), "position")
                        ]
                data[section] = values
            if "progression" in sections:
                progressions = rows("game_progression", clause, ids)
                points = rows("play_metrics", clause, ids, "position")
                drives = rows("drives", clause, ids)
                data["progression"] = {}
                for p in progressions:
                    p["timeline"] = [
                        {k: v for k, v in point.items() if k not in {"game_id", "position"}}
                        for point in points
                        if point["game_id"] == p["game_id"]
                    ]
                    p["drives"] = [
                        {
                            k: d.get(k)
                            for k in [
                                "id",
                                "drive",
                                "team",
                                "drive_epa",
                                "cumulative_team_drive_epa",
                                "wp_before",
                                "wp_after",
                                "wp_change_pp",
                                "missing_epa_rows",
                                "end_play_id",
                            ]
                        }
                        for d in drives
                        if d["game_id"] == p["game_id"]
                    ]
                    data["progression"][p["game_id"]] = p
            # Descriptions have one canonical copy in raw_plays. Rehydrate only requested games.
            if (
                ("progression" in sections or ("drives" in sections and detail))
                and run["pbp_source_id"]
                and ids
            ):
                descriptions = {
                    (r["game_id"], r["play_id"]): r["desc"]
                    for r in db.execute(
                        "SELECT game_id,play_id,desc FROM raw_plays "
                        f"WHERE source_id=? AND {clause}",
                        (run["pbp_source_id"], *ids),
                    )
                }
                for gid, p in data.get("progression", {}).items():
                    for point in p["timeline"]:
                        point["description"] = descriptions.get((gid, point["play_id"]))
                for d in data.get("drives", []):
                    for point in d.get("timeline", []):
                        point["description"] = descriptions.get((d["game_id"], point["play_id"]))
            return data

    def analysis(self, season, week, metric):
        with self.connect() as db:
            run = self._active(db, season)
            args = (run["run_id"], week or 0, metric)
            rows = self._rows(
                db,
                "analysis_results",
                "run_id=? AND scope_week=? AND metric=?",
                args,
                drop=("run_id", "scope_week"),
            )
            if not rows:
                raise LookupError("No analysis for this scope")
            result = rows[0]
            result["points"] = self._rows(
                db,
                "analysis_points",
                "run_id=? AND scope_week=? AND metric=?",
                args,
                drop=("run_id", "scope_week", "metric"),
            )
            return result

    def record_calculation(self, kind, request, result, model_id, code):
        identity = json.dumps([kind, request, model_id, code], sort_keys=True, allow_nan=False)
        cid = hashlib.sha256(identity.encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO calculations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    kind,
                    request.get("game_id"),
                    request.get("play_id"),
                    model_id,
                    code,
                    now(),
                    encode(request),
                    encode(result),
                ),
            )
        return cid

    def calculation(self, calculation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM calculations WHERE calculation_id=?", (calculation_id,)
            ).fetchone()
        if not row:
            return None
        row = dict(row)
        row["request"] = json.loads(row.pop("request_json"))
        row["result"] = json.loads(row.pop("result_json"))
        return row

    def schema(self):
        with self.connect() as db:
            return {
                table: {
                    "columns": [dict(r) for r in db.execute(f"PRAGMA table_info({quoted(table)})")],
                    "foreign_keys": [
                        dict(r) for r in db.execute(f"PRAGMA foreign_key_list({quoted(table)})")
                    ],
                    "indexes": [dict(r) for r in db.execute(f"PRAGMA index_list({quoted(table)})")],
                }
                for table in ("sources", "runs", "active_seasons", *DATA_TABLES)
            }

    def table_rows(self, table, season=None, game_id=None, run_id=None, limit=100, offset=0):
        if table not in DATA_TABLES:
            raise ValueError("Unknown result table")
        with self.connect() as db:
            if table == "calculations":
                rows = db.execute(
                    "SELECT "
                    "calculation_id,kind,game_id,play_id,model_id,code_sha25"
                    "6,created_at FROM calculations"
                    + (" WHERE game_id=?" if game_id else "")
                    + " ORDER BY created_at,calculation_id LIMIT ? OFFSET ?",
                    (*([game_id] if game_id else []), limit, offset),
                )
                return {
                    "table": table,
                    "limit": limit,
                    "offset": offset,
                    "rows": [dict(r) for r in rows],
                }
            run = self.run(run_id) if run_id else self._active(db, season)
            if not run or run["status"] != "completed":
                raise LookupError("No completed transformation")
            key = "source_id" if table in {"raw_plays", "raw_games"} else "run_id"
            value = (
                run["pbp_source_id"]
                if table == "raw_plays"
                else run["schedule_source_id"]
                if table == "raw_games"
                else run["run_id"]
            )
            clause, args = key + "=?", [value]
            if game_id:
                cols = {r["name"] for r in db.execute(f"PRAGMA table_info({quoted(table)})")}
                if "game_id" in cols:
                    clause += " AND game_id=?"
                    args.append(game_id)
                elif "penalty_id" in cols:
                    clause += (
                        " AND penalty_id IN (SELECT id FROM penalties WHERE run_id=? AND game_id=?)"
                    )
                    args.extend([run["run_id"], game_id])
                else:
                    raise ValueError("This table is not keyed by game; omit game_id")
            if table == "raw_games":
                clause += " AND season=?"
                args.append(run["season"])
            return {
                "table": table,
                "run_id": run["run_id"],
                "limit": limit,
                "offset": offset,
                "rows": self._rows(db, table, clause, args, limit=(limit, offset), order="rowid"),
            }
