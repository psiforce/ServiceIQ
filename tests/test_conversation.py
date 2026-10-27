"""Conversational memory: follow-up rewriting (stubbed LLM, no cost)."""
from datetime import date
from types import SimpleNamespace

from serviceiq import conversation
from serviceiq.config import PROJECT_ROOT
from serviceiq.conversation import _format, rewrite_question
from serviceiq.llm import Usage


def test_no_history_means_no_llm_call(monkeypatch):
    monkeypatch.setattr(conversation, "chat_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert rewrite_question(None, "m", [], "How much is a tune-up?") == ("How much is a tune-up?", False, None)


def test_history_is_trimmed_and_markers_removed():
    history = [(f"q{i}", f"answer {i} [1][2]") for i in range(5)]
    text = _format(history, "what about that?")
    assert "q0" not in text and "q1" not in text and "q4" in text  # only the last 3 turns
    assert "[1]" not in text and "<latest_message>\nwhat about that?" in text


def test_follow_up_is_rewritten(monkeypatch):
    monkeypatch.setattr(conversation, "chat_json", lambda *a, **k: (
        {"standalone_question": "How much is an after-hours diagnostic?", "is_follow_up": True}, Usage("m", 50, 10)))
    standalone, follow_up, usage = rewrite_question(None, "m", [("How much is a diagnostic?", "$89 [1]")],
                                                    "what about after hours?")
    assert standalone == "How much is an after-hours diagnostic?" and follow_up and usage.input_tokens == 50


def test_assistant_searches_with_the_rewritten_question(monkeypatch):
    from serviceiq.assistant import Assistant
    from serviceiq.guardrails import guard as guard_module

    monkeypatch.setattr(guard_module, "classify", lambda *a, **k: (
        {"emergency": "none", "intent": "hvac_question", "injection_attempt": False, "reason": "stub"}, None))
    monkeypatch.setattr(conversation, "chat_json", lambda *a, **k: (
        {"standalone_question": "How much is an after-hours diagnostic?", "is_follow_up": True}, None))
    searched = []
    retriever = SimpleNamespace(embedding_tokens=0, search=lambda q, *a, **k: searched.append(q) or [])
    settings = SimpleNamespace(data_dir=PROJECT_ROOT / "data", guard_model="m", answer_model="m",
                               serviceiq_as_of_date=date(2026, 10, 26))
    response = Assistant(settings, retriever=retriever, client=object()).ask(
        "what about after hours?", "cedar-ridge", history=[("How much is a diagnostic?", "$89 [1]")])
    assert searched == ["How much is an after-hours diagnostic?"]
    assert response.debug["standalone_question"] == "How much is an after-hours diagnostic?"
