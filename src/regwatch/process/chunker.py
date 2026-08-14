"""Heading- and page-aware recursive chunker.

Every chunk carries enough metadata to build a citation:
    {doc_id, version_id, normalized_name, dosage_form, route,
     recommended_date, source_url, page, section_path, ordinal}

PSGs are short (often 2-8 pages). We do:
  1. Strip page furniture (running titles/footers, the FDA nonbinding
     disclaimer) from the CHUNK path only -- the stored parsed text keeps it.
  2. Per-page split (so page boundaries are preserved).
  3. Within each page, heading-aware sub-split on Roman-numeral / lettered
     section headers, carrying the last heading across page breaks.
  4. Within each section, sliding window of ~1000 tokens with ~150 overlap,
     cut at sentence/word boundaries.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

# A token ~= 4 chars for English (rough). We use this instead of shipping a
# tokenizer; chunk size is approximate either way.
CHARS_PER_TOKEN = 4
TARGET_TOKENS = 1000
OVERLAP_TOKENS = 150
# Below this a section body is merged into a neighbor on the same page instead
# of being emitted on its own: the 2026-07-30 corpus audit measured ~21% of
# sampled chunks as bare headings / stranded fragments with no citable content.
MIN_SECTION_CHARS = 250
# A carried section holding less prose than this UNDER its heading line is a bare
# stranded heading: it travels into the next section's chunk without claiming that
# chunk's identity. Above it, the carried section has citable content of its own
# and keeps its heading. See _merge_small_sections.
MIN_CARRY_PROSE_CHARS = 40
# BUMP THIS whenever chunk boundaries or section attribution change. sync.py folds
# it into _processing_fingerprint, which is what decides whether an already-indexed
# document is re-chunked. Leaving it stale ships the fix to new documents only and
# leaves the corpus holding two different chunk shapes; chunk_embedding cascades on
# chunk delete, so re-chunking cannot orphan vectors. v3: header regex no longer
# reads genus-species abbreviations as headings, and a forward-merged chunk keeps
# the carried section's path.
CHUNKING_VERSION = "page-section-window-1000-overlap-150-v3"

# Section identity: Roman-numeral / lettered headers plus unnumbered
# "Option N" blocks. Arabic-numbered lines are LIST ITEMS inside a section,
# not boundaries -- v1 split on them, which stranded real headings as tiny
# chunks and promoted list sentences to section_path identity.
#
# The `[A-Z0-9"(]` opener is load-bearing, not tidying. Without it `[A-Z]\.`
# matches the genus abbreviation that opens a species name, so "E. coli is the
# primary pathogen of interest." parsed as marker "E." + heading "coli is the
# primary pathogen of interest." -- a whole sentence promoted to section
# identity, the surrounding paragraph split at it, and section_path (what INV-1
# citations resolve against) left holding prose. An FDA corpus is saturated with
# "E. coli", "S. aureus", "H. pylori", "P. aeruginosa", "C. difficile", so at
# full-corpus scale this is structural, not an edge case. A real header's text
# opens with a capital, digit, quote or paren ("A. Dissolution",
# "II. Recommendations", 'A. Type of Study: ...'); a species name continues in
# lowercase, which is exactly the discriminator. Residual, accepted: a document
# writing "E. Coli" still false-positives, and a genuinely lowercase heading is
# now missed -- a missed header only inherits the previous section_path, while a
# false one invents a wrong one, so failing closed here is the safer direction.
_HEADER_RE = re.compile(
    r"^\s*([IVX]+\.|[A-Z]\.|Option \d+[.:])\s+([A-Z0-9\"(].{1,119})$", re.MULTILINE
)

# Fixed page furniture, dropped line-wise from the chunk path. The revision
# footer carries the page number glued to its end ("Recommended Sep 2012;
# Revised Mar 2015, Aug 2024 13"); body prose starting with "Recommended"
# survives because it lacks the month-year opening + trailing page number.
_FURNITURE_LINE_RES = (
    re.compile(r"^\s*Contains Nonbinding Recommendations\s*$", re.IGNORECASE),
    re.compile(r"^\s*Draft\s*[-\u2013\u2014]\s*Not for Implementation\s*$", re.IGNORECASE),
    re.compile(r"^\s*Recommended\s+[A-Z][a-z]{2,8}\.?\s+\d{4}\b.{0,120}?\s\d{1,3}\s*$"),
)

# FDA disclaimer boilerplate that dominates every page-1 chunk (~1500 of
# ~1850 chars in the audited sample) and embeds near-identically across the
# whole corpus. Matched on stable mid-sentence phrases (source apostrophes
# vary between straight and curly, so no phrase below contains one). The block
# is removed from its marker line to the end of the paragraph. It stays in the
# stored parsed text -- it is legally meaningful; it just is not evidence.
_DISCLAIMER_MARKERS = (
    "This draft guidance, when finalized",
    "guidance documents do not establish legally enforceable responsibilities",
    "do not have the force and effect of law",
)
_DISCLAIMER_END_RE = re.compile(r"^\s*(Active Ingredient|[IVX]+\.\s|[A-Z]\.\s|Option \d)")

_SENTENCE_BREAK_RE = re.compile(r"[.!?:]['\")\]]?\s")
_BOUNDARY_LOOKBACK = 200

_TRAILING_PAGE_NO_RE = re.compile(r"\s+\d{1,3}$")


@dataclass
class Chunk:
    text: str
    page: int
    section_path: str | None
    ordinal: int
    metadata: dict[str, Any]


def _line_key(line: str) -> str:
    """Counting key for the repeated-line rule: the trailing page number is
    stripped so per-page footers ("... Revised Aug 2024 13") still collide."""
    return _TRAILING_PAGE_NO_RE.sub("", line.strip())


def _repeated_furniture_keys(pages: list[str]) -> frozenset[str]:
    """Line keys repeated on >= 60% of a document's pages (3-page minimum):
    per-document furniture (running titles, footers) that body prose does not
    reproduce verbatim on page after page."""
    if len(pages) < 3:
        return frozenset()
    counts: Counter[str] = Counter()
    for page_text in pages:
        counts.update({_line_key(ln) for ln in page_text.splitlines() if ln.strip()})
    threshold = max(3, -(-len(pages) * 3 // 5))  # ceil(0.6 * n)
    return frozenset(key for key, n in counts.items() if n >= threshold and key)


def _strip_furniture_lines(page_text: str, repeated: frozenset[str]) -> str:
    kept: list[str] = []
    for line in page_text.splitlines():
        stripped = line.strip()
        if stripped and _line_key(stripped) in repeated:
            continue
        if any(rx.match(line) for rx in _FURNITURE_LINE_RES):
            continue
        kept.append(line)
    return "\n".join(kept)


def _strip_disclaimer_blocks(page_text: str) -> str:
    """Drop each disclaimer paragraph from its marker line to the block end
    (blank line, next section header, or the "Active Ingredient" spine line
    that follows the disclaimer in the PSG template)."""
    lines = page_text.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(marker in line for marker in _DISCLAIMER_MARKERS):
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip() or _DISCLAIMER_END_RE.match(nxt):
                    break
                i += 1
            continue
        kept.append(line)
        i += 1
    return "\n".join(kept)


def prepare_pages(pages: list[str]) -> list[str]:
    """The chunk-path view of a document's pages: furniture and disclaimer
    boilerplate removed. Page count and order are preserved so page numbers
    keep citing the source PDF; the stored parsed text is untouched."""
    repeated = _repeated_furniture_keys(pages)
    return [
        _strip_disclaimer_blocks(_strip_furniture_lines(page_text, repeated)) for page_text in pages
    ]


def _split_into_sections(page_text: str, inherited: str | None) -> list[tuple[str | None, str]]:
    """Return [(section_path, body), ...] for one page. Text before the first
    header belongs to `inherited` -- the last heading seen on an earlier page
    -- so a section that wraps across a page break keeps its parent (v1 reset
    to None on every page, orphaning every continuation chunk)."""
    headers = list(_HEADER_RE.finditer(page_text))
    if not headers:
        return [(inherited, page_text)]
    sections: list[tuple[str | None, str]] = []
    # Preamble (before the first header)
    if headers[0].start() > 0:
        pre = page_text[: headers[0].start()].strip()
        if pre:
            sections.append((inherited, pre))
    for i, h in enumerate(headers):
        path = f"{h.group(1).rstrip('.')} {h.group(2).strip()}"[:120]
        start = h.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(page_text)
        body = page_text[start:end].strip()
        if body:
            sections.append((path, body))
    return sections


def _has_body_prose(body: str) -> bool:
    """True when a section carries citable content beneath its heading line."""
    _, _, remainder = body.partition("\n")
    return len(remainder.strip()) >= MIN_CARRY_PROSE_CHARS


def _merge_small_sections(
    sections: list[tuple[str | None, str]],
) -> list[tuple[str | None, str]]:
    """Forward-merge bodies under MIN_SECTION_CHARS into the next section on
    the same page (a stranded heading travels as the first line of the chunk
    it introduces); a trailing small body merges backward instead. A page
    whose entire content is small still emits -- merging across pages would
    misattribute the citation page."""
    merged: list[tuple[str | None, str]] = []
    carry: tuple[str | None, str] | None = None
    for path, body in sections:
        if carry is not None:
            carry_path, carry_body = carry
            body = f"{carry_body}\n{body}"
            # The merged chunk's TEXT starts in the carried section, so the
            # citation belongs to the carried heading whenever that section had
            # citable content of its own. Unconditionally preferring `path` (the
            # section being merged INTO) attributed a chunk opening with
            # "A. Dissolution ..." to "B Study design" -- an answer citing it
            # would name a section the quoted text is not in, which is precisely
            # the provenance INV-1 exists to guarantee. A carried section that is
            # only a stranded heading has no content to claim, so it keeps
            # travelling as the first line of the section it introduces.
            if (carry_path is not None and _has_body_prose(carry_body)) or path is None:
                path = carry_path
            carry = None
        if len(body) < MIN_SECTION_CHARS:
            carry = (path, body)
            continue
        merged.append((path, body))
    if carry is not None:
        carry_path, carry_body = carry
        if merged:
            last_path, last_body = merged[-1]
            merged[-1] = (last_path, f"{last_body}\n{carry_body}")
        else:
            merged.append((carry_path, carry_body))
    return merged


def _window_end(body: str, start: int, size: int) -> int:
    """End of the window starting at `start`, backed up to the last sentence
    break -- else whitespace -- inside the final _BOUNDARY_LOOKBACK chars, so
    a forced split never lands mid-word (the v1 raw slice did)."""
    hard_end = min(start + size, len(body))
    if hard_end == len(body):
        return hard_end
    floor = max(start + 1, hard_end - _BOUNDARY_LOOKBACK)
    last: re.Match[str] | None = None
    for m in _SENTENCE_BREAK_RE.finditer(body, floor, hard_end):
        last = m
    if last is not None:
        return last.end()
    ws = body.rfind(" ", floor, hard_end)
    if ws > start:
        return ws + 1
    return hard_end


def _sliding_chunks(body: str) -> list[str]:
    """Sliding character-window chunks (approximated by char count)."""
    size = TARGET_TOKENS * CHARS_PER_TOKEN
    overlap = OVERLAP_TOKENS * CHARS_PER_TOKEN
    if len(body) <= size:
        return [body]
    out: list[str] = []
    pos = 0
    while pos < len(body):
        end = _window_end(body, pos, size)
        out.append(body[pos:end])
        if end >= len(body):
            break
        nxt = max(end - overlap, pos + 1)
        if not body[nxt - 1].isspace():
            # Never start a chunk mid-word; shrinking the overlap is safe --
            # everything before `nxt` is already in the previous chunk.
            adv = body.find(" ", nxt, end)
            if adv != -1:
                nxt = adv + 1
        pos = nxt
    return out


def chunk_pdf(
    pages: list[str],
    *,
    base_metadata: dict[str, Any],
) -> list[Chunk]:
    """Chunk a PSG PDF, removing its repeated template boilerplate first."""
    return chunk_document_pages(prepare_pages(pages), base_metadata=base_metadata)


def chunk_document_pages(
    pages: list[str],
    *,
    base_metadata: dict[str, Any],
) -> list[Chunk]:
    """Chunk generic FDA document pages without PSG-specific text removal.

    Page boundaries are never crossed, preserving the page locator used by
    citations.  Callers that own a source-specific cleanup step apply it before
    this function; silently applying PSG disclaimer rules to review packages or
    approved labeling would delete valid evidence.
    """
    chunks: list[Chunk] = []
    ordinal = 0
    current_section: str | None = None
    for page_idx, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        sections = _split_into_sections(page_text, current_section)
        current_section = sections[-1][0]
        for section_path, body in _merge_small_sections(sections):
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
