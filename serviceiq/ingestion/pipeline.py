"""Ingestion pipeline: the steps chained together. Grows module by module (embed/index in M4)."""
from langchain_core.documents import Document

from serviceiq.config import Settings
from serviceiq.ingestion.chunking import chunk_documents
from serviceiq.ingestion.cleaning import clean_documents
from serviceiq.ingestion.loaders import load_all


def prepare_chunks(settings: Settings, tenant: str = "all") -> list[Document]:
    """Load -> clean -> chunk for one tenant (or all)."""
    cleaned, _ = clean_documents(load_all(settings.data_dir, tenant))
    return chunk_documents(cleaned, settings.chunk_target_tokens, settings.chunk_overlap_tokens)
