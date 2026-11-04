# Database, generation API and data lifecycle

The default source of truth is `.cache/refimpact.sqlite` (SQLite, schema version 1).
Override it with `refimpact refresh --database PATH` and `refimpact serve --database PATH`.
There is no required database server or new third-party database dependency.

## Imports and offline reuse

```sh
# Initial import from local files; originals are retained inside the database.
uv run refimpact refresh --season 2026 --as-of 2026-11-03 \
  --pbp /path/to/play_by_play_2026.parquet --schedule /path/to/games.csv

# Reuse the stored source versions; no upstream requests when both sources exist.
make refresh

# Explicitly fetch the latest schedule/PBP revisions and transform them.
make update
# equivalent: uv run refimpact refresh --download
```

An absent source is downloaded on first CLI import. Subsequent CLI refreshes reuse it
until `--download` or a replacement local input is explicitly supplied. Thus cached
results do not silently claim to include later upstream games/corrections. `/status`
reports source IDs, original content hashes, import times and the generation cutoff.
The browser's Reload view button only reads local results. The generation API never
makes upstream requests or accepts a filesystem path/URL.

`source_heads` identifies the currently imported version of each dataset. A raw import
is keyed by kind, season and SHA-256 of its original bytes. Reimporting identical
bytes neither appends raw rows nor creates another source version. Revising a source
keeps its previous version and creates a new head. Original input files are retained
once in the compressed, content-addressed `artifacts` table.

The schedule import includes all source seasons (source season 0). PBP imports retain
all supplied columns and all rows for their season. The current dataset has 372 PBP
columns. Game/play IDs are normalized to strings for joins; typed Polars frames are
reconstructed with their original source schema before applying football math.

## Generate and materialize transformed tables

```sh
curl -X POST http://127.0.0.1:8787/api/v1/transforms \
  -H 'Content-Type: application/json' \
  --data '{"season":2026,"as_of":"2026-11-03"}'
# HTTP 202: {"run_id":"...","status":"queued","reused":false,"status_url":"..."}

curl http://127.0.0.1:8787/api/v1/transforms/RUN_ID
```

A new request is a durable background job, returning before transformation finishes.
Poll its status URL for `queued`, `running`, `completed` or `failed`. The run stores
source IDs, season, as-of date, model ID, numerical code hash, timestamps and any error.
The `as_of` filter restricts game dates, not when upstream corrections were published.
A generation covers that season's available PBP through the cutoff and its complete
supported schedule, so future games remain visible with unknown results.

The fingerprint hashes source IDs + season + cutoff + pinned model + pipeline/code
version. The same request reuses its queued/running job (202) or completed tables (200).
It performs no download and no repeated calculations. Different simultaneous generation
requests return 409 while the single writer job is active. Missing sources return 422.
A failed identical request can be retried. An abandoned job is failed when its local
owner process is known to have exited; a live process is not expired by a timer.

All output tables and the `active_seasons` pointer publish in one transaction. A failure
rolls back all new result rows and leaves the previous complete run active. Reads use
one consistent SQLite transaction. WAL permits existing results to be read while a new
run is being calculated or published. Completed historical runs retain their lineage.
Reusing a completed historical job does not silently reactivate it.

## Tables and join keys

Use `GET /api/v1/data/schema` for actual columns, types, primary/foreign keys and indexes.
The numeric result fields are scalar columns. Nested state, assumptions and evidence
objects are JSON text and can be inspected with SQLite `json_extract`.

| Table | Key / purpose |
|---|---|
| `sources`, `source_heads` | Immutable import metadata and current source pointers |
| `raw_plays` | `(source_id, game_id, play_id)`; every original PBP column |
| `raw_games` | `(source_id, game_id)`; original schedule columns |
| `runs`, `active_seasons` | Versioned generation lifecycle and one published run per season |
| `games` | `(run_id, game_id)`; scoreboard, coverage and game metrics |
| `team_production` | `(run_id, game_id, team)`; offense, defense and team drive totals |
| `play_metrics` | `(run_id, game_id, play_id)`; native WP/WPA and cumulative ledgers |
| `game_progression` | `(run_id, game_id)`; game-start/final WP and progression metadata |
| `penalties` | `(run_id, id)`; `game_id`, `play_id`, optional `drive_id`, signed effects/evidence |
| `penalty_fouls` | `(run_id, penalty_id, position)`; each foul/disposition within an event |
| `penalty_decisions` | `(run_id, penalty_id)`; reference team and actual branch ID |
| `penalty_branches` | `(run_id, penalty_id, id)`; EP/EPA/WP/WPA, base/effective state, assumptions |
| `penalty_timing` | `(run_id, penalty_id, position)`; declared next-snap branch sensitivities |
| `penalty_timing_comparisons` | `(run_id, penalty_id, position)`; matched-delay ruling comparisons |
| `drives` | `(run_id, id)`; `game_id`, driving team, observed EPA, WPA and separate penalty ledger |
| `drive_plays` | `(run_id, drive_id, position)`; `game_id`, `play_id`, drive cumulative accounting |
| `analysis_results` | `(run_id, scope_week, metric)`; precomputed association/bootstrap statistics |
| `analysis_points` | `(run_id, scope_week, metric, game_id)`; the exact game-level sample |
| `legacy_calls`, `legacy_review` | Original bot compatibility results and unsupported-case evidence |
| `calculations` | Deduplicated interactive compare/decision requests and complete results, model/code version |
| `artifacts`, `archived_files` | Deduplicated compressed bytes and a path-to-hash archive manifest |

`penalty_id` joins `penalties.id`. `drive_id` joins `drives.id`. **Include `run_id` on
every transformed-data join** to avoid mixing model/code/source versions. Join raw
plays through `runs.pbp_source_id`; do not join the current source head to an older run.
Multiple fouls/branches/timing rows multiply result cardinality: aggregate the event
before summing its impact, not after a one-to-many join.

For example, join a penalty to its recorded play and cumulative state:

```sql
SELECT p.game_id, p.play_id, raw.desc,
       raw.down, raw.ydstogo, raw.yardline_100, raw.game_seconds_remaining,
       raw.epa AS observed_play_epa,
       m.home_wp_before, m.home_wp_after, m.home_wp_change_pp,
       p.reference_team, p.enforcement_epa, p.enforcement_wp_swing_pp,
       m.cumulative_home_penalty_benefit_epa
FROM active_seasons active
JOIN runs r ON r.run_id = active.run_id
JOIN penalties p ON p.run_id = r.run_id
JOIN raw_plays raw ON raw.source_id = r.pbp_source_id
                  AND raw.game_id = p.game_id AND raw.play_id = p.play_id
LEFT JOIN play_metrics m ON m.run_id = p.run_id
                       AND m.game_id = p.game_id AND m.play_id = p.play_id
WHERE active.season = 2026 AND p.game_id = '2026_08_CHI_SEA';
```

Inspect branch states alongside the penalty and source:

```sql
SELECT p.game_id, p.play_id, b.id AS branch, b.epa, b.wp, b.wpa_pp,
       b.actual_minus_branch_epa, b.actual_minus_branch_wp_pp,
       json_extract(b.state, '$.possession') AS possession,
       json_extract(b.state, '$.down') AS down,
       json_extract(b.state, '$.ydstogo') AS to_gain
FROM active_seasons active
JOIN penalties p ON p.run_id = active.run_id
JOIN penalty_branches b ON b.run_id = p.run_id AND b.penalty_id = p.id
WHERE active.season = 2026 AND p.game_id = '2026_08_CHI_SEA';
```

Read tables through the API without arbitrary SQL:

```text
GET /api/v1/data/raw_plays?season=2026&game_id=2026_08_CHI_SEA&limit=100&offset=0
GET /api/v1/data/play_metrics?season=2026&game_id=2026_08_CHI_SEA
GET /api/v1/data/penalty_branches?season=2026&game_id=2026_08_CHI_SEA
GET /api/v1/data/analysis_results?season=2026
GET /api/v1/data/penalties?run_id=HISTORICAL_RUN_ID
```

Limits are bounded to 1,000; use offset for another page. Only documented tables are
exposed. Source blobs and arbitrary filesystem/SQL access are not API endpoints.
Interactive `POST /compare` and `POST /decisions` return a `calculation_id`; retrieve
that saved request/result with `GET /api/v1/calculations/{calculation_id}`. A compare
input's explicit game/play IDs can join game data. A free-form decision has no game
identity unless its input establishes one; it is not automatically attached to a game.
Neither endpoint queues or publishes tweets.

## Performance

The web pipeline computes results in memory and writes database tables directly.
It does not write/read weekly JSON/JSONL report copies or a season-wide catalog file.
The standalone `replay` command can still produce an explicit audit export on request.

The site reads indexed season/week/game subsets. Penalty list requests use
`detail=false`, then fetch `GET /api/v1/penalties/{id}` when an event is opened.
Game progression and drive timelines are fetched on demand. Original play descriptions
are joined from raw data instead of copied into each stored timeline. Association and
1,500-resample bootstrap calculations are materialized at generation, never on page load.
Responses support gzip and revision-based ETag revalidation; a new active run invalidates
the old result revision. Three sequential gzip-enabled localhost requests per endpoint measured median response
times of 5.3 ms for the scoreboard, 29.4 ms for weekly penalty summaries, 11.7 ms for
season analysis, 23.0 ms for a game detail and 3.5 ms for one penalty detail. These are
local endpoint timings, not end-to-end phone/Wi-Fi or browser-render timings. Details
are exported in `reports/database-verification.json`.

The migration removed 944 redundant files totaling 832,532,416 bytes (about 794 MiB).
Their recoverable copies deduplicate into content-addressed database blobs; original
raw rows are also queryable. The math audit still reconciles 37,975 identities, and
128 tests cover persistence, offline reuse, deduplication, atomic failure recovery,
foreign keys, cached statistics, HTTP revisions and the football calculations.

## Migrating and cleaning up previous files

```sh
uv run refimpact db-migrate           # Import, verify and archive; keep source files
uv run refimpact db-migrate --prune   # Then remove verified redundant local copies
```

Migration imports existing season caches and the current catalog, verifies a semantic
round trip of games/calls/penalties/drives/progression, and archives old report files
by SHA-256. Each compressed copy is committed and compared byte-for-byte before its
local duplicate is removed. JSON absent fields and SQL NULL are equivalent during
verification; numeric source play IDs are normalized to the same string identity.
Screenshots, `.gitkeep`, and the latest standalone math audit remain convenient files.
Old reports, raw source JSON snapshots and legacy catalogs remain recoverable from the
archive manifest. Model runtime files and the separate tweet outbox are retained.
`make clean` continues to stop only this checkout's web server; it never deletes data.

To restore one archived file (use a new output path):

```python
import hashlib, sqlite3, zlib
from pathlib import Path

with sqlite3.connect(".cache/refimpact.sqlite") as db:
    sha, payload = db.execute(
        """SELECT f.sha256,a.compressed FROM archived_files f
        JOIN artifacts a USING(sha256) WHERE f.path=?""",
        ("/absolute/original/path/to/report.json",),
    ).fetchone()
    content = zlib.decompress(payload)
    assert hashlib.sha256(content).hexdigest() == sha
    Path("/tmp/restored-report.json").write_bytes(content)
```

For a live backup use SQLite's backup API (`source.backup(destination)`) or stop the
server and checkpoint WAL before copying the database. Do not copy just the main DB
file while ignoring an active WAL file.
