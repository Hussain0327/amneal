"""Derive applicant-name aliases from Drugs@FDA (no guessing).

Real records carry sponsor-name variants like:
  "Amneal Pharmaceuticals LLC"
  "Amneal Pharmaceuticals of NY"
  "Amneal EU Limited"
  "Amneal Pharms Co India Pvt Ltd"
  "Amneal Pharms LLC"

These can't be enumerated by hand. We scan the official Drugs@FDA weekday data
snapshot for the company's root token (e.g. "Amneal") and collect all distinct
``SponsorName`` values that contain it. The result is cached as JSON so
subsequent watchlist builds reuse it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from config.settings import get_settings

from regwatch.common.logging import get_logger
from regwatch.sources.drugsfda import get_drugsfda_snapshot

log = get_logger(__name__)


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

    aliases: set[str] = set()
    root_upper = root.upper()
    snapshot = get_drugsfda_snapshot(client=client)
    for application in snapshot.applications:
        sponsor = (application.get("SponsorName") or "").strip()
        if root_upper in sponsor.upper():
            aliases.add(sponsor.upper())

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
