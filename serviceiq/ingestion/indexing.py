"""Embed chunks and keep each tenant's Pinecone namespace in sync with its approved knowledge.

Sync rules:
- One namespace per tenant (namespace = tenant_id): tenant isolation is physical, not just a filter.
- Only approved documents are indexed; drafts never reach the index.
- Expired and internal documents ARE indexed with their metadata; the retrieval filter (M5)
  excludes them. That keeps expiry automatic and lets tests prove the filter works.
- Idempotent: a chunk whose content_hash is unchanged is not re-embedded (no cost).
- Activate before retire: new/changed chunks are upserted first, and only then are vectors
  that no longer exist (old versions, removed chunks, unapproved docs) deleted, so a document
  never disappears from search mid-update.
"""
import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from langchain_core.documents import Document

from serviceiq.config import Settings

EMBED_BATCH = 96
FETCH_BATCH = 100
NO_EXPIRY = 99991231
EMBEDDING_PRICE_PER_MTOK = 0.02  # USD, text-embedding-3-small

Embedder = Callable[[list[str]], tuple[list[list[float]], int]]


def date_number(value: str) -> int:
    """'2026-10-26' -> 20261026, so Pinecone can range-filter dates ($lte/$gt work on numbers only)."""
    return int(value.replace("-", "")) if value else NO_EXPIRY


def as_of_number(as_of: date) -> int:
    return int(as_of.strftime("%Y%m%d"))


def to_vector_metadata(chunk: Document) -> dict:
    m = chunk.metadata
    meta = {k: m[k] for k in ("tenant_id", "document_id", "title", "source", "document_type", "authority_rank",
                              "approval_status", "version", "effective_date", "expiry_date", "access_scope",
                              "chunk_id", "chunk_index", "section", "part_start", "part_end", "token_count",
                              "content_hash")}
    meta["effective_num"] = date_number(m["effective_date"])
    meta["expiry_num"] = date_number(m["expiry_date"])
    meta["text"] = chunk.page_content
    # fingerprint of the metadata, so a metadata-only change (e.g. a new expiry date) is detected
    meta["meta_hash"] = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()[:16]
    return meta


def openai_embedder(settings: Settings) -> Embedder:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=5)

    def embed(texts: list[str]) -> tuple[list[list[float]], int]:
        vectors, tokens = [], 0
        for i in range(0, len(texts), EMBED_BATCH):
            resp = client.embeddings.create(model=settings.openai_embedding_model, input=texts[i:i + EMBED_BATCH])
            vectors += [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
            tokens += resp.usage.total_tokens
        return vectors, tokens

    return embed


def get_index(settings: Settings, create: bool = False):
    from pinecone import Pinecone, ServerlessSpec

    pc = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
    name = settings.pinecone_index_name
    if name not in pc.list_indexes().names():
        if not create:
            raise RuntimeError(f"Pinecone index '{name}' does not exist yet; run `python -m serviceiq ingest`")
        pc.create_index(name=name, dimension=settings.embedding_dimension, metric="cosine",
                        spec=ServerlessSpec(cloud=settings.pinecone_cloud, region=settings.pinecone_region))
    return pc.Index(name)


@dataclass
class SyncReport:
    tenant_id: str
    chunks: int = 0
    embedded: int = 0
    metadata_updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    embedding_tokens: int = 0
    seconds: float = 0.0
    upserted_ids: list[str] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)

    @property
    def embedding_cost(self) -> float:
        return self.embedding_tokens / 1_000_000 * EMBEDDING_PRICE_PER_MTOK


def _existing_ids(index, namespace: str, prefix: str | None = None) -> set[str]:
    ids = set()
    pages = index.list(namespace=namespace, prefix=prefix) if prefix else index.list(namespace=namespace)
    for page in pages:
        ids.update(item if isinstance(item, str) else item.id for item in (page if isinstance(page, list) else page.vectors))
    return ids


def _stored_hashes(index, namespace: str, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
    hashes = {}
    for i in range(0, len(ids), FETCH_BATCH):
        fetched = index.fetch(ids=list(ids[i:i + FETCH_BATCH]), namespace=namespace).vectors
        hashes.update({vid: ((v.metadata or {}).get("content_hash", ""), (v.metadata or {}).get("meta_hash", ""))
                       for vid, v in fetched.items()})
    return hashes


def sync_tenant(index, embed: Embedder, tenant_id: str, chunks: list[Document], prefix: str | None = None) -> SyncReport:
    """Make the tenant's namespace contain exactly the given approved chunks.

    With `prefix` (e.g. "crc-price-book-2026:") only vectors of that document are considered, so one document
    can be updated, expired or retired without touching the rest of the namespace."""
    started = time.perf_counter()
    report = SyncReport(tenant_id=tenant_id)
    wanted = {c.metadata["chunk_id"]: c for c in chunks
              if c.metadata["tenant_id"] == tenant_id and c.metadata["approval_status"] == "approved"}
    report.chunks = len(wanted)

    existing = _existing_ids(index, tenant_id, prefix)
    stored = _stored_hashes(index, tenant_id, sorted(existing & wanted.keys()))
    to_embed = [c for cid, c in wanted.items() if stored.get(cid, ("", ""))[0] != c.metadata["content_hash"]]
    metadata_only = [c for cid, c in wanted.items() if cid in stored and stored[cid][0] == c.metadata["content_hash"]
                     and stored[cid][1] != to_vector_metadata(c)["meta_hash"]]
    report.unchanged = len(wanted) - len(to_embed) - len(metadata_only)

    for c in metadata_only:  # e.g. a new expiry date: update in place, no re-embedding
        index.update(id=c.metadata["chunk_id"], set_metadata=to_vector_metadata(c), namespace=tenant_id)
    report.metadata_updated = len(metadata_only)

    if to_embed:  # 1) activate new and changed chunks
        vectors, report.embedding_tokens = embed([c.page_content for c in to_embed])
        index.upsert(vectors=[{"id": c.metadata["chunk_id"], "values": v, "metadata": to_vector_metadata(c)}
                              for c, v in zip(to_embed, vectors)], namespace=tenant_id, show_progress=False)
        report.embedded = len(to_embed)
        report.upserted_ids = [c.metadata["chunk_id"] for c in to_embed]

    stale = sorted(existing - wanted.keys())
    if stale:  # 2) then retire what no longer exists
        for i in range(0, len(stale), 1000):
            index.delete(ids=stale[i:i + 1000], namespace=tenant_id)
        report.deleted = len(stale)
        report.deleted_ids = stale

    report.seconds = time.perf_counter() - started
    return report


def wait_until_searchable(index, namespace: str, report: SyncReport, timeout: float = 120.0,
                          poll: float = 1.0) -> bool:
    """Pinecone serverless is eventually consistent: wait until new chunks are returned by search and
    retired chunks are not. Returns False if that did not happen within `timeout` seconds."""
    if not report.upserted_ids and not report.deleted_ids:
        return True
    deadline = time.monotonic() + timeout
    probe = report.upserted_ids[0] if report.upserted_ids else None
    while time.monotonic() < deadline:
        ready = True
        if probe:
            fetched = index.fetch(ids=[probe], namespace=namespace).vectors
            vector = fetched.get(probe)
            if vector is None:
                ready = False
            else:  # searchable = a filtered query for this exact chunk finds it
                hits = index.query(vector=list(vector.values), top_k=1, namespace=namespace,
                                   filter={"chunk_id": {"$eq": probe}}).matches
                ready = bool(hits)
        if ready and report.deleted_ids:
            prefix = report.deleted_ids[0].rsplit(":", 2)[0] + ":"
            ready = not (set(report.deleted_ids) & _existing_ids(index, namespace, prefix))
        if ready:
            return True
        time.sleep(poll)
    return False
