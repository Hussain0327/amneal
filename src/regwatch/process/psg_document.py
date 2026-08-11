"""Rebuilds a readable document from the stored chunks of one PSG.

The Compliance Studio renders working documents as an ordered list of typed
blocks (title / meta / heading / paragraph). A reference PSG arrives as PDF
text that ingest already extracted, chunked per page and stored in the
``chunk`` table, so the studio can show it in the same canvas as a working
document instead of framing a PDF -- provided something turns page text back
into blocks. That is this module, and nothing else.

Two properties are deliberate:

* **Pure.** Rows in, blocks out. No database, no clock, no network, so the
  whole thing is unit-testable and cheap enough to run per request (a PSG is
  ~4 KB of text across ~3 pages; the largest in the corpus is 50 KB).
* **Text-faithful.** Nothing is invented, reordered or summarised. The blocks
  carry the extracted words in the order FDA published them, because an
  analyst will cite them. Where the source is ambiguous the parser degrades to
  a plain paragraph rather than guessing at structure.

The chunk text is the same text the retrieval layer quotes, so a passage read
in the studio and a passage cited by an answer cannot disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Chunks that share a page overlap by ~150 tokens (see process.chunker).
# The overlap is a verbatim tail of the previous window, so it can be removed
# exactly; this bounds how far back the search looks.
_MAX_OVERLAP_CHARS = 1200
# ...and this is the floor below which a match is coincidence, not overlap.
# Without it, a seam where the previous chunk merely ENDS with the character
# the next one STARTS with would silently delete that character. Measured over
# the corpus (461 same-page seams): every genuine overlap is 594-599 chars, and
# the rest are exactly 0, so anything short is noise.
_MIN_OVERLAP_CHARS = 24

# Bounds on one response. The largest PSG in the corpus is ~50 KB / 23 chunks,
# so these are runaway guards, not a working limit: a document that hits them
# is malformed, and truncating beats streaming an unbounded body.
_MAX_BLOCKS = 600
_MAX_BLOCK_CHARS = 8000

# Paragraph detection (see _ends_paragraph): how far below the measured text
# width a line has to fall to read as the end of a paragraph, and the width
# below which the measurement is too weak to act on.
_SHORT_LINE_SLACK = 8
_MIN_MEASURED_WIDTH = 40

# "I. Study design", "A. Eligibility", "Option 2: ..." -- the section headers
# the chunker also recognises. Arabic numerals are list items inside a
# section, not headers (chunker.py documents the same distinction).
_HEADING_RE = re.compile(r"^\s*(?:[IVX]+\.|[A-Z]\.|Option \d+[.:])\s+\S")

# "Active Ingredient: Semaglutide", "Additional information:" -- the spine of
# every PSG. A label with no value on the line is a section header; a label
# with a value is a labelled paragraph that must stay on one line.
_LABEL_RE = re.compile(r"^\s*([A-Z][A-Za-z0-9 ,/()'\-]{1,60}):\s*(.*)$")

# "1. Class of study: Bioequivalence" -- a numbered item starts its own block.
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}\.\s+\S")

# Bullets survive extraction as a marker glyph or a dash. Escaped rather than
# literal: this file stays ASCII (house rule), and the glyphs vary by PDF.
_BULLET_RE = re.compile("^\\s*[\\u2022\\u00b7\\u25cf*\\u2013\\u2014-]\\s+\\S")

# "1 Q1 (Qualitative sameness) means ...", "3 21 CFR 314.94(a)(9)(iii)." --
# footnotes land at the foot of a page as a marker digit then the note. They
# must not be glued onto the last body sentence above them.
_FOOTNOTE_RE = re.compile(r"^\s*\d{1,2}\s+(?=[A-Z(\d])")

# "December 2025" under the title, and the two trailer lines FDA closes every
# PSG with. Rendered as document metadata rather than as body prose.
_MONTH_YEAR_RE = re.compile(
    r"^\s*(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{4}\s*$"
)
_TRAILER_LABELS = frozenset({"Document History", "Unique Agency Identifier"})


@dataclass(frozen=True)
class PsgChunkText:
    """One stored ``chunk`` row, reduced to what rebuilding needs."""

    ordinal: int
    page: int
    text: str


@dataclass(frozen=True)
class PsgBlock:
    """One rendered block of the document.

    ``type`` is one of ``title``/``meta``/``h2``/``p`` -- the same vocabulary
    the studio's working documents use, so one canvas renders both. ``page``
    is the 1-indexed PDF page the text came from, kept so a citation can name
    a page without re-parsing the PDF.
    """

    id: str
    type: str
    text: str
    page: int


def _strip_overlap(accumulated: str, addition: str) -> str:
    """Returns ``addition`` without the tail of ``accumulated`` it repeats.

    The chunker cuts its overlap at a sentence or word boundary, so the repeat
    is a verbatim substring; the longest match is taken to avoid leaving a
    partial duplicate behind. Matches shorter than ``_MIN_OVERLAP_CHARS`` are
    treated as coincidence and left alone -- duplicating a few words is
    visible and harmless, whereas deleting a character that only looked like
    an overlap corrupts the text silently. Falls back to the untouched
    addition when the two do not overlap at all, which is the common case.
    """
    window = min(len(accumulated), len(addition), _MAX_OVERLAP_CHARS)
    for size in range(window, _MIN_OVERLAP_CHARS - 1, -1):
        if accumulated.endswith(addition[:size]):
            return addition[size:]
    return addition


def _pages(chunks: list[PsgChunkText]) -> list[tuple[int, str]]:
    """Merges chunks back into one text per page, in page order."""
    merged: dict[int, str] = {}
    for chunk in sorted(chunks, key=lambda c: (c.page, c.ordinal)):
        text = (chunk.text or "").strip("\n")
        if not text:
            continue
        current = merged.get(chunk.page)
        if current is None:
            merged[chunk.page] = text
            continue
        addition = _strip_overlap(current, text)
        if addition.strip():
            merged[chunk.page] = f"{current}\n{addition.lstrip()}"
    return sorted(merged.items())


def _join(existing: str, line: str) -> str:
    """Appends a wrapped line to the paragraph it belongs to.

    PDF extraction breaks prose at the rendered line width, so a paragraph
    arrives as many lines. A line ending in a hyphen is joined without a space
    ("non-" + "peptide"), which keeps genuine compound words intact; every
    other break was a space in the original.
    """
    if existing.endswith("-"):
        return existing + line
    return f"{existing} {line}"


def _starts_block(line: str) -> bool:
    """True when this line begins a new block rather than continuing one."""
    return bool(
        _HEADING_RE.match(line)
        or _NUMBERED_RE.match(line)
        or _BULLET_RE.match(line)
        or _FOOTNOTE_RE.match(line)
        or _MONTH_YEAR_RE.match(line)
        or _LABEL_RE.match(line)
    )


def _text_width(lines: list[str]) -> int:
    """The document's body text width, in characters.

    The 90th percentile rather than the maximum: footnotes are typeset
    smaller, so they run several characters wider than body prose, and taking
    the longest line would make every body line look short.
    """
    lengths = sorted(len(line) for line in lines if line)
    if not lengths:
        return 0
    return lengths[int(0.9 * (len(lengths) - 1))]


def _ends_paragraph(previous_line: str, width: int, line: str) -> bool:
    """True when ``previous_line`` was the last line of its paragraph.

    Extracted PDF text carries no blank lines between paragraphs, so the
    surviving evidence is geometric: a wrapped line runs to the text width and
    a paragraph's last line does not. Length alone over-splits (a sentence can
    end anywhere), so a short line only closes its block when it also reads as
    finished -- terminal punctuation, or a label line that did not break
    mid-list on a comma. A following line that opens lower-case is always a
    continuation.

    ``width`` is measured per document rather than assumed, so a PSG typeset
    at any column width parses; below ``_MIN_MEASURED_WIDTH`` the measurement
    is not trustworthy and the rule stands down entirely.
    """
    if width < _MIN_MEASURED_WIDTH:
        return False
    if len(previous_line) >= width - _SHORT_LINE_SLACK:
        return False
    # Only a capital letter can open a new paragraph. A line starting with a
    # digit or a symbol is a wrapped continuation ("...pursuant to 21 CFR" /
    # "320.22(c) provided that..."); the block-start patterns above have
    # already claimed the numbered items and footnotes that are not.
    if not line[:1].isupper():
        return False
    finished = previous_line.rstrip()
    if finished.endswith((".", ":", ";", "!", "?")):
        return True
    return bool(_LABEL_RE.match(previous_line)) and not finished.endswith(",")


def _line_type(line: str, *, first_of_document: bool) -> str:
    """Classifies one block-starting line into a block type."""
    if first_of_document:
        return "title"
    if _MONTH_YEAR_RE.match(line):
        return "meta"
    if _FOOTNOTE_RE.match(line):
        return "meta"
    if _HEADING_RE.match(line):
        return "h2"
    label = _LABEL_RE.match(line)
    if label:
        if label.group(1) in _TRAILER_LABELS:
            return "meta"
        # "Additional information:" with nothing after it is a section header;
        # "Route: Subcutaneous" is a labelled paragraph.
        if not label.group(2).strip():
            return "h2"
    return "p"


@dataclass
class _Draft:
    """A block under construction: mutable while its lines are still arriving."""

    type: str
    page: int
    text: str


def _continues_across_pages(previous: _Draft) -> bool:
    """True when a page's first line continues the paragraph before it.

    A sentence that runs over a page break is one paragraph, and splitting it
    would put half a sentence in each block. A previous block that ended on
    terminal punctuation, or that is not prose, starts a fresh block instead.
    """
    if previous.type != "p":
        return False
    return not previous.text.rstrip().endswith((".", ":", "?", "!"))


@dataclass(frozen=True)
class PsgDocumentBody:
    """The rebuilt document plus whether the runaway guards fired."""

    blocks: list[PsgBlock]
    truncated: bool


def build_body(doc_id: int, chunks: list[PsgChunkText]) -> PsgDocumentBody:
    """Rebuilds the ordered block list for one PSG.

    Args:
        doc_id: ``psg_document.id``, used only to make block ids unique across
            documents open in one client.
        chunks: The stored chunk rows for the document's current version. Any
            ordering is accepted; the rows are sorted by (page, ordinal).

    Returns:
        The document as typed blocks in reading order, and a ``truncated``
        flag the caller must surface rather than swallow. The block list is
        empty when the document has no stored text, which means "no rendering
        available", not "an empty document".
    """
    pages = _pages(chunks)
    lines_by_page = [
        (page, [" ".join(raw.split()) for raw in text.splitlines()]) for page, text in pages
    ]
    # The text width is a property of the whole document, not of one page: a
    # short final page would otherwise redefine what "full width" means.
    width = _text_width([line for _, lines in lines_by_page for line in lines])

    drafts: list[_Draft] = []
    previous_line = ""
    truncated = False

    for page, lines in lines_by_page:
        for line in lines:
            if not line:
                continue
            last = drafts[-1] if drafts else None
            if last is not None and not _starts_block(line):
                # Same page: an ordinary wrapped line. New page: only a
                # paragraph FDA left mid-sentence carries over the break.
                # A title, a heading and the date line are always exactly one
                # line. Without that rule an older PSG's opening disclaimer is
                # swallowed -- into its title, or into the "December 2025"
                # under it, which would also print the disclaimer in the small
                # metadata face. A date line can never end a paragraph by the
                # width rule either: it carries no terminal punctuation and no
                # colon, so nothing else would break it. Footnote metas are
                # NOT excluded here: their own wrapped lines must still join.
                carries = (
                    last.type not in ("title", "h2")
                    and not _MONTH_YEAR_RE.match(last.text)
                    and (page == last.page or _continues_across_pages(last))
                )
                if (
                    carries
                    and not _ends_paragraph(previous_line, width, line)
                    and len(last.text) < _MAX_BLOCK_CHARS
                ):
                    last.text = _join(last.text, line)
                    previous_line = line
                    continue
            if len(drafts) >= _MAX_BLOCKS:
                truncated = True
                break
            drafts.append(
                _Draft(
                    type=_line_type(line, first_of_document=not drafts),
                    page=page,
                    text=line,
                )
            )
            previous_line = line
        if truncated:
            break

    kept = [d for d in drafts if d.text.strip()]
    return PsgDocumentBody(
        blocks=[
            PsgBlock(id=f"psg-{doc_id}-b{i}", type=d.type, text=d.text, page=d.page)
            for i, d in enumerate(kept)
        ],
        truncated=truncated,
    )


def document_file_name(*, appl_no: str | None, active_ingredient: str) -> str:
    """The name this PSG carries in the studio, as a Word file name.

    The studio's working documents are named files ("3.2.S.4.1
    Specification.docx"), and a reference PSG sits in the same tree, so it is
    named the same way. Falls back to the ingredient alone when FDA's own
    identifier is missing, and strips anything that would be awkward in a file
    name on any platform.
    """
    ingredient = re.sub(r"[^A-Za-z0-9]+", " ", active_ingredient).strip()
    ingredient = re.sub(r"\s+", " ", ingredient) or "PSG"
    if appl_no and appl_no.isdigit():
        return f"PSG_{appl_no} {ingredient}.docx"
    return f"{ingredient} PSG.docx"
