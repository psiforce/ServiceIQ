"""Clean loaded documents so only real content reaches chunking and embedding.

- HTML: drop navigation, headers, footers, cookie banners and scripts; convert the main
  content to Markdown (headings, lists, tables, quotes) so structure survives.
- CSV rows: drop empty "column:" lines.
- All formats: normalize whitespace and Unicode, then fingerprint each part with a
  SHA-256 content hash (stored as metadata) used to skip duplicate content.
- Lines repeated across most of a tenant's web pages (boilerplate that is not inside a
  <nav>/<footer> tag) are removed as a safety net.
"""
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from langchain_core.documents import Document

from serviceiq.ingestion.loaders import LoadedDocument, markdown_table

REMOVE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "iframe"]
BOILERPLATE_ATTR = re.compile(r"cookie|banner|newsletter|subscribe|share|breadcrumb|skip-link", re.I)
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "blockquote", "dt", "dd", "pre"}
REPEATED_LINE_MIN_PAGES = 3
REPEATED_LINE_MIN_SHARE = 0.5


@dataclass
class CleanStats:
    chars_before: int = 0
    chars_after: int = 0
    duplicate_parts: list[int] = field(default_factory=list)
    repeated_lines_removed: int = 0


def _inline_text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(REMOVE_TAGS):
        tag.decompose()
    for tag in soup.find_all(lambda t: BOILERPLATE_ATTR.search(" ".join(t.get("class", [])) + " " + t.get("id", ""))):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup

    blocks = []
    for el in root.find_all(BLOCK_TAGS):
        if any(parent.name in BLOCK_TAGS for parent in el.parents):
            continue  # nested inside a block already rendered (e.g. a <p> inside a <li>)
        if el.name == "table":
            rows = [[_inline_text(c) for c in tr.find_all(["th", "td"])] for tr in el.find_all("tr")]
            blocks.append(markdown_table(rows))
            continue
        text = _inline_text(el)
        if not text:
            continue
        if el.name[0] == "h" and el.name[1:].isdigit():
            text = "#" * int(el.name[1]) + " " + text
        elif el.name == "li":
            if el.parent is not None and el.parent.name == "ol":
                text = f"{el.parent.find_all('li', recursive=False).index(el) + 1}. {text}"
            else:
                text = "- " + text
        elif el.name == "blockquote":
            text = "> " + text
        blocks.append(text)
    if not blocks:  # unstructured page: fall back to its plain text
        blocks = [root.get_text("\n", strip=True)]
    return "\n\n".join(blocks)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_text(text: str, fmt: str) -> str:
    if fmt == "html":
        text = html_to_markdown(text)
    elif fmt == "csv":
        text = "\n".join(line for line in text.splitlines() if not re.fullmatch(r"[^:]+:\s*", line))
    return normalize_text(text)


def _remove_repeated_lines(docs: list[LoadedDocument], stats: dict[str, CleanStats]) -> None:
    """Drop lines that appear on most of a tenant's HTML pages (template text)."""
    html_docs = [d for d in docs if d.entry.format == "html"]
    if len(html_docs) < REPEATED_LINE_MIN_PAGES:
        return
    pages_per_line = Counter(line for d in html_docs
                             for line in {ln for p in d.parts for ln in p.page_content.splitlines() if ln.strip()})
    threshold = max(REPEATED_LINE_MIN_PAGES, REPEATED_LINE_MIN_SHARE * len(html_docs))
    repeated = {line for line, n in pages_per_line.items() if n >= threshold}
    if not repeated:
        return
    for d in html_docs:
        for p in d.parts:
            kept = [ln for ln in p.page_content.splitlines() if ln not in repeated]
            stats[d.entry.document_id].repeated_lines_removed += len(p.page_content.splitlines()) - len(kept)
            p.page_content = normalize_text("\n".join(kept))


def clean_documents(docs: list[LoadedDocument]) -> tuple[list[LoadedDocument], dict[str, CleanStats]]:
    """Clean every part, remove cross-page boilerplate, hash parts and drop duplicates per tenant."""
    stats: dict[str, CleanStats] = {}
    cleaned: list[LoadedDocument] = []
    for d in docs:
        s = stats[d.entry.document_id] = CleanStats(chars_before=d.char_count)
        parts = [Document(page_content=clean_text(p.page_content, d.entry.format), metadata=dict(p.metadata))
                 for p in d.parts]
        cleaned.append(LoadedDocument(entry=d.entry, parts=parts))

    for tenant in {d.entry.tenant_id for d in cleaned}:
        _remove_repeated_lines([d for d in cleaned if d.entry.tenant_id == tenant], stats)

    seen: dict[tuple[str, str], str] = {}
    for d in cleaned:
        kept = []
        for p in d.parts:
            if not p.page_content:
                continue
            p.metadata["content_hash"] = content_hash(p.page_content)
            key = (d.entry.tenant_id, p.metadata["content_hash"])
            if key in seen:
                stats[d.entry.document_id].duplicate_parts.append(p.metadata["part"])
                continue
            seen[key] = d.entry.document_id
            kept.append(p)
        d.parts = kept
        stats[d.entry.document_id].chars_after = d.char_count
    return cleaned, stats
