"""PDF parser — pdfplumber primary, pypdf fallback.

PSG PDFs are digital (no OCR needed). We preserve per-page text because every
citation includes a page number. The return shape is the same regardless of
which engine produced it, so downstream code never special-cases.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from regwatch.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class ParsedPdf:
    text: str  # joined full-document text (page-separated by \n\f\n)
    pages: list[str]  # per-page text, 1-indexed via pages[n-1]
    engine: str


_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")
_PAGE_SEP = "\n\f\n"


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


def _try_pdfplumber(pdf_bytes: bytes) -> ParsedPdf | None:
    try:
        import pdfplumber
    except Exception as exc:  # pragma: no cover
        log.warning("pdfplumber_unavailable", error=str(exc))
        return None
    pages: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                pages.append(_normalize(txt))
    except Exception as exc:
        log.warning("pdfplumber_failed", error=str(exc))
        return None
    return ParsedPdf(
        text=_PAGE_SEP.join(pages),
        pages=pages,
        engine="pdfplumber",
    )


def _try_pypdf(pdf_bytes: bytes) -> ParsedPdf | None:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover
        log.warning("pypdf_unavailable", error=str(exc))
        return None
    pages: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for p in reader.pages:
            try:
                txt = p.extract_text() or ""
            except Exception as exc:
                # Append an empty page rather than dropping it, so page indices stay
                # 1:1 with the PDF — a dropped page would shift every later citation.
                log.warning("pypdf_page_failed", page=len(pages) + 1, error=str(exc))
                txt = ""
            pages.append(_normalize(txt))
    except Exception as exc:
        log.warning("pypdf_failed", error=str(exc))
        return None
    if not pages:
        return None
    return ParsedPdf(text=_PAGE_SEP.join(pages), pages=pages, engine="pypdf")


def parse_pdf(pdf_bytes: bytes) -> ParsedPdf:
    """Parse a PDF blob. Tries pdfplumber, falls back to pypdf. Raises if both fail."""
    parsed = _try_pdfplumber(pdf_bytes)
    if parsed is not None and any(p.strip() for p in parsed.pages):
        return parsed
    parsed = _try_pypdf(pdf_bytes)
    if parsed is not None and any(p.strip() for p in parsed.pages):
        return parsed
    raise RuntimeError("Failed to extract text from PDF with both pdfplumber and pypdf")
