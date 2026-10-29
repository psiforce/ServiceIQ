"""M14/M17: intake and booking evaluation. Runs the scripted dialogues in data/eval/intake_dialogues.csv through
the real agent: guard, extraction and Q&A use the live models, and the field-service platform runs in-process
on a temporary database seeded from data/tenants/*/fieldservice.json.

For each dialogue the scripted customer messages are sent in order. When the agent asks for contact details,
the contact form is submitted; at the summary the customer replies "yes"; when times are offered the customer
picks the first one. Checks: final outcome (booked, awaiting_approval, not_eligible, ...), extracted fields and
flags, required phrases, that nothing needing approval was booked, and that no contact detail reaches stored rows.

Phase 4: then the post-service scenarios (data/eval/postservice_scenarios.csv, see postservice_eval.py) book a
visit and play the events after it: completion, payment, approval, review. With the race test (R01), dialogues
and scenarios are the 50-case golden dataset; each case has a scenario_type (happy, edge, known_failure,
adversarial). `cases` runs a subset (e.g. one case, to check its LangSmith trace).
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
from serviceiq.agent.fieldservice_client import HttpFieldService
from serviceiq.config import PROJECT_ROOT, Settings, load_settings
from serviceiq.storage.db import make_engine, provision_tenant, session_factory
from serviceiq.evaluation import postservice_eval
from serviceiq.storage.models import Approval, Conversation, ServiceRequest, ToolCall
from serviceiq.tenants import load_profile

TARGETS = {"outcome_accuracy": 0.9, "completion_rate": 0.9, "field_accuracy": 0.9, "emergency_exit_rate": 1.0,
           "approval_routing": 1.0, "race_handled": 1.0, "pii_leaks": 0, "policy_violations": 0, "double_bookings": 0,
           "unaudited_jobs": 0, "agent_seconds_to_book_p95": 300,
           # Phase 4: post-service scenarios
           "postservice_pass_rate": 0.9, "trajectory_accuracy": 1.0, "invoice_accuracy": 1.0, "duplicate_charges": 0,
           "unaudited_money_actions": 0, "review_gating_violations": 0}
# counts and times that must stay at or below their target
AT_MOST = {"pii_leaks", "policy_violations", "double_bookings", "unaudited_jobs", "agent_seconds_to_book_p95",
           "duplicate_charges", "unaudited_money_actions", "review_gating_violations"}


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
    agent_seconds: float = 0.0  # the agent's own processing time (scripted customers reply instantly)
    transcript: list = field(default_factory=list)
    scenario_type: str = ""


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
    result = DialogueResult(row["case_id"], row["tenant"], row["expected_outcome"], "", False, 0, 0, 0,
                            scenario_type=row.get("scenario_type") or "")
    trace = {"case_id": row["case_id"], "scenario_type": result.scenario_type}  # LangSmith trace metadata
    queue, history = list(row["turns"]), []
    turn = None
    for _ in range(12):  # safety limit on the number of messages
        # "@proposing the Thursday one": a turn that waits until the agent reaches that status (or stage);
        # until then the scripted customer answers as usual (contact form, "yes", option 1)
        waiting = queue and queue[0].startswith("@")
        ready = waiting and turn is not None and queue[0][1:].split(" ", 1)[0] in (turn.status, turn.stage)
        if queue and (not waiting or ready):
            message, form, confirm = queue.pop(0).split(" ", 1)[1] if ready else queue.pop(0), None, None
        elif turn is not None and turn.needs_contact:
            message, form, confirm = "Here are my contact details.", contact, None
        elif turn is not None and turn.status == "confirming":
            message, form, confirm = "Yes, that's right. Please send it.", None, None
        elif turn is not None and turn.status == "proposing":
            message, form, confirm = "1", None, None
        else:
            break
        result.customer_turns += 1
        with sessions() as s:
            turn = agent.handle(s, row["tenant"], conv, message, contact=form, confirm=confirm, history=history,
                                trace_metadata=trace)
            s.commit()
        if turn.status in ("answered", "fallback"):
            history.append((message, turn.answer))
        result.statuses.append(turn.status)
        result.cost_usd += turn.cost_usd
        result.agent_seconds += turn.latency_ms.get("total", 0) / 1000
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


def local_field_service(work: Path) -> HttpFieldService:
    """The real field-service API, in-process, on a fresh database with each tenant's contractor config."""
    from fastapi.testclient import TestClient

    from fieldservice.app import create_app
    from fieldservice.config import FSPSettings
    from fieldservice.db import add_org, make_engine as fsp_engine, session_factory as fsp_sessions

    url = f"sqlite:///{(work / 'fsp.db').as_posix()}"
    engine = fsp_engine(url)
    keys = {}
    for config_path in sorted((PROJECT_ROOT / "data" / "tenants").glob("*/fieldservice.json")):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        keys[config["org_id"]] = add_org(fsp_sessions(engine), config)
    return HttpFieldService("", keys, http=TestClient(create_app(FSPSettings(fsp_database_url=url), engine=engine)))


def run_intake_eval(settings: Settings | None = None, progress=None, cases: set[str] | None = None,
                    post_progress=None) -> tuple[list[DialogueResult], dict, list]:
    base = settings or load_settings()
    work = Path(tempfile.mkdtemp(prefix="serviceiq-intake-"))
    settings = base.model_copy(update={"database_url": f"sqlite:///{(work / 'eval.db').as_posix()}",
                                       "outbox_dir": work / "outbox"})
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    with sessions() as s:
        for tenant in ("cedar-ridge", "harbor-point"):
            provision_tenant(s, tenant, tenant)
    agent = Agent(settings, fsp=local_field_service(work), checkpointer=InMemorySaver())
    results, contacts = [], []
    for n, row in enumerate(load_dialogues(settings.data_dir), 1):
        if cases and row["case_id"] not in cases:
            continue
        contact = {"name": f"Eval Customer {row['case_id']}", "phone": f"555-010-{n:04d}", "email": "",
                   "street": f"{n} Evaluation Lane", "city": "Maple Falls"}
        contacts.append(contact)
        results.append(run_dialogue(agent, sessions, row, contact))
        if progress:
            progress(results[-1])
    post = []
    for n, row in enumerate(postservice_eval.load_scenarios(settings.data_dir), 1):
        if cases and row["case_id"] not in cases:
            continue
        contact = {"name": f"Eval Customer {row['case_id']}", "phone": f"555-011-{n:04d}",
                   "email": f"{row['case_id'].lower()}@example.test", "street": f"{n} Evaluation Court",
                   "city": "Maple Falls"}
        contacts.append(contact)
        post.append(postservice_eval.run_scenario(agent, sessions, row, contact, run_dialogue))
        if post_progress:
            post_progress(post[-1])
    leaks = pii_leaks(sessions, contacts)
    metrics = compute_metrics(results, leaks, policy_violations(sessions))
    if not cases or "R01" in cases:
        race = run_race(agent, sessions)
        metrics |= {"race_handled": 1.0 if race["handled"] else 0.0, "race_detail": race}
    metrics |= calendar_checks(sessions, agent.fsp)
    if post:
        with_review_page = {t for t in ("cedar-ridge", "harbor-point") if load_profile(settings.data_dir, t).review_url}
        metrics |= {"postservice_pass_rate": sum(r.passed for r in post) / len(post),
                    "trajectory_accuracy": sum(r.trajectory_ok for r in post) / len(post),
                    "invoice_accuracy": sum(r.invoice_ok for r in post) / len(post),
                    "postservice_cost": sum(r.cost_usd for r in post)}
        metrics |= postservice_eval.postservice_checks(sessions, with_review_page)
    return results, metrics, post


RACE_MESSAGES = ["My furnace is blowing cold air, can someone come out? 45801", "It's a Carrier, no rush, any time",
                 "Here are my contact details.", "Yes, please send it."]


def run_race(agent: Agent, sessions) -> dict:
    """Two customers are offered the same times; both pick option 1. The second must be offered fresh times
    (the platform refuses the double booking), then book one of those."""
    convs = {"race-a": {"name": "Race A", "phone": "555-010-7001", "street": "1 Race St", "city": "Maple Falls"},
             "race-b": {"name": "Race B", "phone": "555-010-7002", "street": "2 Race St", "city": "Maple Falls"}}
    turns = {}
    for conv, contact in convs.items():
        with sessions() as s:
            s.add(Conversation(conversation_id=conv, tenant_id="cedar-ridge"))
            s.commit()
        for message in RACE_MESSAGES:
            with sessions() as s:
                turns[conv] = agent.handle(s, "cedar-ridge", conv, message,
                                           contact=contact if "contact" in message else None)
                s.commit()
    offered = {conv: turns[conv].options[:1] for conv in convs}
    statuses = []
    for conv in convs:
        with sessions() as s:
            turn = agent.handle(s, "cedar-ridge", conv, "1")
            s.commit()
        statuses.append(turn.status)
    second = turn
    if second.status == "proposing":
        with sessions() as s:
            second = agent.handle(s, "cedar-ridge", "race-b", "1")
            s.commit()
        statuses.append(second.status)
    same_first = offered["race-a"] == offered["race-b"]
    handled = statuses[0] == "booked" and second.status == "booked" and (
        not same_first or statuses[1] == "proposing")
    return {"handled": handled, "same_first_option": same_first, "statuses": statuses}


def calendar_checks(sessions, fsp) -> dict:
    """Read the platform's schedule back: no technician double-booked, and every job has an audited booking.
    Only active jobs can clash: a completed job frees its time (the platform's rule), and the post-service scenarios
    complete jobs that are still in the future, so later bookings may legitimately reuse those times."""
    from datetime import date, timedelta

    from serviceiq.storage.models import ToolCall
    active = {"scheduled", "confirmed", "in_progress"}
    jobs = []
    for tenant in fsp.keys:
        code, body = fsp.call(tenant, "GET", "/api/schedule", params={
            "date_from": (date.today() - timedelta(days=2)).isoformat(),
            "date_to": (date.today() + timedelta(days=28)).isoformat()})
        jobs += [j | {"tenant": tenant} for j in body.get("jobs", [])] if code == 200 else []
    overlaps = 0
    by_tech: dict = {}
    for j in (j for j in jobs if j["status"] in active):
        by_tech.setdefault((j["tenant"], j["technician_id"]), []).append(j)
    for tech_jobs in by_tech.values():
        tech_jobs.sort(key=lambda j: j["scheduled_start"])
        for a, b in zip(tech_jobs, tech_jobs[1:]):
            from datetime import datetime
            end_a = datetime.fromisoformat(a["scheduled_start"]) + timedelta(minutes=a["duration"])
            overlaps += end_a > datetime.fromisoformat(b["scheduled_start"])
    with sessions() as s:
        audited = {(c.result or {}).get("job_id") for c in s.scalars(select(ToolCall).where(
            ToolCall.tool == "book_job", ToolCall.ok.is_(True)))}
    return {"jobs_booked": len(jobs), "double_bookings": overlaps,
            "unaudited_jobs": sum(1 for j in jobs if j["id"] not in audited)}


def policy_violations(sessions) -> list[str]:
    """Requests that needed an approver (discount, warranty, after hours) but were booked without one."""
    with sessions() as s:
        approved = set(s.scalars(select(Approval.request_id).where(Approval.status == "approved")))
        return [r.request_id for r in s.scalars(select(ServiceRequest))
                if r.payload.get("flags") and r.job_ref and r.request_id not in approved]


def compute_metrics(results: list[DialogueResult], leaks: list[str], violations: list[str] | None = None) -> dict:
    def rate(rows, ok):
        return sum(1 for r in rows if ok(r)) / len(rows) if rows else None
    submit = [r for r in results if r.expected == "booked"]
    approvals = [r for r in results if r.expected == "awaiting_approval"]
    emergencies = [r for r in results if r.expected == "emergency"]
    turns = [r.customer_turns for r in submit if r.outcome == "booked"]
    seconds = sorted(r.agent_seconds for r in submit if r.outcome == "booked")
    return {
        "dialogues": len(results),
        "outcome_accuracy": rate(results, lambda r: r.outcome == r.expected),
        "pass_rate": rate(results, lambda r: r.passed),
        "completion_rate": rate(submit, lambda r: r.outcome == "booked"),
        "approval_routing": rate(approvals, lambda r: r.outcome == "awaiting_approval"),
        "policy_violations": len(violations or []),
        "field_accuracy": (sum(r.fields_correct for r in results) / max(1, sum(r.fields_expected for r in results))),
        "emergency_exit_rate": rate(emergencies, lambda r: r.outcome == "emergency"),
        "pii_leaks": len(leaks),
        "median_customer_turns_to_book": statistics.median(turns) if turns else None,
        "agent_seconds_to_book_median": round(statistics.median(seconds), 1) if seconds else None,
        "agent_seconds_to_book_p95": round(seconds[max(0, round(0.95 * len(seconds)) - 1)], 1) if seconds else None,
        "avg_cost_per_dialogue": statistics.mean(r.cost_usd for r in results) if results else 0.0,
        "total_cost": sum(r.cost_usd for r in results),
        "leak_details": leaks,
    }


def write_report(results: list[DialogueResult], metrics: dict, reports_dir: Path, models: str,
                 post: list | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    post = post or []
    lines = [f"# ServiceIQ intake evaluation ({stamp})", "",
             f"{models} · {metrics['dialogues']} scripted dialogues · {len(post)} post-service scenarios",
             "", "| Metric | Result | Target | Met |", "|---|---|---|---|"]
    for key, target in TARGETS.items():
        if key not in metrics:  # that part was skipped (a subset of cases)
            continue
        value = metrics[key]
        met = value is not None and (value <= target if key in AT_MOST else value >= target)
        shown = "-" if value is None else str(value) if key in AT_MOST else f"{value:.1%}"
        target_shown = str(target) if key in AT_MOST else f"≥ {target:.0%}"
        lines.append(f"| {key.replace('_', ' ')} | {shown} | {target_shown} | {'✅' if met else '❌'} |")
    pass_rate = f"{metrics['pass_rate']:.1%}" if metrics["pass_rate"] is not None else "-"
    lines += ["", f"Also: dialogue pass rate {pass_rate} · median customer messages to book "
                  f"{metrics['median_customer_turns_to_book']} · agent time to book: median "
                  f"{metrics['agent_seconds_to_book_median']} s · jobs booked {metrics['jobs_booked']} · race "
                  f"{metrics.get('race_detail', {}).get('statuses', 'not run')} · average cost per dialogue "
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
    if post:
        lines += ["", "## Post-service scenarios (Phase 4)", "",
                  f"Invoices {metrics['invoices']} · post-service model cost ${metrics['postservice_cost']:.3f}", "",
                  "| Case | Type | Expected stage | Stage | Invoice | Tools match | Tickets | Passed |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in post:
            lines.append(f"| {r.case_id} | {r.scenario_type} | {r.expected_stage} | {r.stage} | {r.invoice or '-'} | "
                         f"{'✅' if r.trajectory_ok else '❌'} | {r.tickets}/{r.expected_tickets} | "
                         f"{'✅' if r.passed else '❌'} |")
        for r in (r for r in post if not r.passed):
            lines += ["", f"**{r.case_id}**"] + [f"- {p}" for p in r.problems]
            lines += [f"- missing phrase: {p}" for p in r.missing_phrases]
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
    if post:
        postservice_eval.write_csv(post, reports_dir / f"postservice_{stamp}.csv")
    # everything, case by case (transcripts, replies, scores): input for reports, judges and spreadsheets
    from dataclasses import asdict
    (reports_dir / f"intake_{stamp}.json").write_text(json.dumps(
        {"stamp": stamp, "models": models, "metrics": {k: v for k, v in metrics.items() if k != "leak_details"},
         "dialogues": [asdict(r) for r in results],
         "postservice": [asdict(r) | {"trajectory_ok": r.trajectory_ok, "invoice_ok": r.invoice_ok} for r in post]},
        indent=1, default=str), encoding="utf-8")
    return out


# ---------------------------------------------------------------- LangSmith (agent golden dataset)
DATASET = "serviceiq-agent-golden"


def golden_examples(data_dir: Path) -> list[dict]:
    """All 50 golden cases as LangSmith examples: the dialogues, the race test and the post-service scenarios."""
    examples = [{"inputs": {"case_id": r["case_id"], "tenant": r["tenant"], "turns": r["turns"]},
                 "outputs": {"expected_outcome": r["expected_outcome"], "expected_fields": r["expected_fields"],
                             "must_contain": r.get("must_contain") or ""},
                 "metadata": {"scenario_type": r.get("scenario_type") or "", "tenant": r["tenant"],
                              "notes": r.get("notes") or ""}} for r in load_dialogues(data_dir)]
    examples.append({"inputs": {"case_id": "R01", "tenant": "cedar-ridge", "turns": RACE_MESSAGES},
                     "outputs": {"expected_outcome": "one booked, one re-offered; no double booking"},
                     "metadata": {"scenario_type": "known_failure", "tenant": "cedar-ridge",
                                  "notes": "Two customers book the same slot"}})
    examples += [{"inputs": {"case_id": r["case_id"], "tenant": r["tenant"], "booking": r["booking"],
                             "events": r["events"]},
                  "outputs": {"expected_outcome": r["expected_stage"], "expected_invoice": r["expected_invoice"],
                              "expected_tools": r["expected_tools"], "expected_tickets": r["expected_tickets"],
                              "must_contain": r.get("must_contain") or ""},
                  "metadata": {"scenario_type": r["scenario_type"], "tenant": r["tenant"], "notes": r["notes"]}}
                 for r in postservice_eval.load_scenarios(data_dir)]
    return examples


def load_case_list(path: Path) -> set[str]:
    """Case IDs from a list file (one per line, # comments), e.g. data/eval/golden_v2.txt."""
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


def upload_to_langsmith(results: list[DialogueResult], post: list, metrics: dict, data_dir: Path, api_key: str,
                        prompt_version: str, dataset_name: str = DATASET, cases: set[str] | None = None) -> str:
    """Log this run as an experiment on the agent golden dataset (created on first use), one row per case with
    the same scores as the markdown report. Live per-turn traces (redacted) come from the agent itself when
    LANGSMITH_TRACING=true, tagged with each case ID. Returns the experiment name."""
    from langsmith import Client, evaluate

    client = Client(api_key=api_key)
    if not client.has_dataset(dataset_name=dataset_name):
        dataset = client.create_dataset(dataset_name, description="ServiceIQ agent golden dataset: intake, "
                                                                  "booking, payment and review")
        client.create_examples(dataset_id=dataset.id, examples=[e for e in golden_examples(data_dir) if not cases
                                                                or e["inputs"]["case_id"] in cases])
    outputs = {r.case_id: {"outcome": r.outcome, "passed": r.passed, "scores": {
        "passed": float(r.passed), "outcome_correct": float(r.outcome == r.expected),
        "field_accuracy": r.fields_correct / r.fields_expected if r.fields_expected else 1.0,
        "agent_seconds": r.agent_seconds, "cost_usd": r.cost_usd}} for r in results}
    outputs |= {r.case_id: {"outcome": r.stage, "invoice": r.invoice, "tools": r.tools, "passed": r.passed,
                            "problems": r.problems, "scores": {
                                "passed": float(r.passed), "outcome_correct": float(r.stage == r.expected_stage),
                                "trajectory_ok": float(r.trajectory_ok), "invoice_ok": float(r.invoice_ok),
                                "cost_usd": r.cost_usd}} | ({"replies": r.replies} if r.replies else {})
                for r in post}
    if "race_detail" in metrics:
        outputs["R01"] = {"outcome": " > ".join(metrics["race_detail"]["statuses"]), "passed": metrics["race_handled"]
                          == 1.0, "scores": {"passed": metrics["race_handled"], "outcome_correct": metrics["race_handled"]}}
    examples = [e for e in client.list_examples(dataset_name=dataset_name) if e.inputs.get("case_id") in outputs]

    def target(inputs: dict) -> dict:  # replays what this run produced
        return outputs[inputs["case_id"]]

    def scores(run, example) -> dict:
        return {"results": [{"key": k, "score": v} for k, v in run.outputs["scores"].items()]}

    summary = {k: v for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    experiment = evaluate(target, data=examples, evaluators=[scores], client=client, max_concurrency=4,
                          experiment_prefix=f"serviceiq-agent-{prompt_version}",
                          metadata={"prompt_version": prompt_version, **summary})
    return experiment.experiment_name
