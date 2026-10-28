"""The ServiceIQ agent: one LangGraph graph per conversation (Phase 2: AI service intake).

Every customer message runs one pass through the graph:

  guard ─┬─ emergency  -> fixed safety script, P1 ticket (ends any intake)
         ├─ blocked    -> guard's canned reply (sensitive data, unsafe request, off topic...)
         ├─ escalate   -> the customer asked for a person
         ├─ answer     -> the MVP Q&A pipeline (read-only tool)
         ├─ submit     -> (customer confirmed the summary) customer record + equipment in the field-service
         │                platform, service request saved, office emailed
         └─ extract -> eligibility -> next_step   (collect details, then show the summary to confirm)

The model only extracts details; the next step, eligibility and submission are decided in code. State that
must survive between messages (stage, draft, contact) is checkpointed per conversation; per-message context
(database session, the guard verdict, the reply being built) is passed at run time and never saved.
"""
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from serviceiq import observability
from serviceiq.agent import eligibility, intake
from serviceiq.agent.checkpoint import make_checkpointer
from serviceiq.agent.fieldservice_client import FieldService, make_field_service
from serviceiq.agent.schema import ServiceRequestV1, missing_contact, missing_fields, summary
from serviceiq.agent.tools import audited
from serviceiq.assistant import GUARD_STATUS
from serviceiq.config import Settings
from serviceiq.escalation import notify_service_request
from serviceiq.guardrails.guard import check_message
from serviceiq.guardrails.pii import redact
from serviceiq.llm import openai_client
from serviceiq.storage.models import ServiceRequest
from serviceiq.tenants import TenantProfile, load_profile

ACTIVE = ("collecting", "confirming")
# No heat or cooling in extreme weather: the safety script is shown, and the visit is still booked (as urgent).
# Every other emergency (gas, CO, fire, electrical) ends the conversation's intake.
COMFORT_EMERGENCIES = {"no_heat_extreme", "no_cool_extreme"}


class AgentState(TypedDict, total=False):
    stage: str               # idle | collecting | confirming | submitted | closed
    draft: dict              # service request fields (no contact details)
    contact: dict            # name, phone, email, street, city, zip: kept only until submission
    last_ask: str | None
    no_progress: int
    eligibility_checked: list | None
    notes_shown: list
    request_id: str | None
    last_turn_at: str


@dataclass
class AgentTurn:
    """What one message produced; same shape as AssistantResponse plus the intake fields."""
    status: str = "answered"
    answer: str = ""
    log_answer: str = ""     # what is stored: never contains contact details
    citations: list = field(default_factory=list)
    escalation: dict | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cost_usd: float = 0.0
    latency_ms: dict = field(default_factory=dict)
    debug: dict = field(default_factory=dict)
    stage: str = "idle"
    needs_contact: bool = False
    service_request: dict | None = None
    ticket_contact: dict | None = None  # attached to the ticket when a hand-off happens after contact was given


@dataclass
class TurnContext:
    session: Session
    tenant_id: str
    conversation_id: str
    profile: TenantProfile
    message: str
    contact_in: dict | None
    confirm: bool | None
    history: list
    verdict: Any = None
    question: str | None = None
    submit: bool = False
    safety_message: str | None = None  # a comfort emergency: shown first, then intake continues
    prefix: list = field(default_factory=list)
    out: AgentTurn = field(default_factory=AgentTurn)

    def add_cost(self, usage) -> None:
        if usage is not None and usage.cost:
            self.out.cost_usd += usage.cost


def _ctx(config) -> TurnContext:
    return config["configurable"]["ctx"]


class Agent:
    def __init__(self, settings: Settings, assistant=None, client=None, fsp: FieldService | None = None,
                 checkpointer=None):
        self.settings = settings
        if client is None:
            observability.configure(settings)
            client = observability.wrap_client(settings, openai_client(settings))
        self.client, self._assistant, self._fsp = client, assistant, fsp
        self.checkpointer = checkpointer or make_checkpointer(settings)
        self.graph = self._build().compile(checkpointer=self.checkpointer)

    @property
    def assistant(self):
        if self._assistant is None:  # the Q&A pipeline is only built when a question needs it
            from serviceiq.assistant import Assistant
            self._assistant = Assistant(self.settings, client=self.client)
        return self._assistant

    @property
    def fsp(self) -> FieldService:
        if self._fsp is None:
            self._fsp = make_field_service(self.settings)
        return self._fsp

    # ------------------------------------------------------------ entry points
    def handle(self, session: Session, tenant_id: str, conversation_id: str, message: str,
               contact: dict | None = None, confirm: bool | None = None,
               history: list[tuple[str, str]] | None = None) -> AgentTurn:
        started = time.perf_counter()
        ctx = TurnContext(session=session, tenant_id=tenant_id, conversation_id=conversation_id,
                          profile=load_profile(self.settings.data_dir, tenant_id), message=message,
                          contact_in=contact, confirm=confirm, history=history or [])
        config = {"configurable": {"thread_id": conversation_id, "ctx": ctx}}
        self.graph.invoke({"last_turn_at": datetime.now(timezone.utc).isoformat()}, config)
        out = ctx.out
        if ctx.safety_message:  # the safety steps come first and the P1 ticket stands, whatever intake replied
            out.answer = f"{ctx.safety_message}\n\n{out.answer}"
            out.log_answer = f"{ctx.safety_message}\n\n{out.log_answer}" if out.log_answer else ""
            out.status = "emergency"
            out.escalation = {"priority": "P1", "reason": ctx.verdict.emergency_type}
        out.stage = self.graph.get_state(config).values.get("stage", "idle")
        out.log_answer = redact(out.log_answer or out.answer)
        out.latency_ms["total"] = round((time.perf_counter() - started) * 1000)
        return out

    def stage(self, conversation_id: str) -> str:
        return self.graph.get_state({"configurable": {"thread_id": conversation_id}}).values.get("stage", "idle")

    def forget(self, conversation_id: str) -> None:
        """Delete a conversation's saved state (retention purge)."""
        self.checkpointer.delete_thread(conversation_id)

    # ------------------------------------------------------------ graph
    def _build(self) -> StateGraph:
        g = StateGraph(AgentState)
        for name in ("guard", "emergency", "blocked", "escalate", "answer", "extract", "eligibility", "next_step",
                     "submit"):
            g.add_node(name, getattr(self, f"_{name}"))
        g.add_edge(START, "guard")
        g.add_conditional_edges("guard", self._route, ["emergency", "blocked", "escalate", "answer", "extract",
                                                       "submit"])
        g.add_conditional_edges("emergency", lambda s, config: "extract" if _ctx(config).safety_message else END,
                                ["extract", END])
        g.add_conditional_edges("extract", lambda s, config: "submit" if _ctx(config).submit else "eligibility",
                                ["submit", "eligibility"])
        g.add_conditional_edges("eligibility", lambda s: END if s.get("stage") == "closed" else "next_step",
                                ["next_step", END])
        for name in ("blocked", "escalate", "answer", "next_step", "submit"):
            g.add_edge(name, END)
        return g

    def _guard(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        ctx.verdict = check_message(ctx.message, ctx.profile, self.client, self.settings.guard_model)
        ctx.add_cost(ctx.verdict.usage)
        ctx.out.debug["guard"] = ctx.verdict.reasons
        return {}

    def _route(self, state: AgentState, config) -> str:
        ctx, verdict = _ctx(config), _ctx(config).verdict
        stage = state.get("stage", "idle")
        active = stage in ACTIVE
        if verdict.action == "emergency":
            return "emergency"
        if active and intake.CANCEL.search(ctx.message):  # "never mind" is not a request for a person
            return "extract"
        if verdict.action == "escalate":
            return "escalate"
        # mid-intake, short answers ("Tuesday morning") can look off-topic: let the extractor read them
        if verdict.action in ("sensitive_data", "refuse_unsafe", "refuse") or (verdict.action == "off_topic"
                                                                                and not active):
            return "blocked"
        if stage == "confirming" and (ctx.confirm is True or intake.YES.match(ctx.message)):
            return "submit"
        if active or ctx.contact_in or verdict.intent == "service_request":
            return "extract"
        return "answer"

    def _emergency(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        wants_visit = state.get("stage") in ACTIVE or ctx.verdict.intent == "service_request"
        if ctx.verdict.emergency_type in COMFORT_EMERGENCIES and wants_visit:
            ctx.safety_message = ctx.verdict.message  # handle() puts it first; intake carries on as urgent
            return {}
        ctx.out.status, ctx.out.answer = "emergency", ctx.verdict.message
        ctx.out.escalation = {"priority": "P1", "reason": ctx.verdict.emergency_type}
        return {"stage": "closed"} if state.get("stage") in ACTIVE else {}

    def _blocked(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        ctx.out.status = GUARD_STATUS[ctx.verdict.action]
        ctx.out.answer = ctx.verdict.message + (f"\n\n{state['last_ask']}" if state.get("stage") in ACTIVE
                                                and state.get("last_ask") else "")
        return {}

    def _escalate(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        ctx.out.status, ctx.out.answer = "escalated", ctx.verdict.message
        ctx.out.escalation = {"priority": "P3", "reason": "customer asked for a person"}
        contact = state.get("contact") or {}
        if not missing_contact(contact):
            ctx.out.ticket_contact = contact
        return {"stage": "closed", "contact": {}} if state.get("stage") in ACTIVE else {}

    def _answer(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        response = self.assistant.ask(ctx.message, ctx.tenant_id, history=ctx.history, verdict=ctx.verdict)
        out = ctx.out
        out.status, out.answer, out.citations, out.escalation = (response.status, response.answer,
                                                                  response.citations, response.escalation)
        out.cost_usd += response.cost_usd
        out.trace_id = response.trace_id
        out.debug |= response.debug
        return {}

    def _extract(self, state: AgentState, config) -> dict:
        ctx, s = _ctx(config), self.settings
        stage = state.get("stage", "idle")
        updates: dict = {}
        draft, contact = dict(state.get("draft") or {}), dict(state.get("contact") or {})
        if stage not in ACTIVE:  # a new request starts from scratch
            draft, contact, stage = {}, {}, "collecting"
            updates = {"no_progress": 0, "eligibility_checked": None, "notes_shown": [], "request_id": None,
                       "last_ask": None}
        progress = False
        if ctx.contact_in:
            contact.update({k: v for k, v in ctx.contact_in.items() if v})
            progress = True
        for key, value in intake.contact_from_text(ctx.message).items():
            if not contact.get(key):
                contact[key], progress = value, True

        found, usage = intake.extract(self.client, s.guard_model, redact(ctx.message), draft,
                                      state.get("last_ask") if stage in ACTIVE else None, s.serviceiq_as_of_date.year)
        ctx.add_cost(usage)
        question = found.pop("customer_question")
        ctx.question = None if ctx.verdict.injection_suspected else question  # never answer an injected "question"
        confirms, cancel = found.pop("confirms"), found.pop("cancel")
        if ctx.safety_message and not found.get("urgency"):
            found["urgency"] = "urgent"
        if cancel:
            ctx.out.status = "intake_cancelled"
            ctx.out.answer = "No problem, I've stopped the request. Let me know if there's anything else I can help with."
            return updates | {"stage": "closed", "draft": {}, "contact": {}}

        new = {k: v for k, v in found.items() if draft.get(k) != v}
        draft.update(new)
        progress = progress or bool(new)
        if not draft.get("zip") and contact.get("zip"):
            draft["zip"] = contact["zip"]
        if draft.get("zip") and not contact.get("zip"):
            contact["zip"] = draft["zip"]
        if stage == "confirming" and confirms and not new and not ctx.contact_in:
            ctx.submit = True
        elif stage == "confirming" and (new or ctx.contact_in):
            stage = "collecting"  # an edit: the summary is shown again by next_step
        ctx.out.debug["extracted"] = sorted(new)
        no_progress = 0 if progress or ctx.question else state.get("no_progress", 0) + 1
        return updates | {"stage": stage, "draft": draft, "contact": contact, "no_progress": no_progress}

    def _eligibility(self, state: AgentState, config) -> dict:
        ctx, s = _ctx(config), self.settings
        draft = state.get("draft") or {}
        key = [draft.get("zip"), draft.get("equipment_type"), draft.get("brand")]
        if not (draft.get("zip") or draft.get("equipment_type")) or state.get("eligibility_checked") == key:
            return {}
        result = audited(ctx.session, ctx.tenant_id, ctx.conversation_id, "check_eligibility",
                         {"zip": key[0], "equipment_type": key[1], "brand": key[2]},
                         lambda: asdict(eligibility.check(ctx.session, s.data_dir, ctx.tenant_id,
                                                          ctx.profile.company_name, *key, s.serviceiq_as_of_date)))
        if not result["ok"]:
            ctx.out.status = "not_eligible"
            ctx.out.answer = (f"{result['reason']} I'm happy to answer other questions, or our team can call you "
                              f"back: {ctx.profile.office_phone}.")
            ctx.out.escalation = {"priority": "P3", "reason": "not eligible for a service visit"}
            return {"stage": "closed", "eligibility_checked": key, "contact": {}}
        draft = draft | {"service_area": result["coverage"], "trip_fee_usd": result["trip_fee_usd"]}
        shown = list(state.get("notes_shown") or [])
        if result["note"] and result["note"] not in shown:
            ctx.prefix.append(result["note"])
            shown.append(result["note"])
        return {"draft": draft, "eligibility_checked": key, "notes_shown": shown}

    def _next_step(self, state: AgentState, config) -> dict:
        ctx, out = _ctx(config), _ctx(config).out
        draft, contact = state.get("draft") or {}, state.get("contact") or {}
        parts = []
        if ctx.question:  # answer a question asked mid-intake, then carry on
            response = self.assistant.ask(ctx.question, ctx.tenant_id, history=ctx.history)
            out.cost_usd += response.cost_usd
            if response.status in ("answered", "fallback"):
                parts.append(response.answer)
                out.citations = response.citations
        parts += ctx.prefix

        if state.get("no_progress", 0) >= self.settings.intake_max_no_progress:
            out.status = "escalated"
            out.answer = "\n\n".join(parts + [
                f"I'm having trouble getting the details right, so I'm passing this to our team. You can also call "
                f"{ctx.profile.company_name} at {ctx.profile.office_phone} ({ctx.profile.business_hours})."])
            out.escalation = {"priority": "P3", "reason": "intake needs a person"}
            if not missing_contact(contact):
                out.ticket_contact = contact
            return {"stage": "closed", "contact": {}}

        missing = missing_fields(draft)
        if missing:
            ask = intake.next_question(missing)
            out.status, out.answer = "collecting", "\n\n".join(parts + [ask])
            return {"stage": "collecting", "last_ask": ask}
        if missing_contact(contact):
            ask = ("Thanks, that's what the technician needs. Please add your name, phone number and service "
                   "address in the form so the office can reach you.")
            out.status, out.answer, out.needs_contact = "collecting", "\n\n".join(parts + [ask]), True
            return {"stage": "collecting", "last_ask": ask}

        closing = "Is everything correct? Reply yes to send it to the office, or tell me what to change."
        out.status = "confirming"
        out.answer = "\n\n".join(parts + ["Here's your service request:", summary(draft, contact), closing])
        out.log_answer = "\n\n".join(parts + ["Here's your service request:", summary(draft), closing])
        out.service_request = {"id": None, "status": "draft", "summary": summary(draft)}
        return {"stage": "confirming", "last_ask": closing}

    def _submit(self, state: AgentState, config) -> dict:
        ctx, out, s = _ctx(config), _ctx(config).out, self.settings
        draft, contact = state.get("draft") or {}, state.get("contact") or {}
        conv = ctx.conversation_id
        try:
            ref = audited(ctx.session, ctx.tenant_id, conv, "upsert_customer", contact,
                          lambda: self.fsp.find_or_create_customer(ctx.tenant_id, contact, f"{conv}-customer"),
                          idempotency_key=f"{conv}-customer")
            asset = audited(ctx.session, ctx.tenant_id, conv, "add_equipment", draft,
                            lambda: self.fsp.add_equipment(ctx.tenant_id, ref, draft, f"{conv}-asset"),
                            idempotency_key=f"{conv}-asset")
        except Exception as exc:  # fail toward a person: the team calls the customer back
            out.status = "escalated"
            out.answer = ("I couldn't save your request automatically, so I've passed it to our team. They'll call "
                          f"you at the number you gave to book a time, or you can call {ctx.profile.office_phone}.")
            out.escalation = {"priority": "P3", "reason": "service request could not be saved automatically"}
            out.ticket_contact = contact
            out.debug["submit_error"] = type(exc).__name__
            return {"stage": "closed", "contact": {}}

        payload = ServiceRequestV1(**{k: v for k, v in draft.items() if k in ServiceRequestV1.model_fields}).model_dump()

        def create() -> dict:
            row = ServiceRequest(tenant_id=ctx.tenant_id, conversation_id=conv, customer_ref=ref,
                                 payload=payload | {"asset_ref": asset})
            ctx.session.add(row)
            ctx.session.flush()
            return {"request_id": row.request_id}

        created = audited(ctx.session, ctx.tenant_id, conv, "create_service_request", payload, create)
        request = ctx.session.get(ServiceRequest, created["request_id"])
        notify_service_request(s, ctx.profile, request, contact, summary(draft))
        reference = request.request_id[:8].upper()
        urgent_line = ctx.profile.emergency_phone or ctx.profile.office_phone
        out.status = "submitted"
        out.answer = (f"Thanks, your request is in (reference {reference}). The {ctx.profile.company_name} office "
                      "will contact you at the phone number you gave to book a time; I can't confirm appointment "
                      "times myself. If anything gets worse, such as a burning smell or water near electrical parts, "
                      f"call {urgent_line}.")
        out.service_request = {"id": request.request_id, "reference": reference, "status": "new",
                               "summary": summary(draft)}
        return {"stage": "submitted", "contact": {}, "request_id": request.request_id}
