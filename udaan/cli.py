import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console

from udaan.config import settings

app = typer.Typer(invoke_without_command=True, no_args_is_help=False, help="Udaan operations console")
console = Console()


@app.callback()
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from udaan.tui import UdaanApp
        UdaanApp().run()


@app.command()
def api():
    """Start the persistent operations/data API."""
    import uvicorn
    cfg = settings()
    if cfg.api_host not in {"127.0.0.1", "localhost", "::1"} and not cfg.api_token:
        raise typer.BadParameter("Set API_TOKEN before binding the API beyond loopback")
    uvicorn.run("udaan.api:create_app", factory=True, host=cfg.api_host, port=cfg.api_port, access_log=False)


@app.command()
def worker():
    """Run persistent scheduling, collection, exports, and repair."""
    from udaan.worker import Worker
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(Worker().run())


@app.command()
def doctor():
    """Check local dependencies; use Test AI explicitly to contact the model."""
    from udaan.health import HealthMonitor
    async def checks():
        monitor = HealthMonitor()
        await monitor.check_dependencies()
        return monitor.snapshot()
    console.print_json(json.dumps(asyncio.run(checks()), default=str))


@app.command()
def migrate():
    """Apply Alembic migrations, requiring the TimescaleDB extension."""
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config("alembic.ini"), "head")


@app.command()
def desktop():
    """Start an explicit visible noVNC desktop; Ctrl+C stops this desktop."""
    from udaan.desktop import run
    run()


@app.command("browser-install")
def browser_install():
    """Fetch the underlying browser binary into the current environment's user cache."""
    import subprocess
    import sys
    typer.echo("Installing Udaan Browser…")
    result = subprocess.run([sys.executable, "-m", "camoufox", "fetch"], check=False, capture_output=True, text=True)
    if result.returncode == 0:
        from udaan.browser import ensure_browser_branding
        ensure_browser_branding()
        typer.echo("Udaan Browser installed.")
    else:
        typer.echo("Udaan Browser installation failed; check network access and the browser dependency setup.", err=True)
    raise typer.Exit(result.returncode)


@app.command("import-weights")
def weights(path: Path = typer.Argument(exists=True, readable=True)):
    """Import a real approved WeightInput JSON dataset through the operations API."""
    import httpx

    from udaan.contracts import WeightInput
    data = WeightInput.model_validate_json(path.read_bytes())
    headers = {"Authorization": "Bearer " + settings().api_token.get_secret_value()} if settings().api_token else {}
    with httpx.Client(base_url=settings().api_url, headers=headers, timeout=30) as client:
        response = client.post("/api/v1/weights", json=data.model_dump(mode="json"))
        response.raise_for_status()
        console.print_json(response.text)


@app.command("import-source")
def import_source(path: Path = typer.Argument(exists=True, readable=True), recipe: str | None = None):
    """Register a source JSON file through the generic API; optionally register/attach its recipe."""
    import httpx

    from udaan.contracts import SourceInput
    data = SourceInput.model_validate_json(path.read_bytes())
    headers = {"Authorization": "Bearer " + settings().api_token.get_secret_value()} if settings().api_token else {}
    with httpx.Client(base_url=settings().api_url, headers=headers, timeout=30) as client:
        existing = client.get("/api/v1/sources", params={"limit": 1000})
        existing.raise_for_status()
        source = next((s for s in existing.json()["items"] if s["slug"] == data.slug), None)
        if source is None:
            response = client.post("/api/v1/sources", json=data.model_dump(mode="json"))
            response.raise_for_status()
            source = response.json()
        if recipe:
            response = client.post("/api/v1/recipes", json={"source_id": source["id"], "path": recipe})
            response.raise_for_status()
            attached = client.post(f"/api/v1/sources/{source['id']}/recipe", json={"recipe_id": response.json()["id"]})
            attached.raise_for_status()
            source = attached.json()
        console.print_json(json.dumps(source))


@app.command()
def ser():
    """Start and verify the existing Udaan runtime, without duplicate processes."""
    from udaan.runtime import start
    try:
        ready, _ = start(lambda value: console.print(value, markup=False))
    except Exception as exc:
        console.print(f"Udaan services failed: {exc}", markup=False)
        raise typer.Exit(1) from exc
    if not ready:
        raise typer.Exit(1)


@app.command("seed-sources")
def seed_supported_sources():
    """Idempotently register the nine prototype airlines; never seed fares."""
    from udaan.catalog import seed_sources
    from udaan.db import session
    with session() as db:
        for source in seed_sources(db):
            typer.echo(f"{source.name}: {source.status}")


@app.command("import-airports")
def import_airports(airports: Path, regions: Path):
    """Import reference metadata; this does not create airline routes."""
    from udaan.airports import import_reference
    from udaan.db import session
    with session() as db:
        print(import_reference(db, airports, regions, "https://ourairports.com/data/"))
