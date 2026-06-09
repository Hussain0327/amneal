"""Structured Orange Book handler.

The official Orange Book data file is a ZIP containing tilde-delimited ASCII
files. This first handler reads Products.txt, which includes TE code, RLD, RS,
approval date, applicant, dosage form/route, and application/product numbers.
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx
from config.settings import get_settings

from regwatch.common.text_normalize import canonical_name
from regwatch.sources._utils import clean_application_number, clean_text, owned_client
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

ORANGE_BOOK_ZIP_URL = "https://www.fda.gov/media/76860/download"
ORANGE_BOOK_SEARCH_URL = "https://www.accessdata.fda.gov/scripts/cder/ob/index.cfm"

PRODUCT_COLUMNS = {
    "ingredient": "Ingredient",
    "dosage_form_route": "DF;Route",
    "trade_name": "Trade_Name",
    "applicant": "Applicant",
    "strength": "Strength",
    "appl_type": "Appl_Type",
    "appl_no": "Appl_No",
    "product_no": "Product_No",
    "te_code": "TE_Code",
    "approval_date": "Approval_Date",
    "rld": "RLD",
    "rs": "RS",
    "type": "Type",
    "applicant_full_name": "Applicant_Full_Name",
}


@dataclass(frozen=True)
class _ProductsCache:
    """Cached Orange Book products text plus the wall-clock fetch time.

    ``monotonic_at`` drives TTL expiry (immune to clock changes); ``fetched_at``
    is the auditable wall-clock timestamp of the underlying download. It is
    stored but not yet read: the Gate-2 OB cache will surface ``fetched_at`` as
    source freshness/provenance (INV-5), so it is intentional forward-looking
    state, not dead state.
    """

    text: str
    fetched_at: float
    monotonic_at: float


# Module-level in-process cache. Shared across handler instances so repeated
# queries within the TTL reuse a single download/unzip/parse. Not thread-safe;
# a benign re-fetch on a race is acceptable for this in-process cache.
_PRODUCTS_CACHE: _ProductsCache | None = None


def reset_products_cache() -> None:
    """Clear the cached Orange Book products text (for deterministic tests)."""
    global _PRODUCTS_CACHE
    _PRODUCTS_CACHE = None


class OrangeBookHandler:
    source = SourceKind.ORANGE_BOOK

    def __init__(self, *, products_text: str | None = None) -> None:
        self._products_text = products_text

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        rows = parse_products_text(self._products_text or _cached_products_text(client))
        records: list[SourceRecord] = []
        app_no = _orange_book_app_no(query.application_number)
        ingredient = canonical_name(query.active_ingredient or "")
        brand = (query.brand_name or "").lower().strip()
        for row in rows:
            if app_no and row.get("appl_no") != app_no:
                continue
            if ingredient and ingredient not in canonical_name(row.get("ingredient") or ""):
                continue
            if brand and brand not in (row.get("trade_name") or "").lower():
                continue
            if (
                query.dosage_form
                and query.dosage_form.lower() not in (row.get("dosage_form_route") or "").lower()
            ):
                continue
            records.append(_record(row))
            if len(records) >= query.limit:
                break
        return records


def parse_products_text(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="~")
    out: list[dict[str, str]] = []
    for row in reader:
        normalized = {
            key: clean_text(row.get(header))
            for key, header in PRODUCT_COLUMNS.items()
            if row.get(header) is not None
        }
        if normalized:
            out.append(normalized)
    return out


def _cached_products_text(client: httpx.Client | None) -> str:
    """Return Orange Book products text, reusing a fresh in-process cache.

    Cache-aside: on a hit within the TTL, return the cached text with NO network
    call. On a miss (cold or expired), fetch once and repopulate the cache with
    an auditable wall-clock ``fetched_at`` timestamp.
    """
    global _PRODUCTS_CACHE
    ttl = get_settings().orange_book_cache_ttl_s
    cached = _PRODUCTS_CACHE
    if cached is not None and ttl > 0 and (time.monotonic() - cached.monotonic_at) < ttl:
        return cached.text
    text = _fetch_products_text(client)
    _PRODUCTS_CACHE = _ProductsCache(
        text=text,
        fetched_at=time.time(),
        monotonic_at=time.monotonic(),
    )
    return text


def _fetch_products_text(client: httpx.Client | None) -> str:
    s = get_settings()
    with owned_client(
        client,
        lambda: httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent}),
    ) as active_client:
        resp = active_client.get(ORANGE_BOOK_ZIP_URL)
        resp.raise_for_status()
        return _products_text_from_zip(resp.content)


def _products_text_from_zip(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.lower().endswith("products.txt"):
                return zf.read(name).decode("latin-1")
    raise RuntimeError("Orange Book ZIP did not contain Products.txt")


def _record(row: dict[str, str]) -> SourceRecord:
    identifiers = {
        "application_number": row.get("appl_no", ""),
        "product_number": row.get("product_no", ""),
    }
    identifiers = {k: v for k, v in identifiers.items() if v}
    fields: dict[str, Any] = {
        "ingredient": row.get("ingredient"),
        "dosage_form_route": row.get("dosage_form_route"),
        "trade_name": row.get("trade_name"),
        "applicant": row.get("applicant"),
        "applicant_full_name": row.get("applicant_full_name"),
        "strength": row.get("strength"),
        "appl_type": row.get("appl_type"),
        "te_code": row.get("te_code"),
        "approval_date": row.get("approval_date"),
        "rld": row.get("rld"),
        "rs": row.get("rs"),
        "type": row.get("type"),
    }
    title = f"Orange Book: {row.get('trade_name') or row.get('ingredient') or 'record'}"
    return SourceRecord(
        source=SourceKind.ORANGE_BOOK,
        title=title,
        source_url=ORANGE_BOOK_SEARCH_URL,
        identifiers=identifiers,
        fields=fields,
        raw=row,
    )


def _orange_book_app_no(value: str | None) -> str | None:
    cleaned = clean_application_number(value)
    if cleaned is None:
        return None
    for prefix in ("NDA", "ANDA", "BLA"):
        if cleaned.startswith(prefix):
            return cleaned.removeprefix(prefix)
    return cleaned
