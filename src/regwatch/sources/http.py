"""Bounded HTTP transport for authoritative FDA artifacts."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from urllib.parse import urljoin, urlsplit

import httpx
from config.settings import get_settings

from regwatch.sources.policy import FdaSourceFamily, normalize_authoritative_url

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_PACE_LOCK = Lock()
_LAST_REQUEST_BY_HOST: dict[str, float] = {}


class SourceTooLargeError(RuntimeError):
    """An FDA response exceeded its source-specific byte budget."""


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
    requested = normalize_authoritative_url(url, family)
    last_error: Exception | None = None
    for attempt in range(attempts):
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
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            raise SourceTooLargeError(
                                f"FDA response exceeded {max_bytes} bytes while streaming"
                            )
                        chunks.append(chunk)
                    final_url = normalize_authoritative_url(str(response.url), family)
                    return final_url, b"".join(chunks), response.headers
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
    """Globally pace request starts per FDA host across worker threads."""

    if min_interval_s <= 0:
        return
    host = (urlsplit(url).hostname or "").lower()
    with _PACE_LOCK:
        now = time.monotonic()
        remaining = min_interval_s - (now - _LAST_REQUEST_BY_HOST.get(host, 0.0))
        if remaining > 0:
            time.sleep(remaining)
        _LAST_REQUEST_BY_HOST[host] = time.monotonic()
