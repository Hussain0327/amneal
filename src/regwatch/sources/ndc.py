"""Structured NDC Directory handler backed by openFDA."""

from __future__ import annotations

from typing import Any

import httpx

from regwatch.sources._utils import (
    application_number_candidates,
    clean_ndc,
    fetch_openfda_results,
    first_str,
    quote_term,
)
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

NDC_ENDPOINT = "https://api.fda.gov/drug/ndc.json"
NDC_DOC_URL = "https://open.fda.gov/apis/drug/ndc/"


class NdcHandler:
    source = SourceKind.NDC

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        searches = _searches(query)
        if not searches:
            return []
        rows = fetch_openfda_results(NDC_ENDPOINT, searches, limit=query.limit, client=client)
        return [_record(row) for row in rows]


def _searches(query: SourceQuery) -> list[str]:
    searches: list[str] = []
    ndc = clean_ndc(query.ndc)
    if ndc:
        quoted = quote_term(ndc)
        searches.extend([f"product_ndc:{quoted}", f"packaging.package_ndc:{quoted}"])
    for app_no in application_number_candidates(query.application_number):
        searches.append(f"application_number:{quote_term(app_no)}")
    if query.active_ingredient:
        term = quote_term(query.active_ingredient)
        searches.extend([f"generic_name:{term}", f"active_ingredients.name:{term}"])
    if query.brand_name:
        searches.append(f"brand_name:{quote_term(query.brand_name)}")
    return searches


def _record(row: dict[str, Any]) -> SourceRecord:
    product_ndc = first_str(row, "product_ndc")
    application_number = first_str(row, "application_number")
    identifiers: dict[str, str] = {}
    if product_ndc:
        identifiers["product_ndc"] = product_ndc
    if application_number:
        identifiers["application_number"] = application_number
    fields: dict[str, Any] = {
        "brand_name": first_str(row, "brand_name"),
        "generic_name": first_str(row, "generic_name"),
        "labeler_name": first_str(row, "labeler_name"),
        "dosage_form": first_str(row, "dosage_form"),
        "route": row.get("route") or [],
        "marketing_category": first_str(row, "marketing_category"),
        "product_type": first_str(row, "product_type"),
        "active_ingredients": row.get("active_ingredients") or [],
        "packaging": row.get("packaging") or [],
    }
    title = f"NDC: {product_ndc or first_str(row, 'brand_name', 'generic_name') or 'record'}"
    return SourceRecord(
        source=SourceKind.NDC,
        title=title,
        source_url=NDC_DOC_URL,
        identifiers=identifiers,
        fields=fields,
        raw=row,
    )
