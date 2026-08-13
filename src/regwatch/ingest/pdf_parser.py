"""PDF parser — pdfplumber primary, pypdf fallback.

PSG PDFs are digital (no OCR needed). We preserve per-page text because every
citation includes a page number. The return shape is the same regardless of
which engine produced it, so downstream code never special-cases.
"""

from __future__ import annotations

import io
import multiprocessing
import queue as queue_mod
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from config.settings import get_settings

from regwatch.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class ParsedPdf:
    text: str  # joined full-document text (page-separated by \n\f\n)
    pages: list[str]  # per-page text, 1-indexed via pages[n-1]
    engine: str


class PdfParseError(RuntimeError):
    """Text extraction failed (both engines) or could not be bounded."""


class PdfParseTimeoutError(PdfParseError):
    """Extraction exceeded the configured wall-clock budget and was killed."""


class PdfPageLimitError(PdfParseError):
    """Page count exceeds the configured pdf_max_pages bound.

    Catchable only when parsing in-process: the spawn child serializes
    exceptions to strings, so through parse_pdf's default subprocess path this
    surfaces as a plain PdfParseError whose message names this class.
    """


_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")
_PAGE_SEP = "\n\f\n"


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


def _check_page_bound(page_count: int, max_pages: int) -> None:
    # Bounds the work BEFORE per-page extraction: a page-flood PDF (hundreds of
    # thousands of near-empty pages fit under pdf_max_bytes) would otherwise
    # burn the whole parse budget one page at a time. Raised, not returned-None,
    # so it punches through the engine fallback chain -- the second engine
    # would only re-count the same pages.
    if 0 < max_pages < page_count:
        raise PdfPageLimitError(f"PDF has {page_count} pages, exceeds pdf_max_pages={max_pages}")


def _try_pdfplumber(pdf_bytes: bytes, max_pages: int) -> ParsedPdf | None:
    try:
        import pdfplumber
    except Exception as exc:  # pragma: no cover
        log.warning("pdfplumber_unavailable", error=str(exc))
        return None
    pages: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            _check_page_bound(len(pdf.pages), max_pages)
            for page in pdf.pages:
                txt = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                pages.append(_normalize(txt))
    except PdfPageLimitError:
        raise
    except Exception as exc:
        log.warning("pdfplumber_failed", error=str(exc))
        return None
    return ParsedPdf(
        text=_PAGE_SEP.join(pages),
        pages=pages,
        engine="pdfplumber",
    )


def _try_pypdf(pdf_bytes: bytes, max_pages: int) -> ParsedPdf | None:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover
        log.warning("pypdf_unavailable", error=str(exc))
        return None
    pages: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        _check_page_bound(len(reader.pages), max_pages)
        for p in reader.pages:
            try:
                txt = p.extract_text() or ""
            except Exception as exc:
                # Append an empty page rather than dropping it, so page indices stay
                # 1:1 with the PDF — a dropped page would shift every later citation.
                log.warning("pypdf_page_failed", page=len(pages) + 1, error=str(exc))
                txt = ""
            pages.append(_normalize(txt))
    except PdfPageLimitError:
        raise
    except Exception as exc:
        log.warning("pypdf_failed", error=str(exc))
        return None
    if not pages:
        return None
    return ParsedPdf(text=_PAGE_SEP.join(pages), pages=pages, engine="pypdf")


def _extract(pdf_bytes: bytes, max_pages: int | None = None) -> ParsedPdf:
    """Pure extraction: pdfplumber, then pypdf. Raises PdfParseError if both fail
    or the document exceeds max_pages (None resolves from settings, 0 disables).

    No timeout/isolation here — that is parse_pdf's job. Kept as a separate
    module-level function so the engine logic stays testable in-process and the
    subprocess worker has a single, picklable entrypoint.
    """
    if max_pages is None:
        max_pages = get_settings().pdf_max_pages
    parsed = _try_pdfplumber(pdf_bytes, max_pages)
    if parsed is not None and any(p.strip() for p in parsed.pages):
        return parsed
    parsed = _try_pypdf(pdf_bytes, max_pages)
    if parsed is not None and any(p.strip() for p in parsed.pages):
        return parsed
    raise PdfParseError("Failed to extract text from PDF with both pdfplumber and pypdf")


# Grace period after terminate() before we hard-kill a parse that ignored it.
_TERMINATE_GRACE_S = 3.0
# How often the parent wakes to check whether a crashed child has died, so a
# segfault/OOM-kill fails fast instead of waiting out the whole parse budget.
_POLL_INTERVAL_S = 0.2
# Final short drain after the child exits, to catch a result it flushed just
# before exiting (a clean exit can race the parent's poll tick).
_DRAIN_S = 0.1


def _child_main(
    target: Callable[..., Any], args: tuple[Any, ...], out: Any
) -> None:  # pragma: no cover - body runs only inside the spawned child
    """Spawn-child entrypoint: run target(*args), ship (ok, payload) back.

    Always puts exactly one result, so the parent's blocking get() returns
    promptly on success AND on in-child failure; only a genuine hang leaves the
    queue empty and trips the timeout path.
    """
    try:
        out.put((True, target(*args)))
    except BaseException as exc:  # report ANY failure to the parent, never hang the queue
        out.put((False, f"{type(exc).__name__}: {exc}"))


def _terminate(proc: Any) -> None:
    """Best-effort kill + reap so no parse process is ever left running."""
    if proc.is_alive():
        proc.terminate()
        proc.join(_TERMINATE_GRACE_S)
        if proc.is_alive():
            proc.kill()
    proc.join()


def _run_with_timeout(target: Callable[..., Any], args: tuple[Any, ...], timeout_s: float) -> Any:
    """Run target(*args) in a killable spawn child, bounded by timeout_s.

    `spawn` (not fork) so the child never inherits the parent's locks/threads —
    the inverse of the 2026-06-18 inherited-state class of bug. target, its args,
    and its return value must be picklable. Raises PdfParseTimeoutError if the
    budget is exceeded (after killing the child); re-raises in-child failures as
    PdfParseError. The child process and queue are released on every path.
    """
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_child_main, args=(target, args, out), daemon=True)
    proc.start()
    try:
        # Poll instead of one long blocking get: Queue.get can't tell that the
        # only writer has died, so a child that crashes WITHOUT delivering a
        # result (native segfault / OS OOM-kill — exactly the malformed-PDF
        # threat this bounds) would otherwise hang the parent for the full budget
        # and then masquerade as a timeout. We detect the dead child and fail
        # fast with the truthful error instead.
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PdfParseTimeoutError(f"PDF parse exceeded {timeout_s:g}s") from None
            try:
                ok, payload = out.get(timeout=min(remaining, _POLL_INTERVAL_S))
            except queue_mod.Empty:
                if proc.is_alive():
                    continue  # still working — keep waiting until the deadline
                # Child has exited. Drain once in case it flushed a result just
                # before exiting; if there is genuinely none, it died without
                # delivering one.
                try:
                    ok, payload = out.get(timeout=_DRAIN_S)
                except queue_mod.Empty:
                    raise PdfParseError(
                        f"PDF parse process died (exitcode={proc.exitcode}) without a result"
                    ) from None
            if not ok:
                raise PdfParseError(str(payload))
            return payload
    finally:
        _terminate(proc)
        out.close()


def parse_pdf(
    pdf_bytes: bytes,
    *,
    timeout_s: float | None = None,
    max_pages: int | None = None,
) -> ParsedPdf:
    """Parse a PDF blob (pdfplumber, then pypdf), bounded by a hard timeout.

    By default extraction runs in a killable child process so a malformed or
    pathological PDF cannot hang or OOM the cron/CLI ingest run that calls this
    (the API never reaches this path). Pass or configure timeout_s<=0 to parse
    in-process — used by tests and bulk back-loads where isolation is unwanted.
    Raises PdfParseError on failure (including a document over the configured
    pdf_max_pages bound), PdfParseTimeoutError on timeout.
    """
    if timeout_s is None:
        timeout_s = get_settings().pdf_parse_timeout_s
    # Resolved in the parent so there is one source of truth: a child-side
    # get_settings() would rebuild Settings from env and miss in-process
    # overrides (a patched get_settings or mutated Settings instance; env-var
    # overrides WOULD survive, since the spawn child inherits os.environ).
    if max_pages is None:
        max_pages = get_settings().pdf_max_pages
    if timeout_s > 0:  # timeout_s is a resolved float here; <=0 means parse in-process
        return _run_with_timeout(_extract, (pdf_bytes, max_pages), timeout_s)
    return _extract(pdf_bytes, max_pages)
