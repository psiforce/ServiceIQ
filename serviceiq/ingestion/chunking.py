"""Structure-aware chunking: split cleaned documents into self-contained, searchable units.

Rules (from the architecture, section 4.4):
- FAQ CSV:        one question-and-answer pair per chunk, never split.
- Other CSVs:     rows grouped by a column (price book by category, service area by
                  coverage), packed up to the target size, with a header line repeated in
                  every chunk. Catalog-style CSVs without a group column: one row per chunk.
- Markdown text (PDF, DOCX, HTML): split at headings into sections. Small consecutive
                  sections are packed together up to the target size; a section larger than
                  the target is split at paragraph/table-row/sentence boundaries with
                  ~75 tokens of overlap between the pieces. Table rows are never cut.
Every chunk starts with "Document: <title>" so it still makes sense when read alone, gets a
deterministic ID `<document_id>:<version>:<n>`, and carries the document metadata.
"""
import re
from dataclasses import dataclass, field

import tiktoken
from langchain_core.documents import Document

from serviceiq.ingestion.cleaning import content_hash
from serviceiq.ingestion.loaders import LoadedDocument

ENCODING = tiktoken.get_encoding("cl100k_base")  # tokenizer used by text-embedding-3-small
CSV_GROUP_COLUMN = {"price_book": "category", "service_area": "coverage"}
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def count_tokens(text: str) -> int:
    return len(ENCODING.encode(text))


@dataclass
class Block:
    text: str
    page: int
    heading_level: int = 0  # 0 = body text, 1..6 = Markdown heading level

    @property
    def is_table(self) -> bool:
        return self.text.startswith("|")


@dataclass
class Section:
    path: list[str]
    blocks: list[Block] = field(default_factory=list)


# ---------------------------------------------------------------- Markdown documents

def _blocks(doc: LoadedDocument) -> list[Block]:
    blocks = []
    for part in doc.parts:
        for i, text in enumerate(re.split(r"\n\s*\n", part.page_content)):
            text = text.strip()
            if not text:
                continue
            level = len(text) - len(text.lstrip("#")) if re.match(r"#{1,6} ", text) else 0
            prev = blocks[-1] if blocks else None
            # A paragraph cut by a page break: previous block has no closing punctuation and this
            # first block of the new page starts in lower case -> rejoin them.
            if (i == 0 and prev and not level and not prev.heading_level and not prev.is_table
                    and not re.search(r"[.!?:|]$", prev.text) and text[:1].islower()):
                prev.text = f"{prev.text} {text}"
                continue
            blocks.append(Block(text, part.metadata.get("part", 1), level))
    return blocks


def _sections(blocks: list[Block]) -> list[Section]:
    sections, stack = [Section(path=[])], []
    for b in blocks:
        if b.heading_level:
            stack = [h for h in stack if h[0] < b.heading_level] + [(b.heading_level, b.text.lstrip("# "))]
            sections.append(Section(path=[t for _, t in stack], blocks=[b]))
        else:
            sections[-1].blocks.append(b)
    return [s for s in sections if s.blocks]


def _split_oversized(block: Block, limit: int) -> list[Block]:
    """Split one block that is larger than `limit` tokens: tables by rows, text by sentences."""
    if count_tokens(block.text) <= limit:
        return [block]
    if block.is_table:
        lines = block.text.splitlines()
        header, rows = lines[:2], lines[2:]
        pieces, current = [], []
        for row in rows:
            if current and count_tokens("\n".join(header + current + [row])) > limit:
                pieces.append(current)
                current = []
            current.append(row)
        pieces.append(current)
        return [Block("\n".join(header + rows_), block.page) for rows_ in pieces if rows_]
    pieces, current = [], ""
    for sentence in SENTENCE_END.split(block.text):
        candidate = f"{current} {sentence}".strip()
        if current and count_tokens(candidate) > limit:
            pieces.append(current)
            candidate = sentence
        if count_tokens(candidate) > limit:  # a single sentence longer than the limit: cut by tokens
            tokens = ENCODING.encode(candidate)
            pieces += [ENCODING.decode(tokens[i:i + limit]) for i in range(0, len(tokens) - limit, limit)]
            candidate = ENCODING.decode(tokens[(len(tokens) - 1) // limit * limit:])
        current = candidate
    pieces.append(current)
    return [Block(p, block.page) for p in pieces if p]


def _overlap_tail(window: list[Block], overlap: int) -> list[Block]:
    """Trailing context (at most `overlap` tokens) repeated at the start of the next window:
    whole paragraphs if they fit, otherwise the last sentences of the final paragraph."""
    carry = []
    for prev in reversed(window):
        if count_tokens("\n\n".join(b.text for b in [prev] + carry)) > overlap:
            break
        carry.insert(0, prev)
    if carry or window[-1].is_table:
        return carry
    sentences = SENTENCE_END.split(window[-1].text)
    tail = []
    for sentence in reversed(sentences):
        if count_tokens(" ".join([sentence] + tail)) > overlap:
            break
        tail.insert(0, sentence)
    return [Block(" ".join(tail), window[-1].page)] if tail else []


def _section_units(section: Section, target: int, overlap: int) -> list[list[Block]]:
    """A section that fits is one unit; a larger one becomes overlapping windows."""
    if count_tokens("\n\n".join(b.text for b in section.blocks)) <= target:
        return [section.blocks]
    headings = [b for b in section.blocks[:1] if b.heading_level]
    body = section.blocks[len(headings):]
    limit = target - count_tokens("\n\n".join(b.text for b in headings)) - 4
    pieces = [p for b in body for p in _split_oversized(b, limit - overlap)]

    windows, current = [], []
    for piece in pieces:
        if current and count_tokens("\n\n".join(b.text for b in current + [piece])) > limit:
            windows.append(current)
            current = _overlap_tail(current, overlap)
        current.append(piece)
    windows.append(current)
    return [headings + w for w in windows]


def _chunk_markdown(doc: LoadedDocument, target: int, overlap: int) -> list[tuple[str, list[Block]]]:
    """Return (section path, blocks) per chunk."""
    header_tokens = count_tokens(f"Document: {doc.entry.title}\n\n")
    budget = target - header_tokens
    chunks, current, current_path = [], [], ""
    for section in _sections(_blocks(doc)):
        units = _section_units(section, budget, overlap)
        path = " > ".join(section.path)
        for unit in units:
            if current and (len(units) > 1 or count_tokens("\n\n".join(b.text for b in current + unit)) > budget):
                chunks.append((current_path, current))
                current = []
            if not current:
                current_path = path
            current = current + unit
            if len(units) > 1:  # pieces of a split section stay separate chunks
                chunks.append((current_path, current))
                current = []
    if current:
        chunks.append((current_path, current))
    return chunks


# ---------------------------------------------------------------- CSV documents

def _csv_value(row_text: str, column: str) -> str:
    match = re.search(rf"^{re.escape(column)}: (.*)$", row_text, re.M)
    return match.group(1) if match else ""


def _chunk_csv(doc: LoadedDocument, target: int) -> list[tuple[str, list[Block]]]:
    rows = [Block(p.page_content, p.metadata["part"]) for p in doc.parts]
    column = CSV_GROUP_COLUMN.get(doc.entry.document_type)
    if column is None:
        return [("", [row]) for row in rows]  # FAQ and catalog rows: one self-contained row per chunk

    groups: dict[str, list[Block]] = {}
    for row in rows:
        groups.setdefault(_csv_value(row.text, column), []).append(row)
    chunks = []
    for value, group_rows in groups.items():
        header = Block(f"{column.replace('_', ' ').title()}: {value}", group_rows[0].page)
        budget = target - count_tokens(f"Document: {doc.entry.title}\n\n")
        current = [header]
        for row in group_rows:
            if len(current) > 1 and count_tokens("\n\n".join(b.text for b in current + [row])) > budget:
                chunks.append((value, current))
                current = [header]
            current.append(row)
        chunks.append((value, current))
    return chunks


# ---------------------------------------------------------------- public API

def chunk_document(doc: LoadedDocument, target: int = 500, overlap: int = 75) -> list[Document]:
    if not doc.parts:
        return []
    pieces = _chunk_csv(doc, target) if doc.entry.format == "csv" else _chunk_markdown(doc, target, overlap)
    base = {k: v for k, v in doc.parts[0].metadata.items() if k not in {"part", "content_hash"}}
    chunks = []
    for n, (section, blocks) in enumerate(pieces):
        text = f"Document: {doc.entry.title}\n\n" + "\n\n".join(b.text for b in blocks)
        pages = sorted({b.page for b in blocks})
        chunks.append(Document(page_content=text, metadata={
            **base,
            "chunk_id": f"{doc.entry.document_id}:{doc.entry.version}:{n}",
            "chunk_index": n,
            "section": section,
            "part_start": pages[0],
            "part_end": pages[-1],
            "token_count": count_tokens(text),
            "content_hash": content_hash(text),
        }))
    return chunks


def chunk_documents(docs: list[LoadedDocument], target: int = 500, overlap: int = 75) -> list[Document]:
    return [chunk for doc in docs for chunk in chunk_document(doc, target, overlap)]
