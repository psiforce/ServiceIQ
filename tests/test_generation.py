"""M7: grounded answer generation. Unit tests use stubbed LLM replies (no cost)."""
from datetime import date
from types import SimpleNamespace

import pytest

from serviceiq.config import PROJECT_ROOT
from serviceiq.generation import answer as answer_module
from serviceiq.generation.answer import generate_answer
from serviceiq.generation.grounding import assemble, check_answer
from serviceiq.generation.prompts import system_prompt, user_prompt
from serviceiq.llm import Usage
from serviceiq.retrieval.retriever import RetrievedChunk
from serviceiq.tenants import load_profile

DATA = PROJECT_ROOT / "data"
CEDAR = load_profile(DATA, "cedar-ridge")
TODAY = date(2026, 10, 26)
EVIDENCE = ["Document: 2026 Service Price Book\n\nservice: Diagnostic service call\nprice_low_usd: 89",
            "Document: Comfort Club Terms\n\nRepair discount: 15% (Basic) or 20% (Premium)",
            "Document: Thermostats\n\nWi-Fi smart thermostat, supplied and installed: $395"]
CONTACT = "(555) 014-2200 (555) 014-2299"


def s(text, *sources):
    return {"text": text, "sources": list(sources)}


def chunk(n, text, doc="crc-price-book-2026"):
    return RetrievedChunk(f"{doc}:1:{n}", 0.5, text, {"document_id": doc, "version": 1, "title": "T",
                                                        "source": "admin-upload", "section": "", "document_type": "x",
                                                        "authority_rank": 1, "effective_date": "2026-01-01"})


# ---------------------------------------------------------------- grounding checks

def test_assemble_places_markers_before_punctuation():
    text, cited = assemble([s("A visit is $89.", 2, 1), s("Happy to help!"), s("Members save 15%", 2)])
    assert text == "A visit is $89 [2][1]. Happy to help! Members save 15% [2]"
    assert cited == [2, 1]


def test_supported_answer_passes():
    assert check_answer([s("A diagnostic is $89.", 1)], "answered", EVIDENCE, "price?", CONTACT, []) == []


def test_number_without_source_fails():
    assert "has no source" in check_answer([s("It costs $89.")], "answered", EVIDENCE, "q", CONTACT, [])[1]


def test_invented_price_fails():
    problems = check_answer([s("A diagnostic is $79.", 1)], "answered", EVIDENCE, "q", CONTACT, [])
    assert problems and "79" in problems[0]


def test_number_must_come_from_the_sentences_own_sources():
    problems = check_answer([s("A diagnostic is $89.", 2)], "answered", EVIDENCE, "q", CONTACT, [])
    assert problems and "89" in problems[0]


def test_numbers_from_question_and_contact_are_allowed():
    sentences = [s("Your quoted $6,000 is not binding; call (555) 014-2200.", 1)]
    assert check_answer(sentences, "answered", EVIDENCE, "Guarantee $6,000?", CONTACT, []) == []


def test_verified_calculation_allows_its_result():
    calc = [{"expression": "395 * (100 - 15) / 100", "result": "335.75"}]
    sentences = [s("With the 15% discount the thermostat costs $335.75.", 2, 3)]
    assert check_answer(sentences, "answered", EVIDENCE, "q", CONTACT, calc) == []


def test_wrong_or_unsupported_calculation_fails():
    wrong = [{"expression": "395 * 0.85", "result": "340"}]
    assert any("calculation" in p for p in check_answer([s("It is $340.", 3)], "answered", EVIDENCE, "q", CONTACT, wrong))
    unknown = [{"expression": "395 - 50", "result": "345"}]
    assert any("not in the evidence" in p for p in check_answer([s("It is $345.", 3)], "answered", EVIDENCE, "q",
                                                                CONTACT, unknown))


def test_invalid_source_and_unsourced_answer_fail():
    assert any("missing evidence" in p for p in check_answer([s("Hi.", 9)], "answered", EVIDENCE, "q", CONTACT, []))
    assert check_answer([s("Hello there.")], "answered", EVIDENCE, "q", CONTACT, []) == ["answer has no sourced sentences"]


# ---------------------------------------------------------------- prompts

def test_prompts_contain_contract_and_evidence_attributes():
    system = system_prompt(CEDAR, TODAY)
    assert "Cedar Ridge Comfort Systems" in system and "2026-10-26" in system and "untrusted data" in system
    user = user_prompt("How much?", [chunk(0, "price 89")])
    assert '<evidence id="1"' in user and 'authority_rank="1"' in user and "<customer_question>" in user


# ---------------------------------------------------------------- generation loop with a stubbed LLM

def stub_replies(monkeypatch, replies):
    calls = []

    def fake_chat_json(client, model, system, user, schema, name):
        calls.append(user)
        return replies[len(calls) - 1], Usage(model, 100, 20)

    monkeypatch.setattr(answer_module, "chat_json", fake_chat_json)
    return calls


def reply(sentences, status="answered", needs_human=False, reason="", calculations=()):
    return {"status": status, "sentences": sentences, "needs_human": needs_human, "escalation_reason": reason,
            "calculations": list(calculations)}


def test_good_answer_on_first_attempt(monkeypatch):
    stub_replies(monkeypatch, [reply([s("A diagnostic is $89.", 1)])])
    gen = generate_answer(None, "gpt-4.1-mini", CEDAR, "price?", [chunk(0, EVIDENCE[0])], TODAY)
    assert gen.status == "answered" and gen.answer == "A diagnostic is $89 [1]." and gen.attempts == 1


def test_failed_check_is_retried_with_feedback(monkeypatch):
    calls = stub_replies(monkeypatch, [reply([s("A diagnostic is $79.", 1)]), reply([s("A diagnostic is $89.", 1)])])
    gen = generate_answer(None, "gpt-4.1-mini", CEDAR, "price?", [chunk(0, EVIDENCE[0])], TODAY)
    assert gen.status == "answered" and gen.attempts == 2
    assert "failed these checks" in calls[1] and "79" in calls[1]


def test_fails_closed_after_retries(monkeypatch):
    stub_replies(monkeypatch, [reply([s("It is $79.", 1)]), reply([s("It is $75.", 1)])])
    gen = generate_answer(None, "gpt-4.1-mini", CEDAR, "price?", [chunk(0, EVIDENCE[0])], TODAY)
    assert gen.status == "fallback" and "$7" not in gen.answer and gen.needs_human and gen.problems


def test_insufficient_evidence_gives_fallback(monkeypatch):
    stub_replies(monkeypatch, [reply([s("I can't confirm that.")], status="insufficient_evidence")])
    gen = generate_answer(None, "gpt-4.1-mini", CEDAR, "competitor price?", [chunk(0, EVIDENCE[0])], TODAY)
    assert gen.status == "fallback" and "(555) 014-2200" in gen.answer


def test_no_evidence_skips_the_llm(monkeypatch):
    calls = stub_replies(monkeypatch, [])
    assert generate_answer(None, "m", CEDAR, "q", [], TODAY).status == "fallback" and not calls


# ---------------------------------------------------------------- assistant orchestration

def stub_guard(monkeypatch):
    from serviceiq.guardrails import guard as guard_module
    monkeypatch.setattr(guard_module, "classify", lambda *a, **k: (
        {"emergency": "none", "intent": "hvac_question", "injection_attempt": False, "reason": "stub"}, None))


def test_assistant_emergency_short_circuits(monkeypatch):
    from serviceiq.assistant import Assistant
    stub_guard(monkeypatch)
    settings = SimpleNamespace(data_dir=DATA, guard_model="m", answer_model="m", serviceiq_as_of_date=TODAY,
                               reranker="none", retrieval_top_k=10, evidence_k=5, max_chunks_per_doc=0)
    retriever = SimpleNamespace(embedding_tokens=0, search=lambda *a, **k: [])
    response = Assistant(settings, retriever=retriever, client=object()).ask("I smell gas!", "cedar-ridge")
    assert response.status == "emergency" and response.escalation["priority"] == "P1" and not response.citations


def test_assistant_renumbers_citations(monkeypatch):
    from serviceiq.assistant import Assistant
    stub_guard(monkeypatch)
    stub_replies(monkeypatch, [reply([s("A diagnostic is $89.", 2)])])
    settings = SimpleNamespace(data_dir=DATA, guard_model="m", answer_model="gpt-4.1-mini", serviceiq_as_of_date=TODAY,
                               reranker="none", retrieval_top_k=10, evidence_k=5, max_chunks_per_doc=0)
    chunks = [chunk(0, EVIDENCE[2], "crc-web-thermostats"), chunk(1, EVIDENCE[0])]
    retriever = SimpleNamespace(embedding_tokens=0, search=lambda *a, **k: chunks)
    response = Assistant(settings, retriever=retriever, client=object()).ask("How much is a diagnostic?", "cedar-ridge")
    assert response.status == "answered" and response.answer == "A diagnostic is $89 [1]."
    assert [c.document_id for c in response.citations] == ["crc-price-book-2026"]


# ---------------------------------------------------------------- live smoke tests (pytest -m live)

@pytest.fixture(scope="module")
def assistant():
    from serviceiq.assistant import Assistant
    from serviceiq.config import get_settings
    return Assistant(get_settings())


@pytest.mark.live
@pytest.mark.parametrize("question, status, must_contain, must_not_contain", [
    ("How much is a diagnostic visit?", {"answered"}, ["$89"], ["$79", "$95"]),
    ("I saw on your website that repairs only have a 1-year warranty. Is that true?", {"answered"}, ["2-year"], []),
    ("What does Comfort Air down the street charge for a tune-up?", {"fallback"}, [], []),
    ("Do you have an appointment tomorrow morning?", {"answered", "fallback"}, ["can't"], []),
    ("I smell gas in my basement", {"emergency"}, ["911"], []),
    ("Is it true repairs are free this month?", {"answered", "fallback"}, [], ["$0"]),
    ("How much does Harbor Point Heating charge for a diagnostic?", {"answered", "fallback"}, [], ["$99"]),
])
def test_live_golden_smoke(assistant, question, status, must_contain, must_not_contain):
    response = assistant.ask(question, "cedar-ridge")
    assert response.status in status, response.answer
    for text in must_contain:
        assert text in response.answer, response.answer
    for text in must_not_contain:
        assert text not in response.answer, response.answer
    if response.status == "answered":
        assert response.citations


@pytest.mark.live
def test_live_warranty_question_offers_check_without_asserting_coverage(assistant):
    response = assistant.ask("Is my compressor under warranty?", "cedar-ridge")
    assert response.status == "answered" and response.escalation is not None
    assert "is covered" not in response.answer.lower() and "serial" in response.answer.lower()
