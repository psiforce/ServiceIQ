"""Engine, per-org sessions and onboarding for the field-service platform.

Isolation between contractors is enforced twice:
  1. in code: routes only query through `OrgScope`, which adds `org_id = <caller's org>` to every statement
  2. in Postgres: row-level security on every org table, keyed on the transaction setting `app.org_id`,
     which `org_session` sets at the start of every transaction (SQLite in dev relies on (1) alone)
"""
import hashlib
import logging
import secrets
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from fieldservice.models import ORG_TABLES, DEFAULT_HOURS, FSPBase, Org, OrgCounter, ServiceType, Technician

log = logging.getLogger("fieldservice.db")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def digits(phone: str) -> str:
    """Normalize a phone number for lookups: last 10 digits."""
    return "".join(ch for ch in phone if ch.isdigit())[-10:]


def _install_row_level_security(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in ORG_TABLES:
            conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))  # applies to the table owner too
            conn.execute(text(f"DROP POLICY IF EXISTS org_isolation ON {table}"))
            conn.execute(text(f"CREATE POLICY org_isolation ON {table} "
                              "USING (org_id = current_setting('app.org_id', true)) "
                              "WITH CHECK (org_id = current_setting('app.org_id', true))"))


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(database_url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(database_url, pool_pre_ping=True)
    FSPBase.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        _install_row_level_security(engine)
        with engine.connect() as conn:  # superusers and BYPASSRLS roles skip row-level security entirely
            if conn.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")):
                log.warning("connected to Postgres as a role that bypasses row-level security; "
                            "use a normal application role so org isolation is enforced by the database")
    return engine


class OrgSession(Session):
    """A session bound to one org; in Postgres every transaction it opens is limited to that org's rows."""


@event.listens_for(OrgSession, "after_begin")
def _set_org(session, transaction, connection):
    org_id = session.info.get("org_id")
    if org_id and connection.dialect.name == "postgresql":
        connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": org_id})


def session_factory(engine: Engine) -> sessionmaker[OrgSession]:
    return sessionmaker(engine, class_=OrgSession, expire_on_commit=False)


@contextmanager
def org_session(factory: sessionmaker, org_id: str):
    session = factory(info={"org_id": org_id})
    try:
        yield session
    finally:
        session.close()


class OrgScope:
    """The only way routes read or write contractor data: every query is filtered to one org."""

    def __init__(self, session: Session, org_id: str):
        self.session, self.org_id = session, org_id

    def select(self, model):
        return select(model).where(model.org_id == self.org_id)

    def get(self, model, row_id: str):
        row = self.session.get(model, row_id)
        return row if row is not None and row.org_id == self.org_id else None

    def add(self, row):
        row.org_id = self.org_id
        self.session.add(row)
        return row

    def next_identifier(self, name: str, prefix: str) -> str:
        """JOB-1, JOB-2, ... per org; the counter row is locked so concurrent bookings never share a number."""
        counter = self.session.scalar(select(OrgCounter).where(OrgCounter.org_id == self.org_id,
                                                               OrgCounter.name == name).with_for_update())
        if counter is None:
            counter = OrgCounter(org_id=self.org_id, name=name, value=0)
            self.session.add(counter)
        counter.value += 1
        self.session.flush()
        return f"{prefix}-{counter.value}"


def org_for_key(session: Session, key: str) -> Org | None:
    org = session.scalar(select(Org).where(Org.key_hash == hash_key(key)))
    return org if org is not None and org.active else None


def add_org(factory: sessionmaker, config: dict, key: str | None = None, rotate: bool = False) -> str | None:
    """Create or update a contractor from its config (see data/tenants/*/fieldservice.json).

    Returns the new API key (shown once; only its hash is stored), or None when the org already had a key.
    Service types are upserted by code; technicians are added by name if missing.
    """
    org_id = config["org_id"]
    with factory() as session:
        org = session.get(Org, org_id)
        new_key = None
        if org is None or rotate or (key and org.key_hash != hash_key(key)):
            new_key = key or f"fsp_{org_id}_{secrets.token_urlsafe(24)}"
        if org is None:
            org = Org(org_id=org_id, name=config["name"], key_hash=hash_key(new_key))
            session.add(org)
        elif new_key:
            org.key_hash = hash_key(new_key)
        org.name, org.timezone = config["name"], config.get("timezone", "America/New_York")
        session.commit()
    with org_session(factory, org_id) as session:
        scope = OrgScope(session, org_id)
        for st in config.get("service_types", []):
            row = session.scalar(scope.select(ServiceType).where(ServiceType.code == st["code"]))
            row = row or scope.add(ServiceType(code=st["code"], name=st["name"]))
            row.name, row.default_duration = st["name"], st.get("duration", 60)
            row.default_price, row.price_high = st.get("price", 0), st.get("price_high", st.get("price", 0))
            row.skill, row.description = st.get("skill", ""), st.get("description", "")
        for tech in config.get("technicians", []):
            if session.scalar(scope.select(Technician).where(Technician.name == tech["name"])) is None:
                scope.add(Technician(name=tech["name"], email=tech.get("email", ""), phone=tech.get("phone", ""),
                                     working_hours=tech.get("working_hours", DEFAULT_HOURS),
                                     skills=tech.get("skills", []), service_zips=tech.get("service_zips", []),
                                     color=tech.get("color", "#16a34a")))
        session.commit()
    return new_key
