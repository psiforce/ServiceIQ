"""Phase 4 (M19-M20) over HTTP: signed completion events, Stripe webhooks, the reviews endpoint (no cost, no network).

Stripe webhooks are signed here with a test secret exactly as Stripe signs them, and checked by the Stripe SDK.
"""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import func, select

from fieldservice import app as fsp_app
from fieldservice.config import FSPSettings
from serviceiq.agent import Agent
from serviceiq.agent import intake as intake_module
from serviceiq.agent.fieldservice_client import MemoryFieldService
from serviceiq.api.app import create_app, event_signature
from serviceiq.config import load_settings
from serviceiq.guardrails import guard as guard_module
from serviceiq.payments import MemoryProvider, StripeProvider
from serviceiq.storage.db import make_engine, provision_tenant, session_factory
from serviceiq.storage.models import Invoice
from tests.test_agent import FakeAssistant, fake_classify, fake_extract
from tests.test_agent_api import chat, run_intake
from tests.test_fieldservice import CONFIGS, book, call, make_customer, next_monday, local

SECRET, STRIPE_SECRET = "event-secret", "whsec_test_secret"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_module, "chat_json", fake_extract)
    monkeypatch.setattr(guard_module, "classify", fake_classify)
    settings = load_settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
                             outbox_dir=tmp_path / "outbox", fsp_webhook_secret=SecretStr(SECRET))
    engine = make_engine(settings.database_url)
    with session_factory(engine)() as s:
        keys = {t: provision_tenant(s, t, t.title()) for t in ("cedar-ridge", "harbor-point")}
    fsp, payments = MemoryFieldService(), MemoryProvider()
    agent = Agent(settings, assistant=FakeAssistant(), client=object(), fsp=fsp, checkpointer=InMemorySaver(),
                  payments=payments)
    client = TestClient(create_app(settings, engine=engine, agent=agent))
    return SimpleNamespace(client=client, keys=keys, engine=engine, fsp=fsp, agent=agent, payments=payments,
                           settings=settings)


def booked_job(env):
    conv, _ = run_intake(env)
    chat(env, {"message": "Confirm", "conversation_id": conv, "confirm": True})
    assert chat(env, {"message": "1", "conversation_id": conv})["status"] == "booked"
    job = env.fsp.jobs[0]
    env.fsp.customers[job["customer_id"]]["email"] = "jordan@example.com"
    return conv, job


def post_event(env, payload, secret=SECRET, timestamp=None):
    body = json.dumps(payload).encode()
    timestamp = timestamp or str(int(time.time()))
    return env.client.post("/v1/events/fieldservice", content=body, headers={
        "Content-Type": "application/json", "X-ServiceIQ-Timestamp": timestamp,
        "X-ServiceIQ-Signature": event_signature(secret, timestamp, body)})


def invoices(env):
    with session_factory(env.engine)() as s:
        return list(s.scalars(select(Invoice)))


# ---------------------------------------------------------------- completion events
def test_a_signed_completion_event_invoices_the_customer(env):
    _, job = booked_job(env)
    env.fsp.complete_job(job["id"])
    event = {"event": "job.completed", "org_id": "cedar-ridge", "job_id": job["id"]}
    response = post_event(env, event)
    assert response.status_code == 200 and response.json() == {"status": "sent"}
    assert post_event(env, event).json() == {"status": "duplicate"}  # the platform retried
    assert len(invoices(env)) == 1 and invoices(env)[0].status == "sent"


def test_unsigned_stale_or_unconfigured_events_are_refused(env):
    _, job = booked_job(env)
    env.fsp.complete_job(job["id"])
    event = {"event": "job.completed", "org_id": "cedar-ridge", "job_id": job["id"]}
    assert post_event(env, event, secret="wrong").status_code == 401
    assert post_event(env, event, timestamp=str(int(time.time()) - 3600)).status_code == 401
    assert env.client.post("/v1/events/fieldservice", json=event).status_code == 401
    assert post_event(env, {"event": "job.updated", "org_id": "cedar-ridge", "job_id": job["id"]}).json() == \
        {"status": "ignored"}
    assert post_event(env, event | {"org_id": "nobody"}).json() == {"status": "ignored"}
    assert invoices(env) == []
    unconfigured = TestClient(create_app(env.settings.model_copy(update={"fsp_webhook_secret": None}),
                                         engine=env.engine, agent=env.agent))
    assert unconfigured.post("/v1/events/fieldservice", json=event).status_code == 503


# ---------------------------------------------------------------- Stripe webhooks
def stripe_post(env, event: dict, secret=STRIPE_SECRET):
    payload = json.dumps(event)
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return env.client.post("/v1/payments/stripe", content=payload,
                           headers={"Stripe-Signature": f"t={timestamp},v1={signature}",
                                    "Content-Type": "application/json"})


def checkout_event(invoice: Invoice, event_type="checkout.session.completed", payment_status="paid",
                   event_id="evt_1"):
    return {"id": event_id, "object": "event", "type": event_type, "data": {"object": {
        "id": invoice.provider_session_id, "object": "checkout.session", "payment_status": payment_status,
        "payment_intent": None, "client_reference_id": invoice.invoice_id,
        "metadata": {"invoice_id": invoice.invoice_id}}}}


def test_stripe_events_are_verified_then_mark_the_invoice_paid(env):
    _, job = booked_job(env)
    env.fsp.complete_job(job["id"])
    post_event(env, {"event": "job.completed", "org_id": "cedar-ridge", "job_id": job["id"]})
    invoice = invoices(env)[0]
    env.agent._payments = StripeProvider("sk_test_unused", STRIPE_SECRET)  # only verifies here; no network call

    assert stripe_post(env, checkout_event(invoice), secret="whsec_wrong").status_code == 400
    assert stripe_post(env, checkout_event(invoice, payment_status="unpaid")).json() == {"status": "processing"}
    succeeded = checkout_event(invoice, "checkout.session.async_payment_succeeded", event_id="evt_2")
    assert stripe_post(env, succeeded).json() == {"status": "paid"}
    assert stripe_post(env, succeeded).json() == {"status": "duplicate"}  # Stripe retried
    assert stripe_post(env, checkout_event(invoice, "customer.created", event_id="evt_3")).json() == \
        {"status": "ignored"}
    paid = invoices(env)[0]
    assert paid.status == "paid" and paid.method == "ach"  # an ACH payment: processing first, then paid


def test_without_stripe_configured_no_webhook_is_accepted(env):
    _, job = booked_job(env)
    env.fsp.complete_job(job["id"])
    post_event(env, {"event": "job.completed", "org_id": "cedar-ridge", "job_id": job["id"]})
    assert stripe_post(env, checkout_event(invoices(env)[0])).status_code == 400  # memory provider refuses all
    assert invoices(env)[0].status == "sent"


# ---------------------------------------------------------------- reviews
def test_review_endpoint_after_payment(env):
    conv, job = booked_job(env)
    review = {"conversation_id": conv, "rating": 2, "comment": "Technician was late"}
    headers = {"X-Widget-Key": env.keys["cedar-ridge"]}
    assert env.client.post("/v1/reviews", json=review, headers=headers).status_code == 404  # nothing requested yet
    env.fsp.complete_job(job["id"])
    post_event(env, {"event": "job.completed", "org_id": "cedar-ridge", "job_id": job["id"]})
    with session_factory(env.engine)() as s:
        env.agent.on_payment(s, env.payments.event(invoices(env)[0].provider_session_id))
        s.commit()
    other = env.client.post("/v1/reviews", json=review, headers={"X-Widget-Key": env.keys["harbor-point"]})
    assert other.status_code == 404  # another company can't review this conversation
    assert env.client.post("/v1/reviews", json=review | {"rating": 0}, headers=headers).status_code == 422
    result = env.client.post("/v1/reviews", json=review, headers=headers).json()
    assert result["status"] == "recovery" and result["public_link"] in result["message"]


# ---------------------------------------------------------------- the platform's side
def test_completing_a_job_sends_one_signed_event(tmp_path, monkeypatch):
    from fieldservice.db import add_org
    from fieldservice.db import make_engine as fsp_engine
    from fieldservice.db import session_factory as fsp_sessions

    sent = []
    monkeypatch.setattr(fsp_app.httpx, "post", lambda url, content, timeout, headers: sent.append(
        (url, content, headers)) or SimpleNamespace(status_code=200))
    url = f"sqlite:///{(tmp_path / 'fsp.db').as_posix()}"
    engine = fsp_engine(url)
    keys = {org: add_org(fsp_sessions(engine), config) for org, config in CONFIGS.items()}
    settings = FSPSettings(fsp_database_url=url, serviceiq_webhook_url="https://serviceiq.test/v1/events/fieldservice",
                           serviceiq_webhook_secret=SECRET)
    fsp = SimpleNamespace(client=TestClient(fsp_app.create_app(settings, engine=engine)), keys=keys)
    start = local(next_monday(), "10:00", "America/New_York").isoformat()
    job = book(fsp, customer_id=make_customer(fsp), service_code="DIAG-01", scheduled_start=start).json()

    assert call(fsp, "PUT", f"/api/jobs/{job['id']}", json={"status": "in_progress"}).status_code == 200
    done = call(fsp, "PUT", f"/api/jobs/{job['id']}", json={"status": "completed", "price": 149,
                                                             "completion_notes": "Replaced capacitor"}).json()
    assert done["status"] == "completed" and done["price"] == 149 and done["completion_notes"] == "Replaced capacitor"
    call(fsp, "PUT", f"/api/jobs/{job['id']}", json={"status": "completed"})  # saved again: no second event
    assert len(sent) == 1
    target, body, headers = sent[0]
    assert target.endswith("/v1/events/fieldservice") and json.loads(body) == {
        "event": "job.completed", "org_id": "cedar-ridge", "job_id": job["id"]}
    assert headers["X-ServiceIQ-Signature"] == event_signature(SECRET, headers["X-ServiceIQ-Timestamp"], body)


def test_sweep_bills_completed_jobs_whose_event_was_lost(env):
    from serviceiq.agent.sweep import sweep_completions

    _, job = booked_job(env)
    with session_factory(env.engine)() as s:
        assert sweep_completions(s, env.agent) == []  # not completed yet
        env.fsp.complete_job(job["id"])
        done = sweep_completions(s, env.agent)
        assert [r["status"] for r in done] == ["sent"] and sweep_completions(s, env.agent) == []
        assert s.scalar(select(func.count()).select_from(Invoice)) == 1
