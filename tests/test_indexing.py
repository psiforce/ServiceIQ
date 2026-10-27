"""M4: embedding and index sync. Unit tests use an in-memory fake index (no cost)."""
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from serviceiq.ingestion.indexing import NO_EXPIRY, sync_tenant, to_vector_metadata


class FakeIndex:
    """Minimal stand-in for a Pinecone index that records the order of writes."""

    def __init__(self):
        self.store: dict[str, dict[str, dict]] = {}
        self.calls: list[str] = []

    def list(self, namespace=""):
        ids = list(self.store.get(namespace, {}))
        yield SimpleNamespace(vectors=[SimpleNamespace(id=i) for i in ids])

    def fetch(self, ids, namespace=""):
        ns = self.store.get(namespace, {})
        return SimpleNamespace(vectors={i: SimpleNamespace(metadata=ns[i]["metadata"]) for i in ids if i in ns})

    def upsert(self, vectors, namespace="", **_):
        self.calls.append("upsert")
        self.store.setdefault(namespace, {}).update({v["id"]: v for v in vectors})

    def delete(self, ids, namespace=""):
        self.calls.append("delete")
        for i in ids:
            self.store[namespace].pop(i, None)


def fake_embed(texts):
    fake_embed.calls += len(texts)
    return [[0.0] * 3 for _ in texts], 10 * len(texts)


fake_embed.calls = 0


def make_chunk(doc_id="doc-a", version=1, n=0, text="hello", tenant="t1", status="approved", expiry=""):
    from serviceiq.ingestion.cleaning import content_hash
    return Document(page_content=text, metadata={
        "tenant_id": tenant, "document_id": doc_id, "title": "T", "source": "s", "document_type": "faq",
        "authority_rank": 4, "approval_status": status, "version": version, "effective_date": "2026-01-01",
        "expiry_date": expiry, "access_scope": "public", "chunk_id": f"{doc_id}:{version}:{n}", "chunk_index": n,
        "section": "", "part_start": 1, "part_end": 1, "token_count": 1, "content_hash": content_hash(text)})


@pytest.fixture
def index():
    fake_embed.calls = 0
    return FakeIndex()


def test_vector_metadata_has_numeric_dates_and_text():
    meta = to_vector_metadata(make_chunk(expiry="2026-12-31"))
    assert meta["effective_num"] == 20260101 and meta["expiry_num"] == 20261231
    assert to_vector_metadata(make_chunk())["expiry_num"] == NO_EXPIRY
    assert meta["text"] == "hello"
    assert all(v is not None for v in meta.values())


def test_first_sync_indexes_only_approved_chunks(index):
    chunks = [make_chunk(n=0), make_chunk(n=1, text="b"), make_chunk("draft", status="pending")]
    report = sync_tenant(index, fake_embed, "t1", chunks)
    assert report.embedded == 2 and report.chunks == 2
    assert set(index.store["t1"]) == {"doc-a:1:0", "doc-a:1:1"}


def test_second_sync_is_a_no_op(index):
    chunks = [make_chunk(n=0), make_chunk(n=1, text="b")]
    sync_tenant(index, fake_embed, "t1", chunks)
    calls = fake_embed.calls
    report = sync_tenant(index, fake_embed, "t1", chunks)
    assert report.embedded == 0 and report.unchanged == 2 and report.deleted == 0
    assert fake_embed.calls == calls  # nothing re-embedded, so no cost


def test_changed_chunk_is_reembedded(index):
    sync_tenant(index, fake_embed, "t1", [make_chunk(n=0), make_chunk(n=1, text="b")])
    report = sync_tenant(index, fake_embed, "t1", [make_chunk(n=0), make_chunk(n=1, text="b changed")])
    assert report.embedded == 1
    assert index.store["t1"]["doc-a:1:1"]["metadata"]["text"] == "b changed"


def test_new_version_activates_before_old_version_is_retired(index):
    sync_tenant(index, fake_embed, "t1", [make_chunk(version=1, n=0), make_chunk(version=1, n=1, text="b")])
    index.calls.clear()
    report = sync_tenant(index, fake_embed, "t1", [make_chunk(version=2, n=0, text="v2")])
    assert set(index.store["t1"]) == {"doc-a:2:0"}
    assert report.deleted == 2
    assert index.calls == ["upsert", "delete"]  # no gap where the document is missing from search


def test_other_tenants_are_untouched(index):
    sync_tenant(index, fake_embed, "t1", [make_chunk()])
    sync_tenant(index, fake_embed, "t2", [make_chunk(tenant="t2", text="other")])
    assert set(index.store) == {"t1", "t2"}
    assert index.store["t1"]["doc-a:1:0"]["metadata"]["tenant_id"] == "t1"


@pytest.mark.live
def test_live_index_matches_approved_chunks():
    """Runs against the real Pinecone index: `pytest -m live`."""
    from serviceiq.config import get_settings
    from serviceiq.ingestion.indexing import get_index
    from serviceiq.ingestion.pipeline import prepare_chunks

    settings = get_settings()
    stats = get_index(settings).describe_index_stats()
    for tenant in ["cedar-ridge", "harbor-point"]:
        approved = [c for c in prepare_chunks(settings, tenant) if c.metadata["approval_status"] == "approved"]
        assert stats.namespaces[tenant].vector_count == len(approved)
