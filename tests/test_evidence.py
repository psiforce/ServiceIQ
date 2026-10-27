"""M10: evidence selection, reranking and model-parameter handling (no network)."""
from types import SimpleNamespace

from openai import BadRequestError

from serviceiq import llm
from serviceiq.retrieval.evidence import rerank, retrieve_evidence, select_evidence
from serviceiq.retrieval.retriever import RetrievedChunk


def chunk(doc, n):
    return RetrievedChunk(f"{doc}:1:{n}", 0.5, f"{doc} {n}", {"document_id": doc})


CHUNKS = [chunk("faq", 0), chunk("faq", 1), chunk("faq", 2), chunk("price", 0), chunk("policy", 0), chunk("price", 1)]


def ids(chunks):
    return [c.chunk_id for c in chunks]


def test_select_without_cap_keeps_order():
    assert ids(select_evidence(CHUNKS, 3)) == ["faq:1:0", "faq:1:1", "faq:1:2"]


def test_select_with_cap_diversifies_documents():
    assert ids(select_evidence(CHUNKS, 4, max_per_doc=1)) == ["faq:1:0", "price:1:0", "policy:1:0"]
    assert ids(select_evidence(CHUNKS, 4, max_per_doc=2)) == ["faq:1:0", "faq:1:1", "price:1:0", "policy:1:0"]


def test_rerank_orders_by_score_and_fails_safe():
    reranker = SimpleNamespace(scores=lambda q, cs: [0.1, 0.9, 0.5][:len(cs)])
    assert ids(rerank(reranker, "q", CHUNKS[:3])) == ["faq:1:1", "faq:1:2", "faq:1:0"]

    def broken(q, cs):
        raise ConnectionError("down")
    assert ids(rerank(SimpleNamespace(scores=broken), "q", CHUNKS[:3])) == ids(CHUNKS[:3])
    assert rerank(None, "q", CHUNKS) is CHUNKS


def test_retrieve_evidence_applies_settings():
    retriever = SimpleNamespace(search=lambda q, t, top_k: CHUNKS[:top_k])
    settings = SimpleNamespace(retrieval_top_k=6, evidence_k=2, max_chunks_per_doc=1)
    ordered, evidence, _ = retrieve_evidence(retriever, None, settings, "q", "t")
    assert len(ordered) == 6 and ids(evidence) == ["faq:1:0", "price:1:0"]


def test_zip_code_pins_matching_service_area_chunk():
    area = [RetrievedChunk("area:1:0", 0.3, "Coverage: Core\n\nzip_code: 45801\ncoverage: Core",
                           {"document_id": "area"}),
            RetrievedChunk("area:1:1", 0.2, "Coverage: Extended\n\nzip_code: 45830\ntrip_fee_usd: 35",
                           {"document_id": "area"})]

    def search(q, t, top_k, document_type=None):
        return area if document_type == "service_area" else CHUNKS[:top_k]

    settings = SimpleNamespace(retrieval_top_k=6, evidence_k=2, max_chunks_per_doc=1)
    _, evidence, _ = retrieve_evidence(SimpleNamespace(search=search), None, settings, "I live in 45830, cost?", "t")
    assert ids(evidence) == ["area:1:1", "faq:1:0", "price:1:0"]
    _, evidence, _ = retrieve_evidence(SimpleNamespace(search=search), None, settings, "no zip here", "t")
    assert "area:1:1" not in ids(evidence)


def test_chat_json_retries_without_temperature(monkeypatch):
    calls = []

    def create(**request):
        calls.append(dict(request))
        if "temperature" in request:
            raise BadRequestError("Unsupported value: 'temperature' does not support 0",
                                  response=SimpleNamespace(request=None, status_code=400, headers={}), body=None)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
                               choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))])

    monkeypatch.setattr(llm, "_NO_TEMPERATURE", set())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    reply, usage = llm.chat_json(client, "reasoning-model", "s", "u", {}, "n")
    assert reply == {"ok": True} and "temperature" in calls[0] and "temperature" not in calls[1]
    llm.chat_json(client, "reasoning-model", "s", "u", {}, "n")
    assert "temperature" not in calls[2]  # remembered: no failed first attempt next time
