"""M14: field-service platform (multi-tenant port of OpenFieldService): auth, org isolation, idempotency (no cost).

Row-level-security tests need Postgres: set FSP_TEST_DATABASE_URL to a throwaway database reached as a normal
(non-superuser) role, since superusers bypass row-level security.
"""
import json
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fieldservice.app import create_app
from fieldservice.config import FSPSettings, PROJECT_ROOT
from fieldservice.db import OrgScope, add_org, make_engine, org_session, session_factory
from fieldservice.models import Customer, Technician

CONFIGS = {t: json.loads((PROJECT_ROOT / "data" / "tenants" / t / "fieldservice.json").read_text(encoding="utf-8"))
           for t in ("cedar-ridge", "harbor-point")}
# a third contractor three time zones west, to check that hours are local to each org
CONFIGS["pacific-air"] = {"org_id": "pacific-air", "name": "Pacific Air", "timezone": "America/Los_Angeles",
                          "service_types": [{"code": "DIAG", "name": "Diagnostic", "duration": 60, "price": 95}],
                          "technicians": [{"name": "Lee Park", "skills": ["ac"],
                                           "working_hours": {d: ["08:00", "17:00"] for d in
                                                             ("mon", "tue", "wed", "thu", "fri")}}]}


def build(database_url: str):
    engine = make_engine(database_url)
    factory = session_factory(engine)
    keys = {org: add_org(factory, config) for org, config in CONFIGS.items()}
    client = TestClient(create_app(FSPSettings(fsp_database_url=database_url), engine=engine))
    return SimpleNamespace(client=client, keys=keys, engine=engine, factory=factory)


@pytest.fixture
def fsp(tmp_path):
    return build(f"sqlite:///{(tmp_path / 'fsp.db').as_posix()}")


def call(fsp, method, path, org="cedar-ridge", **kwargs):
    headers = {"X-FSP-Key": fsp.keys[org], **kwargs.pop("headers", {})}
    return fsp.client.request(method, path, headers=headers, **kwargs)


def test_requires_a_valid_key(fsp):
    assert fsp.client.get("/health").json() == {"status": "ok"}
    assert fsp.client.get("/api/customers").status_code == 401
    assert fsp.client.get("/api/customers", headers={"X-FSP-Key": "nope"}).status_code == 401
    assert call(fsp, "GET", "/api/org").json()["timezone"] == "America/New_York"


def test_onboarding_creates_service_types_and_technicians_per_org(fsp):
    cedar = call(fsp, "GET", "/api/service-types").json()["service_types"]
    harbor = call(fsp, "GET", "/api/service-types", "harbor-point").json()["service_types"]
    assert {t["code"] for t in cedar} >= {"DIAG-01", "TUNE-AC"} and "HP-BOILER" not in {t["code"] for t in cedar}
    assert {t["code"] for t in harbor} >= {"HP-DIAG", "HP-BOILER"}
    assert next(t for t in cedar if t["code"] == "DIAG-01")["default_price"] == 89
    # re-running onboarding is safe: no new key, no duplicate technicians
    assert add_org(fsp.factory, CONFIGS["cedar-ridge"]) is None
    with org_session(fsp.factory, "cedar-ridge") as s:
        names = list(s.scalars(OrgScope(s, "cedar-ridge").select(Technician).order_by(Technician.name)))
    assert [t.name for t in names] == ["Dana Whitfield", "Marcus Lee", "Priya Nair"]


def test_customer_lookup_by_phone_and_details(fsp):
    created = call(fsp, "POST", "/api/customers", json={"name": "Jordan Lee", "phone": "(555) 010-4477",
                                                       "address": "12 Oak St", "zip": "45801"})
    assert created.status_code == 201
    customer_id = created.json()["id"]
    found = call(fsp, "GET", "/api/customers", params={"phone": "555.010.4477"}).json()
    assert found["total"] == 1 and found["customers"][0]["id"] == customer_id
    asset = call(fsp, "POST", f"/api/customers/{customer_id}/assets",
                 json={"name": "Central AC", "manufacturer": "Trane", "installation_year": 2014})
    assert asset.status_code == 201
    detail = call(fsp, "GET", f"/api/customers/{customer_id}").json()
    assert detail["customer"]["address"] == "12 Oak St" and detail["assets"][0]["manufacturer"] == "Trane"
    updated = call(fsp, "PUT", f"/api/customers/{customer_id}", json={"email": "jordan@example.com"}).json()
    assert updated["email"] == "jordan@example.com"


def test_orgs_cannot_see_or_touch_each_others_data(fsp):
    cedar = call(fsp, "POST", "/api/customers", json={"name": "Cedar Customer", "phone": "555-010-1111"}).json()
    # Harbor Point's key sees nothing of Cedar Ridge's customers...
    assert call(fsp, "GET", "/api/customers", "harbor-point").json()["total"] == 0
    assert call(fsp, "GET", "/api/customers", "harbor-point", params={"phone": "555-010-1111"}).json()["total"] == 0
    assert call(fsp, "GET", f"/api/customers/{cedar['id']}", "harbor-point").status_code == 404
    # ...and cannot change them or attach equipment to them
    assert call(fsp, "PUT", f"/api/customers/{cedar['id']}", "harbor-point", json={"name": "x"}).status_code == 404
    assert call(fsp, "POST", f"/api/customers/{cedar['id']}/assets", "harbor-point",
                json={"name": "AC"}).status_code == 404
    assert call(fsp, "GET", f"/api/customers/{cedar['id']}").json()["customer"]["name"] == "Cedar Customer"


def test_retried_writes_with_the_same_idempotency_key_create_one_customer(fsp):
    body = {"name": "Retry Customer", "phone": "555-010-2222"}
    first = call(fsp, "POST", "/api/customers", json=body, headers={"Idempotency-Key": "req-1"})
    again = call(fsp, "POST", "/api/customers", json=body, headers={"Idempotency-Key": "req-1"})
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    assert call(fsp, "GET", "/api/customers", params={"phone": "555-010-2222"}).json()["total"] == 1
    # the same key from another org is a different request
    other = call(fsp, "POST", "/api/customers", "harbor-point", json=body, headers={"Idempotency-Key": "req-1"})
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]


def test_job_identifiers_are_numbered_per_org(fsp):
    with org_session(fsp.factory, "cedar-ridge") as s:
        scope = OrgScope(s, "cedar-ridge")
        cedar = [scope.next_identifier("job", "JOB") for _ in range(2)]
        s.commit()
    with org_session(fsp.factory, "harbor-point") as s:
        harbor = OrgScope(s, "harbor-point").next_identifier("job", "JOB")
        s.commit()
    assert cedar == ["JOB-1", "JOB-2"] and harbor == "JOB-1"


@pytest.mark.skipif(not os.environ.get("FSP_TEST_DATABASE_URL"), reason="needs FSP_TEST_DATABASE_URL (Postgres)")
def test_postgres_row_level_security_blocks_unscoped_queries():
    fsp = build(os.environ["FSP_TEST_DATABASE_URL"])
    call(fsp, "POST", "/api/customers", json={"name": "RLS Cedar", "phone": "555-010-3333"})
    call(fsp, "POST", "/api/customers", "harbor-point", json={"name": "RLS Harbor", "phone": "555-010-4444"})
    with org_session(fsp.factory, "harbor-point") as s:
        # deliberately unscoped: the database itself must still hide the other org's rows
        names = {c.name for c in s.scalars(select(Customer))}
        assert "RLS Harbor" in names and "RLS Cedar" not in names
        # and refuse to write a row for another org
        s.add(Customer(org_id="cedar-ridge", name="Smuggled"))
        with pytest.raises(Exception):
            s.flush()
    with fsp.factory() as s:  # no org set at all: no rows visible
        assert s.scalar(text("SELECT count(*) FROM customers")) == 0
        assert s.scalar(text("SELECT count(*) FROM service_types")) == 0
    for org in CONFIGS:  # clean up the shared test database, one org at a time (RLS applies to deletes too)
        with org_session(fsp.factory, org) as s:
            for table in ("idempotency_keys", "job_notes", "jobs", "assets", "sites", "customers", "service_types",
                          "technicians", "org_counters"):
                s.execute(text(f"DELETE FROM {table}"))
            s.commit()
    with fsp.engine.begin() as conn:
        conn.execute(text("DELETE FROM orgs"))


# ---------------------------------------------------------------- M16: availability and jobs
def next_monday() -> date:
    day = date.today() + timedelta(days=7)
    return day + timedelta(days=(7 - day.weekday()) % 7)


def make_customer(fsp, org="cedar-ridge", zip_code="45801") -> str:
    return call(fsp, "POST", "/api/customers", org, json={"name": "Sched Customer", "phone": "555-010-5555",
                                                         "address": "1 Elm St", "zip": zip_code}).json()["id"]


def slots(fsp, org="cedar-ridge", day=None, **params) -> list[dict]:
    day = day or next_monday()
    response = call(fsp, "GET", "/api/availability", org,
                    params={"date_from": day.isoformat(), "date_to": day.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()["slots"]


def book(fsp, org="cedar-ridge", **body):
    return call(fsp, "POST", "/api/jobs", org, json=body)


def local(day: date, hhmm: str, tz: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), ZoneInfo(tz))


def test_availability_uses_each_orgs_local_working_hours(fsp):
    day = next_monday()
    cedar = slots(fsp, "cedar-ridge", service_code="DIAG-01")
    pacific = slots(fsp, "pacific-air", service_code="DIAG")
    # Cedar Ridge's Monday starts at 07:00 New York time; Pacific Air's at 08:00 Los Angeles time
    assert cedar[0]["local_start"].startswith(f"{day}T07:00")
    assert pacific[0]["local_start"].startswith(f"{day}T08:00")
    assert datetime.fromisoformat(cedar[0]["start"]) == local(day, "07:00", "America/New_York")
    assert datetime.fromisoformat(pacific[0]["start"]) == local(day, "08:00", "America/Los_Angeles")
    # a 90-minute diagnostic must end by 18:00
    latest = max(datetime.fromisoformat(s["local_start"]).strftime("%H:%M")
                 for s in slots(fsp, service_code="DIAG-01", limit=100))
    assert latest == "16:30"


def test_skills_and_service_zips_limit_who_is_offered(fsp):
    boiler = slots(fsp, "harbor-point", service_code="HP-BOILER", limit=100)
    assert boiler and {s["technician_name"] for s in boiler} == {"Sam Okafor"}
    techs = call(fsp, "GET", "/api/technicians").json()["technicians"]
    for tech in techs:  # only Marcus serves 45801 now
        zips = ["45801"] if tech["name"] == "Marcus Lee" else ["45830"]
        assert call(fsp, "PUT", f"/api/technicians/{tech['id']}", json={"service_zips": zips}).status_code == 200
    assert {s["technician_name"] for s in slots(fsp, zip="45801", limit=100)} == {"Marcus Lee"}
    bad = call(fsp, "PUT", f"/api/technicians/{techs[0]['id']}", json={"working_hours": {"mon": ["17:00", "08:00"]}})
    assert bad.status_code == 422


def test_double_booking_is_refused_and_auto_assign_uses_the_next_free_technician(fsp):
    cid, day = make_customer(fsp), next_monday()
    start = local(day, "09:00", "America/New_York").isoformat()
    first = book(fsp, customer_id=cid, service_code="DIAG-01", scheduled_start=start)
    assert first.status_code == 201 and first.json()["identifier"] == "JOB-1"
    taken = first.json()["technician_id"]
    clash = book(fsp, customer_id=cid, service_code="DIAG-01", scheduled_start=start, technician_id=taken)
    assert clash.status_code == 409 and "already has a job" in clash.json()["detail"]
    # 09:30 overlaps the 90-minute visit for that technician, so another one is assigned
    later = local(day, "09:30", "America/New_York").isoformat()
    second = book(fsp, customer_id=cid, service_code="DIAG-01", scheduled_start=later)
    assert second.status_code == 201 and second.json()["technician_id"] != taken
    offered = {(s["local_start"][11:16], s["technician_id"]) for s in slots(fsp, service_code="DIAG-01", limit=100)}
    assert ("09:00", taken) not in offered and ("09:30", taken) not in offered
    # only two technicians work Mondays, and both are busy at 09:30 now
    assert book(fsp, customer_id=cid, service_code="DIAG-01", scheduled_start=later).status_code == 409


def test_outside_working_hours_needs_the_dispatcher_override(fsp):
    cid, day = make_customer(fsp), next_monday()
    late = local(day, "17:30", "America/New_York").isoformat()
    assert book(fsp, customer_id=cid, service_code="DIAG-01", scheduled_start=late).status_code == 409
    tech = call(fsp, "GET", "/api/technicians").json()["technicians"][0]["id"]
    override = book(fsp, customer_id=cid, service_code="DIAG-01", scheduled_start=late, technician_id=tech,
                    allow_outside_hours=True)
    assert override.status_code == 201


def test_reschedule_and_cancel_keep_the_calendar_consistent(fsp):
    cid, day = make_customer(fsp), next_monday()
    tech = call(fsp, "GET", "/api/technicians").json()["technicians"][0]["id"]
    a = book(fsp, customer_id=cid, service_code="DIAG-01", technician_id=tech,
             scheduled_start=local(day, "08:00", "America/New_York").isoformat()).json()
    b = book(fsp, customer_id=cid, service_code="DIAG-01", technician_id=tech,
             scheduled_start=local(day, "13:00", "America/New_York").isoformat()).json()
    assert call(fsp, "PUT", f"/api/jobs/{b['id']}", json={"scheduled_start": a["scheduled_start"]}).status_code == 409
    assert call(fsp, "PUT", f"/api/jobs/{a['id']}", json={"status": "cancelled"}).json()["status"] == "cancelled"
    assert call(fsp, "PUT", f"/api/jobs/{b['id']}", json={"scheduled_start": a["scheduled_start"]}).status_code == 200
    # reactivating the cancelled job would put it on top of the moved one
    assert call(fsp, "PUT", f"/api/jobs/{a['id']}", json={"status": "scheduled"}).status_code == 409
    day_view = call(fsp, "GET", "/api/schedule", params={"date_from": day.isoformat(),
                                                         "date_to": day.isoformat()}).json()
    assert [j["identifier"] for j in day_view["jobs"]] == [b["identifier"]] and day_view["jobs"][0]["customer_name"]


def test_jobs_are_isolated_between_orgs_and_idempotent(fsp):
    cid, day = make_customer(fsp), next_monday()
    start = local(day, "10:00", "America/New_York").isoformat()
    body = {"customer_id": cid, "service_code": "DIAG-01", "scheduled_start": start}
    first = call(fsp, "POST", "/api/jobs", json=body, headers={"Idempotency-Key": "job-1"})
    retry = call(fsp, "POST", "/api/jobs", json=body, headers={"Idempotency-Key": "job-1"})
    assert first.status_code == retry.status_code == 201 and first.json()["id"] == retry.json()["id"]
    job = first.json()
    assert call(fsp, "GET", f"/api/jobs/{job['id']}", "harbor-point").status_code == 404
    assert call(fsp, "PUT", f"/api/jobs/{job['id']}", "harbor-point", json={"status": "cancelled"}).status_code == 404
    assert book(fsp, "harbor-point", customer_id=cid, scheduled_start=start).status_code == 404
    harbor_customer = make_customer(fsp, "harbor-point", "43402")
    assert book(fsp, "harbor-point", customer_id=harbor_customer, scheduled_start=start,
                technician_id=job["technician_id"]).status_code == 404
    harbor_job = book(fsp, "harbor-point", customer_id=harbor_customer, service_code="HP-DIAG", scheduled_start=start)
    assert harbor_job.status_code == 201 and harbor_job.json()["identifier"] == "JOB-1"  # numbered per contractor
    week = {"date_from": day.isoformat(), "date_to": (day + timedelta(days=6)).isoformat()}
    jobs = call(fsp, "GET", "/api/schedule", "harbor-point", params=week).json()["jobs"]
    assert {j["customer_id"] for j in jobs} == {harbor_customer}


@pytest.mark.skipif(not os.environ.get("FSP_TEST_DATABASE_URL"), reason="needs FSP_TEST_DATABASE_URL (Postgres)")
def test_postgres_concurrent_bookings_of_one_slot_succeed_once():
    from concurrent.futures import ThreadPoolExecutor

    fsp = build(os.environ["FSP_TEST_DATABASE_URL"])
    try:
        cid = make_customer(fsp)
        tech = call(fsp, "GET", "/api/technicians").json()["technicians"][0]["id"]
        body = {"customer_id": cid, "service_code": "DIAG-01", "technician_id": tech,
                "scheduled_start": local(next_monday(), "11:00", "America/New_York").isoformat()}
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(lambda _: book(fsp, **body).status_code, range(6)))
        assert sorted(codes) == [201] + [409] * 5
    finally:
        for org in CONFIGS:
            with org_session(fsp.factory, org) as s:
                for table in ("idempotency_keys", "job_notes", "jobs", "assets", "sites", "customers",
                              "service_types", "technicians", "org_counters"):
                    s.execute(text(f"DELETE FROM {table}"))
                s.commit()
        with fsp.engine.begin() as conn:
            conn.execute(text("DELETE FROM orgs"))
