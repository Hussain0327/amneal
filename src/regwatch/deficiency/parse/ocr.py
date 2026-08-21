"""Scanned-page detection and OCR layout reconstruction helpers.

PyMuPDF reads a PDF's embedded text; a scanned page has none of its own -- it's just
an image (or an unreliable scanner-OCR layer underneath). For pages that look scanned
RegWatch currently has no remote OCR provider. Scanned pages fall back to any
embedded text layer. The layout reconstruction helpers remain for local or future
application-owned OCR input.

The boxes drive parse.layout, which rebuilds paragraph blocks and tables. RapidOCR gives
box coordinates in rendered-image pixels; we convert them to PDF points so scanned and
digital pages share one coordinate space. Figures on a scan are found best-effort as
large text-free regions (RapidOCR reports no images), captioned from nearby text.

"""

from __future__ import annotations

import json
import re
from itertools import pairwise

import fitz

from regwatch.common.logging import get_logger
from regwatch.deficiency.parse.layout import (
    OCRRegion,
    blocks_to_text,
    mark_header_footer,
    reconstruct_ocr_page,
)
from regwatch.deficiency.schemas.documents import ExtractedTable, LayoutBlock, LayoutFigure

log = get_logger(__name__)

# A page whose largest image covers more than this fraction of the page is a scan,
# not a digital page that happens to carry a small logo.
_SCANNED_IMAGE_COVERAGE = 0.5

# Region coordinates are recorded against a 200 DPI raster.
_RENDER_DPI = 200

# A text-free vertical band taller than this (in units of median text height) on a
# scanned page is treated as a best-effort figure region.
_FIGURE_MIN_GAP = 6.0
_CAPTION_RE = re.compile(r"\b(figure|appendix|table)\b", re.IGNORECASE)

OcrResult = tuple[str, list[ExtractedTable], list[LayoutBlock], list[LayoutFigure]]


def is_scanned_page(page: fitz.Page) -> bool:
    """True when the page is a scanned image rather than real digital text.

    A scan is one big image covering the page; a digital page has small images at
    most. A glyphless font is the tell-tale of an invisible OCR layer over a scan.
    """
    page_area = abs(page.rect)
    if page_area <= 0:
        return False

    for info in page.get_image_info():
        if abs(fitz.Rect(info["bbox"])) / page_area > _SCANNED_IMAGE_COVERAGE:
            return True

    for font in page.get_fonts():  # noqa: SIM110 - explicit loop is clearer than any()
        if "glyphless" in str(font[3]).lower():
            return True

    return False


def ocr_page(page: fitz.Page) -> OcrResult | None:
    """Return no remote OCR result; callers use the embedded text fallback."""
    del page
    return None


def _reconstruct_prediction(payload, page: fitz.Page) -> OcrResult | None:
    """Turn one endpoint prediction into (text, tables, blocks, figures).

    The current endpoint returns a JSON list of region records ({text, x0, y0, x1, y1,
    score}) in image-pixel coordinates. An older endpoint returned a plain newline-joined
    string; we still accept that so the app works before the endpoint is redeployed --
    just without the layout reconstruction the boxes enable.
    """
    page_number = page.number + 1

    records = None
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("["):
            try:
                records = json.loads(stripped)
            except json.JSONDecodeError:
                records = None
        if records is None:
            return payload, [], [], []  # old endpoint: flat text, no boxes
    else:
        # Unexpected payload shape -> fall back to the embedded text layer.
        log.warning("ocr_unexpected_payload", page=page_number, payload_type=type(payload).__name__)
        return None

    scale = 72.0 / _RENDER_DPI  # image pixels @ _RENDER_DPI -> PDF points
    regions = _regions_from_records(records, scale)
    if not regions:
        return "", [], [], []

    blocks, tables = reconstruct_ocr_page(regions, page_number)
    mark_header_footer(blocks, page.rect.height)
    figures = _detect_figures(regions, page, page_number)
    text = blocks_to_text(blocks, tables)
    return text, tables, blocks, figures


def _regions_from_records(records, scale: float) -> list[OCRRegion]:
    """Parse endpoint region records into OCRRegions (converted to points), skipping bad ones."""
    regions: list[OCRRegion] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        text = str(rec.get("text", "")).strip()
        if not text:
            continue
        try:
            x0 = float(rec["x0"]) * scale
            y0 = float(rec["y0"]) * scale
            x1 = float(rec["x1"]) * scale
            y1 = float(rec["y1"]) * scale
        except (KeyError, TypeError, ValueError):
            continue  # coordinates are required
        try:
            score = float(rec.get("score", 1.0))
        except (TypeError, ValueError):
            score = 1.0  # a bad score must not discard a valid region
        regions.append(OCRRegion(text=text, x0=x0, y0=y0, x1=x1, y1=y1, score=score))
    return regions


def _detect_figures(
    regions: list[OCRRegion], page: fitz.Page, page_number: int
) -> list[LayoutFigure]:
    """Best-effort figures on a scan: large text-free vertical bands, captioned from nearby text.

    RapidOCR reports no images, so we can only infer a figure as a tall gap between text.
    Low confidence by design -- a blank band and a chart are indistinguishable without
    looking at the pixels.
    """
    if len(regions) < 2:
        return []

    unit = _median([r.height for r in regions])
    left = min(r.x0 for r in regions)
    right = max(r.x1 for r in regions)

    intervals = sorted((r.y0, r.y1) for r in regions)
    merged: list[list[float]] = [list(intervals[0])]
    for y0, y1 in intervals[1:]:
        if y0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], y1)
        else:
            merged.append([y0, y1])

    figures: list[LayoutFigure] = []
    for (_, prev_end), (next_start, _) in pairwise(
        merged
    ):  # regwatch ruff (RUF007) prefers pairwise over zip(s, s[1:])
        gap = next_start - prev_end
        if gap > _FIGURE_MIN_GAP * unit:
            caption = _nearest_caption(regions, prev_end, next_start)
            figures.append(
                LayoutFigure(
                    bbox=(left, prev_end, right, next_start),
                    page=page_number,
                    caption=caption,
                    image_ref="",
                    confidence=0.3,
                )
            )
    return figures


def _nearest_caption(regions: list[OCRRegion], band_top: float, band_bottom: float) -> str:
    """Pick the closest Figure/Table/Appendix-looking line just above or below the band."""
    candidates = [r for r in regions if _CAPTION_RE.search(r.text)]
    if not candidates:
        return ""

    def distance(r: OCRRegion) -> float:
        if r.y1 <= band_top:
            return band_top - r.y1
        if r.y0 >= band_bottom:
            return r.y0 - band_bottom
        return 0.0

    best = min(candidates, key=distance)
    return best.text.strip()


def _median(values: list[float]) -> float:
    from statistics import median

    return median(values) if values else 1.0
