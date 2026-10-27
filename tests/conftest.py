"""Shared fixtures: load and clean the corpus once per test session (PDF parsing is the slow part)."""
import pytest

from serviceiq.config import PROJECT_ROOT
from serviceiq.ingestion.cleaning import clean_documents
from serviceiq.ingestion.loaders import load_all

DATA = PROJECT_ROOT / "data"


@pytest.fixture(scope="session")
def loaded_docs():
    return load_all(DATA)


@pytest.fixture(scope="session")
def cleaned_corpus(loaded_docs):
    """(documents by ID, stats by ID) after cleaning. clean_documents copies parts, so loaded_docs is untouched."""
    docs, stats = clean_documents(loaded_docs)
    return {d.entry.document_id: d for d in docs}, stats
