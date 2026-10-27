"""Retrieval evaluation: does search put the right documents in front of the model?

For each golden question with labeled source documents, take the top-k retrieved chunks and
compare their document IDs with the labeled ones:
- Recall@k: share of the labeled documents that appear in the top k (target: Recall@5 >= 90%).
- Hit@k:    whether at least one labeled document appears in the top k.
- MRR:      1 / rank of the first relevant chunk (how high the first good evidence appears).
Every question (including unanswerable ones) is also checked for leaks: a retrieved chunk from
a document that should never be retrievable (draft, expired, internal, other tenant).
"""
import csv
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from serviceiq.evaluation.datasets import GoldenQuestion
from serviceiq.ingestion.manifest import read_manifest
from serviceiq.retrieval.retriever import Retriever


def retrievable_document_ids(data_dir: Path, tenant_id: str, as_of: date) -> set[str]:
    allowed = set()
    for e in read_manifest(data_dir, tenant_id):
        expiry = date.fromisoformat(e.expiry_date) if e.expiry_date else date.max
        if (e.approval_status == "approved" and e.access_scope == "public"
                and date.fromisoformat(e.effective_date) <= as_of <= expiry):
            allowed.add(e.document_id)
    return allowed


@dataclass
class QuestionResult:
    question: GoldenQuestion
    retrieved_docs: list[str]  # document ID per retrieved chunk, in rank order
    recall: float | None
    hit: bool | None
    reciprocal_rank: float | None
    leaked_docs: list[str] = field(default_factory=list)


@dataclass
class RetrievalReport:
    k: int
    results: list[QuestionResult]

    @property
    def scored(self) -> list[QuestionResult]:
        return [r for r in self.results if r.recall is not None]

    def mean(self, attr: str, results: list[QuestionResult] | None = None) -> float:
        values = [getattr(r, attr) for r in (results if results is not None else self.scored)]
        return statistics.mean(float(v) for v in values) if values else 0.0

    @property
    def leaks(self) -> list[QuestionResult]:
        return [r for r in self.results if r.leaked_docs]


def evaluate_retrieval(retriever: Retriever, questions: list[GoldenQuestion], tenant_id: str, data_dir: Path,
                       as_of: date, k: int = 5, top_k: int = 10) -> RetrievalReport:
    allowed = retrievable_document_ids(data_dir, tenant_id, as_of)
    results = []
    for q in questions:
        chunks = retriever.search(q.question, tenant_id, top_k=top_k, as_of=as_of)
        docs = [c.document_id for c in chunks]
        leaked = sorted({d for d in docs if d not in allowed})
        if not q.relevant_document_ids:
            results.append(QuestionResult(q, docs, None, None, None, leaked))
            continue
        relevant = set(q.relevant_document_ids)
        top = set(docs[:k])
        first = next((i + 1 for i, d in enumerate(docs) if d in relevant), None)
        results.append(QuestionResult(
            q, docs,
            recall=len(relevant & top) / len(relevant),
            hit=bool(relevant & top),
            reciprocal_rank=1 / first if first else 0.0,
            leaked_docs=leaked,
        ))
    return RetrievalReport(k=k, results=results)


def write_details(report: RetrievalReport, path: Path) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["qa_id", "category", "question", f"recall@{report.k}", "hit", "rr", "relevant_docs",
                    "missing_from_top_k", "retrieved_docs_in_rank_order", "leaked_docs"])
        for r in report.results:
            relevant = set(r.question.relevant_document_ids)
            missing = sorted(relevant - set(r.retrieved_docs[:report.k]))
            w.writerow([r.question.qa_id, r.question.category, r.question.question,
                        "" if r.recall is None else f"{r.recall:.2f}", "" if r.hit is None else r.hit,
                        "" if r.reciprocal_rank is None else f"{r.reciprocal_rank:.2f}",
                        ";".join(r.question.relevant_document_ids), ";".join(missing),
                        ";".join(r.retrieved_docs), ";".join(r.leaked_docs)])
