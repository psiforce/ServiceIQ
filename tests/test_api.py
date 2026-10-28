"""M11: API service, persistence, escalation and privacy (fake assistant, temporary SQLite; no cost)."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from serviceiq.api.app import create_app
from serviceiq.assistant import AssistantResponse, Citation
from serviceiq.config import PROJECT_ROOT, load_settings
from serviceiq.storage.db import make_engine, provision_tenant, purge_old_conversations, session_factory
from serviceiq.storage.models import Conversation, Escalation, Feedback, Message


class FakeAssistant:
    def __init__(self):
        self.calls = []

    def ask(self, question, tenant_id, history=None):
        self.calls.append((question, tenant_id, history))
        if "gas" in question:
            return AssistantResponse(status="emergency", answer="Leave the building now and call 911.",
                                     escalation={"priority": "P1", "reason": "gas"}, latency_ms={"total": 1})
        if "guarantee" in question:
            return AssistantResponse(status="answered", answer="Ranges are $5,800-$11,500 [1].",
                                     escalation={"priority": "P2", "reason": "binding quote requested"},
                                     latency_ms={"total": 900}, citations=[self._citation()])
        return AssistantResponse(status="answered", answer="A diagnostic visit is $89 [1].", latency_ms={"total": 800},
                                 citations=[self._citation()], debug={"standalone_question": None})

    @staticmethod
    def _citation():
        return Citation(1, "crc-price-book-2026", 3, "2026 Service Price Book", "admin-upload", "Diagnostic",
                        "price_book", "price_low_usd: 89", "full text")


@pytest.fixture
def env(tmp_path):
    settings = load_settings(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                             outbox_dir=tmp_path / "outbox", rate_limit_per_minute=5,
                             agent_enabled=False)  # MVP question path; the agent path is tested in test_agent_api
    engine = make_engine(settings.database_url)
    with session_factory(engine)() as s:
        keys = {t: provision_tenant(s, t, t.title()) for t in ("cedar-ridge", "harbor-point")}
    assistant = FakeAssistant()
    client = TestClient(create_app(settings, assistant=assistant, engine=engine))
    return SimpleNamespace(client=client, keys=keys, assistant=assistant, engine=engine, settings=settings,
                           outbox=tmp_path / "outbox")


def post(env, path, body, tenant="cedar-ridge"):
    return env.client.post(path, json=body, headers={"X-Widget-Key": env.keys[tenant]})


def test_health_needs_no_key(env):
    assert env.client.get("/health").json() == {"status": "ok"}


def test_chat_requires_a_valid_widget_key(env):
    assert env.client.post("/v1/chat", json={"message": "hi"}).status_code == 401
    assert env.client.post("/v1/chat", json={"message": "hi"}, headers={"X-Widget-Key": "wrong"}).status_code == 401


def test_chat_stores_redacted_conversation_and_uses_history(env):
    first = post(env, "/v1/chat", {"message": "How much is a diagnostic? Call me at 555-123-4567"}).json()
    assert first["status"] == "answered" and first["citations"][0]["document_id"] == "crc-price-book-2026"
    second = post(env, "/v1/chat", {"message": "What about after hours?", "conversation_id": first["conversation_id"]})
    assert second.status_code == 200 and second.json()["conversation_id"] == first["conversation_id"]
    # the tenant comes from the key, and the follow-up gets the earlier (redacted) turn as history
    _, tenant, history = env.assistant.calls[-1]
    assert tenant == "cedar-ridge"
    assert history == [("How much is a diagnostic? Call me at [PHONE]", "A diagnostic visit is $89 [1].")]
    with session_factory(env.engine)() as s:
        stored = [m.content for m in s.scalars(select(Message).where(Message.role == "customer"))]
    assert all("555-123-4567" not in m for m in stored)


def test_tenants_cannot_touch_each_others_data(env):
    cedar = post(env, "/v1/chat", {"message": "How much is a diagnostic?"}).json()
    as_harbor = post(env, "/v1/chat", {"message": "hi", "conversation_id": cedar["conversation_id"]}, "harbor-point")
    assert as_harbor.status_code == 404
    assert post(env, "/v1/feedback", {"message_id": cedar["message_id"], "rating": "down"}, "harbor-point").status_code == 404
    callback = {"conversation_id": cedar["conversation_id"], "name": "X", "phone": "555-000-1111"}
    assert post(env, "/v1/escalations", callback, "harbor-point").status_code == 404


def test_emergency_opens_p1_ticket_and_notifies(env):
    reply = post(env, "/v1/chat", {"message": "I smell gas"}).json()
    assert reply["status"] == "emergency" and reply["escalation"]["priority"] == "P1" and reply["escalation"]["ticket_id"]
    with session_factory(env.engine)() as s:
        ticket = s.get(Escalation, reply["escalation"]["ticket_id"])
        assert ticket.priority == "P1" and ticket.notified_at is not None
        assert ticket.handoff_packet["transcript"][0]["text"] == "I smell gas"
    email = next(env.outbox.glob("*-P1-*.eml")).read_text(encoding="utf-8")
    assert "[P1] ServiceIQ handoff" in email and "Customer: I smell gas" in email


def test_callback_attaches_contact_to_flagged_ticket(env):
    reply = post(env, "/v1/chat", {"message": "Can you guarantee $6,000?"}).json()
    assert reply["escalation"]["priority"] == "P2" and reply["escalation"]["ticket_id"] is None  # offered, not opened
    result = post(env, "/v1/escalations", {"conversation_id": reply["conversation_id"], "message_id": reply["message_id"],
                                           "name": "Dana", "phone": "555-222-3333", "preferred_time": "mornings"})
    assert result.status_code == 200 and result.json()["priority"] == "P2"
    with session_factory(env.engine)() as s:
        ticket = s.get(Escalation, result.json()["ticket_id"])
        assert (ticket.contact_name, ticket.contact_phone, ticket.reason) == ("Dana", "555-222-3333",
                                                                              "binding quote requested")
    assert "Phone: 555-222-3333" in next(env.outbox.glob("*-P2-*.eml")).read_text(encoding="utf-8")


def test_feedback_is_recorded_and_validated(env):
    reply = post(env, "/v1/chat", {"message": "How much is a diagnostic?"}).json()
    assert post(env, "/v1/feedback", {"message_id": reply["message_id"], "rating": "sideways"}).status_code == 422
    assert post(env, "/v1/feedback", {"message_id": reply["message_id"], "rating": "up",
                                      "comment": "great, email me at a@b.com"}).json() == {"status": "recorded"}
    with session_factory(env.engine)() as s:
        assert s.scalar(select(Feedback)).comment == "great, email me at [EMAIL]"


def test_rate_limit_per_widget_key(env):
    codes = [post(env, "/v1/chat", {"message": "How much is a diagnostic?"}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    assert post(env, "/v1/chat", {"message": "hi"}, "harbor-point").status_code == 200  # other tenants unaffected


def test_purge_removes_old_conversations(env):
    old = post(env, "/v1/chat", {"message": "I smell gas"}).json()
    recent = post(env, "/v1/chat", {"message": "How much is a diagnostic?"}).json()
    with session_factory(env.engine)() as s:
        s.get(Conversation, old["conversation_id"]).started_at = datetime.now(timezone.utc) - timedelta(days=100)
        s.commit()
        assert purge_old_conversations(s, 90) == 1
        assert s.get(Conversation, old["conversation_id"]) is None and s.scalar(select(Escalation)) is None
        assert s.get(Conversation, recent["conversation_id"]) is not None


def test_language_models_only_see_redacted_text(monkeypatch):
    """The guard's local rules see raw text; rewrite, retrieval and generation get the redacted question."""
    from serviceiq.assistant import Assistant
    from serviceiq.generation import answer as answer_module
    from serviceiq.guardrails import guard as guard_module
    from serviceiq.llm import Usage

    seen = {}
    monkeypatch.setattr(guard_module, "classify", lambda client, model, text: (
        seen.setdefault("guard", text) and {"emergency": "none", "intent": "hvac_question",
                                            "injection_attempt": False, "reason": "stub"}, None))
    monkeypatch.setattr(answer_module, "chat_json", lambda client, model, system, user, schema, name: (
        seen.setdefault("generation", user) and {"status": "insufficient_evidence", "sentences": [],
                                                 "needs_human": True, "escalation_reason": "", "calculations": []},
        Usage(model)))
    retriever = SimpleNamespace(embedding_tokens=0,
                                search=lambda q, *a, **k: seen.setdefault("search", q) and [
                                    Citation(1, "d", 1, "t", "s", "", "x", "y")] and [])
    settings = SimpleNamespace(data_dir=PROJECT_ROOT / "data", guard_model="m", answer_model="m",
                               serviceiq_as_of_date=date(2026, 10, 26), reranker="none", retrieval_top_k=10,
                               evidence_k=5, max_chunks_per_doc=0)
    Assistant(settings, retriever=retriever, client=object()).ask("Is 555-123-4567 in your area? email a@b.com",
                                                                   "cedar-ridge")
    assert "555-123-4567" not in seen["guard"] and "[PHONE]" in seen["guard"]
    assert seen["search"] == "Is [PHONE] in your area? email [EMAIL]"
