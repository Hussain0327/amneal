"""Structured Orange Book handler.

The official Orange Book data file is a ZIP containing tilde-delimited ASCII
files. This handler reads three of them (all from the SAME cached download):

- ``products.txt``  — TE code, RLD, RS, approval date, applicant, dosage
  form/route, application/product numbers;
- ``patent.txt``    — patent number, expiry, substance/product flags, use
  code, delist flag, submission date;
- ``exclusivity.txt`` — exclusivity code and date.

Patent and exclusivity rows are surfaced RAW: paragraph classification and
eligibility are regulatory judgment and never happen here (INV-3).
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from config.settings import get_settings

from regwatch.common.text_normalize import canonical_name
from regwatch.sources._utils import clean_application_number, clean_text, owned_client
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

ORANGE_BOOK_ZIP_URL = "https://www.fda.gov/media/76860/download"
ORANGE_BOOK_SEARCH_URL = "https://www.accessdata.fda.gov/scripts/cder/ob/index.cfm"

PRODUCTS_MEMBER = "products.txt"
PATENT_MEMBER = "patent.txt"
EXCLUSIVITY_MEMBER = "exclusivity.txt"
_ZIP_MEMBERS = (PRODUCTS_MEMBER, PATENT_MEMBER, EXCLUSIVITY_MEMBER)

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

# Header names verified against the live ZIP (May 2026 snapshot) — do not guess.
PATENT_COLUMNS = {
    "appl_type": "Appl_Type",
    "appl_no": "Appl_No",
    "product_no": "Product_No",
    "patent_no": "Patent_No",
    "patent_expire_date": "Patent_Expire_Date_Text",
    "drug_substance_flag": "Drug_Substance_Flag",
    "drug_product_flag": "Drug_Product_Flag",
    "patent_use_code": "Patent_Use_Code",
    "delist_flag": "Delist_Flag",
    "submission_date": "Submission_Date",
}

EXCLUSIVITY_COLUMNS = {
    "appl_type": "Appl_Type",
    "appl_no": "Appl_No",
    "product_no": "Product_No",
    "exclusivity_code": "Exclusivity_Code",
    "exclusivity_date": "Exclusivity_Date",
}

# Orange Book Appl_Type letters by application-number prefix.
_OB_TYPE_BY_PREFIX = {"NDA": "N", "ANDA": "A", "BLA": "B"}


@dataclass(frozen=True)
class OrangeBookRows:
    """Raw Orange Book rows for one application + the ZIP snapshot timestamp.

    ``fetched_at`` is the auditable wall-clock time the underlying ZIP was
    downloaded (source freshness/provenance, INV-5). Rows are surfaced as-is —
    callers never receive a classification, only the file's own columns.
    """

    rows: list[dict[str, str]]
    fetched_at: datetime


@dataclass(frozen=True)
class _ZipCache:
    """Cached Orange Book file texts plus the wall-clock fetch time.

    ``monotonic_at`` drives TTL expiry (immune to clock changes); ``fetched_at``
    is the auditable wall-clock timestamp of the underlying download, surfaced
    on every :class:`OrangeBookRows` as source freshness (INV-5).
    """

    files: Mapping[str, str]
    fetched_at: datetime
    monotonic_at: float


# Module-level in-process cache. Shared across handler instances so repeated
# queries within the TTL reuse a single download/unzip/parse. Not thread-safe;
# a benign re-fetch on a race is acceptable for this in-process cache.
_ZIP_CACHE: _ZipCache | None = None


def reset_products_cache() -> None:
    """Clear the cached Orange Book ZIP texts (for deterministic tests)."""
    global _ZIP_CACHE
    _ZIP_CACHE = None


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


def product_rows(
    application_number: str,
    *,
    client: httpx.Client | None = None,
) -> OrangeBookRows:
    """Raw ``products.txt`` rows for one application (whitepaper Section 1/2)."""
    return _rows_for_application(PRODUCTS_MEMBER, PRODUCT_COLUMNS, application_number, client)


def patent_rows(
    application_number: str,
    *,
    client: httpx.Client | None = None,
) -> OrangeBookRows:
    """Raw ``patent.txt`` rows for one application.

    Raw rows only — paragraph classification is analyst judgment (INV-3).
    """
    return _rows_for_application(PATENT_MEMBER, PATENT_COLUMNS, application_number, client)


def exclusivity_rows(
    application_number: str,
    *,
    client: httpx.Client | None = None,
) -> OrangeBookRows:
    """Raw ``exclusivity.txt`` rows for one application.

    Raw rows only — eligibility determinations never happen here (INV-3).
    """
    return _rows_for_application(
        EXCLUSIVITY_MEMBER, EXCLUSIVITY_COLUMNS, application_number, client
    )


def parse_products_text(text: str) -> list[dict[str, str]]:
    return _parse_tilde_text(text, PRODUCT_COLUMNS)


def parse_patent_text(text: str) -> list[dict[str, str]]:
    return _parse_tilde_text(text, PATENT_COLUMNS)


def parse_exclusivity_text(text: str) -> list[dict[str, str]]:
    return _parse_tilde_text(text, EXCLUSIVITY_COLUMNS)


def _parse_tilde_text(text: str, columns: Mapping[str, str]) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="~")
    out: list[dict[str, str]] = []
    for row in reader:
        normalized = {
            key: clean_text(row.get(header))
            for key, header in columns.items()
            if row.get(header) is not None
        }
        if normalized:
            out.append(normalized)
    return out


def _rows_for_application(
    member: str,
    columns: Mapping[str, str],
    application_number: str,
    client: httpx.Client | None,
) -> OrangeBookRows:
    appl_type, appl_no = _split_application_number(application_number)
    cache = _cached_zip(client)
    rows = [
        row
        for row in _parse_tilde_text(cache.files[member], columns)
        if row.get("appl_no") == appl_no
        and (appl_type is None or row.get("appl_type") == appl_type)
    ]
    return OrangeBookRows(rows=rows, fetched_at=cache.fetched_at)


def _split_application_number(value: str) -> tuple[str | None, str]:
    """Split an application number into (Orange Book Appl_Type letter, 6 digits).

    The type letter is ``None`` when the caller supplied bare digits; matching
    then keys on the number alone. An unparseable value raises instead of
    silently matching nothing — "no rows" must mean "queried and absent".
    """
    cleaned = clean_application_number(value)
    if cleaned is None:
        raise ValueError(f"unparseable application number: {value!r}")
    for prefix, letter in _OB_TYPE_BY_PREFIX.items():
        if cleaned.startswith(prefix):
            return letter, cleaned.removeprefix(prefix)
    return None, cleaned


def _cached_products_text(client: httpx.Client | None) -> str:
    return _cached_zip(client).files[PRODUCTS_MEMBER]


def _cached_zip(client: httpx.Client | None) -> _ZipCache:
    """Return the Orange Book file texts, reusing a fresh in-process cache.

    Cache-aside: on a hit within the TTL, return the cached texts with NO
    network call. On a miss (cold or expired), fetch the ZIP once and extract
    products/patent/exclusivity together so the three row APIs share one
    download and one auditable ``fetched_at``.
    """
    global _ZIP_CACHE
    ttl = get_settings().orange_book_cache_ttl_s
    cached = _ZIP_CACHE
    if cached is not None and ttl > 0 and (time.monotonic() - cached.monotonic_at) < ttl:
        return cached
    files = _fetch_zip_files(client)
    fresh = _ZipCache(
        files=files,
        fetched_at=datetime.now(UTC),
        monotonic_at=time.monotonic(),
    )
    _ZIP_CACHE = fresh
    return fresh


def _fetch_zip_files(client: httpx.Client | None) -> dict[str, str]:
    s = get_settings()
    with owned_client(
        client,
        lambda: httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent}),
    ) as active_client:
        resp = active_client.get(ORANGE_BOOK_ZIP_URL)
        resp.raise_for_status()
        return _file_texts_from_zip(resp.content)


def _file_texts_from_zip(content: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            for member in _ZIP_MEMBERS:
                if name.lower().endswith(member):
                    out[member] = zf.read(name).decode("latin-1")
    missing = [member for member in _ZIP_MEMBERS if member not in out]
    if missing:
        raise RuntimeError(f"Orange Book ZIP is missing expected files: {', '.join(missing)}")
    return out


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
