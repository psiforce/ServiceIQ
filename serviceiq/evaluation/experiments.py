"""M10 experiments: compare retrieval strategies and answer models against the same golden questions.

Retrieval experiment: each question is embedded and searched once (top 20) and each reranker scores those
20 candidates once; every variant is then computed from that cache, so variants differ only in strategy.
"""
import statistics
import time
from dataclasses import dataclass

from serviceiq.config import Settings
from serviceiq.evaluation.datasets import load_golden
from serviceiq.retrieval.evidence import PineconeReranker, select_evidence
from serviceiq.retrieval.retriever import Retriever

RERANKERS = ["bge-reranker-v2-m3", "pinecone-rerank-v0"]
VARIANTS = [  # name, candidates (top_k), reranker, max chunks per document, evidence size
    ("baseline: top 10 by vector score", 10, None, 0, 5),
    ("top 10, max 2 chunks per document", 10, None, 2, 5),
    ("top 10, max 1 chunk per document", 10, None, 1, 5),
    ("top 20, max 2 chunks per document", 20, None, 2, 5),
    ("rerank top 20 (bge-reranker-v2-m3)", 20, "bge-reranker-v2-m3", 0, 5),
    ("rerank top 20 (bge), max 2 per document", 20, "bge-reranker-v2-m3", 2, 5),
    ("rerank top 20 (bge), max 1 per document", 20, "bge-reranker-v2-m3", 1, 5),
    ("rerank top 20 (pinecone-rerank-v0), max 2 per document", 20, "pinecone-rerank-v0", 2, 5),
    ("rerank top 20 (bge), max 2 per document, 6 evidence chunks", 20, "bge-reranker-v2-m3", 2, 6),
]


@dataclass
class VariantResult:
    name: str
    evidence_k: int
    recall: float
    hit: float
    rerank_ms: float | None
    misses: list[str]


def retrieval_experiment(settings: Settings, split: str = "dev", tenant: str = "cedar-ridge") -> list[VariantResult]:
    retriever = Retriever(settings)
    rerankers = {m: PineconeReranker(settings, m) for m in RERANKERS}
    cache = []
    for q in load_golden(settings.data_dir, split):
        if not q.relevant_document_ids:
            continue
        candidates = retriever.search(q.question, tenant, top_k=20)
        scores, timings = {}, {}
        for model, reranker in rerankers.items():
            started = time.perf_counter()
            scores[model] = reranker.scores(q.question, candidates)
            timings[model] = (time.perf_counter() - started) * 1000
        cache.append((q, candidates, scores, timings))

    results = []
    for name, top_k, model, cap, k in VARIANTS:
        recalls, hits, misses = [], [], []
        for q, candidates, scores, _ in cache:
            pool = candidates[:top_k]
            if model:
                pool = [c for _, c in sorted(zip(scores[model][:top_k], pool), key=lambda pair: -pair[0])]
            docs = {c.document_id for c in select_evidence(pool, k, cap)}
            relevant = set(q.relevant_document_ids)
            recalls.append(len(relevant & docs) / len(relevant))
            hits.append(bool(relevant & docs))
            if relevant - docs:
                misses.append(q.qa_id)
        rerank_ms = statistics.mean(t[model] for *_, t in cache) if model else None
        results.append(VariantResult(name, k, statistics.mean(recalls), statistics.mean(hits), rerank_ms, misses))
    return results
