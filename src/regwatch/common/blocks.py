"""Markdown-shape segmentation: a completion becomes claim UNITS with a BLOCK.

Last updated: 2026-08-21.

WHY THIS EXISTS
The synthesizer is told presentation is a feature (headings, bullets, tables
when they help), but the pipeline between its output and the browser used to
know only sentences. ``split_sentences`` splits on terminal punctuation, so a
``## Heading`` line -- which has none -- was welded onto the first sentence
below it, and a whole GFM table collapsed into one unterminated string. The
gate then stripped the ``#`` to keep the claim and the renderer had no idea
it had ever been a heading: "Recommended BE design for albuterol ... FDA's
draft PSG recommends" ran together on screen.

This module reads the markdown SHAPE first and sentences second. Every unit
is still exactly one sentence (or one heading, or one table cell), so the
gate's "one claim = one assertion" property is untouched; what is new is the
``Block`` tag riding beside the text, which says which container the unit
came from so ``turn_gate.render_answer`` can rebuild the structure from the
admitted claims alone. Structure is never trusted as content: a heading can
still be dropped for being an uncited source fact, and a cell still has to
carry its own marker.

Pure: no I/O, no settings. Shared by the prose parser (which needs the
blocks), by the eval metrics and by the pathological-output bounds (which
need only the unit texts, via ``split_units``), so all three agree on what a
unit is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from regwatch.common.sentences import split_sentences

BLOCK_PARAGRAPH = "paragraph"
BLOCK_HEADING = "heading"
BLOCK_BULLET = "bullet"
BLOCK_NUMBERED = "numbered"
BLOCK_TABLE = "table"

# The block kinds whose unit boundary is a LINE (or cell) end rather than a
# sentence terminator. The prose parser relaxes its "marker before the final
# period" rule for these, because "## Design [1]" and "| Yes [1] |" have no
# period to put the marker before.
LINE_TERMINATED_KINDS: frozenset[str] = frozenset(
    {BLOCK_HEADING, BLOCK_BULLET, BLOCK_NUMBERED, BLOCK_TABLE}
)

# STRUCTURAL units: a heading or a table cell. Two properties the gate relies
# on, both consequences of the unit not being part of a sentence flow:
#   * dropping one cannot invert the prose around it the way dropping "a fed
#     study is NOT required" inverts the paragraph it sat in -- the grid keeps
#     a visible hole and the disclosure line says something was removed -- so
#     a structural drop is PARTIAL, never MATERIAL_DROP;
#   * it is a few words long, which is exactly where token-overlap re-stamping
#     is least trustworthy, so a bad cite on one is dropped, never corrected.
# A bullet is a sentence and keeps the full sentence policy.
STRUCTURAL_KINDS: frozenset[str] = frozenset({BLOCK_HEADING, BLOCK_TABLE})

# A cell that only marks absence is structure, not a claim: it emits no unit
# and renders as an empty cell (the CSS draws the dash).
_PLACEHOLDER_CELL_RE = re.compile("^(?:-{1,3}|\u2013|\u2014|n/?a)$", re.IGNORECASE)


def is_label(block: Block) -> bool:
    """True for a heading or a table label cell (header row or first column).

    A label names a topic; it has no predicate. "Recommended BE design" and
    "Requirements" are the vocabulary of every PSG heading, so the attribution
    lexicon (which exists to catch "FDA recommends X" SENTENCES) must not run
    on labels, or every structured answer loses its headings. The materiality
    lexicon still runs: "Fed study not required" is an assertion wherever it
    sits. Value cells (row > 0, column > 0) are assertions and get the full
    sentence policy.
    """
    if block.kind == BLOCK_HEADING:
        return True
    return block.kind == BLOCK_TABLE and (block.item == 0 or block.cell == 0)


# Up to three spaces of indent is still a block marker in CommonMark; list
# markers accept any indent so a nested list flattens into its parent rather
# than parsing as a paragraph of dashes.
_HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*(?:\s#+)?\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(?P<text>.*)$")
_NUMBERED_RE = re.compile(r"^\s*\d{1,3}[.)]\s+(?P<text>.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
_DELIMITER_CELL_RE = re.compile(r"^:?-+:?$")
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?")


@dataclass(frozen=True)
class Block:
    """Where a unit sits in the markdown shape of the completion.

    Attributes:
        kind: One of the ``BLOCK_*`` constants.
        group: Index of the container (a paragraph, a heading, one whole list,
            one whole table), increasing through the document. Consecutive
            units with the same group belong to the same container.
        item: List item index, or table row (0 is the header row); 0 for a
            paragraph or heading.
        cell: Table column; 0 elsewhere.
        level: Heading level 1-6; 0 elsewhere.
        width: Column count of the table the cell belongs to; 0 elsewhere.
    """

    kind: str = BLOCK_PARAGRAPH
    group: int = 0
    item: int = 0
    cell: int = 0
    level: int = 0
    width: int = 0


PARAGRAPH = Block()


@dataclass(frozen=True)
class Unit:
    """One claim-sized piece of text and the block it came from."""

    text: str
    block: Block


def _split_row(line: str) -> list[str]:
    """Cells of one GFM table row, outer pipes removed, each cell stripped."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _is_delimiter_row(cells: list[str]) -> bool:
    return bool(cells) and all(_DELIMITER_CELL_RE.match(c) for c in cells)


class _Segmenter:
    """Line-driven state machine; one instance per ``segment`` call."""

    def __init__(self) -> None:
        self.units: list[Unit] = []
        self.group = -1
        self._paragraph: list[str] = []
        self._list_kind: str | None = None
        self._list_item = -1
        self._item_lines: list[str] = []
        self._table_open = False
        self._table_row = -1
        self._table_width = 0
        # A "|" line is only a table header once the NEXT line is a delimiter
        # row (GFM); until then it is held here, and released as ordinary
        # text if no delimiter follows.
        self._pending_header: str | None = None

    # -- flushing ------------------------------------------------------------

    def _next_group(self) -> int:
        self.group += 1
        return self.group

    def _flush_paragraph(self) -> None:
        if not self._paragraph:
            return
        text = " ".join(self._paragraph)
        self._paragraph = []
        group = self._next_group()
        block = Block(kind=BLOCK_PARAGRAPH, group=group)
        self.units.extend(Unit(s.strip(), block) for s in split_sentences(text) if s.strip())

    def _flush_item(self) -> None:
        if not self._item_lines or self._list_kind is None:
            return
        text = " ".join(self._item_lines)
        self._item_lines = []
        block = Block(kind=self._list_kind, group=self.group, item=self._list_item)
        self.units.extend(Unit(s.strip(), block) for s in split_sentences(text) if s.strip())

    def _close_list(self) -> None:
        self._flush_item()
        self._list_kind = None
        self._list_item = -1

    def _close_table(self) -> None:
        self._table_open = False
        self._table_row = -1
        self._table_width = 0

    def _release_pending_header(self) -> None:
        pending = self._pending_header
        self._pending_header = None
        if pending is not None:
            self._text_line(pending)

    def _close_all(self) -> None:
        self._release_pending_header()
        self._flush_paragraph()
        self._close_list()
        self._close_table()

    # -- per-line handlers ---------------------------------------------------

    def _heading(self, level: int, text: str) -> None:
        self._close_all()
        if not text:
            return
        group = self._next_group()
        self.units.append(Unit(text, Block(kind=BLOCK_HEADING, group=group, level=level)))

    def _list_marker(self, kind: str, text: str) -> None:
        self._flush_paragraph()
        self._close_table()
        if self._list_kind != kind:
            self._close_list()
            self._list_kind = kind
            self._next_group()
        else:
            self._flush_item()
        self._list_item += 1
        self._item_lines = [text] if text else []

    def _open_table(self, header: str) -> None:
        self._flush_paragraph()
        self._close_list()
        self._table_open = True
        self._next_group()
        self._table_row = -1
        self._table_width = len(_split_row(header))
        self._emit_row(header)

    def _emit_row(self, line: str) -> None:
        self._table_row += 1
        block = Block(
            kind=BLOCK_TABLE,
            group=self.group,
            item=self._table_row,
            width=self._table_width,
        )
        # Every cell is kept, including one beyond the header's width: the
        # renderer widens the grid from the largest column index rather than
        # this module silently discarding model text (review finding,
        # 2026-08-21). GFM itself would drop the excess; we would rather show
        # it under an empty header than lose a cited cell.
        for column, cell in enumerate(_split_row(line)):
            if cell and not _PLACEHOLDER_CELL_RE.match(cell):
                self.units.append(Unit(cell, replace(block, cell=column)))

    def _table_row_line(self, line: str) -> None:
        cells = _split_row(line)
        if self._table_open:
            if not _is_delimiter_row(cells):
                self._emit_row(line)
            return
        if self._pending_header is None:
            if not _is_delimiter_row(cells):
                self._pending_header = line
            return
        if _is_delimiter_row(cells):
            header, self._pending_header = self._pending_header, None
            self._open_table(header)
            return
        # Two "|" lines with no delimiter between them: neither is a table.
        self._release_pending_header()
        self._pending_header = line

    def _text_line(self, line: str) -> None:
        if self._list_kind is not None:
            # Lazy continuation: a non-blank, non-marker line after a list item
            # is still that item (CommonMark), indented or not.
            self._item_lines.append(line.strip())
            return
        self._close_table()
        self._paragraph.append(_BLOCKQUOTE_RE.sub("", line, count=1).strip())

    def feed(self, line: str) -> None:
        """Consume one raw line of the completion."""
        if not line.strip():
            self._close_all()
            return
        if self._pending_header is not None and not _TABLE_ROW_RE.match(line):
            self._release_pending_header()
        heading = _HEADING_RE.match(line)
        if heading:
            self._heading(len(heading.group("hashes")), heading.group("text").strip())
            return
        if _TABLE_ROW_RE.match(line):
            self._table_row_line(line)
            return
        bullet = _BULLET_RE.match(line)
        if bullet:
            self._list_marker(BLOCK_BULLET, bullet.group("text").strip())
            return
        numbered = _NUMBERED_RE.match(line)
        if numbered:
            self._list_marker(BLOCK_NUMBERED, numbered.group("text").strip())
            return
        self._text_line(line)

    def finish(self) -> list[Unit]:
        """Flush whatever is open and return every unit in document order."""
        self._close_all()
        return self.units


def segment(text: str) -> list[Unit]:
    """Split a markdown-shaped completion into claim units in document order.

    Args:
        text: The raw completion.

    Returns:
        One ``Unit`` per sentence of a paragraph or list item, per heading, and
        per non-empty table cell. A table's delimiter row and an empty cell
        produce no unit; the cell's position survives through ``Block.cell``
        and ``Block.width`` so a renderer can pad the grid back.
    """
    machine = _Segmenter()
    for line in (text or "").splitlines():
        machine.feed(line)
    return machine.finish()


def split_units(text: str) -> list[str]:
    """The texts of ``segment(text)``: what a metric or a bound should count."""
    return [unit.text for unit in segment(text)]
