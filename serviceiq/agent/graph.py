"""The ServiceIQ agent: one LangGraph graph per conversation (Phase 2 intake + Phase 3 operations).

Every customer message runs one pass through the graph:

  guard ─┬─ emergency  -> fixed safety script, P1 ticket (ends any intake)
         ├─ blocked    -> guard's canned reply (sensitive data, unsafe request, off topic...)
         ├─ escalate   -> the customer asked for a person
         ├─ answer     -> the MVP Q&A pipeline (read-only tool)
         ├─ submit     -> (customer confirmed the summary) customer record + equipment in the field-service
         │                platform, service request saved, office emailed; then the policy layer decides:
         │                  within policy -> offer up to 3 free times   (stage choosing_slot)
         │                  needs approval -> Approval row, admins emailed (stage awaiting_approval)
         ├─ choose     -> the customer picks a time -> job booked in the platform, confirmation email
         └─ extract -> eligibility -> next_step   (collect details, then show the summary to confirm)

An approver's decision arrives from the admin console through `Agent.decide`, which books (or declines) and
moves the conversation's saved state on, so the customer can keep chatting while a request waits.

The model only extracts details; the next step, eligibility, policy and booking are decided in code. State that
must survive between messages (stage, draft, contact, offered slots) is checkpointed per conversation;
per-message context (database session, the guard verdict, the reply being built) is passed at run time.
"""
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from serviceiq import observability
from serviceiq.agent import eligibility, intake, operations, policy
from serviceiq.agent.checkpoint import make_checkpointer
from serviceiq.agent.fieldservice_client import FieldService, SlotTaken, make_field_service
from serviceiq.agent.schema import ServiceRequestV1, missing_contact, missing_fields, summary
from serviceiq.agent.tools import audited
from serviceiq.assistant import GUARD_STATUS
from serviceiq.config import Settings
from serviceiq.escalation import notify, notify_service_request, open_ticket, send
from serviceiq.guardrails.guard import check_message
from serviceiq.guardrails.pii import redact
from serviceiq.llm import openai_client
from serviceiq.storage.models import Approval, Conversation, ServiceRequest
from serviceiq.tenants import TenantProfile, load_profile

ACTIVE = ("collecting", "confirming")  # intake in progress
CHOOSING = "choosing_slot"             # times offered, waiting for the customer to pick one
# No heat or cooling in extreme weather: the safety script is shown, and the visit is still booked (as urgent).
# Every other emergency (gas, CO, fire, electrical) ends the conversation's intake.
COMFORT_EMERGENCIES = {"no_heat_extreme", "no_cool_extreme"}


class AgentState(TypedDict, total=False):
    # idle | collecting | confirming | choosing_slot | awaiting_approval | booked | submitted | closed
    stage: str
    draft: dict              # service request fields (no contact details)
    contact: dict            # name, phone, email, street, city, zip: kept only until submission
    last_ask: str | None
    no_progress: int
    eligibility_checked: list | None
    notes_shown: list
    request_id: str | None
    slot_options: list       # the times offered (start, local_start, technician, label)
    service: dict | None     # the field-service service type being booked
    price_line: str | None
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
    options: list = field(default_factory=list)  # visit times offered (labels; the customer replies 1, 2 or 3)
    booking: dict | None = None


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
                     "submit", "choose"):
            g.add_node(name, getattr(self, f"_{name}"))
        g.add_edge(START, "guard")
        g.add_conditional_edges("guard", self._route, ["emergency", "blocked", "escalate", "answer", "extract",
                                                       "submit", "choose"])
        g.add_conditional_edges("emergency", lambda s, config: "extract" if _ctx(config).safety_message else END,
                                ["extract", END])
        g.add_conditional_edges("extract", lambda s, config: "submit" if _ctx(config).submit else "eligibility",
                                ["submit", "eligibility"])
        g.add_conditional_edges("eligibility", lambda s: END if s.get("stage") == "closed" else "next_step",
                                ["next_step", END])
        for name in ("blocked", "escalate", "answer", "next_step", "submit", "choose"):
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
        active = stage in ACTIVE or stage == CHOOSING  # short replies ("1", "Tuesday") are expected here
        if verdict.action == "emergency":
            return "emergency"
        if active and intake.CANCEL.search(ctx.message):  # "never mind" is not a request for a person
            return "choose" if stage == CHOOSING else "extract"
        if verdict.action == "escalate":
            return "escalate"
        # mid-intake, short answers ("Tuesday morning") can look off-topic: let the extractor read them
        if verdict.action in ("sensitive_data", "refuse_unsafe", "refuse") or (verdict.action == "off_topic"
                                                                                and not active):
            return "blocked"
        if stage == "confirming" and (ctx.confirm is True or intake.YES.match(ctx.message)):
            return "submit"
        if stage == CHOOSING:
            return "choose"
        if stage in ACTIVE or ctx.contact_in or verdict.intent == "service_request":
            return "extract"
        return "answer"

    def _emergency(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        wants_visit = state.get("stage") in ACTIVE + (CHOOSING,) or ctx.verdict.intent == "service_request"
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
        found.pop("slot_choice")
        flags = found.pop("flags")
        if flags:  # discount, warranty or after-hours asks: the request will need an approver (Phase 3)
            draft["flags"] = sorted(set(draft.get("flags") or []) | set(flags))
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
        reference = request.request_id[:8].upper()
        out.service_request = {"id": request.request_id, "reference": reference, "status": "new",
                               "summary": summary(draft)}
        done = {"contact": {}, "request_id": request.request_id}

        # Phase 3: within policy -> offer times; otherwise -> an approver decides
        booking = s.agent_booking_enabled and policy.allowed(s, ctx.tenant_id, "book_job")
        decision = policy.evaluate(ctx.session, ctx.tenant_id, payload.get("flags") or []) if booking else None
        service, options, price_line = None, [], None
        if booking:
            try:
                service, options = audited(ctx.session, ctx.tenant_id, conv, "check_availability",
                                           {"zip": draft.get("zip"), "equipment_type": draft.get("equipment_type")},
                                           lambda: operations.propose(self.fsp, s, ctx.tenant_id, draft, self._today()))
                price = operations.published_price(ctx.session, s, ctx.tenant_id, service["code"]) if service else None
                price_line = operations.price_text(price, draft.get("trip_fee_usd"))
            except Exception as exc:  # the platform can't offer times: the office books, as in Phase 2
                out.debug["availability_error"] = type(exc).__name__
        next_step = ("The assistant offered the customer available times; a booking will appear in the Schedule tab."
                     if decision and decision.auto and options else
                     "Waiting for an approver in the admin console (Approvals tab)." if decision and not decision.auto
                     else "Reply to the customer to book a time; the assistant did not book this one.")
        notify_service_request(s, ctx.profile, request, contact, summary(draft), next_step)

        if decision and not decision.auto:
            approval = Approval(tenant_id=ctx.tenant_id, request_id=request.request_id,
                                policy_id=decision.policy_ids[0] if decision.policy_ids else None,
                                reason="; ".join(decision.conditions),
                                proposal={"service": service, "options": options, "price_line": price_line})
            ctx.session.add(approval)
            request.status = "needs_approval"  # request statuses fit 16 characters
            ctx.session.flush()
            send(s, _approval_email(s, ctx.profile, request, approval, decision, draft), f"APPROVAL-{approval.approval_id}")
            out.status = "awaiting_approval"
            out.service_request["status"] = request.status
            out.answer = (f"Thanks, your request is in (reference {reference}). Because {' and '.join(decision.reasons)}, "
                          f"the {ctx.profile.company_name} office needs to review it before booking. They'll confirm "
                          f"a time with you, usually within one business day.")
            return done | {"stage": "awaiting_approval", "service": service, "price_line": price_line}

        if options:
            out.status, out.options = "proposing", [o["label"] for o in options]
            out.answer = "\n\n".join([
                f"Thanks, your request is in (reference {reference}). These are the next available times:",
                "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)),
                f"Reply 1, 2 or 3 to book, or tell me another day or time that suits you. {price_line}"])
            return done | {"stage": CHOOSING, "slot_options": options, "service": service, "price_line": price_line,
                           "no_progress": 0}

        urgent_line = ctx.profile.emergency_phone or ctx.profile.office_phone
        out.status = "submitted"
        out.answer = (f"Thanks, your request is in (reference {reference}). The {ctx.profile.company_name} office "
                      "will contact you at the phone number you gave to book a time; I can't confirm appointment "
                      "times myself. If anything gets worse, such as a burning smell or water near electrical parts, "
                      f"call {urgent_line}.")
        return done | {"stage": "submitted"}

    def _today(self):
        """Search from yesterday (UTC): in US evenings the UTC date is already tomorrow, and the platform drops
        past times itself, so nothing bookable is skipped."""
        return datetime.now(timezone.utc).date() - timedelta(days=1)

    def _choose(self, state: AgentState, config) -> dict:
        """The customer picks one of the offered times, asks for other times, or stops."""
        ctx, out, s = _ctx(config), _ctx(config).out, self.settings
        options, service = state.get("slot_options") or [], state.get("service")
        request = ctx.session.get(ServiceRequest, state.get("request_id"))
        draft = dict(state.get("draft") or {})
        reference = request.request_id[:8].upper() if request else ""
        if intake.CANCEL.search(ctx.message):
            out.status = "submitted"
            out.answer = (f"No problem. Your request (reference {reference}) stays with the office, and they'll call "
                          "you to find a time.")
            return {"stage": "submitted"}

        choice = intake.slot_choice(ctx.message)
        windows = None
        if choice is None:
            found, usage = intake.extract(self.client, s.guard_model, redact(ctx.message), draft,
                                          state.get("last_ask"), s.serviceiq_as_of_date.year)
            ctx.add_cost(usage)
            choice, windows = found.get("slot_choice"), found.get("preferred_windows")
        if choice is None or not 1 <= choice <= len(options):
            if windows:  # "Friday afternoon instead": offer times that match
                draft["preferred_windows"] = windows
                _, options = operations.propose(self.fsp, s, ctx.tenant_id, draft, self._today())
            no_progress = 0 if windows else state.get("no_progress", 0) + 1
            if not options or no_progress >= s.intake_max_no_progress:
                out.status = "submitted"
                out.answer = (f"I'll leave this with the office: they'll call you to find a time for request "
                              f"{reference}.")
                return {"stage": "submitted"}
            out.status, out.options = "proposing", [o["label"] for o in options]
            out.answer = "\n\n".join(["Here are the times I can offer:",
                                      "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1)),
                                      "Reply 1, 2 or 3 to book."])
            return {"slot_options": options, "draft": draft, "no_progress": no_progress}

        slot = options[choice - 1]
        try:
            booked = audited(ctx.session, ctx.tenant_id, ctx.conversation_id, "book_job",
                             {"slot": slot["start"], "service": service["code"], "request": request.request_id},
                             lambda: operations.book(ctx.session, self.fsp, s, ctx.tenant_id, request, slot, service),
                             idempotency_key=f"job-{request.request_id}-{slot['start']}")
        except SlotTaken:
            _, fresh = operations.propose(self.fsp, s, ctx.tenant_id, draft, self._today())
            out.status, out.options = "proposing", [o["label"] for o in fresh]
            out.answer = "\n\n".join(["Sorry, that time was just taken. Here are the next available ones:",
                                      "\n".join(f"{i}. {o['label']}" for i, o in enumerate(fresh, 1)),
                                      "Reply 1, 2 or 3 to book."])
            return {"slot_options": fresh}
        except Exception as exc:  # fail toward a person
            out.status = "escalated"
            out.answer = (f"I couldn't book that time automatically, so I've passed request {reference} to our team; "
                          f"they'll call you to confirm a time. You can also call {ctx.profile.office_phone}.")
            out.escalation = {"priority": "P3", "reason": "booking failed; call the customer to schedule"}
            out.debug["booking_error"] = f"{type(exc).__name__}: {exc}"
            return {"stage": "closed"}

        emailed = self._confirm_to_customer(ctx.session, ctx.tenant_id, ctx.profile, request, booked, service,
                                            state.get("price_line") or "")
        out.status, out.booking = "booked", booked
        out.service_request = {"id": request.request_id, "reference": reference, "status": "scheduled",
                               "summary": summary(draft), "job": booked["identifier"]}
        technician = (booked.get("technician_name") or "a technician").split()[0]
        out.answer = (f"You're booked for {slot['label']} with {technician} (job {booked['identifier']}). "
                      + (f"A confirmation is on its way to {emailed}. " if emailed else "")
                      + f"To change or cancel, call {ctx.profile.office_phone}.")
        out.log_answer = redact(out.answer)
        return {"stage": "booked"}

    def _confirm_to_customer(self, session, tenant_id, profile, request, booked, service, price_line) -> str | None:
        """Email the confirmation when the customer gave an email address (looked up in the platform)."""
        try:
            record = self.fsp.get_customer(tenant_id, request.customer_ref) or {}
            email = (record.get("customer") or {}).get("email")
            if not email:
                return None
            audited(session, tenant_id, request.conversation_id, "notify_customer",
                    {"job": booked["identifier"], "channel": "email"},
                    lambda: operations.confirmation_email(self.settings, profile, email, booked, service, price_line))
            return email
        except Exception:
            return None

    # ------------------------------------------------------------ approver decisions (admin console)
    def decide(self, session: Session, approval_id: str, approver: str, approve: bool, slot: dict | None = None,
               price: float | None = None, price_range: tuple[float, float] | None = None,
               outside_hours: bool = False, note: str = "") -> dict:
        """An admin approves (books `slot`, optionally at a `price` within the published range) or declines.
        The customer is emailed when they gave an address; otherwise a ticket asks the office to call them."""
        approval = session.get(Approval, approval_id)
        if approval is None or approval.status != "pending":
            raise ValueError("approval not found or already decided")
        request = session.get(ServiceRequest, approval.request_id)
        profile = load_profile(self.settings.data_dir, approval.tenant_id)
        service = (approval.proposal or {}).get("service")
        approval.decided_by, approval.decided_at = approver, datetime.now(timezone.utc)
        result: dict = {"approved": approve}
        if approve:
            if not slot or not service:
                raise ValueError("choose a time to book")
            booked = audited(session, approval.tenant_id, request.conversation_id, "book_job",
                             {"slot": slot["start"], "service": service["code"], "request": request.request_id,
                              "price": price, "outside_hours": outside_hours},
                             lambda: operations.book(session, self.fsp, self.settings, approval.tenant_id, request,
                                                     slot, service, approver=approver, price=price,
                                                     price_range=price_range, outside_hours=outside_hours),
                             idempotency_key=f"job-{request.request_id}-{slot['start']}", approver=approver)
            approval.status = "approved"
            price_line = (operations.price_text({"low": price, "high": price, "unit": "", "name": service["name"]},
                                                request.payload.get("trip_fee_usd"))
                          if price is not None else (approval.proposal or {}).get("price_line") or "")
            emailed = self._confirm_to_customer(session, approval.tenant_id, profile, request, booked, service,
                                                price_line)
            result |= {"booking": booked, "emailed": emailed}
            stage = "booked"
        else:
            approval.status, request.status = "rejected", "new"
            request.staff_notes = "\n".join(x for x in (request.staff_notes, f"Approval declined by {approver}: {note}")
                                            if x)
            emailed = None
            stage = "submitted"
        if not emailed:  # nobody can tell the customer by email: the office calls
            conversation = session.get(Conversation, request.conversation_id) if request.conversation_id else None
            if conversation is not None:
                reason = ("call the customer to confirm the approved booking" if approve else
                          "call the customer: request not approved" + (f" ({note})" if note else ""))
                ticket = open_ticket(session, approval.tenant_id, conversation, None, "P3", reason)
                notify(self.settings, profile, ticket)
                result["ticket_id"] = ticket.escalation_id
        if request.conversation_id:  # move the customer's conversation on
            self.graph.update_state({"configurable": {"thread_id": request.conversation_id}},
                                    {"stage": stage, "slot_options": []})
        return result


def _approval_email(settings: Settings, profile: TenantProfile, request: ServiceRequest, approval: Approval,
                    decision, draft: dict):
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"[APPROVAL] Service request {request.request_id[:8].upper()} needs a decision"
    msg["From"] = settings.escalation_from_email or "serviceiq@localhost"
    msg["To"] = profile.escalation_email
    options = (approval.proposal or {}).get("options") or []
    msg.set_content("\n".join(
        [f"Why: {', '.join(decision.conditions)}", f"Request: {request.request_id}", "", summary(draft), "",
         "Times the agent could offer:"] + [f"  - {o['label']} ({o.get('technician_name')})" for o in options]
        + ["", "Decide in the admin console, Approvals tab. Undecided approvals are escalated after 24 hours."]))
    return msg
