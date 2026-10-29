"""Phase 4 evaluation: the post-service scenarios (P01-P15) of the golden dataset.

Each scenario books a visit through the real agent and the in-process field-service platform (one scripted
booking dialogue), then plays its events in order: the technician completes the job, the customer pays (Stripe
events from the in-memory provider), an admin approves or records a Zelle payment, the customer asks how to pay or
leaves a review, the hourly sweep runs. Then it checks, against data/eval/postservice_scenarios.csv:
  - the conversation's final stage, the invoice (status, amount, method), the tickets opened
  - the trajectory: the audited money and review tools, in order, with nothing extra
  - phrases the customer must see (the review link, the payment options)
Run-wide checks (integrity, privacy) are computed by `postservice_checks`.
"""
import csv
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from serviceiq.agent import Agent
from serviceiq.agent.sweep import sweep_payments
from serviceiq.api.app import AUTO_ESCALATE
from serviceiq.escalation import open_ticket
from serviceiq.storage.models import Approval, Conversation, Escalation, Invoice, Review, ServiceRequest, ToolCall

MONEY_TOOLS = ("create_invoice", "send_payment_link", "record_payment", "request_review", "record_review")
BOOKINGS = {  # scripted booking dialogues (all pass in the intake evaluation)
    ("cedar-ridge", "standard"): ["I'd like to book a furnace tune-up for my Carrier furnace at 45804. Any weekday "
                                  "afternoon works, no rush."],
    ("harbor-point", "standard"): ["Can someone come out to service my boiler? It's not heating, 43402",
                                   "Not urgent, sometime next week, mornings"],
    ("cedar-ridge", "approval"): ["Can someone come out to fix my furnace? It's blowing cold air. 45801",
                                  "Can you give me a discount? I've been a customer for years",
                                  "Within a few days, weekday mornings"],
}


@dataclass
class ScenarioResult:
    case_id: str
    tenant: str
    scenario_type: str
    expected_stage: str
    stage: str = ""
    expected_invoice: str = ""
    invoice: str = ""
    expected_tools: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    expected_tickets: int = 0
    tickets: int = 0
    missing_phrases: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    replies: list = field(default_factory=list)
    reply_statuses: list = field(default_factory=list)  # statuses of the agent's chat replies ("ask" steps)
    expected_reply_status: str = ""
    cost_usd: float = 0.0
    passed: bool = False

    @property
    def trajectory_ok(self) -> bool:
        return self.tools == self.expected_tools

    @property
    def invoice_ok(self) -> bool:
        return self.invoice == self.expected_invoice


def load_scenarios(data_dir: Path) -> list[dict]:
    with open(data_dir / "eval" / "postservice_scenarios.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run_scenario(agent: Agent, sessions, row: dict, contact: dict, run_dialogue) -> ScenarioResult:
    case, tenant = row["case_id"], row["tenant"]
    result = ScenarioResult(case, tenant, row["scenario_type"], row["expected_stage"],
                            expected_tools=[t.strip() for t in row["expected_tools"].split(">") if t.strip()],
                            expected_tickets=int(row["expected_tickets"] or 0),
                            expected_reply_status=row.get("expected_reply_status") or "")
    approval_booking = row["booking"] == "approval"
    booking = run_dialogue(agent, sessions, {
        "case_id": case, "tenant": tenant, "turns": BOOKINGS[(tenant, row["booking"])], "expected_fields": {},
        "expected_outcome": "awaiting_approval" if approval_booking else "booked", "must_contain": "",
        "scenario_type": row["scenario_type"]}, contact)
    result.cost_usd += booking.cost_usd
    conv = f"eval-{case}"
    if approval_booking and booking.outcome == "awaiting_approval":
        with sessions() as s:
            approval = s.scalar(select(Approval).join(ServiceRequest, ServiceRequest.request_id == Approval.request_id)
                                .where(ServiceRequest.conversation_id == conv))
            agent.decide(s, approval.approval_id, "eval-admin", True, slot=approval.proposal["options"][0])
            s.commit()
    with sessions() as s:
        request = s.scalar(select(ServiceRequest).where(ServiceRequest.conversation_id == conv))
        job_id = request.job_ref if request else None
        before_calls = {c for c in s.scalars(select(ToolCall.call_id).where(ToolCall.conversation_id == conv))}
        before_tickets = s.scalar(select(func.count()).select_from(Escalation)
                                  .where(Escalation.conversation_id == conv))
    if not job_id:
        result.problems.append(f"booking failed: {booking.outcome}")
        return result
    booked_price = float(agent.fsp.get_job(tenant, job_id)["price"])

    last_event = None
    for step in [x.strip() for x in row["events"].split("||") if x.strip()]:
        name, _, arg = step.partition(":")
        with sessions() as s:
            invoice = s.scalar(select(Invoice).where(Invoice.request_id == request.request_id))
            if name == "complete":
                body = {"status": "completed", "completion_notes": "Evaluation visit"}
                if arg:
                    body["price"] = float(arg)
                agent.fsp.call(tenant, "PUT", f"/api/jobs/{job_id}", json=body)
                agent.on_job_completed(s, tenant, job_id)
            elif name == "complete_again":
                agent.on_job_completed(s, tenant, job_id)
            elif name == "approve":
                approval = s.scalar(select(Approval).where(Approval.request_id == request.request_id,
                                                           Approval.status == "pending"))
                agent.decide(s, approval.approval_id, "eval-admin", True, price=float(arg) if arg else None)
            elif name in ("pay", "ach_processing", "ach_paid", "ach_failed"):
                kind, method = {"pay": ("paid", arg or "card"), "ach_processing": ("processing", "ach"),
                                "ach_paid": ("paid", "online"), "ach_failed": ("failed", "ach")}[name]
                last_event = agent.payments.event(invoice.provider_session_id, kind, method)
                agent.on_payment(s, last_event)
            elif name == "replay":
                agent.on_payment(s, last_event)
            elif name == "zelle":
                agent.record_manual_payment(s, invoice.invoice_id, "zelle", "eval-admin")
            elif name == "ask":
                turn = agent.handle(s, tenant, conv, arg, trace_metadata={"case_id": case})
                result.replies.append(turn.answer)
                result.reply_statuses.append(turn.status)
                result.cost_usd += turn.cost_usd
                if turn.status in AUTO_ESCALATE and turn.escalation:  # what the API does with such a reply
                    open_ticket(s, tenant, s.get(Conversation, conv), None, turn.escalation["priority"],
                                turn.escalation["reason"] or turn.status)
            elif name == "review":
                rating, _, comment = arg.partition(":")
                result.replies.append(agent.record_review(s, tenant, conv, int(rating), comment)["message"])
            elif name == "sweep_payments":
                sent = invoice.sent_at.replace(tzinfo=invoice.sent_at.tzinfo or timezone.utc)
                sweep_payments(s, agent.settings, agent, sent + timedelta(days=int(arg)))
            else:
                raise ValueError(f"unknown scenario step {step!r}")
            s.commit()

    with sessions() as s:
        invoice = s.scalar(select(Invoice).where(Invoice.request_id == request.request_id))
        result.stage = agent.stage(conv)
        if invoice is not None:
            amount = invoice.amount_cents / 100
            shown = "booked" if abs(amount - booked_price) < 0.005 else f"{amount:g}"
            result.invoice = f"{invoice.status}:{shown}:{invoice.method or ''}"
        result.tools = [c.tool for c in s.scalars(select(ToolCall).where(ToolCall.conversation_id == conv)
                                                  .order_by(ToolCall.created_at))
                        if c.call_id not in before_calls and c.tool in MONEY_TOOLS]
        result.tickets = s.scalar(select(func.count()).select_from(Escalation)
                                  .where(Escalation.conversation_id == conv)) - before_tickets
        requests = s.scalar(select(func.count()).select_from(ServiceRequest)
                            .where(ServiceRequest.conversation_id == conv))
        if requests > 1:
            result.problems.append(f"a new service request was started ({requests} in the conversation)")
        review = s.scalar(select(Review).where(Review.request_id == request.request_id))
        if review is not None and review.status == "submitted":
            if not review.public_link_shown:
                result.problems.append("public review link not shown (review gating)")
            comment = review.comment or ""
            if contact["phone"] in comment or "555-010-9999" in comment:  # P15 types a phone number
                result.problems.append("phone number stored in a review comment")
    result.expected_invoice = row["expected_invoice"]
    shown = " ".join(result.replies)
    result.missing_phrases = [p for p in (row.get("must_contain") or "").split("|") if p and p not in shown]
    if result.expected_reply_status and result.reply_statuses[-1:] != [result.expected_reply_status]:
        result.problems.append(f"reply: expected {result.expected_reply_status!r}, got {result.reply_statuses[-1:]}")
    for label, expected, actual in (("stage", result.expected_stage, result.stage),
                                    ("invoice", result.expected_invoice, result.invoice),
                                    ("tools", result.expected_tools, result.tools),
                                    ("tickets", result.expected_tickets, result.tickets)):
        if expected != actual:
            result.problems.append(f"{label}: expected {expected!r}, got {actual!r}")
    result.passed = not result.problems and not result.missing_phrases
    return result


def postservice_checks(sessions, profiles_with_review_url: set[str]) -> dict:
    """Run-wide integrity: one paid record per invoice, every money action audited, no review gating."""
    with sessions() as s:
        invoices = list(s.scalars(select(Invoice)))
        calls = list(s.scalars(select(ToolCall).where(ToolCall.tool.in_(MONEY_TOOLS), ToolCall.ok.is_(True))))
        reviews = list(s.scalars(select(Review).where(Review.status == "submitted")))
    created = {(c.result or {}).get("invoice_id") for c in calls if c.tool == "create_invoice"}
    paid_records: dict = {}
    for c in calls:
        if c.tool == "record_payment" and (c.inputs or {}).get("kind") == "paid":
            paid_records[(c.inputs or {}).get("invoice")] = paid_records.get((c.inputs or {}).get("invoice"), 0) + 1
    links = {(c.inputs or {}).get("invoice") for c in calls if c.tool == "send_payment_link"}
    unaudited = sum(1 for i in invoices if i.invoice_id not in created)
    unaudited += sum(1 for i in invoices if i.status == "paid" and not paid_records.get(i.invoice_id))
    unaudited += sum(1 for i in invoices if i.payment_url and i.invoice_id not in links)
    return {"invoices": len(invoices),
            "duplicate_charges": sum(1 for n in paid_records.values() if n > 1),
            "unaudited_money_actions": unaudited,
            "review_gating_violations": sum(1 for r in reviews if r.tenant_id in profiles_with_review_url
                                            and not r.public_link_shown)}


def write_csv(results: list[ScenarioResult], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["case_id", "tenant", "scenario_type", "expected_stage", "stage", "expected_invoice", "invoice",
                         "expected_tools", "tools", "expected_tickets", "tickets", "passed", "problems", "cost_usd"])
        for r in results:
            writer.writerow([r.case_id, r.tenant, r.scenario_type, r.expected_stage, r.stage, r.expected_invoice,
                             r.invoice, " > ".join(r.expected_tools), " > ".join(r.tools), r.expected_tickets,
                             r.tickets, r.passed, "; ".join(r.problems + [f"missing: {p}" for p in r.missing_phrases]),
                             f"{r.cost_usd:.5f}"])
