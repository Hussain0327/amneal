"""WS1 parser-hardening tests.

These exercise the failure paths the hardening exists for — they fail if the
byte cap, the %PDF validation, the 4xx-no-retry rule, or the subprocess timeout
regress. The parse path runs only in the cron/CLI ingest worker (never the API),
so the threat is a malformed/oversized FDA PDF hanging or OOMing the daily run.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import httpx
import pytest
import respx

from regwatch.ingest.pdf_parser import (
    ParsedPdf,
    PdfParseError,
    PdfParseTimeoutError,
    _extract,
    _run_with_timeout,
    parse_pdf,
)
from regwatch.ingest.psg_crawler import (
    PdfInvalidError,
    PdfTooLargeError,
    _looks_like_pdf,
    _stream_capped,
    download_pdf,
)

_PDF_URL = "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_012345.pdf"


def _make_text_pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF whose page renders `text` (verified to
    extract cleanly with pdfplumber). Avoids committing a binary fixture."""
    content = b"BT /F1 24 Tf 72 700 Td (%s) Tj ET" % text.encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref_pos = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    return pdf


# ---------------------------------------------------------------------------
# Boundary: download byte cap + %PDF validation (psg_crawler)
# ---------------------------------------------------------------------------


def test_looks_like_pdf() -> None:
    assert _looks_like_pdf(b"%PDF-1.7\n...")
    assert _looks_like_pdf(b"\x00\x00%PDF-1.4")  # tolerates a little leading junk
    assert not _looks_like_pdf(b"<html>503 Service Unavailable</html>")
    assert not _looks_like_pdf(b"")


@respx.mock
def test_stream_capped_rejects_oversize_by_content_length() -> None:
    # Pin the fast-fail branch specifically: a large *declared* Content-Length
    # with a small streamed body. This must raise WITHOUT consuming the body —
    # the DoS value of the fast-fail is not downloading a huge declared payload.
    # (A plain 500-byte body would also trip the streaming-total branch, so it
    # would not distinguish this branch from that one.)
    consumed = {"chunks": 0}

    def body() -> Iterator[bytes]:
        consumed["chunks"] += 1
        yield b"x" * 50

    respx.get(_PDF_URL).mock(
        return_value=httpx.Response(200, headers={"content-length": str(10**9)}, content=body())
    )
    with httpx.Client() as client, pytest.raises(PdfTooLargeError):
        _stream_capped(client, _PDF_URL, max_bytes=100)
    assert consumed["chunks"] == 0  # body was never streamed — fast-fail tripped first


@respx.mock
def test_stream_capped_rejects_oversize_by_stream() -> None:
    # Streaming body with NO Content-Length (chunked iterator): the running total
    # must trip the cap mid-stream — the real OOM guard against a lying length.
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=iter([b"%PDF-", b"x" * 500])))
    with httpx.Client() as client, pytest.raises(PdfTooLargeError):
        _stream_capped(client, _PDF_URL, max_bytes=100)


@respx.mock
def test_stream_capped_passes_small_body() -> None:
    body = _make_text_pdf("ok")
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=body))
    with httpx.Client() as client:
        assert _stream_capped(client, _PDF_URL, max_bytes=10_000) == body


@respx.mock
def test_stream_capped_does_not_retry_4xx() -> None:
    route = respx.get(_PDF_URL).mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        _stream_capped(client, _PDF_URL, max_bytes=10_000)
    assert route.call_count == 1  # 4xx is terminal, not retried


@respx.mock
def test_download_pdf_rejects_non_pdf() -> None:
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=b"<html>error page</html>"))
    with pytest.raises(PdfInvalidError):
        download_pdf(_PDF_URL)


@respx.mock
def test_download_pdf_happy_path_caches_and_hashes() -> None:
    body = _make_text_pdf("RegWatch hardening test")
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=body))
    path, data, digest = download_pdf(_PDF_URL)
    assert data == body
    assert path.exists() and path.read_bytes() == body
    import hashlib

    assert digest == hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Boundary: parse timeout / isolation (pdf_parser)
# ---------------------------------------------------------------------------


def test_extract_happy_path_in_process() -> None:
    parsed = _extract(_make_text_pdf("RegWatch hardening test"))
    assert isinstance(parsed, ParsedPdf)
    assert len(parsed.pages) == 1
    assert "RegWatch hardening test" in parsed.text
    assert parsed.engine in {"pdfplumber", "pypdf"}


def test_extract_raises_on_garbage() -> None:
    with pytest.raises(PdfParseError):
        _extract(b"this is not a pdf at all")


def test_parse_pdf_in_process_when_timeout_disabled() -> None:
    parsed = parse_pdf(_make_text_pdf("inline"), timeout_s=0)
    assert "inline" in parsed.text


def test_parse_pdf_happy_path_through_subprocess() -> None:
    # Real spawn child round-trips the ParsedPdf back unchanged, including the
    # per-page structure (INV: pages[] must stay 1:1 across the pickle boundary,
    # because every citation carries a page number).
    parsed = parse_pdf(_make_text_pdf("subprocess works"), timeout_s=30)
    assert "subprocess works" in parsed.text
    assert len(parsed.pages) == 1
    assert parsed.engine in {"pdfplumber", "pypdf"}


def test_parse_pdf_propagates_child_failure_as_parse_error() -> None:
    # A non-parseable body must surface as PdfParseError (not a timeout) and
    # return promptly — the child reports its failure rather than hanging.
    with pytest.raises(PdfParseError) as excinfo:
        parse_pdf(b"%PDF-but-not-really-a-pdf", timeout_s=10)
    assert not isinstance(excinfo.value, PdfParseTimeoutError)


def test_run_with_timeout_fails_fast_when_child_dies_without_result() -> None:
    # A child that crashes WITHOUT delivering a result (native segfault / OS
    # OOM-kill — the exact threat the subprocess bounds) must surface as
    # PdfParseError, NOT be mislabeled as a timeout, and must return promptly
    # instead of waiting out the whole budget. os._exit bypasses _child_main's
    # put() to simulate a child that dies before delivering.
    start = time.monotonic()
    with pytest.raises(PdfParseError) as excinfo:
        _run_with_timeout(os._exit, (139,), timeout_s=10)
    elapsed = time.monotonic() - start
    assert not isinstance(excinfo.value, PdfParseTimeoutError)
    assert elapsed < 3.0  # failed fast — did not wait the 10s budget


def test_run_with_timeout_returns_result() -> None:
    # Success path of the machinery, with a picklable builtin target.
    assert _run_with_timeout(abs, (-7,), timeout_s=5) == 7


def test_run_with_timeout_kills_on_timeout() -> None:
    start = time.monotonic()
    with pytest.raises(PdfParseTimeoutError):
        _run_with_timeout(time.sleep, (30.0,), timeout_s=0.4)
    elapsed = time.monotonic() - start
    # Must not have waited for the full 30s sleep — the child is killed.
    assert elapsed < 6.0
