"""Field-service platform REST API (customers, sites, equipment, service types).

Ported from OpenFieldService's Hono routes (src/server/index.ts, MIT, (c) 2026 Clawnify) with the additions
ServiceIQ needs: an API key per contractor (header `X-FSP-Key`; the key alone decides the org), every query
scoped to that org, exact phone lookup, and `Idempotency-Key` replay on writes so a retried request never
creates a second customer. Scheduling endpoints (availability, jobs) are added in M16.

Run locally:  python -m fieldservice serve
"""
import logging

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from fieldservice.config import FSPSettings, get_fsp_settings
from fieldservice.db import OrgScope, digits, make_engine, org_for_key, org_session, session_factory
from fieldservice.models import Asset, Customer, IdempotencyRecord, Org, ServiceType, Site

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

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "something went wrong; please try again"})

    app.state.sessions = sessions
    return app
