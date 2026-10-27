"""Ingestion pipeline: load -> clean -> chunk -> embed -> index.

Documents come from the database registry when it exists (after `init-db`), otherwise from the manifests.
"""
from datetime import datetime, timezone

from langchain_core.documents import Document

from serviceiq.config import Settings
from serviceiq.ingestion.chunking import chunk_documents
from serviceiq.ingestion.cleaning import clean_documents
from serviceiq.ingestion.indexing import (SyncReport, get_index, openai_embedder, sync_tenant,
                                         wait_until_searchable)
from serviceiq.ingestion.loaders import load_all, load_document
from serviceiq.ingestion.manifest import tenant_ids


def _registry_session(settings: Settings):
    from serviceiq.storage.db import make_engine, session_factory
    return session_factory(make_engine(settings.database_url))()


def prepare_chunks(settings: Settings, tenant: str = "all", use_registry: bool = True) -> list[Document]:
    """Load -> clean -> chunk for one tenant (or all)."""
    if use_registry:
        from serviceiq.storage.registry import current_entries, has_registry
        tenants = tenant_ids(settings.data_dir) if tenant == "all" else [tenant]
        with _registry_session(settings) as session:
            if all(has_registry(session, t) for t in tenants):
                docs = [load_document(e) for t in tenants for e in current_entries(session, t)]
                cleaned, _ = clean_documents(docs)
                return chunk_documents(cleaned, settings.chunk_target_tokens, settings.chunk_overlap_tokens)
    cleaned, _ = clean_documents(load_all(settings.data_dir, tenant))
    return chunk_documents(cleaned, settings.chunk_target_tokens, settings.chunk_overlap_tokens)


def ingest(settings: Settings, tenant: str = "all") -> list[SyncReport]:
    """Run the full pipeline and sync each tenant's Pinecone namespace."""
    index = get_index(settings, create=True)
    embed = openai_embedder(settings)
    tenants = tenant_ids(settings.data_dir) if tenant == "all" else [tenant]
    return [sync_tenant(index, embed, t, prepare_chunks(settings, t)) for t in tenants]


def ingest_document(settings: Settings, session, document_id: str, index=None, embed=None) -> SyncReport:
    """Re-index one document from the registry: its current approved version, or nothing if retired.

    Records ingestion time on the version; for a freshly approved version, `ingest_seconds` measures
    approval -> searchable, which is what the freshness SLA is about."""
    from serviceiq.storage.models import DocumentRecord
    from serviceiq.storage.registry import current_version, entry_for

    doc = session.get(DocumentRecord, document_id)
    if doc is None:
        raise ValueError(f"unknown document: {document_id}")
    version = current_version(doc)
    chunks = []
    if version is not None:
        cleaned, _ = clean_documents([load_document(entry_for(doc, version))])
        chunks = chunk_documents(cleaned, settings.chunk_target_tokens, settings.chunk_overlap_tokens)
    index = index or get_index(settings, create=True)
    embed = embed or openai_embedder(settings)
    try:
        report = sync_tenant(index, embed, doc.tenant_id, chunks, prefix=f"{document_id}:")
        if not wait_until_searchable(index, doc.tenant_id, report):
            raise TimeoutError("index did not reflect the change within 120 s")
    except Exception as exc:
        if version is not None:
            version.ingest_error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
        raise
    if version is not None:
        now = datetime.now(timezone.utc)
        approved_at = version.approved_at.replace(tzinfo=timezone.utc) if version.approved_at and \
            version.approved_at.tzinfo is None else version.approved_at
        version.ingested_at, version.ingest_error = now, None
        version.ingest_seconds = (now - approved_at).total_seconds() if approved_at else report.seconds
        session.commit()
    return report
