from __future__ import annotations

import csv
import hashlib
import io
import threading
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.gzip import GZipMiddleware

from refimpact.decisions import DecisionRequest, evaluate_decision
from refimpact.models import MODEL_ID, Scorer
from refimpact.pipeline import code_digest, execute_transform, request_transform
from refimpact.schema import Comparison, Record
from refimpact.storage import DATA_TABLES, DEFAULT_DATABASE, Database
from refimpact.tweets import render_tweet

Year = Annotated[int, Query(ge=2000, le=2100)]
Week = Annotated[int, Query(ge=1, le=22)]
WEB = Path(__file__).parent / "web"


class StoredResults:
    """Compatibility response shape with indexed, scoped database queries."""

    def __init__(self, database):
        self.database = database

    def read(self, season=None, week=None, **options):
        try:
            return self.database.read(season, week, **options)
        except LookupError as exc:
            raise HTTPException(404 if season is not None else 503, str(exc)) from None


class TransformRequest(Record):
    season: int = Field(ge=2000, le=2100)
    as_of: date = Field(default_factory=date.today)


def selection(data: dict, season: int | None, week: int | None, all_weeks: bool = False):
    season = season if season is not None else data["default_season"]
    week = week if week is not None else (None if all_weeks else data["default_week"])
    games = [
        g for g in data["games"] if g["season"] == season and (week is None or g["week"] == week)
    ]
    if not games:
        raise HTTPException(404, "Season or week is not present in the database")
    return season, week, games


def create_app(
    database_path: Path = DEFAULT_DATABASE, models_path: Path = Path(".cache/models")
) -> FastAPI:
    app = FastAPI(
        title="Ref Impact API",
        version="0.4.0",
        description="NFL ruling impact with explicit alternatives. EPA is in points; WP swing is "
        "in percentage points. Indexed database results, offline "
        "transformations and persisted comparisons.",
    )
    database = Database(database_path)
    database.recover_abandoned()
    results = StoredResults(database)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    numerical_code = code_digest()
    scorer = None
    score_lock = threading.Lock()

    @app.middleware("http")
    async def revision_cache(request, call_next):
        eligible = (
            request.method == "GET"
            and request.url.path.startswith("/api/v1/")
            and not request.url.path.startswith("/api/v1/transforms")
            and request.url.path not in {"/api/v1/data/schema", "/api/v1/data/calculations"}
        )
        etag = None
        if eligible:
            with database.connect() as db:
                revision = tuple(
                    tuple(r)
                    for r in db.execute("SELECT season,run_id FROM active_seasons ORDER BY season")
                )
            etag = (
                '"'
                + hashlib.sha256(
                    repr(
                        (
                            revision,
                            request.url.path,
                            request.url.query,
                            numerical_code,
                            (WEB / "math.md").stat().st_mtime_ns
                            if request.url.path == "/api/v1/math"
                            else None,
                        )
                    ).encode()
                ).hexdigest()
                + '"'
            )
            if request.headers.get("if-none-match") == etag:
                return Response(
                    status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"}
                )
        response = await call_next(request)
        if etag and response.status_code == 200:
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "private, no-cache"
        return response

    @app.get("/api/v1/status")
    def status():
        try:
            data = results.read()
        except HTTPException:
            return {"status": "no_data", "instruction": "Run refimpact refresh", "mode": "postgame"}
        return {
            "status": "ready",
            **{
                key: data[key]
                for key in [
                    "generated_at",
                    "as_of",
                    "default_season",
                    "default_week",
                    "model_id",
                    "mode",
                    "sources",
                ]
            },
        }

    @app.get("/api/v1/weeks")
    def weeks():
        data = results.read()
        return {
            "default_season": data["default_season"],
            "default_week": data["default_week"],
            "weeks": data["weeks"],
        }

    @app.get("/api/v1/methodology")
    def methodology():
        return {
            "epa_unit": "expected points, actual minus alternative, fixed reference team",
            "wp_unit": "percentage points: 100 * (actual WP - alternative WP)",
            "wpa": "Branch WPA = 100 * (branch WP - before WP). Penalty swing = "
            "actual branch WPA minus alternative WPA, not the entire flagged play's WPA. "
            "The non-Vegas WP model uses score, clocks, down/distance, field position, "
            "possession, timeouts, home field and second-half receiver; EPA is not converted "
            "to win probability by a constant multiplier.",
            "net_home_epa": "sum of measured call EPA impacts oriented to the home team; "
            "a descriptive local-impact ledger, not a final-score adjustment",
            "automatic_penalties": "All reported dispositions, including flags with penalty=0; "
            "accept/decline/no-flag states are valued where recoverable.",
            "alternative": "Hold described play result fixed; missing foul spots become labeled "
            "scenarios. Runoffs and next-snap timing are separate interventions.",
            "clock": "Apply reported ten-second runoffs once; a timeout alternative consumes a "
            "timeout. Rule 4 restart states are evaluated at declared delays, not guessed "
            "by adding a feature to the published model. timing_comparisons pair actual "
            "and alternative branches at the same declared next-snap opportunity, with "
            "separate restart assumptions. Conflicting runoff timestamps remain unpriced.",
            "progression": "Observed win chance comes from native nflfastR home_wp/home_wp_post. "
            "Play and drive changes are before/after percentage-point differences; game cumulative "
            "change is latest minus game-start chance, not the sum of ruling comparisons. "
            "Cumulative offensive EPA already includes penalties. Estimated penalty benefit is "
            "a separate ledger across both possessions.",
            "production": "Native scrimmage EPA; lower defensive EPA allowed is better. "
            "Penalty effects must not be added to production again.",
            "drive_epa": "Sum native EPA for the driving team, including punt/FG endings and "
            "excluding kickoff/try EPA. Unknown rows are disclosed.",
            "nullified_plays": "Automatically extract the written action preceding the "
            "flag. A no-play "
            "statistical zero is never an erased zero-yard play. Compare actual enforcement "
            "with keeping that written result, at a common clock; missing/unsupported results "
            "remain unavailable. No human review step or video is required.",
            "mode": "postgame",
            "correctness_assessed": False,
            "math_documentation": "/api/v1/math",
            "storage_schema": "/api/v1/data/schema",
            "analysis_unit": "one completed game, not two mirrored team rows",
            "missing_data": "Incomplete games and games without the selected metric are excluded "
            "from association estimates, never imputed as zero",
            "legacy_bot_scope": "The /calls and tweet queue retain the original four verified "
            "dead-ball types. Expanded scenarios are available through /penalties and /decisions.",
            "model_id": MODEL_ID,
        }

    @app.get("/api/v1/games")
    def games(season: Year | None = None, week: Week | None = None):
        data = results.read(season, week)
        season, week, rows = selection(data, season, week)
        return {
            "season": season,
            "week": week,
            "games": rows,
            "scored_calls": sum(g["scored_calls"] for g in rows),
            "penalty_rows": sum(g["penalty_rows"] for g in rows),
            "review_calls": sum(g["review_calls"] for g in rows),
            "valued_penalties": sum(g.get("valued_penalties", g["scored_calls"]) for g in rows),
            "penalty_dispositions": {
                name: sum(
                    g.get("penalty_coverage", {}).get("dispositions", {}).get(name, 0) for g in rows
                )
                for name in ["accepted", "declined", "offsetting", "rescinded", "mixed", "unknown"]
            },
        }

    @app.get("/api/v1/games/{game_id}")
    def game_detail(game_id: str):
        data = results.read(
            int(game_id[:4]) if game_id[:4].isdigit() else None,
            all_weeks=True,
            game_id=game_id,
            sections=("calls", "review", "penalties", "drives", "progression"),
        )
        game = next((g for g in data["games"] if g["game_id"] == game_id), None)
        if game is None:
            raise HTTPException(404, "Game is not present in the database")
        return JSONResponse(
            {
                "game": game,
                "progression": data.get("progression", {}).get(game_id),
                "calls": [c for c in data["calls"] if c["comparison"]["game_id"] == game_id],
                "review": [c for c in data["review"] if c["game_id"] == game_id],
                "penalties": [e for e in data.get("penalties", []) if e["game_id"] == game_id],
                "drives": [d for d in data.get("drives", []) if d["game_id"] == game_id],
            }
        )

    @app.get("/api/v1/games/{game_id}/progression")
    def game_progression(game_id: str):
        data = results.read(
            int(game_id[:4]) if game_id[:4].isdigit() else None,
            all_weeks=True,
            game_id=game_id,
            sections=("progression",),
        )
        if not any(g["game_id"] == game_id for g in data["games"]):
            raise HTTPException(404, "Game is not present in the database")
        return {"progression": data.get("progression", {}).get(game_id)}

    @app.get("/api/v1/penalties")
    def penalties(
        season: Year | None = None,
        week: Week | None = None,
        disposition: Literal["accepted", "declined", "offsetting", "rescinded", "mixed", "unknown"]
        | None = None,
        nullified_status: Literal["valued", "not_recorded", "unpriced"] | None = None,
        detail: bool = True,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        data = results.read(season, week, sections=("penalties",), detail=detail)
        season, week, _ = selection(data, season, week)
        rows = [
            e
            for e in data.get("penalties", [])
            if e["season"] == season
            and e["week"] == week
            and (disposition is None or any(f["disposition"] == disposition for f in e["fouls"]))
            and (
                nullified_status is None
                or (e.get("nullified_play") or {}).get("status") == nullified_status
            )
        ]
        return {
            "total": len(rows),
            "penalties": rows[offset : offset + limit],
            "nullified_coverage": dict(
                Counter(e["nullified_play"]["status"] for e in rows if e.get("nullified_play"))
            ),
        }

    @app.get("/api/v1/production")
    def production(season: Year | None = None, week: Week | None = None):
        data = results.read(season, week)
        season, week, games = selection(data, season, week)
        return {
            "season": season,
            "week": week,
            "games": [{"game_id": g["game_id"], "teams": g.get("production", {})} for g in games],
            "note": "Observed EPA already includes enforcement; do not add penalty EPA again.",
        }

    @app.get("/api/v1/drives")
    def drives(season: Year | None = None, week: Week | None = None, game_id: str | None = None):
        data = results.read(season, week, sections=("drives",), detail=False)
        season, week, _ = selection(data, season, week)
        rows = [
            d
            for d in data.get("drives", [])
            if d["season"] == season
            and d["week"] == week
            and (game_id is None or d["game_id"] == game_id)
        ]
        return {
            "total": len(rows),
            "drives": [{k: v for k, v in d.items() if k != "timeline"} for d in rows],
        }

    @app.get("/api/v1/drives/{drive_id}")
    def drive_detail(drive_id: str):
        data = results.read(
            int(drive_id[:4]) if drive_id[:4].isdigit() else None,
            all_weeks=True,
            drive_id=drive_id,
            sections=("drives", "penalties"),
        )
        drive = next((d for d in data.get("drives", []) if d["id"] == drive_id), None)
        if drive is None:
            raise HTTPException(404, "Drive is not present in the database")
        return {
            "drive": drive,
            "penalties": [e for e in data.get("penalties", []) if e["drive_id"] == drive_id],
        }

    @app.get("/api/v1/calls")
    def calls(
        season: Year | None = None,
        week: Week | None = None,
        team: Annotated[str | None, Query(pattern=r"^[A-Z]{2,3}$")] = None,
        min_swing: Annotated[float, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        data = results.read(season, week, sections=("calls",))
        season, week, _ = selection(data, season, week)
        rows = [
            c
            for c in data["calls"]
            if c["season"] == season
            and c["week"] == week
            and abs(c["wp_swing_pp"]) >= min_swing
            and (
                team is None
                or team
                in {c["comparison"]["before"]["home_team"], c["comparison"]["before"]["away_team"]}
            )
        ]
        rows.sort(key=lambda c: abs(c["wp_swing_pp"]), reverse=True)
        return {
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "calls": rows[offset : offset + limit],
        }

    @app.get("/api/v1/analysis")
    def analysis(
        season: Year | None = None,
        week: Week | None = None,
        metric: Literal["penalty_epa", "production_epa", "drive_epa"] = "penalty_epa",
    ):
        """Omit week for season-to-date; otherwise one row per game in that slate."""
        data = results.read(season, week, all_weeks=True)
        season, week, rows = selection(data, season, week, all_weeks=True)
        return {"season": season, "week": week, **database.analysis(season, week, metric)}

    @app.get("/api/v1/export.csv")
    def export(season: Year | None = None, week: Week | None = None):
        data = results.read(season, week)
        _, _, rows = selection(data, season, week)
        stream = io.StringIO()
        keys = [
            "game_id",
            "season",
            "week",
            "gameday",
            "away_team",
            "home_team",
            "away_score",
            "home_score",
            "status",
            "pbp_complete",
            "scored_calls",
            "penalty_rows",
            "net_home_epa",
            "largest_call_swing_pp",
            "valued_penalties",
            "net_home_production_epa_per_snap",
            "net_home_drive_epa",
        ]
        writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            stream.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="ref-impact-games.csv"'},
        )

    @app.get("/api/v1/penalties/{penalty_id}")
    def penalty_detail(penalty_id: str):
        data = results.read(
            int(penalty_id[:4]) if penalty_id[:4].isdigit() else None,
            all_weeks=True,
            sections=("penalties",),
            penalty_id=penalty_id,
        )
        if not data["penalties"]:
            raise HTTPException(404, "Penalty not found")
        return JSONResponse({"penalty": data["penalties"][0]})

    @app.post("/api/v1/transforms")
    def transform(body: TransformRequest, tasks: BackgroundTasks):
        try:
            run, created = request_transform(database, body.season, body.as_of)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        if created:

            def generate():
                try:
                    execute_transform(database, run["run_id"], models_path)
                except Exception:
                    pass  # The durable run row contains the error; previous results remain active.

            tasks.add_task(generate)
        return JSONResponse(
            {
                "run_id": run["run_id"],
                "status": run["status"],
                "reused": not created,
                "status_url": f"/api/v1/transforms/{run['run_id']}",
            },
            status_code=202 if run["status"] in {"queued", "running"} else 200,
        )

    @app.get("/api/v1/transforms/{run_id}")
    def transformation_status(run_id: str):
        run = database.run(run_id)
        if not run:
            raise HTTPException(404, "Transformation not found")
        return run

    @app.get("/api/v1/data/schema")
    def data_schema():
        return {
            "database": "SQLite",
            "schema_version": 1,
            "tables": database.schema(),
            "joins": "Use run_id + game_id/play_id. Join runs.pbp_source_id "
            "to raw_plays.source_id. "
            "penalty_id joins penalties.id; drive_id joins drives.id.",
        }

    @app.get("/api/v1/data/{table}")
    def data_rows(
        table: str,
        season: Year | None = None,
        game_id: str | None = None,
        run_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if table not in DATA_TABLES:
            raise HTTPException(404, "Unknown data table")
        try:
            return database.table_rows(table, season, game_id, run_id, limit, offset)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/compare")
    def compare(body: Comparison):
        nonlocal scorer
        with score_lock:
            try:
                scorer = scorer or Scorer(models_path)
                impact = scorer.compare(body)
                tweet = render_tweet(impact)
            except (ValueError, OSError) as exc:
                raise HTTPException(422, str(exc)) from None
        result = impact.model_dump(mode="json") | {"tweet": tweet}
        cid = database.record_calculation(
            "compare", body.model_dump(mode="json"), result, MODEL_ID, numerical_code
        )
        return result | {"calculation_id": cid}

    @app.post("/api/v1/decisions")
    def decision(body: DecisionRequest):
        nonlocal scorer
        with score_lock:
            try:
                scorer = scorer or Scorer(models_path)
                result = evaluate_decision(body, scorer)
            except (ValueError, OSError) as exc:
                raise HTTPException(422, str(exc)) from None

        cid = database.record_calculation(
            "decision", body.model_dump(mode="json"), result, MODEL_ID, numerical_code
        )
        return result | {"calculation_id": cid}

    @app.get("/api/v1/calculations/{calculation_id}")
    def saved_calculation(calculation_id: str):
        calculation = database.calculation(calculation_id)
        if calculation is None:
            raise HTTPException(404, "Calculation not found")
        return calculation

    @app.get("/api/v1/math", include_in_schema=True)
    def math_documentation():
        return FileResponse(WEB / "math.md", media_type="text/markdown")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return app
