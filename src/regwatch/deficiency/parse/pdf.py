from __future__ import annotations

import re
from pathlib import Path

import fitz

from regwatch.common.logging import get_logger
from regwatch.deficiency.parse.layout import RawLine, group_lines_into_blocks, mark_header_footer
from regwatch.deficiency.parse.ocr import is_scanned_page, ocr_page
from regwatch.deficiency.schemas.documents import ExtractedTable, LayoutBlock, LayoutFigure

log = get_logger(__name__)

# An image covering less than this fraction of the page is a logo, not a figure.
_MIN_FIGURE_COVERAGE = 0.03
_PAGE_LABEL_RE = re.compile(r"page\s+(\d+)\s+of\s+\d+", re.IGNORECASE)
_CAPTION_RE = re.compile(r"\b(figure|appendix|table)\b", re.IGNORECASE)


def _rotated_bbox(bbox, page: fitz.Page) -> tuple[float, float, float, float]:
    """Map an unrotated PyMuPDF bbox into the page's displayed (rotated) space.

    get_text/find_tables/get_image_info return unrotated coordinates, but page.rect and
    the OCR path (get_pixmap) are in displayed space. Converting here keeps all geometry
    in one coordinate system. A no-op when the page is not rotated.
    """
    if page.rotation == 0:
        return tuple(bbox)
    rect = fitz.Rect(bbox) * page.rotation_matrix
    rect.normalize()
    return (rect.x0, rect.y0, rect.x1, rect.y1)


def extract_tables(page: fitz.Page) -> list[ExtractedTable]:
    results: list[ExtractedTable] = []
    finder = page.find_tables()
    page_height = page.rect.height or 1.0
    for table in finder.tables:
        raw = table.extract()
        if not raw or len(raw) < 2:
            continue

        grid = [[str(cell or "").strip() for cell in row] for row in raw]

        # (#1) A caption inside the outer border becomes grid row 0 -- either a blank
        # spacer row or a single-cell title spanning the table. Drop/lift it so the real
        # header row isn't demoted into the data.
        while len(grid) >= 3 and not any(grid[0]):
            grid = grid[1:]
        in_table_title = ""
        if len(grid) >= 3 and sum(1 for c in grid[0] if c) == 1:
            in_table_title = next(c for c in grid[0] if c)
            grid = grid[1:]

        # (#2) Drop columns empty in every row -- spurious splits from merged/nested
        # cells (the OCR path already does this). Never removes a cell that holds text.
        if grid and grid[0]:
            width = len(grid[0])
            keep = [
                c for c in range(width) if any((row[c] if c < len(row) else "") for row in grid)
            ]
            if 0 < len(keep) < width:
                grid = [[(row[c] if c < len(row) else "") for c in keep] for row in grid]

        if len(grid) < 2:
            continue
        headers = grid[0]
        rows = grid[1:]

        bbox = table.bbox
        # (#3) Only read the band above the table as a title when the table isn't at the
        # very top of the page, where that band is the running header, not a caption.
        above_text = ""
        if bbox[1] > page_height * 0.15:
            above_rect = fitz.Rect(bbox[0], max(0, bbox[1] - 30), bbox[2], bbox[1])
            candidate = page.get_text("text", clip=above_rect).strip()
            if candidate and len(candidate) < 200:
                above_text = candidate
        title = in_table_title or above_text

        results.append(
            ExtractedTable(
                title=title,
                headers=headers,
                rows=rows,
                page=page.number + 1,
                kind="grid",
                bbox=_rotated_bbox(bbox, page),
                n_cols=len(headers),
                n_rows=len(rows) + 1,
                source_pages=[page.number + 1],
                confidence=1.0,
            )
        )
    return results


def _join_spans(spans: list[dict]) -> str:
    """Join a line's spans, inserting a space where they are visually separated.

    PyMuPDF splits a line into style runs; "1.4.2" and "Linearity" (different fonts) are
    two spans with a gap between them and no space of their own, so a naive join fuses
    them. Insert a space when spans don't touch.
    """
    parts: list[str] = []
    prev_x1 = None
    for sp in spans:
        text = sp.get("text", "")
        if not text:
            continue
        x0 = sp["bbox"][0]
        if (
            prev_x1 is not None
            and x0 - prev_x1 > 1.0
            and parts
            and not parts[-1].endswith(" ")
            and not text.startswith(" ")
        ):
            parts.append(" ")
        parts.append(text)
        prev_x1 = sp["bbox"][2]
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _digital_lines(page: fitz.Page, tables: list[ExtractedTable]) -> list[RawLine]:
    """Build one RawLine per visual line from the embedded text, skipping table content."""
    table_rects = [fitz.Rect(t.bbox) for t in tables if t.bbox]
    lines: list[RawLine] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:  # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _join_spans(spans)
            if not text:
                continue
            bbox = _rotated_bbox(line["bbox"], page)
            center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
            if any(r.contains(fitz.Point(center)) for r in table_rects):
                continue  # this line belongs to a table, not the prose
            main = max(spans, key=lambda sp: len(sp["text"]))
            lines.append(
                RawLine(
                    text=text,
                    bbox=bbox,
                    confidence=1.0,
                    font=str(main.get("font", "")),
                    size=float(main.get("size", 0.0)),
                    bold=bool(int(main.get("flags", 0)) & (1 << 4)),
                )
            )
    return lines


def _digital_blocks(page: fitz.Page, tables: list[ExtractedTable]) -> list[LayoutBlock]:
    blocks = group_lines_into_blocks(_digital_lines(page, tables), page.number + 1)
    return mark_header_footer(blocks, page.rect.height)


def _digital_figures(page: fitz.Page, blocks: list[LayoutBlock]) -> list[LayoutFigure]:
    page_area = abs(page.rect)
    if page_area <= 0:
        return []
    figures: list[LayoutFigure] = []
    for info in page.get_image_info():
        rect = fitz.Rect(info["bbox"])
        if abs(rect) / page_area < _MIN_FIGURE_COVERAGE:
            continue  # a logo, not a figure
        figures.append(
            LayoutFigure(
                bbox=_rotated_bbox(info["bbox"], page),
                page=page.number + 1,
                caption=_nearest_caption_block(blocks, tuple(rect)),
                image_ref=f"xref:{info.get('xref', '')}",
                confidence=1.0,
            )
        )
    return figures


def _nearest_caption_block(
    blocks: list[LayoutBlock], bbox: tuple[float, float, float, float]
) -> str:
    candidates = [b for b in blocks if _CAPTION_RE.search(b.text) and len(b.text) < 200]
    if not candidates:
        return ""
    top, bottom = bbox[1], bbox[3]

    def distance(b: LayoutBlock) -> float:
        by0, by1 = b.bbox[1], b.bbox[3]
        if by1 <= top:
            return top - by1
        if by0 >= bottom:
            return by0 - bottom
        return 0.0

    return min(candidates, key=distance).text.strip()


def _detect_page_label(text: str) -> str:
    match = _PAGE_LABEL_RE.search(text)
    return match.group(1) if match else ""


def extract_pdf(path: str | Path) -> dict:
    """Parse a PDF into the structured-document JSON.

    Returns a plain dict (no custom datatypes) -- this IS the parsed representation the
    rest of the pipeline flows:
        {filename, page_count, toc:[{level,title,page}],
         pages:[{page_number, page_label, width, height, rotation, source, is_scanned,
                 blocks:[...], tables:[...], figures:[...]}]}
    Blocks/tables/figures are the schema objects serialized to JSON.
    """
    path = Path(path)
    doc = fitz.open(str(path))

    toc = [
        {"level": level, "title": title, "page": page_num}
        for level, title, page_num in doc.get_toc()
    ]

    pages: list[dict] = []
    ocr_count = 0
    for page in doc:
        tables: list[ExtractedTable] = extract_tables(page)
        scanned = is_scanned_page(page)
        blocks: list[LayoutBlock] = []
        figures: list[LayoutFigure] = []
        source = "pymupdf"

        if scanned:
            source = "rapidocr"
            ocr_result = ocr_page(page)
            if ocr_result is not None:
                text, ocr_tables, blocks, figures = ocr_result
                # find_tables() finds nothing on a scan (grid lines are pixels, not
                # vectors), so these reconstructed tables are all this page has.
                tables = tables + ocr_tables
                ocr_count += 1
            else:
                source = "rapidocr-fallback"
                text = page.get_text("text")
        else:
            text = page.get_text("text")
            blocks = _digital_blocks(page, tables)
            figures = _digital_figures(page, blocks)

        pages.append(
            {
                "page_number": page.number + 1,
                "page_label": _detect_page_label(text),
                "width": page.rect.width,
                "height": page.rect.height,
                "rotation": page.rotation,
                "source": source,
                "is_scanned": scanned,
                "blocks": [b.model_dump() for b in blocks],
                "tables": [t.model_dump() for t in tables],
                "figures": [f.model_dump() for f in figures],
            }
        )

    if ocr_count:
        log.info("ocr_pages", count=ocr_count, filename=path.name)

    page_count = len(doc)
    doc.close()
    return {"filename": path.name, "page_count": page_count, "toc": toc, "pages": pages}
