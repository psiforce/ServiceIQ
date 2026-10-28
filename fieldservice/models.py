"""Field-service platform schema, ported from OpenFieldService (src/server/schema.sql, MIT, (c) 2026 Clawnify).

Changes from the original:
  - multi-tenant: every row belongs to an org (one HVAC contractor); `org_id` is part of every lookup index
  - times are timezone-aware UTC (`scheduled_start`); each org's timezone is used for display and working hours
  - job identifiers (JOB-n) are unique per org, from `org_counters`
  - technicians carry working hours, skills and service ZIPs so availability can be computed
  - invoices, materials and checklists are not ported (not needed by ServiceIQ)
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_HOURS = {"mon": ["08:00", "17:00"], "tue": ["08:00", "17:00"], "wed": ["08:00", "17:00"],
                 "thu": ["08:00", "17:00"], "fri": ["08:00", "17:00"]}


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FSPBase(DeclarativeBase):
    pass


class Org(FSPBase):
    """One contractor. Not row-level secured: it is looked up by API key before the org is known."""
    __tablename__ = "orgs"
    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # SHA-256 of the API key
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OrgCounter(FSPBase):
    __tablename__ = "org_counters"
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Customer(FSPBase):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customers_org_phone", "org_id", "phone_digits"),
                      Index("ix_customers_org_name", "org_id", "name"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    phone_digits: Mapped[str] = mapped_column(String(20), default="")  # normalized for exact lookup
    address: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(50), default="")
    zip: Mapped[str] = mapped_column(String(10), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Site(FSPBase):
    __tablename__ = "sites"
    __table_args__ = (Index("ix_sites_org_customer", "org_id", "customer_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(300), default="")
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    contact_phone: Mapped[str] = mapped_column(String(50), default="")
    access_instructions: Mapped[str] = mapped_column(Text, default="")
    safety_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Asset(FSPBase):
    """Customer equipment (an AC, furnace, heat pump...)."""
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("org_id", "customer_id", "serial_number", name="uq_assets_org_serial"),
                      Index("ix_assets_org_customer", "org_id", "customer_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    site_id: Mapped[str | None] = mapped_column(ForeignKey("sites.id"))
    name: Mapped[str] = mapped_column(String(200))
    serial_number: Mapped[str | None] = mapped_column(String(100))
    manufacturer: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="in_service")  # in_service | out_of_service | retired
    installation_year: Mapped[int | None] = mapped_column(Integer)
    warranty_end: Mapped[str] = mapped_column(String(10), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Technician(FSPBase):
    __tablename__ = "technicians"
    __table_args__ = (Index("ix_technicians_org", "org_id", "active"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    color: Mapped[str] = mapped_column(String(16), default="#16a34a")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    working_hours: Mapped[dict] = mapped_column(JSON, default=lambda: dict(DEFAULT_HOURS))  # org-local "HH:MM"
    skills: Mapped[list] = mapped_column(JSON, default=list)        # e.g. ["ac", "furnace", "heat_pump"]
    service_zips: Mapped[list] = mapped_column(JSON, default=list)  # empty = whole service area
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ServiceType(FSPBase):
    __tablename__ = "service_types"
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_service_types_org_code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    code: Mapped[str] = mapped_column(String(32))  # the contractor's price-book item code
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    default_duration: Mapped[int] = mapped_column(Integer, default=60)  # minutes
    default_price: Mapped[float] = mapped_column(Float, default=0)
    price_high: Mapped[float] = mapped_column(Float, default=0)
    skill: Mapped[str] = mapped_column(String(32), default="")  # technician skill required, if any
    color: Mapped[str] = mapped_column(String(16), default="#6b7280")


class Job(FSPBase):
    """A scheduled visit; it is both the appointment and the work order (as in OpenFieldService)."""
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("org_id", "identifier", name="uq_jobs_org_identifier"),
                      Index("ix_jobs_org_tech_start", "org_id", "technician_id", "scheduled_start"),
                      Index("ix_jobs_org_start", "org_id", "scheduled_start"),
                      Index("ix_jobs_org_customer", "org_id", "customer_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    identifier: Mapped[str] = mapped_column(String(32))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    technician_id: Mapped[str | None] = mapped_column(ForeignKey("technicians.id", ondelete="SET NULL"))
    service_type_id: Mapped[str | None] = mapped_column(ForeignKey("service_types.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled|confirmed|in_progress|completed|cancelled
    priority: Mapped[str] = mapped_column(String(10), default="normal")   # low | normal | high | urgent
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # UTC
    duration: Mapped[int] = mapped_column(Integer, default=60)  # minutes
    price: Mapped[float] = mapped_column(Float, default=0)
    address: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    completion_notes: Mapped[str] = mapped_column(Text, default="")
    external_ref: Mapped[str] = mapped_column(String(64), default="")  # e.g. the ServiceIQ service request ID
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class JobNote(FSPBase):
    __tablename__ = "job_notes"
    __table_args__ = (Index("ix_job_notes_org_job", "org_id", "job_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class IdempotencyRecord(FSPBase):
    """The first response to a write with a given Idempotency-Key, replayed on retries."""
    __tablename__ = "idempotency_keys"
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.org_id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    path: Mapped[str] = mapped_column(String(200))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# Every table that holds a contractor's data; row-level security is enabled on all of them in Postgres
ORG_TABLES = ["org_counters", "customers", "sites", "assets", "technicians", "service_types", "jobs", "job_notes",
              "idempotency_keys"]
