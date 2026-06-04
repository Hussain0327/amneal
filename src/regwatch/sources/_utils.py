"""Helpers shared by FDA source handlers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import httpx
from config.settings import get_settings

APPLICATION_PREFIXES = ("NDA", "ANDA", "BLA")


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def clean_application_number(value: str | None) -> str | None:
    if not value:
        return None
    raw = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    for prefix in APPLICATION_PREFIXES:
        if raw.startswith(prefix):
            digits = re.sub(r"\D", "", raw.removeprefix(prefix))
            return f"{prefix}{digits.zfill(6)}" if digits else raw
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(6) if digits else None


def application_number_candidates(value: str | None) -> list[str]:
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
    owned = client is None
    active_client = client or get_openfda_client()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for search in searches:
            if len(out) >= limit:
                break
            resp = active_client.get(endpoint, params=openfda_params(search, limit))
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            for row in resp.json().get("results") or []:
                key = repr(row)
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
                if len(out) >= limit:
                    break
    finally:
        if owned:
            active_client.close()
    return out
