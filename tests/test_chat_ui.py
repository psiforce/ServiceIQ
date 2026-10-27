"""M8: Streamlit chat UI, driven headlessly with a fake assistant (no cost)."""
import json

import pytest
from streamlit.testing.v1 import AppTest

from serviceiq import assistant as assistant_module
from serviceiq.assistant import AssistantResponse, Citation
from serviceiq.config import PROJECT_ROOT

APP = str(PROJECT_ROOT / "apps" / "chat.py")


class FakeAssistant:
    def __init__(self, settings):
        pass

    def ask(self, question, tenant_id):
        if "gas" in question:
            return AssistantResponse(status="emergency", answer="Leave the building now and call 911.",
                                     escalation={"priority": "P1", "reason": "gas"})
        return AssistantResponse(
            status="answered", answer="A diagnostic visit is $89 [1].",
            citations=[Citation(1, "crc-price-book-2026", 3, "2026 Service Price Book", "admin-upload", "Diagnostic",
                                "price_book", "service: Diagnostic service call price_low_usd: 89")],
            escalation={"priority": "P2", "reason": "binding quote requested"}, cost_usd=0.0012)


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(assistant_module, "Assistant", FakeAssistant)
    monkeypatch.setenv("SERVICEIQ_FEEDBACK_PATH", str(tmp_path / "feedback.jsonl"))
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    return at, tmp_path / "feedback.jsonl"


def test_initial_page_shows_tenant_and_suggestions(app):
    at, _ = app
    assert not at.exception
    assert at.title[0].value == "Cedar Ridge Comfort Systems"
    assert any(b.label == "How much is a diagnostic visit?" for b in at.button)


def test_question_shows_answer_with_escaped_price_and_sources(app):
    at, _ = app
    at.chat_input[0].set_value("How much is a diagnostic visit?").run()
    assert not at.exception
    texts = [m.value for m in at.markdown]
    assert r"A diagnostic visit is \$89 [1]." in texts
    assert any(e.label == "Sources (1)" for e in at.expander)


def test_emergency_shows_error_banner(app):
    at, _ = app
    at.chat_input[0].set_value("I smell gas").run()
    assert at.error and "Safety first" in at.error[0].value


def test_callback_request_is_recorded(app):
    at, path = app
    at.chat_input[0].set_value("Can you guarantee the price?").run()
    next(b for b in at.button if b.label == "Request a callback").click().run()
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["event"] == "callback_request" and entry["priority"] == "P2"
    assert at.success


def test_switching_tenant_changes_company(app):
    at, _ = app
    at.sidebar.selectbox[0].set_value("harbor-point").run()
    assert at.title[0].value == "Harbor Point Heating & Air"
