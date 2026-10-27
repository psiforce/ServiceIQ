"""Database schema (architecture section 4.5). Works on SQLite locally and PostgreSQL (Cloud SQL) on GCP.

Privacy rules: message text is stored PII-redacted. The only personal data stored in clear is the contact
information a customer explicitly gives when requesting a callback (needed to call them back), and it is
deleted with the rest of the conversation after the retention period.
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
