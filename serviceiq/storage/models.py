"""Database schema (architecture section 4.5). Works on SQLite locally and PostgreSQL (Cloud SQL) on GCP.

Privacy rules: message text is stored PII-redacted. The only personal data stored in clear is the contact
information a customer explicitly gives when requesting a callback (needed to call them back), and it is
deleted with the rest of the conversation after the retention period. Service requests (Phase 2) hold only a
reference to the customer record in the field-service platform, never the contact details themselves.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200))
    widget_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # SHA-256 of the key
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Conversation(Base):
    __tablename__ = "conversations"
    conversation_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="web")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="open")  # last response status
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan",
                                                     order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"
    message_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), index=True)
    role: Mapped[str] = mapped_column(String(16))           # customer | assistant
    content: Mapped[str] = mapped_column(Text)             # PII-redacted
    status: Mapped[str | None] = mapped_column(String(32))  # assistant replies only
    trace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON)      # guard reasons, evidence IDs, standalone question
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    citations: Mapped[list["CitationRecord"]] = relationship(cascade="all, delete-orphan")
    feedback: Mapped[list["Feedback"]] = relationship(cascade="all, delete-orphan")


class CitationRecord(Base):
    __tablename__ = "citations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.message_id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    section: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(500))


class Feedback(Base):
    __tablename__ = "feedback"
    feedback_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.message_id"), index=True)
    rating: Mapped[str] = mapped_column(String(8))          # up | down
    comment: Mapped[str | None] = mapped_column(Text)        # PII-redacted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AdminUser(Base):
    """Tenant staff who sign in to the admin console. Roles: admin (everything) or csr (ticket inbox only)."""
    __tablename__ = "admin_users"
    username: Mapped[str] = mapped_column(String(100), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(String(200))  # pbkdf2_sha256$iterations$salt$hash
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DocumentRecord(Base):
    """A knowledge document; its content lives in immutable versions."""
    __tablename__ = "documents"
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(500))
    document_type: Mapped[str] = mapped_column(String(64))
    authority_rank: Mapped[int] = mapped_column(Integer)
    access_scope: Mapped[str] = mapped_column(String(32), default="public")
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    versions: Mapped[list["DocumentVersionRecord"]] = relationship(cascade="all, delete-orphan",
                                                                   order_by="DocumentVersionRecord.version")


class DocumentVersionRecord(Base):
    __tablename__ = "document_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.document_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(500))      # relative to the project root
    approval_status: Mapped[str] = mapped_column(String(16))  # pending | approved | rejected | superseded
    effective_date: Mapped[str] = mapped_column(String(10))   # ISO date
    expiry_date: Mapped[str] = mapped_column(String(10), default="")
    uploaded_by: Mapped[str | None] = mapped_column(String(100))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingest_seconds: Mapped[float | None] = mapped_column(Float)  # approval -> searchable (freshness SLA)
    ingest_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Escalation(Base):
    __tablename__ = "escalations"
    escalation_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
                                                 index=True)
    message_id: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(4))         # P1 | P2 | P3
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | contacted | resolved
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    contact_email: Mapped[str | None] = mapped_column(String(200))
    preferred_time: Mapped[str | None] = mapped_column(String(200))
    handoff_packet: Mapped[dict] = mapped_column(JSON, default=dict)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    resolution: Mapped[str | None] = mapped_column(Text)


class ServiceRequest(Base):
    """A structured service request created by the intake agent (Phase 2, versioned JSON payload)."""
    __tablename__ = "service_requests"
    request_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.conversation_id",
                                                                   ondelete="SET NULL"), index=True)
    customer_ref: Mapped[str] = mapped_column(String(64))  # customer ID in the field-service platform
    payload: Mapped[dict] = mapped_column(JSON)            # equipment, symptoms, urgency, windows; no contact details
    schema_version: Mapped[str] = mapped_column(String(8), default="1.0")
    # new | needs_approval | contacted | scheduled | closed; Phase 4: completed | invoiced | paid | reviewed
    status: Mapped[str] = mapped_column(String(16), default="new")
    staff_notes: Mapped[str | None] = mapped_column(Text)
    job_ref: Mapped[str | None] = mapped_column(String(64))  # the booked job in the field-service platform (M16)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ToolCall(Base):
    """Audit log: every tool the agent runs (inputs redacted), with its result and any approver."""
    __tablename__ = "tool_calls"
    call_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    approver: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ApprovalPolicy(Base):
    """Per-tenant rule for when the agent may act alone (Phase 3); rules are data, not code."""
    __tablename__ = "approval_policies"
    policy_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    condition: Mapped[str] = mapped_column(String(64))  # e.g. standard_visit, after_hours, binding_quote
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    approver_role: Mapped[str] = mapped_column(String(16), default="admin")
    timeout_hours: Mapped[int] = mapped_column(Integer, default=24)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Approval(Base):
    """A pending or decided approval for an action outside the auto-approval policy (Phase 3)."""
    __tablename__ = "approvals"
    approval_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("service_requests.request_id"), index=True)
    policy_id: Mapped[str | None] = mapped_column(ForeignKey("approval_policies.policy_id"))
    reason: Mapped[str] = mapped_column(String(500))
    proposal: Mapped[dict] = mapped_column(JSON)  # what the agent wants to do (slot, price range)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Invoice(Base):
    """The bill for a completed job (Phase 4). One per service request: the unique key makes a repeated
    completion event (webhook retry, sweep) unable to bill twice. No card or bank details are stored."""
    __tablename__ = "invoices"
    invoice_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("service_requests.request_id"), unique=True)
    job_ref: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    # pending_approval | sent | processing (ACH) | paid | failed | void
    status: Mapped[str] = mapped_column(String(16), default="pending_approval")
    method: Mapped[str | None] = mapped_column(String(16))  # card | ach | cashapp | online | zelle | check | cash
    provider_session_id: Mapped[str | None] = mapped_column(String(255), index=True)
    payment_url: Mapped[str | None] = mapped_column(String(1000))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    recorded_by: Mapped[str | None] = mapped_column(String(100))  # the admin who recorded an off-line payment
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Review(Base):
    """A review request and the customer's answer (Phase 4). The public review link is shown to every reviewer,
    whatever the rating; a rating of 3 or less also opens a recovery ticket."""
    __tablename__ = "reviews"
    review_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("service_requests.request_id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="requested")  # requested | submitted | closed
    rating: Mapped[int | None] = mapped_column(Integer)            # 1-5
    comment: Mapped[str | None] = mapped_column(Text)              # PII-redacted
    public_link_shown: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_escalation_id: Mapped[str | None] = mapped_column(String(32))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
