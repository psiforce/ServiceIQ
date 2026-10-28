"""M14: intake evaluation. Runs the scripted dialogues in data/eval/intake_dialogues.csv through the real agent
(guard, extraction and Q&A use the live models; the field-service platform is an in-memory stand-in).

For each dialogue the scripted customer messages are sent in order. When the agent asks for contact details,
the contact form is submitted; when the scripted messages run out at the summary, the customer replies "yes".
Checks: final outcome, extracted fields, required phrases, and that no contact detail reaches stored rows.
"""
import csv
import json
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from serviceiq.agent import Agent
from serviceiq.agent.fieldservice_client import MemoryFieldService
from serviceiq.config import Settings, load_settings
from serviceiq.storage.db import make_engine, provision_tenant, session_factory
from serviceiq.storage.models import Conversation, ServiceRequest, ToolCall

TARGETS = {"outcome_accuracy": 0.9, "completion_rate": 0.9, "field_accuracy": 0.9, "emergency_exit_rate": 1.0,
           "pii_leaks": 0}


@dataclass
class DialogueResult:
    case_id: str
    tenant: str
    expected: str
    outcome: str
    passed: bool
    customer_turns: int
    fields_expected: int
    fields_correct: int
    field_errors: list = field(default_factory=list)
    missing_phrases: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    cost_usd: float = 0.0
    transcript: list = field(default_factory=list)


def load_dialogues(data_dir: Path) -> list[dict]:
    with open(data_dir / "eval" / "intake_dialogues.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["turns"] = [t.strip() for t in row["turns"].split("||")]
        row["expected_fields"] = json.loads(row["expected_fields"] or "{}")
    return rows


def _field_ok(expected, actual) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.lower() in actual.lower()
    return expected == actual


def run_dialogue(agent: Agent, sessions, row: dict, contact: dict) -> DialogueResult:
    conv = f"eval-{row['case_id']}"
    with sessions() as s:
        s.add(Conversation(conversation_id=conv, tenant_id=row["tenant"]))
        s.commit()
    result = DialogueResult(row["case_id"], row["tenant"], row["expected_outcome"], "", False, 0, 0, 0)
    queue, history = list(row["turns"]), []
    turn = None
    for _ in range(12):  # safety limit on the number of messages
        if queue:
            message, form, confirm = queue.pop(0), None, None
        elif turn is not None and turn.needs_contact:
            message, form, confirm = "Here are my contact details.", contact, None
        elif turn is not None and turn.status == "confirming":
            message, form, confirm = "Yes, that's right. Please send it.", None, None
        else:
            break
        result.customer_turns += 1
        with sessions() as s:
            turn = agent.handle(s, row["tenant"], conv, message, contact=form, confirm=confirm, history=history)
            s.commit()
        if turn.status in ("answered", "fallback"):
            history.append((message, turn.answer))
        result.statuses.append(turn.status)
        result.cost_usd += turn.cost_usd
        result.transcript.append({"customer": message if form is None else "[contact form]", "status": turn.status,
                                  "assistant": turn.log_answer})
    result.outcome = turn.status if turn else "none"

    draft = agent.graph.get_state({"configurable": {"thread_id": conv}}).values.get("draft") or {}
    with sessions() as s:
        request = s.scalar(select(ServiceRequest).where(ServiceRequest.conversation_id == conv))
    fields = request.payload if request is not None else draft
    for key, expected in row["expected_fields"].items():
        result.fields_expected += 1
        if _field_ok(expected, fields.get(key)):
            result.fields_correct += 1
        else:
            result.field_errors.append(f"{key}: expected {expected!r}, got {fields.get(key)!r}")
    shown = " ".join(t["assistant"] for t in result.transcript)
    result.missing_phrases = [p for p in (row.get("must_contain") or "").split("|") if p and p not in shown]
    result.passed = (result.outcome == row["expected_outcome"] and not result.field_errors
                     and not result.missing_phrases)
    return result


def pii_leaks(sessions, contacts: list[dict]) -> list[str]:
    """Contact details must never appear in service requests or the audit log."""
    needles = {v for c in contacts for k, v in c.items() if k in ("name", "phone", "street") and v}
    leaks = []
    with sessions() as s:
        for r in s.scalars(select(ServiceRequest)):
            leaks += [f"service_request {r.request_id}: {n}" for n in needles if n in json.dumps(r.payload)]
        for c in s.scalars(select(ToolCall)):
            leaks += [f"tool_call {c.tool}: {n}" for n in needles if n in json.dumps(c.inputs) + json.dumps(c.result)]
    return leaks


def run_intake_eval(settings: Settings | None = None, progress=None) -> tuple[list[DialogueResult], dict]:
    base = settings or load_settings()
    work = Path(tempfile.mkdtemp(prefix="serviceiq-intake-"))
    settings = base.model_copy(update={"database_url": f"sqlite:///{(work / 'eval.db').as_posix()}",
                                       "outbox_dir": work / "outbox"})
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    with sessions() as s:
        for tenant in ("cedar-ridge", "harbor-point"):
            provision_tenant(s, tenant, tenant)
    agent = Agent(settings, fsp=MemoryFieldService(), checkpointer=InMemorySaver())
    results, contacts = [], []
    for n, row in enumerate(load_dialogues(settings.data_dir), 1):
        contact = {"name": f"Eval Customer {row['case_id']}", "phone": f"555-010-{n:04d}", "email": "",
                   "street": f"{n} Evaluation Lane", "city": "Maple Falls"}
        contacts.append(contact)
        results.append(run_dialogue(agent, sessions, row, contact))
        if progress:
            progress(results[-1])
    leaks = pii_leaks(sessions, contacts)
    return results, compute_metrics(results, leaks)


def compute_metrics(results: list[DialogueResult], leaks: list[str]) -> dict:
    def rate(rows, ok):
        return sum(1 for r in rows if ok(r)) / len(rows) if rows else None
    submit = [r for r in results if r.expected == "submitted"]
    emergencies = [r for r in results if r.expected == "emergency"]
    turns = [r.customer_turns for r in submit if r.outcome == "submitted"]
    return {
        "dialogues": len(results),
        "outcome_accuracy": rate(results, lambda r: r.outcome == r.expected),
        "pass_rate": rate(results, lambda r: r.passed),
        "completion_rate": rate(submit, lambda r: r.outcome == "submitted"),
        "field_accuracy": (sum(r.fields_correct for r in results) / max(1, sum(r.fields_expected for r in results))),
        "emergency_exit_rate": rate(emergencies, lambda r: r.outcome == "emergency"),
        "pii_leaks": len(leaks),
        "median_customer_turns_to_submit": statistics.median(turns) if turns else None,
        "avg_cost_per_dialogue": statistics.mean(r.cost_usd for r in results) if results else 0.0,
        "total_cost": sum(r.cost_usd for r in results),
        "leak_details": leaks,
    }


def write_report(results: list[DialogueResult], metrics: dict, reports_dir: Path, models: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    lines = [f"# ServiceIQ intake evaluation ({stamp})", "", f"{models} · {metrics['dialogues']} scripted dialogues",
             "", "| Metric | Result | Target | Met |", "|---|---|---|---|"]
    for key, target in TARGETS.items():
        value = metrics[key]
        met = (value <= target) if key == "pii_leaks" else (value is not None and value >= target)
        shown = str(value) if key == "pii_leaks" else (f"{value:.1%}" if value is not None else "-")
        target_shown = str(target) if key == "pii_leaks" else f"≥ {target:.0%}"
        lines.append(f"| {key.replace('_', ' ')} | {shown} | {target_shown} | {'✅' if met else '❌'} |")
    lines += ["", f"Also: pass rate {metrics['pass_rate']:.1%} · median customer messages to submit "
                  f"{metrics['median_customer_turns_to_submit']} · average cost per dialogue "
                  f"${metrics['avg_cost_per_dialogue']:.4f} · total ${metrics['total_cost']:.3f}", "",
              "| Case | Tenant | Expected | Outcome | Turns | Fields | Passed |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.case_id} | {r.tenant} | {r.expected} | {r.outcome} | {r.customer_turns} | "
                     f"{r.fields_correct}/{r.fields_expected} | {'✅' if r.passed else '❌'} |")
    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "## Needs attention", ""]
        for r in failed:
            lines.append(f"**{r.case_id}** expected {r.expected}, got {r.outcome} (statuses: {' → '.join(r.statuses)})")
            lines += [f"- {e}" for e in r.field_errors] + [f"- missing phrase: {p}" for p in r.missing_phrases]
            for t in r.transcript:
                lines.append(f"  - Customer: {t['customer']}")
                lines.append(f"    - Assistant [{t['status']}]: {t['assistant'][:400]}".replace("\n", " "))
            lines.append("")
    if metrics["leak_details"]:
        lines += ["", "## Contact-detail leaks", ""] + [f"- {x}" for x in metrics["leak_details"]]
    reports_dir.mkdir(exist_ok=True)
    out = reports_dir / f"intake_{stamp}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(reports_dir / f"intake_{stamp}.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["case_id", "tenant", "expected", "outcome", "passed", "turns", "fields_correct",
                         "fields_expected", "statuses", "cost_usd"])
        for r in results:
            writer.writerow([r.case_id, r.tenant, r.expected, r.outcome, r.passed, r.customer_turns,
                             r.fields_correct, r.fields_expected, " > ".join(r.statuses), f"{r.cost_usd:.5f}"])
    return out
