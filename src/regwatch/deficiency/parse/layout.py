"""Geometry-driven layout reconstruction shared by the digital and OCR parse paths.

Both PyMuPDF (`get_text("dict")`) and RapidOCR return text with bounding boxes. This
module turns those boxes back into structure using geometry only, so the same logic
serves a digital page and a scanned one:

  * `group_lines_into_blocks` groups visual lines into paragraph blocks (keeping each
    line, so a wrong grouping is reversible), using the vertical-gap / indent /
    short-line / style-change signals.
  * `reconstruct_ocr_page` additionally rebuilds tables from RapidOCR cell boxes:
    it finds runs of row-like lines, splits a run where the column layout changes or a
    gap appears (so a key-value block is not fused into the data table beside it), and
    classifies each piece as a grid table or a key-value table.

All coordinates are PDF points (the OCR caller converts image pixels to points first),
and every threshold is a multiple of the median text height, so the logic is
independent of resolution and font size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import median

from regwatch.deficiency.schemas.documents import (
    ExtractedTable,
    LayoutBlock,
    LayoutLine,
    TablePair,
    TextStyle,
)

# --- running header/footer bands (share by both parse paths) -----------------
_HEADER_BAND = 0.12  # a block ending within the top this-fraction of the page is a header
_FOOTER_BAND = 0.92  # a block starting below this-fraction of the page is a footer

# --- block grouping (paragraph segmentation) ---------------------------------
_LINE_OVERLAP = 0.4  # regions sharing >= this fraction of vertical overlap are one line
_PARA_GAP = 0.5  # vertical gap > this*unit above a line starts a new paragraph
_SHORT_LINE = 2.0  # a line ending > this*unit short of the right margin ends a paragraph
_INDENT = 1.2  # a line indented > this*unit past the block's left edge starts a paragraph

# --- table detection ---------------------------------------------------------
_ROW_MIN_CELLS = 2  # a line with at least this many regions can be a table row
_TABLE_MIN_ROWS = 2  # a table needs at least this many rows
_GRID_MIN_COLS = 3  # a grid table needs at least this many columns
_COL_GAP = 1.6  # cell centres more than this*unit apart are different columns
_TABLE_SPLIT_GAP = 1.3  # a vertical gap > this*unit inside a row-run splits it in two
_KV_LABEL_FRACTION = 0.4  # a row with this fraction of "Label:"-cells is a key-value row
_CAPTION_MAX = 200  # a preceding prose line this short can title a table

# A key-value label: a word starting with a letter, then a colon (so a time like
# "5:43:14" or a numeric cell is NOT mistaken for a label).
_LABEL_RE = re.compile(r"^[A-Za-z][^:]{0,40}:")

# A colon-less form-field label -- a short MULTI-word Title-Case-ish phrase with no value
# in it, so CoA pairs like "Lot number  L00036941" / "Molecular Formula  C18H24O2" are
# recognised as key-value. Requiring >=2 words keeps single tokens (compound names,
# "Missing") from being mistaken for labels and flipping data rows into key-value.
_COLONLESS_LABEL_RE = re.compile(r"^[A-Z][A-Za-z]+(?:\s+[A-Za-z]+){1,4}$")
# \u2264/\u2265 are the upstream literal <=/>= glyphs: escape form keeps the
# source ASCII (repo rule) with byte-identical matching to upstream.
_VALUEISH_RE = re.compile(r"[0-9<>\u2264\u2265%]|^[A-Z]{1,5}[-\d]")


def _cell_is_label(cell: str) -> bool:
    if _LABEL_RE.match(cell):
        return True
    return bool(_COLONLESS_LABEL_RE.match(cell)) and not _VALUEISH_RE.search(cell)


@dataclass
class OCRRegion:
    """One piece of text RapidOCR recognized, with its axis-aligned box (PDF points)."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 1.0

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


@dataclass
class RawLine:
    """A visual line of text with its box and (optional, digital-only) font info.

    This is the shared input to `group_lines_into_blocks` -- the digital path builds it
    from PyMuPDF spans, the OCR path from clustered RapidOCR regions.
    """

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = 1.0
    font: str = ""
    size: float = 0.0
    bold: bool = False

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def height(self) -> float:
        return max(1.0, self.bbox[3] - self.bbox[1])

    @property
    def has_style(self) -> bool:
        return bool(self.font) or self.size > 0


def _unit_from_heights(heights: list[float]) -> float:
    return median(heights) if heights else 1.0


def _style_differs(a: RawLine, b: RawLine) -> bool:
    """True when two lines carry clearly different font styling (digital pages only)."""
    if not a.has_style or not b.has_style:
        return False
    if a.font != b.font:
        return True
    if abs(a.size - b.size) > 1.0:
        return True
    return a.bold != b.bold


def _dominant_style(lines: list[RawLine]) -> TextStyle | None:
    styled = [ln for ln in lines if ln.has_style]
    if not styled:
        return None
    first = styled[0]
    return TextStyle(font=first.font, size=first.size, bold=first.bold)


def group_lines_into_blocks(raw_lines: list[RawLine], page: int) -> list[LayoutBlock]:
    """Group visual lines into paragraph blocks, keeping the constituent lines.

    A new block starts when the vertical gap jumps, the previous line ended short of the
    right margin, the left edge indents, or the font style changes -- any one is enough.
    """
    lines = sorted(
        (ln for ln in raw_lines if ln.text and ln.text.strip()),
        key=lambda ln: (ln.bbox[1], ln.bbox[0]),
    )
    if not lines:
        return []

    unit = _unit_from_heights([ln.height for ln in lines])
    right_margin = max(ln.x1 for ln in lines)

    blocks: list[LayoutBlock] = []
    current: list[RawLine] = [lines[0]]
    for prev, line in pairwise(lines):  # regwatch ruff (RUF007) prefers pairwise over zip(s, s[1:])
        gap = line.bbox[1] - prev.bbox[3]
        block_left = min(ln.x0 for ln in current)
        starts_new = (
            gap > _PARA_GAP * unit
            or prev.x1 < right_margin - _SHORT_LINE * unit
            or line.x0 > block_left + _INDENT * unit
            or _style_differs(prev, line)
        )
        if starts_new:
            blocks.append(_make_block(current, page, len(blocks)))
            current = [line]
        else:
            current.append(line)
    blocks.append(_make_block(current, page, len(blocks)))
    return blocks


_HEADING_HINT = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S")


def mark_header_footer(blocks: list[LayoutBlock], page_height: float) -> list[LayoutBlock]:
    """Tag blocks in the top/bottom band of the page as running header/footer.

    A positional heuristic (top/bottom band), not a cross-page repetition check. A block
    that looks like a numbered section heading is never tagged, so a heading sitting near
    a band edge is not mistaken for a header/footer and dropped by the splitter. Mutates
    and returns the blocks; the splitter drops the tagged ones ("Estradiol, USP..." /
    "Confidential Page X of 54").
    """
    if page_height <= 0:
        return blocks
    for block in blocks:
        if _HEADING_HINT.match(block.text):
            continue  # never drop a section heading
        if block.bbox[3] <= page_height * _HEADER_BAND:
            block.role = "page_header"
        elif block.bbox[1] >= page_height * _FOOTER_BAND:
            block.role = "page_footer"
    return blocks


def _make_block(lines: list[RawLine], page: int, order: int) -> LayoutBlock:
    x0 = min(ln.bbox[0] for ln in lines)
    y0 = min(ln.bbox[1] for ln in lines)
    x1 = max(ln.bbox[2] for ln in lines)
    y1 = max(ln.bbox[3] for ln in lines)
    text = " ".join(ln.text.strip() for ln in lines if ln.text.strip()).strip()
    confidence = min((ln.confidence for ln in lines), default=1.0)
    return LayoutBlock(
        role="paragraph",
        text=text,
        bbox=(x0, y0, x1, y1),
        page=page,
        reading_order=order,
        confidence=confidence,
        style=_dominant_style(lines),
        lines=[
            LayoutLine(text=ln.text.strip(), bbox=ln.bbox, confidence=ln.confidence) for ln in lines
        ],
    )


# --- OCR line clustering -----------------------------------------------------
@dataclass
class _RegionLine:
    y0: float
    y1: float
    regions: list[OCRRegion] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(r.text for r in self.regions if r.text).strip()

    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(r.x0 for r in self.regions),
            min(r.y0 for r in self.regions),
            max(r.x1 for r in self.regions),
            max(r.y1 for r in self.regions),
        )

    def to_raw(self) -> RawLine:
        conf = min((r.score for r in self.regions), default=1.0)
        return RawLine(text=self.text(), bbox=self.bbox(), confidence=conf)


def _group_regions_into_lines(regions: list[OCRRegion]) -> list[_RegionLine]:
    ordered = sorted(regions, key=lambda r: (r.y_center, r.x0))
    lines: list[_RegionLine] = []
    for r in ordered:
        line = lines[-1] if lines else None
        if line is not None:
            overlap = min(r.y1, line.y1) - max(r.y0, line.y0)
            unit = min(r.height, line.y1 - line.y0)
            same = unit > 0 and overlap / unit >= _LINE_OVERLAP
        else:
            same = False
        if same:
            line.regions.append(r)
            line.y0 = min(line.y0, r.y0)
            line.y1 = max(line.y1, r.y1)
        else:
            lines.append(_RegionLine(y0=r.y0, y1=r.y1, regions=[r]))
    for line in lines:
        line.regions.sort(key=lambda r: r.x0)
    return lines


# --- table reconstruction from OCR cell boxes --------------------------------
def _cluster_columns(regions: list[OCRRegion], unit: float) -> list[float]:
    centers = sorted(r.x_center for r in regions)
    clusters: list[list[float]] = [[centers[0]]]
    for c in centers[1:]:
        if c - clusters[-1][-1] > _COL_GAP * unit:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    return [sum(group) / len(group) for group in clusters]


def _row_is_kv(line: _RegionLine) -> bool:
    """A row is key-value if it is a label/value pair.

    A clean 2-cell row counts via a colon OR a colon-less form-field label, so CoA pairs
    ("Lot number  L00036941") are caught. Wider rows count only via colons -- otherwise a
    table header of multi-word column names would look like a key-value row.
    """
    cells = [r.text.strip() for r in line.regions if r.text.strip()]
    if not cells:
        return False
    if len(cells) == 2 and _cell_is_label(cells[0]) and not _cell_is_label(cells[1]):
        return True
    colon_labels = sum(1 for c in cells if _LABEL_RE.match(c))
    return colon_labels / len(cells) >= _KV_LABEL_FRACTION


def _segment_run(run: list[_RegionLine], unit: float) -> list[list[_RegionLine]]:
    """Split a run of row-like lines at a vertical gap OR a row-type change.

    This is the over-merge fix: a key-value block (label rows) above a data table (grid
    rows) is split at the point the row type flips, so the grid's numeric data is never
    swallowed by the key-value classifier and dropped.
    """
    segments: list[list[_RegionLine]] = [[run[0]]]
    for prev, line in pairwise(run):  # regwatch ruff (RUF007) prefers pairwise over zip(s, s[1:])
        gap = line.y0 - prev.y1
        type_changed = _row_is_kv(prev) != _row_is_kv(line)
        if gap > _TABLE_SPLIT_GAP * unit or type_changed:
            segments.append([line])
        else:
            segments[-1].append(line)
    return segments


def _segment_is_kv(segment: list[_RegionLine]) -> bool:
    kv_rows = sum(1 for line in segment if _row_is_kv(line))
    return kv_rows * 2 >= len(segment)  # majority are key-value rows


def _build_key_value(segment: list[_RegionLine], page: int) -> ExtractedTable | None:
    """Emit a key-value block as label/value pairs rather than a fake grid."""
    pairs: list[TablePair] = []
    for line in segment:
        cells = [r.text.strip() for r in line.regions if r.text.strip()]
        i = 0
        while i < len(cells):
            cell = cells[i]
            if _LABEL_RE.match(cell):  # "Label: value"
                label, _, tail = cell.partition(":")
                value = tail.strip()
                if not value and i + 1 < len(cells) and not _cell_is_label(cells[i + 1]):
                    value = cells[i + 1]
                    i += 1
                pairs.append(TablePair(label=label.strip(), value=value))
            elif _cell_is_label(cell) and i + 1 < len(cells) and not _cell_is_label(cells[i + 1]):
                pairs.append(TablePair(label=cell, value=cells[i + 1]))  # colon-less "Label  value"
                i += 1
            i += 1
    if len(pairs) < 2:
        return None
    regions = [r for line in segment for r in line.regions]
    return ExtractedTable(
        kind="key_value",
        pairs=pairs,
        page=page,
        bbox=_segment_bbox(segment),
        source_pages=[page],
        confidence=min((r.score for r in regions), default=1.0),
    )


def _build_grid(segment: list[_RegionLine], page: int, unit: float) -> ExtractedTable | None:
    regions = [r for line in segment for r in line.regions]
    col_centers = _cluster_columns(regions, unit)
    if len(col_centers) < _GRID_MIN_COLS:
        return None

    grid: list[list[str]] = []
    for line in segment:
        cells = [""] * len(col_centers)
        for r in line.regions:
            col = min(range(len(col_centers)), key=lambda i: abs(col_centers[i] - r.x_center))
            cells[col] = (cells[col] + " " + r.text).strip() if cells[col] else r.text
        grid.append(cells)

    # Drop columns that are empty in every row -- spurious clusters from mixed content.
    # Only ever removes blank columns, so no cell value is lost.
    keep = [c for c in range(len(col_centers)) if any(row[c] for row in grid)]
    if len(keep) < _GRID_MIN_COLS:
        return None
    if len(keep) < len(col_centers):
        grid = [[row[c] for c in keep] for row in grid]

    return ExtractedTable(
        kind="grid",
        headers=grid[0],
        rows=grid[1:],
        page=page,
        bbox=_segment_bbox(segment),
        n_cols=len(keep),
        n_rows=len(grid),
        source_pages=[page],
        confidence=min((r.score for r in regions), default=1.0),
    )


def _segment_bbox(segment: list[_RegionLine]) -> tuple[float, float, float, float]:
    regions = [r for line in segment for r in line.regions]
    return (
        min(r.x0 for r in regions),
        min(r.y0 for r in regions),
        max(r.x1 for r in regions),
        max(r.y1 for r in regions),
    )


def reconstruct_ocr_page(
    regions: list[OCRRegion], page: int
) -> tuple[list[LayoutBlock], list[ExtractedTable]]:
    """Rebuild paragraph blocks and tables from RapidOCR regions for one page."""
    regions = [r for r in regions if r.text and r.text.strip()]
    if not regions:
        return [], []

    unit = _unit_from_heights([r.height for r in regions])
    lines = _group_regions_into_lines(regions)

    tables: list[ExtractedTable] = []
    prose_lines: list[RawLine] = []
    last_prose_text = ""

    i = 0
    while i < len(lines):
        if len(lines[i].regions) >= _ROW_MIN_CELLS:
            j = i
            while j < len(lines) and len(lines[j].regions) >= _ROW_MIN_CELLS:
                j += 1
            run = lines[i:j]
            if len(run) >= _TABLE_MIN_ROWS:
                for segment in _segment_run(run, unit):
                    if len(segment) < _TABLE_MIN_ROWS:
                        prose_lines.extend(line.to_raw() for line in segment)
                        continue
                    # Build the segment's own type first; fall back to the other so a
                    # grid is never dropped by the key-value builder (and vice versa).
                    if _segment_is_kv(segment):
                        table = _build_key_value(segment, page) or _build_grid(segment, page, unit)
                    else:
                        table = _build_grid(segment, page, unit) or _build_key_value(segment, page)
                    if table is not None:
                        if last_prose_text and len(last_prose_text) <= _CAPTION_MAX:
                            table.title = last_prose_text
                            last_prose_text = ""
                        tables.append(table)
                    else:
                        prose_lines.extend(line.to_raw() for line in segment)
            else:
                prose_lines.extend(line.to_raw() for line in run)
            i = j
            continue

        raw = lines[i].to_raw()
        prose_lines.append(raw)
        if raw.text:
            last_prose_text = raw.text
        i += 1

    blocks = group_lines_into_blocks(prose_lines, page)
    return blocks, tables


def blocks_to_text(blocks: list[LayoutBlock], tables: list[ExtractedTable]) -> str:
    """Flatten blocks + tables into reading-order text for the page `.text` field."""
    parts: list[str] = [b.text for b in blocks if b.text]
    for t in tables:
        if t.title:
            parts.append(t.title)
        if t.kind == "key_value":
            parts.extend(f"{p.label}: {p.value}".strip(": ").strip() for p in t.pairs)
        else:
            if t.headers:
                parts.append("\t".join(t.headers).rstrip())
            for row in t.rows:
                parts.append("\t".join(row).rstrip())
    return "\n".join(p for p in parts if p)
