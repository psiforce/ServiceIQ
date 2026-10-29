"""Policy layer (Phase 3): when the agent may act alone, and the limits it can never cross.

Approval rules are data (approval_policies table, seeded per tenant with the accepted defaults), so a company can
tighten them without a code change. Hard limits are enforced in code at the moment a job is booked, whatever the
model asked for: no booking that needs approval without one, no price but the published one (an approver may
pick a price within the published range), no visit outside working hours without an approver, and a per-tenant
rate limit on bookings.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from serviceiq.config import Settings
from serviceiq.storage.models import ApprovalPolicy, ToolCall

# condition -> (agent may act alone, what the customer is told needs review)
DEFAULT_POLICIES = {
    "standard_visit": (True, "a standard visit during business hours at the published price"),
    "binding_quote": (False, "you asked about a discount or a guaranteed price"),
    "after_hours": (False, "you asked for a visit outside regular business hours"),
    "warranty": (False, "warranty coverage has to be confirmed by the office"),
    "reschedule_within_policy": (True, "a reschedule or cancellation within policy"),
}
# request flags (set during intake) -> the policy condition they trigger
FLAG_CONDITIONS = {"discount_or_quote": "binding_quote", "after_hours": "after_hours", "warranty_claim": "warranty"}
AGENT_TOOLS = ["search_knowledge", "check_eligibility", "upsert_customer", "add_equipment", "create_service_request",
               "check_availability", "book_job", "notify_customer"]


class PolicyViolation(RuntimeError):
    pass


@dataclass
class Decision:
    auto: bool
    conditions: list[str]
    reasons: list[str] = field(default_factory=list)  # customer-facing, for conditions that need approval
    policy_ids: list[str] = field(default_factory=list)


def ensure_policies(session: Session, tenant_id: str) -> list[ApprovalPolicy]:
    rows = list(session.scalars(select(ApprovalPolicy).where(ApprovalPolicy.tenant_id == tenant_id,
                                                              ApprovalPolicy.active.is_(True))))
    if not rows:
        rows = [ApprovalPolicy(tenant_id=tenant_id, condition=c, auto_approve=auto, approver_role="admin",
                               timeout_hours=24) for c, (auto, _) in DEFAULT_POLICIES.items()]
        session.add_all(rows)
        session.flush()
    return rows


def evaluate(session: Session, tenant_id: str, flags: list[str]) -> Decision:
    policies = {p.condition: p for p in ensure_policies(session, tenant_id)}
    conditions = sorted({FLAG_CONDITIONS[f] for f in flags if f in FLAG_CONDITIONS}) or ["standard_visit"]
    decision = Decision(auto=True, conditions=conditions)
    for condition in conditions:
        policy = policies.get(condition)
        if policy is None or not policy.auto_approve:  # an unknown condition is never auto-approved
            decision.auto = False
            decision.reasons.append(DEFAULT_POLICIES.get(condition, (False, condition))[1])
        if policy is not None:
            decision.policy_ids.append(policy.policy_id)
    return decision


def allowed(settings: Settings, tenant_id: str, tool: str) -> bool:
    """Per-tenant tool allow-list (AGENT_TOOL_ALLOWLIST JSON {"tenant": ["tool", ...]}); unlisted tenants get all."""
    allowlist = json.loads(settings.agent_tool_allowlist) if settings.agent_tool_allowlist else {}
    return tool in allowlist.get(tenant_id, AGENT_TOOLS)


def check_booking(session: Session, settings: Settings, tenant_id: str, flags: list[str], approver: str | None,
                  price: float | None, price_range: tuple[float, float] | None, outside_hours: bool) -> None:
    """Hard limits, checked right before a job is created. Raises PolicyViolation."""
    if not allowed(settings, tenant_id, "book_job"):
        raise PolicyViolation("booking is not enabled for this company")
    needs_approval = any(f in FLAG_CONDITIONS for f in flags)
    if needs_approval and not approver:
        raise PolicyViolation("this booking needs an approver")
    if outside_hours and not approver:
        raise PolicyViolation("visits outside working hours need an approver")
    if price is not None:
        if not approver:
            raise PolicyViolation("only an approver can set the price")
        if price_range and not (price_range[0] <= price <= price_range[1]):
            raise PolicyViolation(f"price must be within the published range ${price_range[0]:g}-${price_range[1]:g}")
    recent = session.scalar(select(func.count()).select_from(ToolCall).where(
        ToolCall.tenant_id == tenant_id, ToolCall.tool == "book_job",
        ToolCall.created_at >= datetime.now(timezone.utc) - timedelta(minutes=1)))
    if recent >= settings.agent_bookings_per_minute:
        raise PolicyViolation("too many bookings in the last minute; try again shortly")
