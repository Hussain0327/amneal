"""Structured Drug Shortages handler backed by openFDA."""

from __future__ import annotations

from typing import Any

import httpx

from regwatch.sources._utils import (
    application_number_candidates,
    fetch_openfda_results,
    first_str,
    quote_term,
)
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

SHORTAGES_ENDPOINT = "https://api.fda.gov/drug/shortages.json"
SHORTAGES_DOC_URL = "https://open.fda.gov/apis/drug/drugshortages/"


class ShortagesHandler:
    source = SourceKind.SHORTAGE

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        searches = _searches(query)
        if not searches:
            return []
        rows = fetch_openfda_results(
            SHORTAGES_ENDPOINT,
            searches,
            limit=query.limit,
            client=client,
        )
        records = [_record(row) for row in rows]
        if query.dosage_form:
            want = query.dosage_form.strip().lower()
            records = [r for r in records if want in (r.fields.get("dosage_form") or "").lower()]
        return records


def _searches(query: SourceQuery) -> list[str]:
    searches: list[str] = []
    for app_no in application_number_candidates(query.application_number):
        searches.append(f"openfda.application_number:{quote_term(app_no)}")
    if query.active_ingredient:
        term = quote_term(query.active_ingredient)
        searches.extend([f"generic_name:{term}", f"openfda.generic_name:{term}"])
    if query.brand_name:
        term = quote_term(query.brand_name)
        searches.extend([f"brand_name:{term}", f"openfda.brand_name:{term}"])
    # dosage_form is intentionally NOT a standalone search term: on openFDA it would
    # match every shortage of that form (a false-positive flood). It is applied as a
    # post-filter in `search()` instead, mirroring the orange_book/psg handlers.
    return searches


def _record(row: dict[str, Any]) -> SourceRecord:
    # first_str applies clean_text, keeping this identifier normalized like
    # every other handler's (the old local helper skipped normalization).
    application_number = first_str(row.get("openfda") or {}, "application_number")
    identifiers = {"application_number": application_number} if application_number else {}
    fields: dict[str, Any] = {
        "generic_name": first_str(row, "generic_name"),
        "brand_name": first_str(row, "brand_name"),
        "dosage_form": first_str(row, "dosage_form"),
        "status": first_str(row, "status", "shortage_status"),
        "availability": first_str(row, "availability"),
        "company_name": first_str(row, "company_name", "manufacturer_name"),
        "update_type": first_str(row, "update_type"),
        "therapeutic_category": first_str(row, "therapeutic_category"),
    }
    title_name = first_str(row, "brand_name", "generic_name") or "record"
    return SourceRecord(
        source=SourceKind.SHORTAGE,
        title=f"Drug Shortage: {title_name}",
        source_url=SHORTAGES_DOC_URL,
        identifiers=identifiers,
        fields=fields,
        raw=row,
    )
