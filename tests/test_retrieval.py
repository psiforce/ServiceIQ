"""M5: retrieval filters and search."""
import importlib.util
from datetime import date
from types import SimpleNamespace

import pytest

from serviceiq.config import PROJECT_ROOT
from serviceiq.evaluation.retrieval_eval import retrievable_document_ids
from serviceiq.ingestion.indexing import to_vector_metadata
from serviceiq.ingestion.manifest import read_manifest
from serviceiq.retrieval.retriever import Retriever, build_filter

DATA = PROJECT_ROOT / "data"
AS_OF = date(2026, 10, 26)


def matches(flt: dict, meta: dict) -> bool:
    """Evaluate a Pinecone metadata filter in Python (only the operators we use)."""
    ops = {"$eq": lambda a, b: a == b, "$lte": lambda a, b: a <= b, "$gte": lambda a, b: a >= b,
           "$in": lambda a, b: a in b}
    return all(ops[op](meta[field], value) for field, cond in flt.items() for op, value in cond.items())


def test_filter_shape():
    flt = build_filter(AS_OF)
    assert flt == {"approval_status": {"$eq": "approved"}, "effective_num": {"$lte": 20261026},
                   "expiry_num": {"$gte": 20261026}, "access_scope": {"$in": ["public"]}}


def test_filter_matches_reference_rules(cleaned_corpus):
    """The vector filter must allow exactly the documents data/scripts/validate_datasets.py calls retrievable."""
    spec = importlib.util.spec_from_file_location("validate_datasets", DATA / "scripts" / "validate_datasets.py")
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)

    from serviceiq.ingestion.chunking import chunk_documents
    docs, _ = cleaned_corpus
    chunks = chunk_documents(list(docs.values()))
    flt = build_filter(AS_OF)
    allowed_by_filter = {c.metadata["document_id"] for c in chunks if matches(flt, to_vector_metadata(c))}
    for tenant in ["cedar-ridge", "harbor-point"]:
        rows = {e.document_id: {"approval_status": e.approval_status, "access_scope": e.access_scope,
                                "effective_date": e.effective_date, "expiry_date": e.expiry_date}
                for e in read_manifest(DATA, tenant)}
        expected = {d for d, row in rows.items() if reference.retrievable(row, AS_OF)}
        assert allowed_by_filter & rows.keys() == expected
        assert expected == retrievable_document_ids(DATA, tenant, AS_OF)


@pytest.mark.parametrize("doc_id, day, allowed", [
    ("crc-web-fall-promo-2026", date(2026, 11, 30), True),   # last valid day (inclusive)
    ("crc-web-fall-promo-2026", date(2026, 12, 1), False),   # expired the next day
    ("crc-web-fall-promo-2026", date(2026, 9, 14), False),   # not yet effective
    ("crc-price-book-2025", date(2026, 10, 26), False),      # expired price book
    ("crc-internal-discount-guidelines", date(2026, 10, 26), False),  # internal only
])
def test_date_and_scope_boundaries(cleaned_corpus, doc_id, day, allowed):
    from serviceiq.ingestion.chunking import chunk_document
    docs, _ = cleaned_corpus
    meta = to_vector_metadata(chunk_document(docs[doc_id])[0])
    assert matches(build_filter(day), meta) is allowed


def test_search_uses_tenant_namespace_and_filter():
    captured = {}

    class FakeIndex:
        def query(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(matches=[SimpleNamespace(id="d:1:0", score=0.9,
                                                            metadata={"document_id": "d", "text": "hi"})])

    retriever = Retriever(settings=SimpleNamespace(serviceiq_as_of_date=AS_OF), index=FakeIndex(),
                          embed=lambda texts: ([[0.1, 0.2]], 3))
    results = retriever.search("question", "cedar-ridge", top_k=10)
    assert captured["namespace"] == "cedar-ridge" and captured["top_k"] == 10
    assert captured["filter"] == build_filter(AS_OF)
    assert results[0].document_id == "d" and results[0].text == "hi"
    assert retriever.embedding_tokens == 3


@pytest.mark.live
@pytest.mark.parametrize("query, forbidden", [
    ("What will your prices be in 2027?", "crc-price-book-2027-draft"),
    ("What was the AC tune-up price last year?", "crc-price-book-2025"),
    ("Can I get the spring cooling rebate of $1,000?", "crc-web-spring-rebate-2026"),
    ("What discounts can CSRs give customers?", "crc-internal-discount-guidelines"),
    ("Do you service boilers and water heaters?", "hp-faq"),
])
def test_live_traps_are_never_retrieved(query, forbidden):
    from serviceiq.config import get_settings
    results = Retriever(get_settings()).search(query, "cedar-ridge", top_k=10)
    assert results
    assert forbidden not in {r.document_id for r in results}
    assert all(r.metadata["tenant_id"] == "cedar-ridge" for r in results)
