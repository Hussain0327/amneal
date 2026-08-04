"""Fetch-and-cache the official CRA Word template from private object storage.

Prod Fly machines have no persistent volume and the template is gitignored, so
every prod docx render used to hit the from-scratch FALLBACK_MARKER path. The
fix (design doc section 8): the template lives in a private bucket and
``WHITEPAPER_TEMPLATE_URL`` (a long-lived signed URL, set as a Fly secret) lets
the render path lazily fetch it on first use and cache it at
``whitepaper_template_path``.

The fetch is URL-generic -- it only needs a signed HTTPS URL, so re-homing the
object to a Databricks Volume needs no code change here, just a new URL. As of
2026-08-04 nothing is hosted: /health reports ``whitepaper_template: absent``
and the setting is unset in prod, so this module is dormant.

Failure discipline: ANY fetch problem (timeout, HTTP error, oversize body, not
a docx, disk error) logs a structured warning and returns None, so the caller
keeps today's LOUD fallback behavior (warning log + visible FALLBACK_MARKER in
the document) and the next render retries naturally. A broken storage bucket
must never turn a working fallback render into a 500.

Concurrency: a module-level threading.Lock serializes the fetch within one
process (two concurrent renders fetch once). Across processes (multiple Fly
machines) a double fetch is benign - both write the identical bytes via an
atomic os.replace - so no cross-process lock is taken.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from regwatch.common.logging import get_logger

log = get_logger(__name__)

# Explicit bound on the fetch: a stalled or runaway storage endpoint must not
# pin a render worker (house rule: every external call gets a timeout).
_FETCH_TIMEOUT_S = 10.0

# The real template is ~1 MB; 5 MB is a wide margin that still stops a runaway
# (or lying-Content-Length) body before it is fully buffered.
_MAX_TEMPLATE_BYTES = 5 * 1024 * 1024

# A .docx is a ZIP archive; its first four bytes are the local-file-header
# magic. Preferred over Content-Type because object storage commonly serves
# application/octet-stream for uploaded files.
_DOCX_MAGIC = b"PK\x03\x04"

_fetch_lock = threading.Lock()


class TemplateTooLargeError(RuntimeError):
    """The fetched template body exceeded the byte cap (DoS/OOM guard)."""


def _redacted(url: str) -> str:
    """Strip query/fragment before logging: the signed URL's token is a secret."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _safe_error(exc: Exception) -> str:
    """Loggable error detail that can never carry the signed URL's token.

    ``str()`` on httpx errors embeds the full request URL -- HTTPStatusError's
    message interpolates ``response.url`` verbatim, token included -- so only
    our own TemplateTooLargeError message is trusted; httpx errors log the
    class name plus the HTTP status when one exists.
    """
    if isinstance(exc, TemplateTooLargeError):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__}: HTTP {exc.response.status_code}"
    return type(exc).__name__


def _fetch_capped(url: str, client: httpx.Client | None) -> bytes:
    """GET `url` streamed, aborting once the body exceeds the byte cap.

    Streams so an oversized body is cut off before it is fully buffered. No
    retry loop here: ensure_template's caller retries naturally on the next
    render, and a cold-start render should not stack waits on a dead bucket.
    The response and any owned client are closed on every path.
    """
    owned = False
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(_FETCH_TIMEOUT_S), follow_redirects=True)
        owned = True
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_TEMPLATE_BYTES:
                raise TemplateTooLargeError(
                    f"template Content-Length {declared} exceeds cap {_MAX_TEMPLATE_BYTES} bytes"
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_TEMPLATE_BYTES:
                    raise TemplateTooLargeError(
                        f"template body exceeded cap {_MAX_TEMPLATE_BYTES} bytes"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    finally:
        if owned:
            client.close()


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` via same-directory tmp file + os.replace.

    Same-directory keeps the rename atomic (no cross-filesystem copy), so a
    concurrent render only ever sees the old state or the complete file -
    never a partial template that python-docx would choke on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def ensure_template(
    path: Path, url: str | None, *, client: httpx.Client | None = None
) -> Path | None:
    """Return `path` if the template is (or can be made) present, else None.

    - `path` already exists -> returned as-is, no HTTP call.
    - `url` unset -> None (today's behavior, from-scratch fallback).
    - Otherwise fetch, validate the ZIP magic, atomic-write, return `path`.
      ANY failure -> structured warning + None; the caller falls back loudly
      (FALLBACK_MARKER) and the next render retries.

    `client` is injectable for tests; a passed client is never closed here.
    """
    if path.exists():
        return path
    if not url:
        return None
    with _fetch_lock:
        # A concurrent render may have completed the fetch while we waited.
        if path.exists():
            return path
        try:
            data = _fetch_capped(url, client)
        except (httpx.HTTPError, TemplateTooLargeError) as exc:
            log.warning(
                "whitepaper_template_fetch_failed",
                url=_redacted(url),
                error=_safe_error(exc),
            )
            return None
        if not data.startswith(_DOCX_MAGIC):
            log.warning(
                "whitepaper_template_fetch_not_docx",
                url=_redacted(url),
                size=len(data),
            )
            return None
        try:
            _write_atomic(path, data)
        except OSError as exc:
            log.warning(
                "whitepaper_template_write_failed",
                template_path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        log.info(
            "whitepaper_template_fetched",
            template_path=str(path),
            size=len(data),
        )
        return path


def template_status(path: Path, url: str | None) -> str:
    """Health-check view of template availability (for /health wiring).

    "present"   - the file is on disk; renders use the real template.
    "fetchable" - absent but a fetch URL is configured; the first render
                  will fetch-and-cache (or fall back loudly if that fails).
    "absent"    - neither; every render uses the FALLBACK_MARKER document.
    """
    if path.exists():
        return "present"
    if url:
        return "fetchable"
    return "absent"
