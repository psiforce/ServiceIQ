"""M3: structure-aware chunking."""
import csv
import re

import pytest
from langchain_core.documents import Document

from serviceiq.config import PROJECT_ROOT
from serviceiq.ingestion.chunking import chunk_document, chunk_documents, count_tokens
from serviceiq.ingestion.loaders import LoadedDocument
from serviceiq.ingestion.manifest import read_manifest

DATA = PROJECT_ROOT / "data"
TARGET, OVERLAP = 500, 75


@pytest.fixture(scope="module")
def chunks(cleaned_corpus):
    docs, _ = cleaned_corpus
    return chunk_documents(list(docs.values()), TARGET, OVERLAP)


def _of(chunks, doc_id):
    return [c for c in chunks if c.metadata["document_id"] == doc_id]


def test_every_chunk_within_token_target(chunks):
    assert chunks
    for c in chunks:
        assert c.metadata["token_count"] == count_tokens(c.page_content)
        assert c.metadata["token_count"] <= TARGET, c.metadata["chunk_id"]


def test_chunk_ids_unique_and_deterministic(chunks, cleaned_corpus):
    ids = [c.metadata["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids))
    docs, _ = cleaned_corpus
    again = chunk_documents(list(docs.values()), TARGET, OVERLAP)
    assert [(c.metadata["chunk_id"], c.page_content) for c in again] == \
           [(c.metadata["chunk_id"], c.page_content) for c in chunks]
    assert ids[0].count(":") == 2  # <document_id>:<version>:<n>


def test_metadata_carried_to_every_chunk(chunks):
    required = {"tenant_id", "document_id", "title", "source", "document_type", "authority_rank",
                "approval_status", "version", "effective_date", "expiry_date", "access_scope",
                "chunk_id", "section", "token_count", "content_hash"}
    for c in chunks:
        assert required <= c.metadata.keys(), c.metadata["chunk_id"]
        assert c.page_content.startswith(f"Document: {c.metadata['title']}")


def test_each_faq_is_one_complete_chunk(chunks):
    faq = _of(chunks, "crc-faq")
    with (DATA / "tenants/cedar-ridge/raw/crc-faq.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(faq) == len(rows)
    for chunk, row in zip(faq, rows):
        assert chunk.page_content.count("question:") == 1
        assert f"answer: {row['answer']}" in chunk.page_content


def test_price_rows_grouped_by_category_never_split(chunks):
    price = _of(chunks, "crc-price-book-2026")
    with (DATA / "tenants/cedar-ridge/raw/crc-price-book-2026.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    text = "\n".join(c.page_content for c in price)
    for row in rows:
        assert text.count(f"item_code: {row['item_code']}\n") == 1, row["item_code"]
    for c in price:
        category = re.search(r"^Category: (.+)$", c.page_content, re.M).group(1)
        assert set(re.findall(r"^category: (.+)$", c.page_content, re.M)) == {category}


def test_pdf_table_rows_stay_whole(chunks):
    manual = "\n".join(c.page_content for c in _of(chunks, "mfr-aerotherm-ag96-furnace-manual"))
    assert ("| 3 flashes | Pressure switch did not close (possible blocked vent or drain) | Check that outdoor vent "
            "pipes are not blocked by snow or debris, then call for service |") in manual


def test_heading_stays_with_its_body(chunks):
    warranty = _of(chunks, "crc-policy-warranty")
    owner = [c for c in warranty if "## 6. Transfer" in c.page_content]
    assert len(owner) == 1 and "$50 transfer fee" in owner[0].page_content


def test_page_break_does_not_split_a_sentence(chunks):
    text = "\n".join(c.page_content for c in _of(chunks, "crc-policy-warranty"))
    assert "the standard diagnostic fee applies and you will receive a price" in text


def test_long_section_splits_with_overlap():
    entry = read_manifest(DATA, "cedar-ridge")[0]
    paragraphs = [f"Paragraph {i}. " + "The technician checks the system carefully. " * 12 for i in range(12)]
    content = "## Long section\n\n" + "\n\n".join(paragraphs)
    doc = LoadedDocument(entry.__class__(**{**entry.__dict__, "path": entry.path.with_suffix(".pdf")}),
                         [Document(page_content=content, metadata={**entry.metadata(), "part": 1})])
    pieces = chunk_document(doc, TARGET, OVERLAP)
    assert len(pieces) > 1
    for c in pieces:
        assert c.metadata["token_count"] <= TARGET
        assert "## Long section" in c.page_content  # heading repeated on every piece
    for a, b in zip(pieces, pieces[1:]):
        # overlap: the next piece starts (after its heading) with the tail of the previous piece
        carried = b.page_content.split("\n\n")[2]
        assert a.page_content.endswith(carried)
        assert 0 < count_tokens(carried) <= OVERLAP
