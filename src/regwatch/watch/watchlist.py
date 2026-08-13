"""Watchlist construction (INV-5: verified sources only).

We build the product watchlist from THREE verified sources, in this order:

  1. `drugsfda`     — official Drugs@FDA weekday snapshot, filtered to
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
from sqlmodel import select

from regwatch.common.logging import get_logger
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.sources.drugsfda import DRUGSFDA_DOC_URL, get_drugsfda_snapshot
from regwatch.store.db import session_scope
from regwatch.store.models import Product

log = get_logger(__name__)

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


def fetch_drugsfda_for_company(
    aliases: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    page_limit: int = 100,
    max_pages: int = 20,
) -> list[WatchlistEntry]:
    """Read the official Drugs@FDA snapshot for matching applicant aliases.

    Returns one WatchlistEntry per (active_ingredient, dosage_form, route, appl_no).

    Aliases default to Drugs@FDA-discovered variants (see
    `regwatch.watch.aliases.get_aliases`). The hardcoded env list is a
    fallback only.

    Nothing calls this today. It is parked on purpose as the reuse foundation
    for the planned multi-source watchlist rebuild; deleting it (with
    `_fetch_page`, `_drugsfda_query` and `aliases.get_aliases`) needs a product
    decision that drugsfda auto-import is abandoned.
    """
    from regwatch.watch.aliases import get_aliases

    aliases = aliases or get_aliases()
    if not aliases:
        log.warning("no_applicant_aliases")
        return []
    # Retained keyword arguments keep callers source-compatible; the official
    # snapshot is local once downloaded and has no pagination contract.
    del page_limit, max_pages
    snapshot = get_drugsfda_snapshot(client=client)
    accepted = {alias.strip().upper() for alias in aliases if alias.strip()}
    out: dict[tuple[str, str | None, str | None, str], WatchlistEntry] = {}
    for application in snapshot.applications:
        sponsor = (application.get("SponsorName") or "").strip().upper()
        if sponsor not in accepted:
            continue
        bare = application.get("ApplNo") or ""
        appl_type = application.get("ApplType") or ""
        appl_no = f"{appl_type}{bare}" if appl_type else bare
        for product in snapshot.application_products(bare):
            ingredient = product.get("ActiveIngredient") or ""
            if not ingredient:
                continue
            dosage_form, _, route = (product.get("Form") or "").partition(";")
            key = (canonical_name(ingredient), dosage_form or None, route or None, appl_no)
            if key in out:
                continue
            statuses = snapshot.product_marketing_status.get(
                (bare, product.get("ProductNo") or ""), ()
            )
            out[key] = WatchlistEntry(
                active_ingredient=ingredient,
                normalized_name=canonical_name(ingredient),
                dosage_form=dosage_form or None,
                route=route or None,
                rld_name=product.get("DrugName") or None,
                rld_application_number=appl_no,
                company_status=_status_from_marketing_status(statuses),
                source="drugsfda",
                source_url=f"{DRUGSFDA_DOC_URL}?event=overview.process&ApplNo={bare}",
            )
    log.info("drugsfda_fetched", aliases=aliases, count=len(out))
    return list(out.values())


def _status_from_marketing_status(raw: object) -> str | None:
    """Map official Drugs@FDA marketing-status labels to our status."""
    if isinstance(raw, dict):
        raw = raw.get("marketing_status")
    statuses = []
    items = raw if isinstance(raw, list | tuple) else ([raw] if raw else [])
    for ms in items:
        text = str(ms or "").lower()
        if "prescription" in text:
            statuses.append("approved")
        elif "discontinued" in text:
            statuses.append("discontinued")
        elif "tentative" in text or text == "none":
            statuses.append("tentative")
    # Explicit precedence (NOT scan order): a single application can carry
    # several marketing_status values, so return the most decision-relevant one
    # deterministically. approved > discontinued > tentative.
    for status in ("approved", "discontinued", "tentative"):
        if status in statuses:
            return status
    return None


def _identity_attr(value: str | None) -> str | None:
    """Casefold + collapse whitespace for the upsert identity key.

    The matcher compares dosage_form/route case-insensitively (_norm_attr in
    matcher.py), so upsert equality must agree or cross-source case differences
    ("Tablet" vs FDA's "TABLET") create duplicate rows and duplicate alerts.
    None stays None: an unknown form/route matches only another unknown.
    """
    if value is None:
        return None
    return " ".join(value.strip().casefold().split())


def upsert_entries(entries: list[WatchlistEntry]) -> int:
    """Upsert WatchlistEntries into the `product` table. Returns rows added."""
    added = 0
    with session_scope() as s:
        for e in entries:
            if e.source not in ALLOWED_SOURCES:
                continue  # INV-5
            # Select on the exact columns only; form/route are matched in
            # Python below so 'Tablet' and 'TABLET' resolve to one row.
            stmt = (
                select(Product)
                .where(Product.normalized_name == e.normalized_name)
                .where(Product.rld_application_number == e.rld_application_number)
            )
            existing = [
                r
                for r in s.scalars(stmt)
                if _identity_attr(r.dosage_form) == _identity_attr(e.dosage_form)
                and _identity_attr(r.route) == _identity_attr(e.route)
            ]
            if existing:
                row = existing[0]
                # INV-5 trust gate covers the DATA fields too, not just the
                # source label: a lower-trust re-import overwriting a manual
                # override would silently revert user data while the row kept
                # its trusted label. Lower rank may only FILL empty fields.
                if _SOURCE_RANK.get(e.source, 0) >= _SOURCE_RANK.get(row.source, 0):
                    # Equal rank takes the incoming value.
                    row.company_status = e.company_status or row.company_status
                    row.rld_name = e.rld_name or row.rld_name
                    row.source = e.source
                    row.source_url = e.source_url or row.source_url
                else:
                    row.company_status = row.company_status or e.company_status
                    row.rld_name = row.rld_name or e.rld_name
                    row.source_url = row.source_url or e.source_url
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


def set_on_watchlist(product_id: int, on: bool) -> bool:
    """Flip a product's watchlist membership. Returns whether the row exists.

    SOFT by design: the row is kept (``on_watchlist=False``) rather than
    deleted, because durable alert rows reference ``product_id`` and a hard
    delete would orphan that history (INV-4: the feed must keep resolving to
    real products) -- and the row's INV-5 provenance survives for audit.
    Idempotent: re-applying the current state still returns True, so a
    double-unwatch is a no-op, not an error. NOTE: ``upsert_entries`` above
    re-sets ``on_watchlist=True`` when a re-import matches the same identity
    key, so unwatching a drugsfda row lasts until the next import refreshes it
    -- the import-refresh trust model, unchanged here.
    """
    with session_scope() as s:
        row = s.get(Product, product_id)
        if row is None:
            return False
        row.on_watchlist = on
        s.add(row)
        return True


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
