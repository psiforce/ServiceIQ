"""Create or update ServiceIQ secrets in Google Secret Manager without printing any secret value.

Usage:  python deploy/gcp_secrets.py --project serviceiq-512906
Reads API keys from .env; generates the database URLs, widget keys, field-service API keys and admin passwords on
first run and keeps them in var/gcp/ (git-ignored) so they stay stable across re-runs. See deploy/DEPLOY.md.
"""
import argparse
import json
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GCP_DIR = ROOT / "var" / "gcp"
TENANTS = ["cedar-ridge", "harbor-point"]
ADMINS = {"cedar-admin": ("cedar-ridge", "admin"), "cedar-csr": ("cedar-ridge", "csr"),
          "harbor-admin": ("harbor-point", "admin")}


def read_env() -> dict:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def stable(name: str, make) -> str:
    """Generate once, then reuse from var/gcp/<name>."""
    path = GCP_DIR / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(make(), encoding="utf-8")
    return path.read_text(encoding="utf-8")


def gcloud(*args: str, data: str | None = None) -> subprocess.CompletedProcess:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    return subprocess.run([exe, *args, "--quiet"], input=data, capture_output=True, text=True)


def put_secret(project: str, name: str, value: str) -> str:
    exists = gcloud("secrets", "describe", name, f"--project={project}").returncode == 0
    if not exists:
        r = gcloud("secrets", "create", name, f"--project={project}", "--replication-policy=automatic",
                   "--data-file=-", data=value)
    else:
        r = gcloud("secrets", "versions", "add", name, f"--project={project}", "--data-file=-", data=value)
    if r.returncode != 0:
        raise SystemExit(f"{name}: failed ({r.stderr.strip().splitlines()[-1] if r.stderr else 'unknown error'})")
    return "updated" if exists else "created"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--instance", default="serviceiq-db")
    args = ap.parse_args()

    env = read_env()
    db_password = (GCP_DIR / "db_password").read_text(encoding="utf-8").strip()
    socket = f"/cloudsql/{args.project}:{args.region}:{args.instance}"
    widget_keys = stable("widget_keys.json", lambda: json.dumps(
        {t: f"wk_{t}_{secrets.token_urlsafe(24)}" for t in TENANTS}, indent=2))
    fsp_keys = stable("fsp_keys.json", lambda: json.dumps(
        {t: f"fsp_{t}_{secrets.token_urlsafe(24)}" for t in TENANTS}, indent=2))
    fsp_db_password = stable("fsp_db_password", lambda: secrets.token_urlsafe(24))
    admin_creds = stable("admin_credentials.json", lambda: json.dumps(
        {u: {"tenant": t, "role": r, "password": secrets.token_urlsafe(14)} for u, (t, r) in ADMINS.items()}, indent=2))

    values = {
        "openai-api-key": env["OPENAI_API_KEY"],
        "pinecone-api-key": env["PINECONE_API_KEY"],
        "langsmith-api-key": env.get("LANGSMITH_API_KEY", ""),
        "database-url": f"postgresql+psycopg://serviceiq:{db_password}@/serviceiq?host={socket}",
        "chat-widget-keys": json.dumps(json.loads(widget_keys)),
        # field-service platform: its own database and a normal role, so row-level security applies
        "fsp-database-url": f"postgresql+psycopg://fsp_app:{fsp_db_password}@/fieldservice?host={socket}",
        "fsp-api-keys": json.dumps(json.loads(fsp_keys)),
        "admin-passwords": json.dumps({u: c["password"] for u, c in json.loads(admin_creds).items()}),
    }
    for name, value in values.items():
        if not value:
            print(f"{name}: skipped (no value in .env)")
            continue
        print(f"{name}: {put_secret(args.project, name, value)}")


if __name__ == "__main__":
    main()
