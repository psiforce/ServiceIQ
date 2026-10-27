"""Ingestion pipeline: load -> clean -> chunk -> embed -> index."""
from langchain_core.documents import Document

from serviceiq.config import Settings
from serviceiq.ingestion.chunking import chunk_documents
from serviceiq.ingestion.cleaning import clean_documents
from serviceiq.ingestion.indexing import SyncReport, get_index, openai_embedder, sync_tenant
from serviceiq.ingestion.loaders import load_all
from serviceiq.ingestion.manifest import tenant_ids


def prepare_chunks(settings: Settings, tenant: str = "all") -> list[Document]:
    """Load -> clean -> chunk for one tenant (or all)."""
    cleaned, _ = clean_documents(load_all(settings.data_dir, tenant))
    return chunk_documents(cleaned, settings.chunk_target_tokens, settings.chunk_overlap_tokens)


def ingest(settings: Settings, tenant: str = "all") -> list[SyncReport]:
    """Run the full pipeline and sync each tenant's Pinecone namespace."""
    index = get_index(settings, create=True)
    embed = openai_embedder(settings)
    tenants = tenant_ids(settings.data_dir) if tenant == "all" else [tenant]
    return [sync_tenant(index, embed, t, prepare_chunks(settings, t)) for t in tenants]
