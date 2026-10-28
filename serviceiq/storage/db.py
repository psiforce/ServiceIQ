"""Database engine, sessions, tenant provisioning and retention."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from serviceiq.storage.models import Base, Conversation, Escalation, ServiceRequest, Tenant, ToolCall


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(database_url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    _add_new_columns(engine)
    return engine


# Columns added to existing tables after they shipped (create_all only creates missing tables)
NEW_COLUMNS = [("service_requests", "job_ref", "VARCHAR(64)")]  # M16: the booked job in the field-service platform


def _add_new_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, sql_type in NEW_COLUMNS:
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def provision_tenant(session: Session, tenant_id: str, company_name: str, rotate: bool = False,
                     key: str | None = None) -> str | None:
    """Create (or rotate the key of) a tenant. Returns the new widget key, shown once; only its hash is stored.
    Pass `key` to use a key generated elsewhere (e.g. one kept in Secret Manager)."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is not None and not rotate and (key is None or tenant.widget_key_hash == hash_key(key)):
        return None
    key = key or f"wk_{tenant_id}_{secrets.token_urlsafe(24)}"
    if tenant is None:
        session.add(Tenant(tenant_id=tenant_id, company_name=company_name, widget_key_hash=hash_key(key)))
    else:
        tenant.widget_key_hash = hash_key(key)
    session.commit()
    return key


def tenant_for_key(session: Session, key: str) -> Tenant | None:
    tenant = session.scalar(select(Tenant).where(Tenant.widget_key_hash == hash_key(key)))
    return tenant if tenant is not None and tenant.active else None


def purge_old_conversations(session: Session, days: int, forget=None) -> int:
    """Delete conversations (with their messages, citations, feedback, tickets and agent audit rows) older than
    `days`. Service requests are kept (they hold no personal data) but unlinked. `forget(conversation_id)` is
    called for each one, to delete the agent's saved state."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    old = list(session.scalars(select(Conversation.conversation_id).where(Conversation.started_at < cutoff)))
    if old:
        session.execute(delete(Escalation).where(Escalation.conversation_id.in_(old)))
        session.execute(delete(ToolCall).where(ToolCall.conversation_id.in_(old)))
        session.execute(update(ServiceRequest).where(ServiceRequest.conversation_id.in_(old))
                        .values(conversation_id=None))
        for conversation in session.scalars(select(Conversation).where(Conversation.conversation_id.in_(old))):
            session.delete(conversation)
        session.commit()
        for conversation_id in old if forget else []:
            forget(conversation_id)
    return len(old)
