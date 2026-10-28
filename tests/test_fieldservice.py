"""M14: field-service platform (multi-tenant port of OpenFieldService): auth, org isolation, idempotency (no cost).

Row-level-security tests need Postgres: set FSP_TEST_DATABASE_URL to a throwaway database reached as a normal
(non-superuser) role, since superusers bypass row-level security.
"""
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fieldservice.app import create_app
from fieldservice.config import FSPSettings, PROJECT_ROOT
from fieldservice.db import OrgScope, add_org, make_engine, org_session, session_factory
from fieldservice.models import Customer, Technician

CONFIGS = {t: json.loads((PROJECT_ROOT / "data" / "tenants" / t / "fieldservice.json").read_text(encoding="utf-8"))
           for t in ("cedar-ridge", "harbor-point")}


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
            for table in ("idempotency_keys", "assets", "sites", "customers", "service_types", "technicians",
                          "org_counters"):
                s.execute(text(f"DELETE FROM {table}"))
            s.commit()
    with fsp.engine.begin() as conn:
        conn.execute(text("DELETE FROM orgs"))
