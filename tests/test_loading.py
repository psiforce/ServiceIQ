"""M1: document loading."""
import csv

import pytest

from serviceiq.config import PROJECT_ROOT
from serviceiq.ingestion.loaders import load_all
from serviceiq.ingestion.manifest import read_manifest

DATA = PROJECT_ROOT / "data"


@pytest.fixture(scope="module")
def docs():
    return {d.entry.document_id: d for d in load_all(DATA)}


def test_every_manifest_document_loads(docs):
    assert len(docs) == 65
    for doc_id, doc in docs.items():
        assert doc.parts, doc_id
        assert all(p.page_content.strip() for p in doc.parts), doc_id


def test_metadata_matches_manifest(docs):
    for tenant in ["cedar-ridge", "harbor-point"]:
        for entry in read_manifest(DATA, tenant):
            meta = docs[entry.document_id].parts[0].metadata
            assert meta["tenant_id"] == tenant
            assert meta["approval_status"] == entry.approval_status
            assert meta["effective_date"] == entry.effective_date
            assert meta["expiry_date"] == entry.expiry_date
            assert meta["access_scope"] == entry.access_scope
            assert meta["authority_rank"] == entry.authority_rank


def test_csv_rows_keep_column_names(docs):
    faq = docs["crc-faq"]
    with (DATA / "tenants/cedar-ridge/raw/crc-faq.csv").open(encoding="utf-8") as f:
        assert len(faq.parts) == sum(1 for _ in csv.DictReader(f))
    first = faq.parts[0].page_content
    assert "question: How long has Cedar Ridge been in business?" in first
    assert first.count("answer:") == 1
    assert [p.metadata["part"] for p in faq.parts[:3]] == [0, 1, 2]


def test_pdf_keeps_headings_and_table_rows(docs):
    text = docs["mfr-aerotherm-ag96-furnace-manual"].parts[0].page_content
    assert "## Status light (LED) codes" in text
    # the LED code, its meaning and the action must stay on one table row, with spaces intact
    assert "| 3 flashes | Pressure switch did not close" in text
    assert "ﬁ" not in text  # ligatures normalized


def test_pdf_bullets_are_separate_items(docs):
    text = docs["mfr-aerotherm-ag96-furnace-manual"].parts[0].page_content
    assert "\n- Install carbon monoxide alarms" in text


def test_docx_keeps_headings_lists_and_tables(docs):
    payment = docs["crc-policy-payment"].parts[0].page_content
    assert payment.startswith("# Payment Policy")
    assert "## Accepted payment methods" in payment
    assert "- ACH bank transfer" in payment
    rebates = docs["crc-guide-rebates-incentives"].parts[0].page_content
    assert "| Offer | Amount |" in rebates


def test_html_is_loaded_raw_for_cleaning(docs):
    html = docs["crc-web-ac-repair"].parts[0].page_content
    assert "<nav>" in html  # boilerplate is removed in M2, not here
