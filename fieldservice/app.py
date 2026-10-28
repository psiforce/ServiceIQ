"""Field-service platform REST API (customers, equipment, technicians, availability, jobs).

Ported from OpenFieldService's Hono routes (src/server/index.ts, MIT, (c) 2026 Clawnify) with the additions
ServiceIQ needs: an API key per contractor (header `X-FSP-Key`; the key alone decides the org), every query
scoped to that org, exact phone lookup, `Idempotency-Key` replay on writes so a retried request never creates
a second customer or job, and (M16) technician working hours, an availability search and a double-booking
check that locks the technician while a job is booked or moved.

Run locally:  python -m fieldservice serve
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from fieldservice.config import FSPSettings, get_fsp_settings
from fieldservice.db import OrgScope, digits, make_engine, org_for_key, org_session, session_factory
from fieldservice.models import Asset, Customer, IdempotencyRecord, Job, Org, ServiceType, Site, Technician
from fieldservice.scheduling import (ACTIVE_STATUSES, DAYS, can_do, free_slots, overlaps, utc,
                                     within_hours)

JOB_STATUSES = ["scheduled", "confirmed", "in_progress", "completed", "cancelled"]
PRIORITIES = ["low", "normal", "high", "urgent"]
MAX_RANGE_DAYS = 31

log = logging.getLogger("fieldservice.api")


# ---------------------------------------------------------------- request / response models
class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    address: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=50)
    zip: str = Field(default="", max_length=10)
    notes: str = Field(default="", max_length=2000)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=50)
    zip: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=300)
    contact_name: str = Field(default="", max_length=200)
    contact_phone: str = Field(default="", max_length=50)
    access_instructions: str = Field(default="", max_length=2000)
    safety_notes: str = Field(default="", max_length=2000)


class AssetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    site_id: str | None = None
    serial_number: str | None = Field(default=None, max_length=100)
    manufacturer: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=100)
    installation_year: int | None = Field(default=None, ge=1950, le=2100)
    notes: str = Field(default="", max_length=2000)


def _check_hours(value: dict | None) -> dict | None:
    if value is None:
        return value
    for day, hours in value.items():
        if day not in DAYS or not isinstance(hours, list) or len(hours) != 2:
            raise ValueError("working_hours must map mon..sun to [start, end], e.g. {'mon': ['08:00', '17:00']}")
        start, end = (time.fromisoformat(h) for h in hours)
        if start >= end:
            raise ValueError(f"{day}: start must be before end")
    return value


class TechnicianIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    color: str = Field(default="#16a34a", max_length=16)
    working_hours: dict = Field(default_factory=lambda: {d: ["08:00", "17:00"] for d in DAYS[:5]})
    skills: list[str] = Field(default_factory=list)
    service_zips: list[str] = Field(default_factory=list)

    @field_validator("working_hours")
    @classmethod
    def check_hours(cls, value):
        return _check_hours(value)


class TechnicianUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    active: bool | None = None
    working_hours: dict | None = None
    skills: list[str] | None = None
    service_zips: list[str] | None = None

    @field_validator("working_hours")
    @classmethod
    def check_hours(cls, value):
        return _check_hours(value)


class JobIn(BaseModel):
    customer_id: str
    scheduled_start: datetime  # with a UTC offset, e.g. 2026-11-02T14:00:00Z
    service_type_id: str | None = None
    service_code: str | None = None
    technician_id: str | None = None  # empty = assign the first technician free for the whole visit
    asset_id: str | None = None
    duration: int | None = Field(default=None, ge=15, le=12 * 60)
    price: float | None = Field(default=None, ge=0)
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    address: str | None = Field(default=None, max_length=300)
    notes: str = Field(default="", max_length=4000)
    external_ref: str = Field(default="", max_length=64)
    allow_outside_hours: bool = False  # dispatcher override; the agent never sets it


class JobUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(scheduled|confirmed|in_progress|completed|cancelled)$")
    priority: str | None = Field(default=None, pattern="^(low|normal|high|urgent)$")
    technician_id: str | None = None
    scheduled_start: datetime | None = None
    duration: int | None = Field(default=None, ge=15, le=12 * 60)
    notes: str | None = Field(default=None, max_length=4000)
    completion_notes: str | None = Field(default=None, max_length=4000)
    allow_outside_hours: bool = False


def _technician(t: Technician) -> dict:
    return {"id": t.id, "name": t.name, "email": t.email, "phone": t.phone, "color": t.color, "active": t.active,
            "working_hours": t.working_hours, "skills": t.skills, "service_zips": t.service_zips}


def _job(j: Job, tz: ZoneInfo, names: dict | None = None) -> dict:
    start = utc(j.scheduled_start)
    names = names or {}
    return {"id": j.id, "identifier": j.identifier, "customer_id": j.customer_id,
            "customer_name": names.get(j.customer_id), "technician_id": j.technician_id,
            "technician_name": names.get(j.technician_id), "service_type_id": j.service_type_id,
            "service_name": names.get(j.service_type_id), "asset_id": j.asset_id, "status": j.status,
            "priority": j.priority, "scheduled_start": start.isoformat(),
            "local_start": start.astimezone(tz).isoformat(), "duration": j.duration, "price": j.price,
            "address": j.address, "notes": j.notes, "completion_notes": j.completion_notes,
            "external_ref": j.external_ref}


def _customer(c: Customer) -> dict:
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "address": c.address, "city": c.city,
            "state": c.state, "zip": c.zip, "notes": c.notes, "created_at": c.created_at.isoformat()}


def _site(s: Site) -> dict:
    return {"id": s.id, "customer_id": s.customer_id, "name": s.name, "address": s.address,
            "contact_name": s.contact_name, "contact_phone": s.contact_phone,
            "access_instructions": s.access_instructions, "safety_notes": s.safety_notes}


def _asset(a: Asset) -> dict:
    return {"id": a.id, "customer_id": a.customer_id, "site_id": a.site_id, "name": a.name,
            "serial_number": a.serial_number, "manufacturer": a.manufacturer, "model": a.model, "status": a.status,
            "installation_year": a.installation_year, "notes": a.notes}


def _service_type(t: ServiceType) -> dict:
    return {"id": t.id, "code": t.code, "name": t.name, "description": t.description,
            "default_duration": t.default_duration, "default_price": t.default_price, "price_high": t.price_high,
            "skill": t.skill}


# ---------------------------------------------------------------- application factory
def create_app(settings: FSPSettings | None = None, engine=None) -> FastAPI:
    settings = settings or get_fsp_settings()
    engine = engine or make_engine(settings.fsp_database_url)
    sessions = session_factory(engine)

    app = FastAPI(title="ServiceIQ Field Service Platform", version="0.1.0",
                  description="Multi-tenant port of OpenFieldService (MIT, (c) 2026 Clawnify).")

    def caller(x_fsp_key: str = Header(default="")) -> Org:
        with sessions() as session:
            org = org_for_key(session, x_fsp_key) if x_fsp_key else None
        if org is None:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return org

    def scope(org: Org = Depends(caller)):
        with org_session(sessions, org.org_id) as session:
            yield OrgScope(session, org.org_id)

    def replay(s: OrgScope, key: str | None, path: str):
        if not key:
            return None
        saved = s.session.get(IdempotencyRecord, (s.org_id, key))
        if saved is None:
            return None
        if saved.path != path:
            raise HTTPException(status_code=422, detail="Idempotency-Key was already used for a different request")
        return JSONResponse(status_code=saved.status_code, content=saved.response)

    def remember(s: OrgScope, key: str | None, path: str, status_code: int, body: dict) -> dict:
        if key:
            s.add(IdempotencyRecord(key=key, path=path, status_code=status_code, response=body))
        s.session.commit()
        return body

    def own(s: OrgScope, model, row_id: str, what: str):
        row = s.get(model, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{what} not found")  # same answer for other orgs' rows
        return row

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/org")
    def org_info(org: Org = Depends(caller)):
        return {"org_id": org.org_id, "name": org.name, "timezone": org.timezone}

    # ------------------------------------------------------------ customers
    @app.get("/api/customers")
    def list_customers(phone: str = "", search: str = "", page: int = 1, limit: int = 50,
                       s: OrgScope = Depends(scope)):
        query = s.select(Customer)
        if phone:
            query = query.where(Customer.phone_digits == digits(phone))
        if search:
            like = f"%{search.lower()}%"
            query = query.where(or_(func.lower(Customer.name).like(like), func.lower(Customer.email).like(like),
                                    func.lower(Customer.address).like(like), Customer.phone_digits.like(f"%{digits(search) or '#'}%")))
        total = s.session.scalar(select(func.count()).select_from(query.subquery()))
        limit = max(1, min(limit, 200))
        rows = s.session.scalars(query.order_by(Customer.name).offset((max(page, 1) - 1) * limit).limit(limit))
        return {"customers": [_customer(c) for c in rows], "total": total}

    @app.post("/api/customers", status_code=201)
    def create_customer(body: CustomerIn, request: Request, s: OrgScope = Depends(scope),
                        idempotency_key: str | None = Header(default=None)):
        if (saved := replay(s, idempotency_key, request.url.path)) is not None:
            return saved
        customer = s.add(Customer(**body.model_dump(), phone_digits=digits(body.phone)))
        s.session.flush()
        return remember(s, idempotency_key, request.url.path, 201, _customer(customer))

    @app.get("/api/customers/{customer_id}")
    def get_customer(customer_id: str, s: OrgScope = Depends(scope)):
        customer = own(s, Customer, customer_id, "customer")
        sites = s.session.scalars(s.select(Site).where(Site.customer_id == customer.id))
        assets = s.session.scalars(s.select(Asset).where(Asset.customer_id == customer.id))
        return {"customer": _customer(customer), "sites": [_site(x) for x in sites],
                "assets": [_asset(x) for x in assets]}

    @app.put("/api/customers/{customer_id}")
    def update_customer(customer_id: str, body: CustomerUpdate, s: OrgScope = Depends(scope)):
        customer = own(s, Customer, customer_id, "customer")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(customer, field, value)
        if body.phone is not None:
            customer.phone_digits = digits(body.phone)
        s.session.commit()
        return _customer(customer)

    # ------------------------------------------------------------ sites and equipment
    @app.post("/api/customers/{customer_id}/sites", status_code=201)
    def create_site(customer_id: str, body: SiteIn, request: Request, s: OrgScope = Depends(scope),
                    idempotency_key: str | None = Header(default=None)):
        if (saved := replay(s, idempotency_key, request.url.path)) is not None:
            return saved
        own(s, Customer, customer_id, "customer")
        site = s.add(Site(customer_id=customer_id, **body.model_dump()))
        s.session.flush()
        return remember(s, idempotency_key, request.url.path, 201, _site(site))

    @app.post("/api/customers/{customer_id}/assets", status_code=201)
    def create_asset(customer_id: str, body: AssetIn, request: Request, s: OrgScope = Depends(scope),
                     idempotency_key: str | None = Header(default=None)):
        if (saved := replay(s, idempotency_key, request.url.path)) is not None:
            return saved
        own(s, Customer, customer_id, "customer")
        if body.site_id:
            site = own(s, Site, body.site_id, "site")
            if site.customer_id != customer_id:
                raise HTTPException(status_code=422, detail="site belongs to a different customer")
        asset = s.add(Asset(customer_id=customer_id, **body.model_dump()))
        s.session.flush()
        return remember(s, idempotency_key, request.url.path, 201, _asset(asset))

    @app.get("/api/service-types")
    def service_types(s: OrgScope = Depends(scope)):
        return {"service_types": [_service_type(t) for t in s.session.scalars(s.select(ServiceType)
                                                                                .order_by(ServiceType.code))]}

    # ------------------------------------------------------------ technicians
    def org_tz(s: OrgScope) -> ZoneInfo:
        return ZoneInfo(s.session.get(Org, s.org_id).timezone)

    @app.get("/api/technicians")
    def list_technicians(include_inactive: bool = False, s: OrgScope = Depends(scope)):
        query = s.select(Technician).order_by(Technician.name)
        if not include_inactive:
            query = query.where(Technician.active.is_(True))
        return {"technicians": [_technician(t) for t in s.session.scalars(query)]}

    @app.post("/api/technicians", status_code=201)
    def create_technician(body: TechnicianIn, s: OrgScope = Depends(scope)):
        tech = s.add(Technician(**body.model_dump()))
        s.session.commit()
        return _technician(tech)

    @app.put("/api/technicians/{technician_id}")
    def update_technician(technician_id: str, body: TechnicianUpdate, s: OrgScope = Depends(scope)):
        tech = own(s, Technician, technician_id, "technician")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(tech, field, value)
        s.session.commit()
        return _technician(tech)

    # ------------------------------------------------------------ availability and jobs
    def service_type(s: OrgScope, type_id: str | None, code: str | None) -> ServiceType | None:
        if type_id:
            return own(s, ServiceType, type_id, "service type")
        if code:
            found = s.session.scalar(s.select(ServiceType).where(ServiceType.code == code))
            if found is None:
                raise HTTPException(status_code=404, detail="service type not found")
            return found
        return None

    def tech_jobs(s: OrgScope, tech_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Job]]:
        rows = s.session.scalars(s.select(Job).where(Job.technician_id.in_(tech_ids),
                                                     Job.scheduled_start >= start - timedelta(hours=12),
                                                     Job.scheduled_start < end + timedelta(hours=12)))
        grouped: dict[str, list[Job]] = {}
        for job in rows:
            grouped.setdefault(job.technician_id, []).append(job)
        return grouped

    def local_range(tz: ZoneInfo, date_from: date, date_to: date) -> tuple[datetime, datetime]:
        if date_to < date_from or (date_to - date_from).days > MAX_RANGE_DAYS:
            raise HTTPException(status_code=422, detail=f"date range must be 0 to {MAX_RANGE_DAYS} days")
        return (datetime.combine(date_from, time.min, tz).astimezone(timezone.utc),
                datetime.combine(date_to + timedelta(days=1), time.min, tz).astimezone(timezone.utc))

    @app.get("/api/availability")
    def availability(date_from: date, date_to: date, service_type_id: str | None = None,
                     service_code: str | None = None, zip: str | None = None, duration: int | None = None,
                     limit: int = 20, lead_minutes: int = 60, s: OrgScope = Depends(scope)):
        """Free start times (UTC and org-local) for a visit, earliest first, with the technician who would go."""
        tz = org_tz(s)
        start, end = local_range(tz, date_from, date_to)
        st = service_type(s, service_type_id, service_code)
        minutes = duration or (st.default_duration if st else 60)
        techs = [t for t in s.session.scalars(s.select(Technician)) if can_do(t, st.skill if st else "", zip)]
        jobs = tech_jobs(s, [t.id for t in techs], start, end)
        not_before = max(start, datetime.now(timezone.utc) + timedelta(minutes=lead_minutes))
        slots = free_slots(techs, jobs, date_from, date_to, minutes, tz, not_before, max(1, min(limit, 100)))
        return {"timezone": tz.key, "duration": minutes,
                "slots": [{"start": x.start.isoformat(), "local_start": x.start.astimezone(tz).isoformat(),
                           "technician_id": x.technician_id, "technician_name": x.technician_name} for x in slots]}

    def bookable(s: OrgScope, tech: Technician, start: datetime, minutes: int, tz: ZoneInfo, outside_ok: bool,
                 ignore_job_id: str | None = None) -> str | None:
        """Why this technician can't take the visit, or None. Call with the technician row locked."""
        if not tech.active:
            return "technician is inactive"
        if not outside_ok and not within_hours(tech, start, minutes, tz):
            return "outside the technician's working hours"
        jobs = tech_jobs(s, [tech.id], start, start + timedelta(minutes=minutes)).get(tech.id, [])
        if overlaps(start, minutes, jobs, ignore_job_id):
            return "technician already has a job at that time"
        return None

    def lock(s: OrgScope, technician_id: str) -> Technician | None:
        """Lock the technician row so two bookings can't take the same time (Postgres; SQLite has one writer)."""
        return s.session.scalar(s.select(Technician).where(Technician.id == technician_id).with_for_update())

    def names(s: OrgScope, jobs: list[Job]) -> dict:
        ids = {x for j in jobs for x in (j.customer_id, j.technician_id, j.service_type_id) if x}
        found = {}
        for model, label in ((Customer, "name"), (Technician, "name"), (ServiceType, "name")):
            for row in s.session.scalars(s.select(model).where(model.id.in_(ids))):
                found[row.id] = getattr(row, label)
        return found

    @app.post("/api/jobs", status_code=201)
    def create_job(body: JobIn, request: Request, s: OrgScope = Depends(scope),
                   idempotency_key: str | None = Header(default=None)):
        if (saved := replay(s, idempotency_key, request.url.path)) is not None:
            return saved
        tz = org_tz(s)
        customer = own(s, Customer, body.customer_id, "customer")
        st = service_type(s, body.service_type_id, body.service_code)
        if body.asset_id and own(s, Asset, body.asset_id, "asset").customer_id != customer.id:
            raise HTTPException(status_code=422, detail="asset belongs to a different customer")
        start = utc(body.scheduled_start)
        minutes = body.duration or (st.default_duration if st else 60)
        skill = st.skill if st else ""
        if body.technician_id:
            own(s, Technician, body.technician_id, "technician")
            candidates = [body.technician_id]
        else:
            candidates = [t.id for t in s.session.scalars(s.select(Technician).order_by(Technician.name))
                          if can_do(t, skill, customer.zip or None)]
        tech, reason = None, "no technician can do this service"
        for technician_id in candidates:
            candidate = lock(s, technician_id)
            if body.technician_id and not can_do(candidate, skill, customer.zip or None):
                raise HTTPException(status_code=422, detail="technician lacks the skill or doesn't serve this ZIP")
            reason = bookable(s, candidate, start, minutes, tz, body.allow_outside_hours)
            if reason is None:
                tech = candidate
                break
        if tech is None:
            s.session.rollback()
            raise HTTPException(status_code=409, detail=reason if body.technician_id else
                                f"no technician is free for the whole visit ({reason})")
        address = body.address or ", ".join(x for x in (customer.address, customer.city, customer.zip) if x)
        job = s.add(Job(identifier=s.next_identifier("job", "JOB"), customer_id=customer.id, asset_id=body.asset_id,
                        technician_id=tech.id, service_type_id=st.id if st else None, priority=body.priority,
                        scheduled_start=start, duration=minutes,
                        price=body.price if body.price is not None else (st.default_price if st else 0),
                        address=address, notes=body.notes, external_ref=body.external_ref))
        s.session.flush()
        return remember(s, idempotency_key, request.url.path, 201, _job(job, tz, names(s, [job])))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, s: OrgScope = Depends(scope)):
        job = own(s, Job, job_id, "job")
        return _job(job, org_tz(s), names(s, [job]))

    @app.put("/api/jobs/{job_id}")
    def update_job(job_id: str, body: JobUpdate, s: OrgScope = Depends(scope)):
        tz = org_tz(s)
        job = own(s, Job, job_id, "job")
        changes = body.model_dump(exclude_none=True, exclude={"allow_outside_hours"})
        technician_id = changes.get("technician_id", job.technician_id)
        start = utc(changes["scheduled_start"]) if "scheduled_start" in changes else utc(job.scheduled_start)
        minutes = changes.get("duration", job.duration)
        moving = {"technician_id", "scheduled_start", "duration"} & changes.keys()
        reactivating = changes.get("status") in ACTIVE_STATUSES and job.status not in ACTIVE_STATUSES
        if (moving or reactivating) and changes.get("status", job.status) in ACTIVE_STATUSES and technician_id:
            if s.get(Technician, technician_id) is None:
                raise HTTPException(status_code=404, detail="technician not found")
            reason = bookable(s, lock(s, technician_id), start, minutes, tz, body.allow_outside_hours,
                              ignore_job_id=job.id)
            if reason:
                s.session.rollback()
                raise HTTPException(status_code=409, detail=reason)
        for field, value in changes.items():
            setattr(job, field, utc(value) if field == "scheduled_start" else value)
        s.session.commit()
        return _job(job, tz, names(s, [job]))

    @app.get("/api/schedule")
    def schedule(date_from: date, date_to: date, technician_id: str | None = None,
                 include_cancelled: bool = False, s: OrgScope = Depends(scope)):
        tz = org_tz(s)
        start, end = local_range(tz, date_from, date_to)
        query = s.select(Job).where(Job.scheduled_start >= start, Job.scheduled_start < end)
        if technician_id:
            query = query.where(Job.technician_id == technician_id)
        if not include_cancelled:
            query = query.where(Job.status != "cancelled")
        jobs = list(s.session.scalars(query.order_by(Job.scheduled_start)))
        found = names(s, jobs)
        return {"timezone": tz.key, "jobs": [_job(j, tz, found) for j in jobs]}

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "something went wrong; please try again"})

    app.state.sessions = sessions
    return app
