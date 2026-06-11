"""Helpers shared by FDA source handlers."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from config.settings import get_settings

APPLICATION_PREFIXES = ("NDA", "ANDA", "BLA")

# Single-letter application-type prefixes (the UI advertises "N020503").
_SINGLE_LETTER_TYPES = {"N": "NDA", "A": "ANDA", "B": "BLA"}

# Prefixed form after separator stripping: full or single-letter type, optional
# leading zeros, 1-6 digits. Equivalent to matching the raw input against
# ^(NDA|ANDA|BLA|[NAB])[\s#]*0*(\d{1,6})$ case-insensitively.
_PREFIXED_APP_NO_RE = re.compile(r"(NDA|ANDA|BLA|[NAB])0*(\d{1,6})")


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def clean_application_number(value: str | None) -> str | None:
    """Normalize an application number to ``NDA######`` or bare six digits.

    Accepts the long prefixes (NDA/ANDA/BLA) and the single-letter forms
    (N/A/B, mapped to NDA/ANDA/BLA) with any spacing or punctuation between
    prefix and digits ("NDA #022549", "N020503"). Bare digits stay bare —
    callers that need expansion use :func:`application_number_candidates`.
    """
    if not value:
        return None
    raw = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    match = _PREFIXED_APP_NO_RE.fullmatch(raw)
    if match:
        prefix = _SINGLE_LETTER_TYPES.get(match.group(1), match.group(1))
        return f"{prefix}{match.group(2).zfill(6)}"
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(6) if digits else None


def application_number_candidates(value: str | None) -> list[str]:
    """Prefixed candidates for a possibly-bare application number.

    A prefixed input — long or single-letter — yields exactly its own
    application; only genuinely bare digits expand to the NDA/ANDA/BLA triple
    (plus the bare form for sources that key on digits alone).
    """
    cleaned = clean_application_number(value)
    if not cleaned:
        return []
    if any(cleaned.startswith(prefix) for prefix in APPLICATION_PREFIXES):
        return [cleaned]
    return [f"{prefix}{cleaned}" for prefix in APPLICATION_PREFIXES] + [cleaned]


def clean_ndc(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9-]", "", value)
    return cleaned or None


def quote_term(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def first_str(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, list):
            value = next((v for v in value if v not in (None, "")), None)
        if value not in (None, ""):
            return clean_text(value)
    return None


def get_openfda_client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent})


@contextmanager
def owned_client(
    client: httpx.Client | None,
    factory: Callable[[], httpx.Client],
) -> Iterator[httpx.Client]:
    active_client = client or factory()
    try:
        yield active_client
    finally:
        if client is None:
            active_client.close()


def openfda_params(search: str, limit: int) -> dict[str, Any]:
    s = get_settings()
    params: dict[str, Any] = {"search": search, "limit": limit}
    if s.openfda_api_key:
        params["api_key"] = s.openfda_api_key
    return params


def fetch_openfda_results(
    endpoint: str,
    searches: Iterable[str],
    *,
    limit: int,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    with owned_client(client, get_openfda_client) as active_client:
        for search in searches:
            if len(out) >= limit:
                break
            resp = get_with_retry(active_client, endpoint, openfda_params(search, limit))
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            for row in resp.json().get("results") or []:
                key = json.dumps(row, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
                if len(out) >= limit:
                    break
    return out


def get_with_retry(
    client: httpx.Client,
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    attempts: int = 3,
) -> httpx.Response:
    """GET with exponential backoff on 429/5xx (polite-crawler house rule)."""
    for attempt in range(attempts):
        resp = client.get(endpoint, params=params)
        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        if attempt == attempts - 1:
            return resp
        time.sleep(0.5 * (2**attempt))
    return resp
