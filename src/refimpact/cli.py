from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from refimpact.models import Scorer, fetch_models
from refimpact.outbox import Outbox
from refimpact.publisher import publish, x_client
from refimpact.replay import load_pbp, replay
from refimpact.schema import Comparison
from refimpact.tweets import render_tweet

app = typer.Typer(help="NFL ruling impact: explicit alternatives, EPA and win-probability swing.")
DEFAULT_MODELS = Path(".cache/models")
DEFAULT_DB = Path(".cache/outbox.sqlite")


@app.command("models-fetch")
def models_fetch(directory: Path = DEFAULT_MODELS):
    """Download and verify pinned open-source nflfastR EP/WP models."""
    typer.echo(json.dumps(fetch_models(directory), indent=2))


@app.command("compare")
def compare_file(
    path: Path,
    models: Path = DEFAULT_MODELS,
    db: Path = DEFAULT_DB,
    enqueue: bool = False,
    output: Path | None = None,
):
    """Score a JSON comparison. Preview only unless --enqueue is passed."""
    c = Comparison.model_validate_json(path.read_text())
    impact = Scorer(models).compare(c)
    tweet = render_tweet(impact)
    result = impact.model_dump(mode="json") | {"tweet": tweet}
    if enqueue:
        result["outbox_id"] = Outbox(db).enqueue(impact, tweet)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
    typer.echo(json.dumps(result, indent=2))


@app.command("replay")
def replay_week(
    season: int = typer.Option(..., min=2000, max=2100),
    week: int = typer.Option(..., min=1, max=22),
    models: Path = DEFAULT_MODELS,
    output: Path = Path("reports"),
    pbp: Path | None = None,
    db: Path = DEFAULT_DB,
    enqueue: bool = False,
    min_swing: float = typer.Option(0, min=0),
):
    """Replay an NFL week; retain scored calls, review cases, source and parity rows."""
    scorer = Scorer(models)
    summary = replay(
        load_pbp(season, pbp),
        scorer,
        output,
        season,
        week,
        Outbox(db) if enqueue else None,
        min_swing,
    )
    typer.echo(json.dumps(summary, indent=2))


@app.command("queue")
def queue(db: Path = DEFAULT_DB, status: str | None = None):
    """Inspect queued posts and their audit state."""
    for item in Outbox(db).list(status):
        typer.echo(json.dumps({k: v for k, v in item.items() if k != "payload"}))


@app.command("publish")
def publish_posts(
    db: Path = DEFAULT_DB,
    live: bool = False,
    limit: int = typer.Option(1, min=1, max=100),
    env_file: Path | None = None,
):
    """Preview pending posts; --live posts them to the verified X account."""
    store = Outbox(db)
    if not live:
        for post in store.list("pending")[:limit]:
            typer.echo(post["id"] + "\n" + post["text"] + "\n")
        return
    if env_file:
        from dotenv import load_dotenv

        if not env_file.is_file():
            raise typer.BadParameter("Environment file does not exist")
        load_dotenv(env_file, override=False)
    outcomes = publish(store, x_client(), limit)
    typer.echo(json.dumps(outcomes, indent=2))
    if any(item["status"] != "posted" for item in outcomes):
        raise typer.Exit(1)


@app.command("reconcile")
def reconcile(
    post_id: str,
    db: Path = DEFAULT_DB,
    tweet_id: str | None = None,
    confirm_not_posted: bool = False,
):
    """Resolve an uncertain/revised post after inspecting the X account. Stop workers first."""
    Outbox(db).reconcile(post_id, tweet_id, confirm_not_posted)
    typer.echo("Reconciled " + post_id)


@app.command("run")
def run_bot(
    season: int = typer.Option(..., min=2000, max=2100),
    week: int = typer.Option(..., min=1, max=22),
    models: Path = DEFAULT_MODELS,
    db: Path = DEFAULT_DB,
    output: Path = Path("reports"),
    min_swing: float = typer.Option(1.0, min=0),
    limit: int = typer.Option(5, min=1, max=100),
    interval: int = typer.Option(900, min=60),
    watch: bool = False,
    download: bool = False,
    live: bool = False,
    env_file: Path | None = None,
):
    """Poll postgame data and preview queued calls (or publish with --live).

    One cycle by default; --watch repeats until Ctrl-C. Not a live PBP feed.
    """
    if env_file:
        from dotenv import load_dotenv

        if not env_file.is_file():
            raise typer.BadParameter("Environment file does not exist")
        load_dotenv(env_file, override=False)
    scorer, store = Scorer(models), Outbox(db)
    client = x_client() if live else None
    while True:
        try:
            if download:
                from refimpact.pipeline import ingest_sources
                from refimpact.storage import Database

                ingest_sources(Database(), season, download=True, include_schedule=False)
            summary = replay(load_pbp(season), scorer, output, season, week, store, min_swing)
            typer.echo(json.dumps(summary))
            if client is not None:
                outcomes = publish(store, client, limit)
                typer.echo(json.dumps(outcomes))
                if any(item["status"] != "posted" for item in outcomes) and not watch:
                    raise typer.Exit(1)
            else:
                for post in store.list("pending")[:limit]:
                    typer.echo(post["text"] + "\n")
        except (ValueError, OSError) as exc:
            typer.echo(f"Cycle failed ({type(exc).__name__}): {exc}", err=True)
            if not watch:
                raise typer.Exit(1) from None
        if not watch:
            return
        time.sleep(interval)


@app.command("refresh")
def refresh_site(
    season: int | None = typer.Option(None, min=2000, max=2100),
    as_of: str | None = None,
    models: Path = DEFAULT_MODELS,
    database: Path = Path(".cache/refimpact.sqlite"),
    pbp: Path | None = None,
    schedule: Path | None = None,
    download: bool = False,
):
    """Import missing/explicit sources and transform offline. --download refreshes upstream."""
    from datetime import date

    from refimpact.pipeline import execute_transform, ingest_sources, request_transform
    from refimpact.storage import Database

    db = Database(database)
    day = date.fromisoformat(as_of) if as_of else date.today()
    sources = ingest_sources(
        db, season, pbp_path=pbp, schedule_path=schedule, download=download, as_of=day
    )
    run, created = request_transform(db, sources["season"], day)
    if created:
        run = execute_transform(db, run["run_id"], models)
    typer.echo(
        json.dumps(
            {
                "database": str(database),
                "run_id": run["run_id"],
                "status": run["status"],
                "reused": not created,
                "coverage": (run["metadata"] or {}).get("coverage"),
            }
        )
    )


@app.command("db-migrate")
def migrate_database(database: Path = Path(".cache/refimpact.sqlite"), prune: bool = False):
    """Import local caches, archive/deduplicate old reports, optionally remove verified copies."""
    from refimpact.migration import migrate
    from refimpact.storage import Database

    typer.echo(json.dumps(migrate(Database(database), Path.cwd(), prune=prune), indent=2))


@app.command("serve")
def serve_site(
    host: str = "127.0.0.1",
    port: int = typer.Option(8787, min=1, max=65535),
    database: Path = Path(".cache/refimpact.sqlite"),
    models: Path = DEFAULT_MODELS,
):
    """Serve the local dashboard and documented REST API. Refresh data separately."""
    import uvicorn

    from refimpact.api import create_app

    uvicorn.run(create_app(database, models), host=host, port=port)


def main():
    try:
        app()
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
