"""M9: evaluation harness - metrics, judges and report (stubbed LLM, no cost)."""
from serviceiq.assistant import AssistantResponse, Citation
from serviceiq.evaluation import judges
from serviceiq.evaluation.datasets import GoldenQuestion
from serviceiq.evaluation.run_eval import EvalRun, GoldenResult, _recall, compute_metrics, write_report
from serviceiq.llm import Usage


def question(qa_id="P01", expected="answer", relevant=("crc-price-book-2026",), critical=True):
    return GoldenQuestion(qa_id, "pricing", "How much?", expected, "It is $89.", relevant, critical, "dev", "")


def response(status="answered", latency=1000, retrieved=("crc-price-book-2026:3:0", "crc-faq:5:9")):
    return AssistantResponse(status=status, answer="It is $89 [1].", latency_ms={"total": latency}, cost_usd=0.001,
                             debug={"retrieved": list(retrieved)} if retrieved is not None else {},
                             citations=[Citation(1, "crc-price-book-2026", 3, "Price Book", "s", "", "price_book",
                                                 "snippet", "price_low_usd: 89")], context=["price_low_usd: 89"])


def result(q, r, correct=True, behavior=True, critical=False, claims=(2, 2), cited=(1, 1)):
    gr = GoldenResult(q, r, correctness={"correct": correct, "behavior_ok": behavior, "critical_error": critical,
                                         "explanation": "x"})
    if r.status == "answered":
        gr.faithfulness = {"claims": claims[1], "supported": claims[0], "cited_sentences": cited[1],
                           "cited_supported": cited[0], "unsupported": []}
    gr.recall = _recall(q, r)
    return gr


def test_recall_uses_document_ids_of_top_five_chunks():
    assert _recall(question(relevant=("crc-price-book-2026", "crc-faq")), response()) == 1.0
    assert _recall(question(relevant=("crc-policy-warranty",)), response()) == 0.0
    assert _recall(question(relevant=()), response()) is None
    assert _recall(question(), response(retrieved=None)) is None  # guard answered without retrieval


def test_metrics_and_targets():
    golden = [
        result(question("P01"), response(latency=1000)),
        result(question("P02"), response(latency=2000), correct=False, critical=True, claims=(1, 2), cited=(0, 1)),
        result(question("U01", expected="fallback", relevant=()), response("fallback", 6000, retrieved=None)),
    ]
    run = EvalRun("dev", "gpt-4.1-mini", "gpt-4.1", golden, [{"passed": True}], [{"passed": False}], Usage("gpt-4.1"))
    m = compute_metrics(run)
    assert m["faithfulness"] == 3 / 4 and m["citation_accuracy"] == 1 / 2
    assert m["answer_correctness"] == 0.5 and m["unsupported_handling"] == 1.0
    assert m["critical_errors"] == 1 and m["p95_latency_ms"] == 6000
    assert m["adversarial_pass_rate"] == 1.0 and m["multiturn_pass_rate"] == 0.0
    assert m["targets_met"]["unsupported_handling"] and not m["targets_met"]["critical_errors"]
    assert not m["targets_met"]["p95_latency_ms"]


def test_report_lists_failures_and_critical_rows(tmp_path):
    golden = [result(question("P01"), response()), result(question("P02"), response(), correct=False, critical=True)]
    run = EvalRun("dev", "m", "j", golden, [], [], Usage("j"))
    run.metrics = compute_metrics(run)
    path = write_report(run, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "| Critical pricing/policy errors | 1 | 0 | ❌ |" in text
    assert "**P02** **CRITICAL**" in text and "- [ ] **P01**" in text
    assert path.with_suffix(".csv").exists()


def test_faithfulness_judge_counts_claims_and_cited_sentences(monkeypatch):
    reply = {"sentences": [
        {"sentence_id": 1, "claims": [{"claim": "costs $89", "supported": True}], "cited_passages_support_sentence": True},
        {"sentence_id": 2, "claims": [{"claim": "free on Sundays", "supported": False}],
         "cited_passages_support_sentence": False},
        {"sentence_id": 3, "claims": [], "cited_passages_support_sentence": True}]}
    seen = {}

    def fake(client, model, system, user, schema, name):
        seen["user"] = user
        return reply, Usage(model, 10, 5)

    monkeypatch.setattr(judges, "chat_json", fake)
    out, _ = judges.faithfulness_judge(None, "gpt-4.1", "It costs $89 [1]. It is free on Sundays [1]. Happy to help!",
                                       ["price 89"], {1: "price 89"}, "(555) 014-2200")
    assert out == {"claims": 2, "supported": 1, "cited_sentences": 2, "cited_supported": 1,
                   "unsupported": ["free on Sundays"]}
    assert "S3: Happy to help!\n    cites: nothing" in seen["user"]
