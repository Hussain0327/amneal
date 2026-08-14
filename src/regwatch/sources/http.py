"""Bounded HTTP transport for authoritative FDA artifacts."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx
from config.settings import get_settings

from regwatch.sources.policy import FdaSourceFamily, normalize_authoritative_url

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_PACE_LOCK = Lock()
_LAST_REQUEST_BY_HOST: dict[str, float] = {}


class SourceTooLargeError(RuntimeError):
    """An FDA response exceeded its source-specific byte budget."""


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    final_url: str
    headers: httpx.Headers
    byte_size: int
    sha256: str


def build_fda_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_s,
        follow_redirects=False,
    )


@contextmanager
def owned_fda_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with build_fda_client() as built:
        yield built


def get_authoritative_bytes(
    client: httpx.Client,
    url: str,
    family: FdaSourceFamily,
    *,
    max_bytes: int,
    attempts: int = 3,
    min_interval_s: float = 0.0,
) -> tuple[str, bytes, httpx.Headers]:
    """Fetch one FDA artifact with retry, redirect, and size enforcement.

    Returns ``(canonical_final_url, body, headers)``.  Every redirect in the
    response chain is revalidated against the same source family so an FDA
    endpoint cannot redirect the corpus worker outside its authority boundary.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    if min_interval_s < 0:
        raise ValueError("min_interval_s must be non-negative")
    sink = io.BytesIO()
    final_url, headers, _, _ = _stream_authoritative_to(
        client,
        url,
        family,
        sink=sink,
        max_bytes=max_bytes,
        attempts=attempts,
        min_interval_s=min_interval_s,
    )
    return final_url, sink.getvalue(), headers


@contextmanager
def download_authoritative_file(
    client: httpx.Client,
    url: str,
    family: FdaSourceFamily,
    *,
    max_bytes: int,
    directory: Path | None = None,
    attempts: int = 3,
    min_interval_s: float = 0.0,
) -> Iterator[DownloadedFile]:
    """Stream one bounded FDA response into a temporary file and always unlink it."""

    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="regwatch-fda-", suffix=".part", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(fd, "w+b") as sink:
            final_url, headers, byte_size, digest = _stream_authoritative_to(
                client,
                url,
                family,
                sink=sink,
                max_bytes=max_bytes,
                attempts=attempts,
                min_interval_s=min_interval_s,
            )
            sink.flush()
            os.fsync(sink.fileno())
        yield DownloadedFile(
            path=path,
            final_url=final_url,
            headers=headers,
            byte_size=byte_size,
            sha256=digest,
        )
    finally:
        path.unlink(missing_ok=True)


def _stream_authoritative_to(
    client: httpx.Client,
    url: str,
    family: FdaSourceFamily,
    *,
    sink: BinaryIO,
    max_bytes: int,
    attempts: int,
    min_interval_s: float,
) -> tuple[str, httpx.Headers, int, str]:
    """Validated/retried FDA transport shared by memory and file consumers."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    if min_interval_s < 0:
        raise ValueError("min_interval_s must be non-negative")

    requested = normalize_authoritative_url(url, family)
    last_error: Exception | None = None
    for attempt in range(attempts):
        sink.seek(0)
        sink.truncate(0)
        digest = hashlib.sha256()
        try:
            current = requested
            retry_delay: float | None = None
            for redirect_count in range(6):
                _pace_request(current, min_interval_s)
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("authoritative FDA redirect omitted Location")
                        if redirect_count == 5:
                            raise httpx.TooManyRedirects(
                                "authoritative FDA request exceeded 5 redirects",
                                request=response.request,
                            )
                        # Validate the target BEFORE making the next request.
                        current = normalize_authoritative_url(
                            urljoin(str(response.url), location), family
                        )
                        continue
                    if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                        retry_after = response.headers.get("retry-after", "").strip()
                        retry_delay = (
                            float(retry_after) if retry_after.isdigit() else 0.25 * (2**attempt)
                        )
                        break
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise SourceTooLargeError(
                            f"FDA response declares {declared} bytes; limit is {max_bytes}"
                        )
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            raise SourceTooLargeError(
                                f"FDA response exceeded {max_bytes} bytes while streaming"
                            )
                        sink.write(chunk)
                        digest.update(chunk)
                    final_url = normalize_authoritative_url(str(response.url), family)
                    return (
                        final_url,
                        httpx.Headers(response.headers),
                        received,
                        digest.hexdigest(),
                    )
            if retry_delay is not None:
                time.sleep(min(retry_delay, 5.0))
                continue
            raise RuntimeError("authoritative FDA redirect loop did not terminate")
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if attempt + 1 >= attempts or (status is not None and status not in _RETRYABLE_STATUS):
                raise
            time.sleep(0.25 * (2**attempt))
    if last_error is None:  # defensive: attempts is validated by the loop contract
        raise RuntimeError("authoritative FDA request did not run")
    raise last_error


def _pace_request(url: str, min_interval_s: float) -> None:
    """Pace request starts per FDA host: host-global when configured.

    The in-process branch is complete only while ONE process crawls. The lock
    and the last-start table are module state, so N concurrent worker processes
    (Dagster max_concurrent_runs > 1) each pace themselves independently and
    multiply pressure on FDA by N -- the politeness interval silently stops
    being a budget. When ``crawl_pace_dir`` is set, every process serializes
    request starts through one flock-guarded timestamp file per host instead,
    making the interval a true host-wide budget regardless of process count.
    """

    if min_interval_s <= 0:
        return
    host = (urlsplit(url).hostname or "").lower()
    pace_dir = get_settings().crawl_pace_dir
    if pace_dir is not None:
        _pace_request_host_global(host, min_interval_s, pace_dir)
        return
    with _PACE_LOCK:
        now = time.monotonic()
        remaining = min_interval_s - (now - _LAST_REQUEST_BY_HOST.get(host, 0.0))
        if remaining > 0:
            time.sleep(remaining)
        _LAST_REQUEST_BY_HOST[host] = time.monotonic()


def _pace_request_host_global(host: str, min_interval_s: float, pace_dir: Path) -> None:
    """Serialize request starts across PROCESSES via one flock'd file per host.

    The sleep happens WHILE HOLDING the lock, deliberately: the contract is
    "request starts are at least min_interval_s apart host-wide", so the next
    contender must queue behind the wait, not race it. Wall-clock time (not
    monotonic) because monotonic clocks are not comparable across processes;
    the sleep is capped at min_interval_s so a backwards clock step can delay
    one request by at most one interval, never wedge the crawl. flock is
    POSIX-only, matching everywhere this crawler runs (Linux worker image,
    macOS dev); it also serializes threads within one process, because each
    call opens its own file description.
    """
    import fcntl

    pace_dir.mkdir(parents=True, exist_ok=True)
    path = pace_dir / f"pace-{host or 'unknown-host'}"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64)
        try:
            previous = float(raw.decode("ascii"))
        except ValueError:
            previous = 0.0
        remaining = min_interval_s - (time.time() - previous)
        if remaining > 0:
            time.sleep(min(remaining, min_interval_s))
        stamp = f"{time.time():.6f}".encode("ascii")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, stamp)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
