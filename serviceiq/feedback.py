"""Customer feedback (ratings and callback requests). Stored as JSON lines until the database arrives in M11.

Only PII-redacted text is stored.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from serviceiq.config import PROJECT_ROOT
from serviceiq.guardrails.pii import redact


def feedback_path() -> Path:
    return Path(os.environ.get("SERVICEIQ_FEEDBACK_PATH", PROJECT_ROOT / "reports" / "feedback.jsonl"))


def record(event: str, tenant_id: str, trace_id: str, question: str, status: str, **extra) -> None:
    path = feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, "tenant_id": tenant_id,
             "trace_id": trace_id, "question": redact(question), "status": status, **extra}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
