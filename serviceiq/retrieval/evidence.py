"""From retrieved candidates to the evidence the answer model sees: optional reranking, then selection.

- Reranking (optional): a cross-encoder scores each (question, chunk) pair jointly, which is more accurate
  than vector similarity. Hosted on Pinecone Inference; `bge-reranker-v2-m3` is open source, so it can be
  self-hosted later. If the reranker fails, the vector order is kept (fail-safe, never fail-closed).
- Selection: take the top `evidence_k` chunks, optionally allowing at most `max_chunks_per_doc` chunks from
  the same document so that one long FAQ or price book cannot crowd out other sources.
"""
import logging
import re
import time

from serviceiq.config import Settings
from serviceiq.retrieval.retriever import RetrievedChunk, Retriever

log = logging.getLogger(__name__)


def select_evidence(chunks: list[RetrievedChunk], k: int, max_per_doc: int = 0) -> list[RetrievedChunk]:
    if max_per_doc <= 0:
        return chunks[:k]
    picked, per_doc = [], {}
    for c in chunks:
        if per_doc.get(c.document_id, 0) < max_per_doc:
            picked.append(c)
            per_doc[c.document_id] = per_doc.get(c.document_id, 0) + 1
        if len(picked) == k:
            break
    return picked


class PineconeReranker:
    def __init__(self, settings: Settings, model: str):
        from pinecone import Pinecone

        self.pc = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
        self.model = model

    def scores(self, query: str, chunks: list[RetrievedChunk]) -> list[float]:
        """Relevance score per chunk, in the input order."""
        if not chunks:
            return []
        result = self.pc.inference.rerank(model=self.model, query=query, documents=[c.text for c in chunks],
                                          top_n=len(chunks), return_documents=False)
        scores = [0.0] * len(chunks)
        for item in result.data:
            scores[item.index] = item.score
        return scores


def rerank(reranker, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    if reranker is None:
        return chunks
    try:
        scores = reranker.scores(query, chunks)
    except Exception as exc:  # keep answering with vector order if the reranker is unavailable
        log.warning("reranker failed, keeping vector order: %s", exc)
        return chunks
    return [c for _, c in sorted(zip(scores, chunks), key=lambda pair: -pair[0])]


def make_reranker(settings: Settings):
    return None if settings.reranker == "none" else PineconeReranker(settings, settings.reranker)


ZIP_CODE = re.compile(r"(?<!\d)\d{5}(?!\d)")


def service_area_evidence(retriever: Retriever, query: str, tenant_id: str) -> list[RetrievedChunk]:
    """Exact lookup for ZIP codes: return the service-area chunks that list each ZIP in the question.
    Semantic search is unreliable for exact identifiers, and coverage/trip fees depend on them."""
    zips = set(ZIP_CODE.findall(query))
    if not zips:
        return []
    area = retriever.search(query, tenant_id, top_k=10, document_type="service_area")
    return [c for c in area if any(re.search(rf"zip_code: {z}\b", c.text) for z in zips)]


def retrieve_evidence(retriever: Retriever, reranker, settings: Settings, query: str,
                      tenant_id: str) -> tuple[list[RetrievedChunk], list[RetrievedChunk], float]:
    """Return (candidates in final order, evidence for the model, rerank seconds)."""
    candidates = retriever.search(query, tenant_id, top_k=settings.retrieval_top_k)
    started = time.perf_counter()
    ordered = rerank(reranker, query, candidates)
    rerank_seconds = time.perf_counter() - started if reranker else 0.0
    evidence = select_evidence(ordered, settings.evidence_k, settings.max_chunks_per_doc)
    pinned = [c for c in service_area_evidence(retriever, query, tenant_id)
              if c.chunk_id not in {e.chunk_id for e in evidence}]
    return ordered, pinned + evidence, rerank_seconds
