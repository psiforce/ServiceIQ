"""M14: the intake agent behind /v1/chat: contact form, confirmation, tickets, privacy of stored text (no cost)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from serviceiq.agent import Agent
from serviceiq.agent import intake as intake_module
from serviceiq.agent.fieldservice_client import MemoryFieldService
from serviceiq.api.app import create_app
from serviceiq.config import load_settings
from serviceiq.guardrails import guard as guard_module
from serviceiq.storage.db import make_engine, provision_tenant, purge_old_conversations, session_factory
from serviceiq.storage.models import Conversation, Escalation, Message, ServiceRequest, ToolCall
from tests.test_agent import CONTACT, FakeAssistant, fake_classify, fake_extract


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_module, "chat_json", fake_extract)
    monkeypatch.setattr(guard_module, "classify", fake_classify)
    settings = load_settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
                             outbox_dir=tmp_path / "outbox")
    engine = make_engine(settings.database_url)
    with session_factory(engine)() as s:
        keys = {t: provision_tenant(s, t, t.title()) for t in ("cedar-ridge", "harbor-point")}
    fsp = MemoryFieldService()
    agent = Agent(settings, assistant=FakeAssistant(), client=object(), fsp=fsp, checkpointer=InMemorySaver())
    client = TestClient(create_app(settings, engine=engine, agent=agent))
    return SimpleNamespace(client=client, keys=keys, engine=engine, fsp=fsp, agent=agent, outbox=tmp_path / "outbox")


def chat(env, body, tenant="cedar-ridge"):
    response = env.client.post("/v1/chat", json=body, headers={"X-Widget-Key": env.keys[tenant]})
    assert response.status_code == 200, response.text
    return response.json()


def run_intake(env):
    first = chat(env, {"message": "My AC is running but not cooling, can someone come out?"})
    conv = first["conversation_id"]
    for message in ("45801", "Tomorrow please, weekday mornings"):
        last = chat(env, {"message": message, "conversation_id": conv})
    assert last["needs_contact"] and last["stage"] == "collecting"
    summary = chat(env, {"message": "My contact details", "conversation_id": conv, "contact": CONTACT})
    assert summary["status"] == "confirming" and summary["service_request"]["status"] == "draft"
    return conv, summary


def test_intake_through_the_api_with_contact_form_and_confirm_button(env):
    conv, summary = run_intake(env)
    assert "Jordan Lee" in summary["answer"]  # shown to the customer...
    done = chat(env, {"message": "Confirm", "conversation_id": conv, "confirm": True})
    assert done["status"] == "submitted" and done["stage"] == "submitted" and done["service_request"]["id"]
    with session_factory(env.engine)() as s:
        stored = " ".join(m.content for m in s.scalars(select(Message)))
        request = s.scalar(select(ServiceRequest))
    assert "Jordan" not in stored and "4477" not in stored and "Oak St" not in stored  # ...but never stored
    assert request.conversation_id == conv and request.tenant_id == "cedar-ridge"


def test_conversations_belong_to_their_tenant(env):
    first = chat(env, {"message": "My AC is running but not cooling, can someone come out?"})
    other = env.client.post("/v1/chat", json={"message": "45801", "conversation_id": first["conversation_id"]},
                            headers={"X-Widget-Key": env.keys["harbor-point"]})
    assert other.status_code == 404


def test_emergency_during_intake_opens_a_p1_ticket(env):
    first = chat(env, {"message": "My AC is running but not cooling, can someone come out?"})
    emergency = chat(env, {"message": "I smell gas now", "conversation_id": first["conversation_id"]})
    assert emergency["status"] == "emergency" and emergency["escalation"]["ticket_id"]


def test_hand_off_after_contact_carries_the_contact_on_the_ticket(env):
    env.agent._fsp = MemoryFieldService(fail=True)
    conv, _ = run_intake(env)
    done = chat(env, {"message": "yes", "conversation_id": conv})
    assert done["status"] == "escalated" and done["escalation"]["ticket_id"]
    with session_factory(env.engine)() as s:
        ticket = s.get(Escalation, done["escalation"]["ticket_id"])
    assert ticket.contact_phone == CONTACT["phone"] and ticket.priority == "P3"
    assert list(env.outbox.glob("*P3*.eml"))


def test_purge_keeps_requests_but_removes_the_conversation_trail(env):
    conv, _ = run_intake(env)
    chat(env, {"message": "yes", "conversation_id": conv})
    forgotten = []
    with session_factory(env.engine)() as s:
        s.get(Conversation, conv).started_at = s.get(Conversation, conv).started_at.replace(year=2000)
        s.commit()
        assert purge_old_conversations(s, 90, forget=forgotten.append) == 1
        request = s.scalar(select(ServiceRequest))
        assert request is not None and request.conversation_id is None
        assert s.scalar(select(ToolCall)) is None
    assert forgotten == [conv]


def test_existing_databases_get_new_columns(tmp_path):
    """M16 added service_requests.job_ref; databases created by M14 get it on the next start."""
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE service_requests (request_id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(64))")
    make_engine(f"sqlite:///{path.as_posix()}")
    with sqlite3.connect(path) as conn:
        assert "job_ref" in {row[1] for row in conn.execute("PRAGMA table_info(service_requests)")}
