"""Heading- and page-aware recursive chunker.

Every chunk carries enough metadata to build a citation:
    {doc_id, version_id, normalized_name, dosage_form, route,
     recommended_date, source_url, page, section_path, ordinal}

PSGs are short (often 2-8 pages). We do:
  1. Per-page split (so page boundaries are preserved).
  2. Within each page, heading-aware sub-split on Roman-numeral / lettered
     section headers if present.
  3. Within each section, sliding window of ~1000 tokens with ~150 overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A token ~= 4 chars for English (rough). We use this instead of shipping a
# tokenizer; chunk size is approximate either way.
CHARS_PER_TOKEN = 4
TARGET_TOKENS = 1000
OVERLAP_TOKENS = 150

_HEADER_RE = re.compile(r"^\s*([IVX]+\.|[A-Z]\.|\d+\.)\s+(.{2,120})$", re.MULTILINE)


@dataclass
class Chunk:
    text: str
    page: int
    section_path: str | None
    ordinal: int
    metadata: dict[str, Any]


def _split_into_sections(page_text: str) -> list[tuple[str | None, str]]:
    """Return [(section_path or None, body), ...] for one page."""
    headers = list(_HEADER_RE.finditer(page_text))
    if not headers:
        return [(None, page_text)]
    sections: list[tuple[str | None, str]] = []
    # Preamble (before the first header)
    if headers[0].start() > 0:
        pre = page_text[: headers[0].start()].strip()
        if pre:
            sections.append((None, pre))
    for i, h in enumerate(headers):
        path = f"{h.group(1).rstrip('.')} {h.group(2).strip()}"[:120]
        start = h.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(page_text)
        body = page_text[start:end].strip()
        if body:
            sections.append((path, body))
    return sections


def _sliding_chunks(body: str) -> list[str]:
    """Sliding character-window chunks (approximated by char count)."""
    if len(body) <= TARGET_TOKENS * CHARS_PER_TOKEN:
        return [body]
    step = (TARGET_TOKENS - OVERLAP_TOKENS) * CHARS_PER_TOKEN
    size = TARGET_TOKENS * CHARS_PER_TOKEN
    out: list[str] = []
    pos = 0
    while pos < len(body):
        out.append(body[pos : pos + size])
        pos += step
    return out


def chunk_pdf(
    pages: list[str],
    *,
    base_metadata: dict[str, Any],
) -> list[Chunk]:
    """Chunk a PDF's per-page text. Page numbers are 1-indexed."""
    chunks: list[Chunk] = []
    ordinal = 0
    for page_idx, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        for section_path, body in _split_into_sections(page_text):
            for piece in _sliding_chunks(body):
                cleaned = piece.strip()
                if not cleaned:
                    continue
                metadata = {
                    **base_metadata,
                    "page": page_idx,
                    "section_path": section_path,
                    "ordinal": ordinal,
                }
                chunks.append(
                    Chunk(
                        text=cleaned,
                        page=page_idx,
                        section_path=section_path,
                        ordinal=ordinal,
                        metadata=metadata,
                    )
                )
                ordinal += 1
    return chunks
