"""Field-service platform command line. Run `python -m fieldservice --help` to list commands."""
import json
from pathlib import Path

import typer

from fieldservice.config import PROJECT_ROOT, get_fsp_settings

app = typer.Typer(help="ServiceIQ field-service platform.", no_args_is_help=True, add_completion=False)
KEYS_FILE = PROJECT_ROOT / "var" / "dev_fsp_keys.json"


def _register(config_path: Path, rotate: bool) -> None:
    from fieldservice.db import add_org, make_engine, session_factory

    settings = get_fsp_settings()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    given = json.loads(settings.fsp_api_keys) if settings.fsp_api_keys else {}  # cloud: keys from Secret Manager
    key = add_org(session_factory(make_engine(settings.fsp_database_url)), config, key=given.get(config["org_id"]),
                  rotate=rotate)
    if key and config["org_id"] not in given:  # supplied keys are never written out
        keys = json.loads(KEYS_FILE.read_text(encoding="utf-8")) if KEYS_FILE.exists() else {}
        keys[config["org_id"]] = key
        KEYS_FILE.parent.mkdir(exist_ok=True)
        KEYS_FILE.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    typer.echo(f"{config['org_id']}: {'new API key issued' if key else 'up to date'}")


@app.command("add-org")
def add_org_command(config: Path = typer.Option(..., exists=True, help="Contractor config JSON."),
                    rotate: bool = typer.Option(False, help="Issue a new API key.")):
    """Onboard (or update) one contractor: org, API key, service types, technicians and working hours."""
    _register(config, rotate)


@app.command()
def seed(rotate: bool = typer.Option(False, help="Issue new API keys.")):
    """Onboard every tenant that has data/tenants/<id>/fieldservice.json (dev keys go to var/dev_fsp_keys.json)."""
    for config in sorted((get_fsp_settings().data_dir / "tenants").glob("*/fieldservice.json")):
        _register(config, rotate)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8100):
    """Run the field-service API (docs at http://127.0.0.1:8100/docs)."""
    import uvicorn

    uvicorn.run("fieldservice.main:app", host=host, port=port)


if __name__ == "__main__":
    app()
