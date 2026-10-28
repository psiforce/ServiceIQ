"""Audit logging for agent tools: every call is recorded with redacted inputs, result, timing and errors."""
import json
import time

from sqlalchemy.orm import Session

from serviceiq.guardrails.pii import redact
from serviceiq.storage.models import ToolCall

CONTACT_KEYS = {"name", "phone", "email", "street", "address", "city"}


def _safe(value) -> dict:
    """Inputs as stored in the audit log: contact fields dropped, everything else PII-redacted."""
    if isinstance(value, dict):
        value = {k: ("[REDACTED]" if k in CONTACT_KEYS else v) for k, v in value.items()}
    return json.loads(redact(json.dumps(value, default=str)))


def audited(session: Session, tenant_id: str, conversation_id: str | None, tool: str, inputs: dict, fn,
            idempotency_key: str | None = None, approver: str | None = None):
    """Run `fn()`, record a ToolCall row either way, and return its result (re-raising errors)."""
    started = time.perf_counter()
    call = ToolCall(tenant_id=tenant_id, conversation_id=conversation_id, tool=tool, inputs=_safe(inputs),
                    idempotency_key=idempotency_key, approver=approver)
    try:
        result = fn()
        call.result = _safe(result if isinstance(result, dict) else {"value": result})
        return result
    except Exception as exc:
        call.ok, call.error = False, f"{type(exc).__name__}: {redact(str(exc))[:500]}"
        raise
    finally:
        call.duration_ms = round((time.perf_counter() - started) * 1000)
        session.add(call)
