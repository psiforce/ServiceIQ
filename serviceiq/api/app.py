"""ServiceIQ HTTP API.

Every request is authenticated with the tenant's widget key (header `X-Widget-Key`). The tenant is derived
from that key only - never from the request body - and every conversation, message and ticket lookup is
scoped to it, so one company can never read or write another company's data.

Endpoints
  POST /v1/chat         ask a question or request service (optionally continuing a conversation); with the
                        agent enabled (M14) it also collects service-request details, takes the contact form
                        (`contact`) and the customer's confirmation (`confirm`)
  POST /v1/escalations  request a callback (attaches contact details to the handoff ticket)
  POST /v1/feedback     rate an answer
  GET  /health          liveness check (no auth)
Run locally:  python -m serviceiq serve
"""
import logging
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from serviceiq.config import Settings, get_settings
from serviceiq.escalation import notify, open_ticket
from serviceiq.guardrails.pii import redact
from serviceiq.storage.db import make_engine, session_factory, tenant_for_key
from serviceiq.storage.models import CitationRecord, Conversation, Escalation, Feedback, Message, Tenant
from serviceiq.tenants import load_profile

log = logging.getLogger("serviceiq.api")
AUTO_ESCALATE = {"emergency", "escalated"}  # these statuses open a ticket immediately


# ---------------------------------------------------------------- request / response models
class ContactIn(BaseModel):
    """The intake contact form: goes to the field-service platform, never into message text or a model prompt."""
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=7, max_length=50)
    email: str = Field(default="", max_length=200)
    street: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=100)
    zip: str = Field(default="", pattern=r"^(\d{5})?$")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    contact: ContactIn | None = None
    confirm: bool | None = None  # the customer pressed "Confirm" on the request summary


class CitationOut(BaseModel):
    n: int
    title: str
    section: str
    source: str
    document_id: str
    snippet: str


class EscalationOut(BaseModel):
    priority: str
    reason: str
    ticket_id: str | None = None
    callback_available: bool = True


class ChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    status: str
    answer: str
    citations: list[CitationOut]
    escalation: EscalationOut | None
    trace_id: str
    latency_ms: int
    stage: str | None = None          # agent: idle | collecting | confirming | submitted | closed
    needs_contact: bool = False       # agent: show the contact form
    service_request: dict | None = None


class CallbackRequest(BaseModel):
    conversation_id: str
    message_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=7, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    preferred_time: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class CallbackResponse(BaseModel):
    ticket_id: str
    priority: str
    message: str


class FeedbackRequest(BaseModel):
    message_id: str
    rating: str = Field(pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------- rate limiting (per widget key, in memory)
class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            window = self.hits[key]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return False
            window.append(now)
            return True


# ---------------------------------------------------------------- application factory
def create_app(settings: Settings | None = None, assistant=None, engine=None, agent=None) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    sessions = session_factory(engine)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    state = {"assistant": assistant, "agent": agent}

    def get_assistant():
        if state["assistant"] is None:  # created on first use so the API starts fast
            from serviceiq.assistant import Assistant
            state["assistant"] = Assistant(settings)
        return state["assistant"]

    def get_agent():
        if state["agent"] is None:
            from serviceiq.agent import Agent
            state["agent"] = Agent(settings, assistant=state["assistant"])
        return state["agent"]

    app = FastAPI(title="ServiceIQ API", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "X-Widget-Key"])

    def db():
        with sessions() as session:
            yield session

    def tenant(x_widget_key: str = Header(default=""), session: Session = Depends(db)) -> Tenant:
        found = tenant_for_key(session, x_widget_key) if x_widget_key else None
        if found is None:
            raise HTTPException(status_code=401, detail="invalid or missing widget key")
        if not limiter.check(found.tenant_id + x_widget_key[-6:]):
            raise HTTPException(status_code=429, detail="too many requests, please slow down")
        return found

    def own_conversation(session: Session, tenant_id: str, conversation_id: str) -> Conversation:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None or conversation.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="conversation not found")  # same answer for other tenants
        return conversation

    def history(conversation: Conversation) -> list[tuple[str, str]]:
        turns, last_question = [], None
        for m in conversation.messages:
            if m.role == "customer":
                last_question = m.content
            elif last_question and m.status in ("answered", "fallback"):
                turns.append((last_question, m.content))
        return turns[-settings.history_turns:]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, t: Tenant = Depends(tenant), session: Session = Depends(db)):
        if body.conversation_id:
            conversation = own_conversation(session, t.tenant_id, body.conversation_id)
        else:
            conversation = Conversation(tenant_id=t.tenant_id)
            session.add(conversation)
            session.flush()
        if settings.agent_enabled:
            response = get_agent().handle(session, t.tenant_id, conversation.conversation_id, body.message,
                                          contact=body.contact.model_dump() if body.contact else None,
                                          confirm=body.confirm, history=history(conversation))
            stored_answer = response.log_answer  # the request summary is stored without contact details
        else:
            response = get_assistant().ask(body.message, t.tenant_id, history=history(conversation))
            stored_answer = response.answer

        session.add(Message(conversation_id=conversation.conversation_id, role="customer",
                            content=redact(body.message)))
        reply = Message(conversation_id=conversation.conversation_id, role="assistant", content=stored_answer,
                        status=response.status, trace_id=response.trace_id,
                        latency_ms=response.latency_ms.get("total"), cost_usd=response.cost_usd,
                        details={k: response.debug.get(k) for k in ("standalone_question", "guard", "evidence",
                                                                    "attempts", "grounding_problems", "extracted")}
                        | {"escalation": response.escalation,
                           "service_request": getattr(response, "service_request", None)})
        reply.citations = [CitationRecord(ordinal=c.n, document_id=c.document_id, version=c.version, title=c.title,
                                          section=c.section, source=c.source) for c in response.citations]
        session.add(reply)
        conversation.outcome = response.status
        session.flush()
        session.refresh(conversation)

        escalation = None
        if response.escalation:
            ticket_id = None
            if response.status in AUTO_ESCALATE:
                ticket = open_ticket(session, t.tenant_id, conversation, reply.message_id,
                                     response.escalation["priority"], response.escalation["reason"] or response.status)
                if contact := getattr(response, "ticket_contact", None):  # intake hand-off: contact already given
                    ticket.contact_name, ticket.contact_phone = contact.get("name"), contact.get("phone")
                    ticket.contact_email = contact.get("email") or None
                destination = notify(settings, load_profile(settings.data_dir, t.tenant_id), ticket)
                log.info("ticket %s (%s) notified: %s", ticket.escalation_id, ticket.priority, destination)
                ticket_id = ticket.escalation_id
            escalation = EscalationOut(priority=response.escalation["priority"],
                                       reason=response.escalation["reason"] or response.status, ticket_id=ticket_id)
        session.commit()
        return ChatResponse(
            conversation_id=conversation.conversation_id, message_id=reply.message_id, status=response.status,
            answer=response.answer, escalation=escalation, trace_id=response.trace_id,
            latency_ms=response.latency_ms.get("total", 0),
            citations=[CitationOut(n=c.n, title=c.title, section=c.section, source=c.source,
                                   document_id=c.document_id, snippet=c.snippet) for c in response.citations],
            stage=getattr(response, "stage", None), needs_contact=getattr(response, "needs_contact", False),
            service_request=getattr(response, "service_request", None))

    @app.post("/v1/escalations", response_model=CallbackResponse)
    def request_callback(body: CallbackRequest, t: Tenant = Depends(tenant), session: Session = Depends(db)):
        conversation = own_conversation(session, t.tenant_id, body.conversation_id)
        ticket = session.scalar(select(Escalation).where(Escalation.conversation_id == conversation.conversation_id,
                                                         Escalation.status == "open"))
        if ticket is None:
            source = session.get(Message, body.message_id) if body.message_id else None
            flagged = ((source.details or {}).get("escalation") if source and
                       source.conversation_id == conversation.conversation_id else None) or {}
            ticket = open_ticket(session, t.tenant_id, conversation, body.message_id, flagged.get("priority", "P3"),
                                 flagged.get("reason") or "customer requested a callback")
        ticket.contact_name, ticket.contact_phone = body.name, body.phone
        ticket.contact_email, ticket.preferred_time = body.email, body.preferred_time
        if body.note:
            ticket.handoff_packet = {**ticket.handoff_packet, "customer_note": redact(body.note)}
        notify(settings, load_profile(settings.data_dir, t.tenant_id), ticket)
        session.commit()
        profile = load_profile(settings.data_dir, t.tenant_id)
        return CallbackResponse(ticket_id=ticket.escalation_id, priority=ticket.priority,
                                message=f"Thanks, {body.name}. {profile.company_name} will contact you at "
                                        f"{body.phone}. For anything urgent, call {profile.office_phone}.")

    @app.post("/v1/feedback")
    def feedback(body: FeedbackRequest, t: Tenant = Depends(tenant), session: Session = Depends(db)):
        message = session.get(Message, body.message_id)
        if message is None or message.role != "assistant":
            raise HTTPException(status_code=404, detail="message not found")
        own_conversation(session, t.tenant_id, message.conversation_id)
        session.add(Feedback(message_id=message.message_id, rating=body.rating,
                             comment=redact(body.comment) if body.comment else None))
        session.commit()
        return {"status": "recorded"}

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception):  # never leak internals to the widget
        from fastapi.responses import JSONResponse
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "something went wrong; please try again"})

    return app
