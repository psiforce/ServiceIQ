"""M2: cleaning."""
import csv

import pytest
from langchain_core.documents import Document

from serviceiq.config import PROJECT_ROOT
from serviceiq.ingestion.cleaning import clean_documents, clean_text, content_hash, html_to_markdown
from serviceiq.ingestion.loaders import LoadedDocument
from serviceiq.ingestion.manifest import read_manifest

DATA = PROJECT_ROOT / "data"
BOILERPLATE = ["We use cookies", "Accept", "Privacy | Terms", "Careers</a>", "© 2026", "<", "Call (555) 014-2200"]


@pytest.fixture(scope="module")
def cleaned(cleaned_corpus):
    return cleaned_corpus


def test_html_boilerplate_removed(cleaned):
    docs, _ = cleaned
    html_docs = [d for d in docs.values() if d.entry.format == "html"]
    assert len(html_docs) == 35
    for d in html_docs:
        text = d.parts[0].page_content
        for marker in BOILERPLATE:
            assert marker not in text, (d.entry.document_id, marker)


def test_html_structure_preserved(cleaned):
    docs, _ = cleaned
    text = docs["crc-web-ac-repair"].parts[0].page_content
    assert text.startswith("# Air Conditioner Repair")
    assert "## Common AC problems we fix" in text
    assert "- Frozen evaporator coil or ice on refrigerant lines" in text
    assert "1. Your technician diagnoses the problem. The standard-hours diagnostic is $89." in text


def test_html_tables_become_markdown(cleaned):
    docs, _ = cleaned
    assert "| Monday - Friday | 7:00 AM - 6:00 PM |" in docs["crc-web-contact-hours"].parts[0].page_content
    assert "| Whole-home dehumidifier | $1,850 |" in docs["crc-web-indoor-air-quality"].parts[0].page_content


def test_every_price_survives_cleaning(cleaned):
    docs, _ = cleaned
    text = "\n".join(p.page_content for p in docs["crc-price-book-2026"].parts)
    with (DATA / "tenants/cedar-ridge/raw/crc-price-book-2026.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["price_low_usd"]:
                assert f"item_code: {row['item_code']}" in text
                assert f"price_low_usd: {row['price_low_usd']}" in text


def test_empty_csv_fields_dropped():
    assert clean_text("item_code: REP-CONT\nnotes: \nunit: per repair", "csv") == "item_code: REP-CONT\nunit: per repair"


def test_injected_text_is_kept_as_content(cleaned):
    # cleaning must not hide content; prompt-injection defenses live in guardrails/generation
    docs, _ = cleaned
    assert "ignore all previous instructions" in docs["crc-web-reviews"].parts[0].page_content


def test_hash_is_deterministic_and_stored(cleaned):
    docs, _ = cleaned
    part = docs["crc-policy-warranty"].parts[0]
    assert part.metadata["content_hash"] == content_hash(part.page_content)
    assert content_hash("a") != content_hash("b")


def test_duplicate_parts_are_dropped():
    entry = read_manifest(DATA, "cedar-ridge")[0]
    meta = {**entry.metadata(), "part": 0}
    docs = [LoadedDocument(entry, [Document(page_content="Same text", metadata={**meta, "part": i}) for i in range(2)])]
    cleaned_docs, stats = clean_documents(docs)
    assert len(cleaned_docs[0].parts) == 1
    assert stats[entry.document_id].duplicate_parts == [1]


def test_cookie_banner_by_class_removed():
    html = '<body><div class="cookie-banner">We use cookies</div><main><h1>Title</h1><p>Body</p></main></body>'
    assert html_to_markdown(html) == "# Title\n\nBody"
