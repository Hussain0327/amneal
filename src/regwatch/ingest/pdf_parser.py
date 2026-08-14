"""Bounded PDF parser — pdfplumber, pypdf, then sandboxed OCR fallback.

We preserve per-page text because every citation includes a page number. The
authoritative-corpus path stages one file and parses that path without copying
the complete PDF into Python bytes. OCR runs only inside the already-killable
parser child and invokes a reviewed executable without a shell.
"""

from __future__ import annotations

import io
import math
import multiprocessing
import os
import queue as queue_mod
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import get_settings

from regwatch.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class ParsedPdf:
    text: str  # joined full-document text (page-separated by \n\f\n)
    pages: list[str]  # per-page text, 1-indexed via pages[n-1]
    engine: str


@dataclass(frozen=True)
class OcrConfig:
    """Resource and fidelity bounds for one document's OCR fallback."""

    enabled: bool
    binary: str
    language: str
    dpi: int
    page_timeout_s: float
    max_pages: int
    max_pixels: int
    max_output_bytes: int
    memory_limit_bytes: int


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
_OCR_LANGUAGE_RE = re.compile(r"[A-Za-z0-9_.+-]+")


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


def _try_pdfplumber_path(path: str, max_pages: int) -> ParsedPdf | None:
    try:
        import pdfplumber
    except Exception as exc:  # pragma: no cover
        log.warning("pdfplumber_unavailable", error=str(exc))
        return None
    pages: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            _check_page_bound(len(pdf.pages), max_pages)
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                pages.append(_normalize(text))
    except PdfPageLimitError:
        raise
    except Exception as exc:
        log.warning("pdfplumber_failed", error=str(exc))
        return None
    return ParsedPdf(text=_PAGE_SEP.join(pages), pages=pages, engine="pdfplumber")


def _try_pypdf_path(path: str, max_pages: int) -> ParsedPdf | None:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover
        log.warning("pypdf_unavailable", error=str(exc))
        return None
    pages: list[str] = []
    try:
        reader = PdfReader(path)
        _check_page_bound(len(reader.pages), max_pages)
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                log.warning("pypdf_page_failed", page=len(pages) + 1, error=str(exc))
                text = ""
            pages.append(_normalize(text))
    except PdfPageLimitError:
        raise
    except Exception as exc:
        log.warning("pypdf_failed", error=str(exc))
        return None
    if not pages:
        return None
    return ParsedPdf(text=_PAGE_SEP.join(pages), pages=pages, engine="pypdf")


def _resolve_ocr_binary(binary: str) -> str:
    candidate = binary.strip()
    if not candidate:
        raise PdfParseError("OCR binary is empty")
    resolved = shutil.which(candidate)
    if resolved is None:
        raise PdfParseError(f"OCR binary is unavailable: {candidate}")
    path = Path(resolved).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise PdfParseError(f"OCR binary is not executable: {path}")
    return str(path)


def _set_ocr_resource_limits(config: OcrConfig) -> None:
    """Apply process limits immediately before exec in the parser child."""

    import resource

    def bounded(limit: int, requested: int) -> tuple[int, int]:
        _, hard = resource.getrlimit(limit)
        value = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        return value, value

    cpu_seconds = max(1, math.ceil(config.page_timeout_s) + 1)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, bounded(resource.RLIMIT_CPU, cpu_seconds))
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        bounded(resource.RLIMIT_FSIZE, config.max_output_bytes),
    )
    # RLIMIT_AS cannot be lowered beneath the already-mapped address space on
    # macOS, so development workers rely on the enclosing parser process and
    # container memory bound. The production worker is Linux, where this limit
    # is enforced before tesseract execs.
    if sys.platform.startswith("linux"):
        resource.setrlimit(
            resource.RLIMIT_AS,
            bounded(resource.RLIMIT_AS, config.memory_limit_bytes),
        )
    resource.setrlimit(resource.RLIMIT_NOFILE, bounded(resource.RLIMIT_NOFILE, 64))


def _ocr_page(png: bytes, page_number: int, config: OcrConfig, directory: Path) -> str:
    binary = _resolve_ocr_binary(config.binary)
    if _OCR_LANGUAGE_RE.fullmatch(config.language) is None:
        raise PdfParseError("OCR language contains unsupported characters")

    input_path = directory / f"page-{page_number:05d}.png"
    output_base = directory / f"page-{page_number:05d}"
    output_path = output_base.with_suffix(".txt")
    log_path = directory / f"page-{page_number:05d}.log"
    input_path.write_bytes(png)
    environment = {
        "HOME": str(directory),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": str(directory),
    }
    with log_path.open("wb") as command_log:
        process = subprocess.Popen(  # noqa: S603 - absolute reviewed binary, no shell
            [
                binary,
                str(input_path),
                str(output_base),
                "-l",
                config.language,
                "--dpi",
                str(config.dpi),
                "txt",
            ],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=command_log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            preexec_fn=lambda: _set_ocr_resource_limits(config),
        )
        try:
            return_code = process.wait(timeout=config.page_timeout_s)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise PdfParseError(
                f"OCR page {page_number} exceeded {config.page_timeout_s:g}s"
            ) from exc
    if return_code != 0:
        detail = log_path.read_text(encoding="utf-8", errors="replace")[:500].strip()
        raise PdfParseError(f"OCR page {page_number} failed ({return_code}): {detail}")
    if not output_path.is_file():
        raise PdfParseError(f"OCR page {page_number} did not produce text")
    if output_path.stat().st_size > config.max_output_bytes:
        raise PdfParseError(f"OCR page {page_number} exceeded its output byte limit")
    return _normalize(output_path.read_text(encoding="utf-8", errors="replace"))


def _fill_blank_pages_with_ocr(
    path: str,
    pages: list[str],
    config: OcrConfig,
    max_pages: int,
) -> list[str]:
    try:
        import pymupdf
    except Exception as exc:  # pragma: no cover
        raise PdfParseError("PyMuPDF is unavailable for OCR rendering") from exc

    try:
        document = pymupdf.open(path)  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise PdfParseError(f"OCR could not open PDF: {exc}") from exc
    with document:
        page_count = len(document)
        _check_page_bound(page_count, max_pages)
        if page_count > config.max_pages:
            raise PdfPageLimitError(
                f"PDF has {page_count} pages, exceeds fda_corpus_ocr_max_pages="
                f"{config.max_pages}"
            )
        if pages and len(pages) != page_count:
            raise PdfParseError("PDF engines disagreed on page count before OCR")
        result = list(pages) if pages else [""] * page_count
        scale = config.dpi / 72.0
        matrix = pymupdf.Matrix(scale, scale)  # type: ignore[no-untyped-call]
        # dir= keeps OCR scratch inside the same bounded volume as the staged
        # artifact it is rendering. Without it these page renders -- up to
        # fda_corpus_ocr_max_pixels each -- land in the OS default temp dir,
        # outside the FDA_CORPUS_TEMP_DIR budget the runbook tells operators to
        # size. Derived from `path` rather than from settings because this runs
        # in a SPAWN child: `path` crosses the process boundary as an argument,
        # while get_settings() there would rebuild from env and miss an
        # in-process override. sync.py stages that artifact with
        # dir=fda_corpus_temp_dir, so this is the same volume; when that setting
        # is unset both are the OS default and behaviour is unchanged.
        #
        # RESIDUAL: a parse TIMEOUT still leaks this directory. _terminate()
        # SIGTERMs then SIGKILLs the child, and neither runs __exit__, so the
        # renders survive the process. The leak is now inside the operator's
        # bounded scratch volume where it can be swept, instead of system /tmp,
        # but a long backfill still needs that sweep.
        with tempfile.TemporaryDirectory(
            prefix="regwatch-ocr-", dir=Path(path).parent
        ) as temporary:
            directory = Path(temporary)
            for index, current in enumerate(result):
                if current.strip():
                    continue
                page = document.load_page(index)  # type: ignore[no-untyped-call]
                width = math.ceil(page.rect.width * scale)
                height = math.ceil(page.rect.height * scale)
                pixels = width * height
                if pixels > config.max_pixels:
                    raise PdfParseError(
                        f"OCR page {index + 1} has {pixels} pixels, exceeds "
                        f"fda_corpus_ocr_max_pixels={config.max_pixels}"
                    )
                pixmap = page.get_pixmap(
                    matrix=matrix,
                    colorspace=pymupdf.csGRAY,
                    alpha=False,
                )
                result[index] = _ocr_page(
                    pixmap.tobytes("png"),
                    index + 1,
                    config,
                    directory,
                )
    return result


def _extract_path(path: str, max_pages: int, ocr: OcrConfig) -> ParsedPdf:
    """Extract one staged PDF path without buffering the full document."""

    parsed = _try_pdfplumber_path(path, max_pages)
    if parsed is None:
        parsed = _try_pypdf_path(path, max_pages)
    if parsed is not None and all(page.strip() for page in parsed.pages):
        return parsed
    if ocr.enabled:
        pages = _fill_blank_pages_with_ocr(
            path,
            parsed.pages if parsed is not None else [],
            ocr,
            max_pages,
        )
        if any(page.strip() for page in pages):
            base_engine = parsed.engine if parsed is not None else "none"
            return ParsedPdf(
                text=_PAGE_SEP.join(pages),
                pages=pages,
                engine=f"{base_engine}+tesseract",
            )
    if parsed is not None and any(page.strip() for page in parsed.pages):
        return parsed
    raise PdfParseError("Failed to extract text from PDF with pdfplumber, pypdf, and OCR")


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


def corpus_ocr_config() -> OcrConfig:
    """Resolve the authoritative-corpus OCR contract in the parent process."""

    settings = get_settings()
    return OcrConfig(
        enabled=settings.fda_corpus_ocr_enabled,
        binary=settings.fda_corpus_ocr_binary,
        language=settings.fda_corpus_ocr_language,
        dpi=settings.fda_corpus_ocr_dpi,
        page_timeout_s=settings.fda_corpus_ocr_page_timeout_s,
        max_pages=settings.fda_corpus_ocr_max_pages,
        max_pixels=settings.fda_corpus_ocr_max_pixels,
        max_output_bytes=settings.fda_corpus_ocr_max_output_bytes,
        memory_limit_bytes=settings.fda_corpus_ocr_memory_limit_bytes,
    )


def parse_pdf_path(
    path: Path,
    *,
    timeout_s: float | None = None,
    max_pages: int | None = None,
    ocr: OcrConfig | None = None,
) -> ParsedPdf:
    """Parse one staged file with path-based extraction and bounded OCR fallback."""

    if timeout_s is None:
        timeout_s = get_settings().fda_corpus_pdf_parse_timeout_s
    if max_pages is None:
        max_pages = get_settings().fda_corpus_pdf_max_pages
    selected_ocr = ocr or corpus_ocr_config()
    resolved = path.resolve(strict=True)
    if timeout_s > 0:
        return _run_with_timeout(
            _extract_path,
            (str(resolved), max_pages, selected_ocr),
            timeout_s,
        )
    return _extract_path(str(resolved), max_pages, selected_ocr)
