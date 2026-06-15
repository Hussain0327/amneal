"""Derive applicant-name aliases from Drugs@FDA (no guessing).

Real records carry sponsor-name variants like:
  "Amneal Pharmaceuticals LLC"
  "Amneal Pharmaceuticals of NY"
  "Amneal EU Limited"
  "Amneal Pharms Co India Pvt Ltd"
  "Amneal Pharms LLC"

These can't be enumerated by hand. We query `api.fda.gov/drug/drugsfda.json`
for the company's root token (e.g. "Amneal") and collect all distinct
`sponsor_name` values that contain it. The result is cached as JSON so
subsequent watchlist builds reuse it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from config.settings import get_settings
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from regwatch.common.logging import get_logger

log = get_logger(__name__)


DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _fetch(client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
    resp = client.get(DRUGSFDA_URL, params=params)
    if resp.status_code == 429:
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
    if resp.status_code == 404:
        return {"results": []}
    resp.raise_for_status()
    return resp.json()


def discover_applicant_aliases(
    root: str | None = None,
    *,
    client: httpx.Client | None = None,
    cache_path: Path | None = None,
    refresh: bool = False,
) -> list[str]:
    """Return all distinct `sponsor_name` variants in Drugs@FDA containing `root`.

    Args:
        root: company root token. Defaults to `COMPANY_NAME` from settings.
        cache_path: where to persist the result. Defaults to
            `data/processed/applicant_aliases.json`.
        refresh: bypass cache and re-query.
    """
    s = get_settings()
    root = (root or s.company_name).strip()
    if not root:
        return []
    cache = cache_path or (s.processed_dir / "applicant_aliases.json")
    cache.parent.mkdir(parents=True, exist_ok=True)

    if not refresh and cache.exists():
        try:
            existing = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("root") == root:
                return list(existing.get("aliases") or [])
        except json.JSONDecodeError:
            pass

    owned = False
    if client is None:
        client = httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent})
        owned = True

    aliases: set[str] = set()
    root_upper = root.upper()
    try:
        # openFDA `sponsor_name` is keyword-matched. To catch every variant
        # ("AMNEAL", "AMNEAL PHARMS", "AMNEAL PHARMACEUTICALS OF NY", ...) we
        # use an upper-cased trailing wildcard, which openFDA accepts.
        page_limit = 100
        for page in range(50):
            params: dict[str, Any] = {
                "search": f"sponsor_name:{root_upper}*",
                "limit": page_limit,
                "skip": page * page_limit,
            }
            if s.openfda_api_key:
                params["api_key"] = s.openfda_api_key
            payload = _fetch(client, params)
            results = payload.get("results") or []
            if not results:
                break
            for app in results:
                sponsor = (app.get("sponsor_name") or "").strip()
                if root_upper in sponsor.upper():
                    aliases.add(sponsor.upper())
            if len(results) < page_limit:
                break
    finally:
        if owned:
            client.close()

    ordered = sorted(aliases)
    cache.write_text(
        json.dumps({"root": root, "aliases": ordered}, indent=2),
        encoding="utf-8",
    )
    log.info("aliases_discovered", root=root, count=len(ordered))
    return ordered


def get_aliases() -> list[str]:
    """Return aliases for the active company.

    Priority: cached Drugs@FDA discovery → COMPANY_APPLICANT_ALIASES env →
    [COMPANY_NAME].
    """
    s = get_settings()
    cache = s.processed_dir / "applicant_aliases.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            cached = list(data.get("aliases") or [])
            # Only trust the cache when it was computed for the CURRENT company
            # root (mirrors discover_applicant_aliases' own root check), so a
            # changed COMPANY_NAME doesn't keep serving the prior company's
            # aliases until a manual refresh.
            if cached and data.get("root") == s.company_name.strip():
                return cached
        except json.JSONDecodeError:
            pass
    env_aliases = s.applicant_aliases
    if env_aliases:
        return env_aliases
    return [s.company_name.upper()]
