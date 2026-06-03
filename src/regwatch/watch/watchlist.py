"""Watchlist construction (INV-5: verified sources only).

We build the product watchlist from THREE allowed sources, in this order:

  1. `drugsfda`     — openFDA Drugs@FDA `drug/drugsfda.json`, filtered to
                       applications where the sponsor matches the company.
  2. `anda_letter`  — explicit user-uploaded ANDA approval letters
                       (one row per letter; the user is asserting the source).
  3. `manual`       — explicit user override.

We NEVER populate from the LLM. We NEVER make up product status. If a
product appears with no verifiable source, it is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from config.settings import get_settings
from sqlmodel import select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from regwatch.common.logging import get_logger
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.store.db import session_scope
from regwatch.store.models import Product

log = get_logger(__name__)

DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
ALLOWED_SOURCES = {"drugsfda", "anda_letter", "manual"}
# Trust hierarchy: the most trustworthy source wins on update.
# manual (user override) > anda_letter (user-verified letter) > drugsfda (re-import).
_SOURCE_RANK = {"manual": 3, "anda_letter": 2, "drugsfda": 1}


@dataclass
class WatchlistEntry:
    active_ingredient: str
    normalized_name: str
    dosage_form: str | None
    route: str | None
    rld_name: str | None
    rld_application_number: str | None
    company_status: str | None
    source: str
    source_url: str | None

    def __post_init__(self) -> None:
        if self.source not in ALLOWED_SOURCES:
            raise ValueError(
                f"INV-5 violation: source must be in {ALLOWED_SOURCES}, got {self.source!r}"
            )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _fetch_page(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = client.get(url, params=params)
    if resp.status_code == 429:
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json()


def _drugsfda_query(applicant: str) -> str:
    """openFDA query string — applications where sponsor matches `applicant`."""
    # openFDA `search=` syntax uses Lucene-ish. Quote multi-word applicant names.
    quoted = f'"{applicant}"' if " " in applicant else applicant
    return f"sponsor_name:{quoted}"


def fetch_drugsfda_for_company(
    aliases: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    page_limit: int = 100,
    max_pages: int = 20,
) -> list[WatchlistEntry]:
    """Query openFDA Drugs@FDA for applications matching each applicant alias.

    Returns one WatchlistEntry per (active_ingredient, dosage_form, route, appl_no).

    Aliases default to Drugs@FDA-discovered variants (see
    `regwatch.watch.aliases.get_aliases`). The hardcoded env list is a
    fallback only.
    """
    from regwatch.watch.aliases import get_aliases

    aliases = aliases or get_aliases()
    s = get_settings()
    if not aliases:
        log.warning("no_applicant_aliases")
        return []

    owned = False
    if client is None:
        client = httpx.Client(
            timeout=s.http_timeout_s,
            headers={"User-Agent": s.user_agent},
        )
        owned = True

    out: dict[tuple[str, str | None, str | None, str], WatchlistEntry] = {}
    try:
        for alias in aliases:
            params: dict[str, Any] = {
                "search": _drugsfda_query(alias),
                "limit": page_limit,
            }
            if s.openfda_api_key:
                params["api_key"] = s.openfda_api_key
            for page in range(max_pages):
                params["skip"] = page * page_limit
                try:
                    payload = _fetch_page(client, DRUGSFDA_URL, params)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        break  # openFDA returns 404 when skip > result count
                    raise
                results = payload.get("results") or []
                if not results:
                    break
                for app in results:
                    appl_no = app.get("application_number") or ""
                    for prod in app.get("products") or []:
                        ai_list = prod.get("active_ingredients") or []
                        ai_raw = "; ".join(
                            (a.get("name") or "").strip() for a in ai_list if a.get("name")
                        )
                        if not ai_raw:
                            continue
                        df = prod.get("dosage_form")
                        route = prod.get("route")
                        rld_name = prod.get("brand_name") or None
                        status = _status_from_marketing_status(prod)
                        key = (canonical_name(ai_raw), df, route, appl_no)
                        if key in out:
                            continue
                        out[key] = WatchlistEntry(
                            active_ingredient=ai_raw,
                            normalized_name=canonical_name(ai_raw),
                            dosage_form=df,
                            route=route,
                            rld_name=rld_name,
                            rld_application_number=appl_no,
                            company_status=status,
                            source="drugsfda",
                            source_url=DRUGSFDA_URL,
                        )
                if len(results) < page_limit:
                    break
    finally:
        if owned:
            client.close()
    log.info("drugsfda_fetched", aliases=aliases, count=len(out))
    return list(out.values())


def _status_from_marketing_status(prod: dict[str, Any]) -> str | None:
    """Map openFDA marketing_status fields to our condensed status."""
    statuses = []
    for ms in prod.get("marketing_status") or []:
        text = (ms or "").lower()
        if "prescription" in text:
            statuses.append("approved")
        elif "discontinued" in text:
            statuses.append("discontinued")
        elif "tentative" in text:
            statuses.append("tentative")
    if not statuses:
        return None
    if "approved" in statuses:
        return "approved"
    return statuses[0]


def upsert_entries(entries: list[WatchlistEntry]) -> int:
    """Upsert WatchlistEntries into the `product` table. Returns rows added."""
    added = 0
    with session_scope() as s:
        for e in entries:
            if e.source not in ALLOWED_SOURCES:
                continue  # INV-5
            stmt = (
                select(Product)
                .where(Product.normalized_name == e.normalized_name)
                .where(Product.dosage_form == e.dosage_form)
                .where(Product.route == e.route)
                .where(Product.rld_application_number == e.rld_application_number)
            )
            existing = list(s.scalars(stmt))
            if existing:
                row = existing[0]
                row.company_status = e.company_status or row.company_status
                row.rld_name = e.rld_name or row.rld_name
                # Keep the higher-trust source (INV-5 set is preserved).
                # Equal rank takes the incoming value.
                if _SOURCE_RANK.get(e.source, 0) >= _SOURCE_RANK.get(row.source, 0):
                    row.source = e.source
                row.source_url = e.source_url or row.source_url
                row.on_watchlist = True
                s.add(row)
            else:
                s.add(
                    Product(
                        active_ingredient=e.active_ingredient,
                        normalized_name=e.normalized_name,
                        dosage_form=e.dosage_form,
                        route=e.route,
                        rld_name=e.rld_name,
                        rld_application_number=e.rld_application_number,
                        company_status=e.company_status,
                        source=e.source,
                        source_url=e.source_url,
                        on_watchlist=True,
                    )
                )
                added += 1
    return added


def add_manual_product(
    *,
    active_ingredient: str,
    dosage_form: str | None,
    route: str | None,
    rld_name: str | None,
    rld_application_number: str | None,
    company_status: str | None,
    source: str,
    source_url: str | None,
) -> int:
    """Direct manual/anda_letter insertion. INV-5 enforced at the entry level."""
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"INV-5 violation: source must be one of {ALLOWED_SOURCES}")
    entry = WatchlistEntry(
        active_ingredient=active_ingredient,
        normalized_name=canonical_name(active_ingredient),
        dosage_form=dosage_form,
        route=route,
        rld_name=rld_name,
        rld_application_number=rld_application_number,
        company_status=company_status,
        source=source,
        source_url=source_url,
    )
    return upsert_entries([entry])


def list_watchlist() -> list[dict[str, Any]]:
    """Return the current on-watchlist products, projected to plain dicts."""
    with session_scope() as s:
        rows = list(s.scalars(select(Product).where(Product.on_watchlist == True)))  # noqa: E712
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": r.id,
                    "active_ingredient": r.active_ingredient,
                    "normalized_name": r.normalized_name,
                    "stripped_name": stripped_name(r.active_ingredient),
                    "dosage_form": r.dosage_form,
                    "route": r.route,
                    "rld_name": r.rld_name,
                    "rld_application_number": r.rld_application_number,
                    "company_status": r.company_status,
                    "source": r.source,
                    "source_url": r.source_url,
                }
            )
        return out
