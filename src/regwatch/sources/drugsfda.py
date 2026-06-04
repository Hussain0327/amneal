"""Structured Drugs@FDA handler backed by openFDA."""

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

DRUGSFDA_ENDPOINT = "https://api.fda.gov/drug/drugsfda.json"
DRUGSFDA_DOC_URL = "https://open.fda.gov/apis/drug/drugsfda/"


class DrugsFdaHandler:
    source = SourceKind.DRUGSFDA

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
            DRUGSFDA_ENDPOINT,
            searches,
            limit=query.limit,
            client=client,
        )
        return [_record(row) for row in rows]


def _searches(query: SourceQuery) -> list[str]:
    searches: list[str] = []
    for app_no in application_number_candidates(query.application_number):
        searches.append(f"application_number:{quote_term(app_no)}")
    if query.active_ingredient:
        term = quote_term(query.active_ingredient)
        searches.extend(
            [
                f"openfda.generic_name:{term}",
                f"products.active_ingredients.name:{term}",
            ]
        )
    if query.brand_name:
        term = quote_term(query.brand_name)
        searches.extend([f"openfda.brand_name:{term}", f"products.brand_name:{term}"])
    return searches


def _record(row: dict[str, Any]) -> SourceRecord:
    application_number = first_str(row, "application_number")
    products = _products(row)
    title = f"Drugs@FDA: {application_number or first_str(row, 'sponsor_name') or 'record'}"
    identifiers = {"application_number": application_number} if application_number else {}
    fields: dict[str, Any] = {
        "sponsor_name": first_str(row, "sponsor_name"),
        "submissions": row.get("submissions") or [],
        "products": products,
    }
    return SourceRecord(
        source=SourceKind.DRUGSFDA,
        title=title,
        source_url=DRUGSFDA_DOC_URL,
        identifiers=identifiers,
        fields=fields,
        raw=row,
    )


def _products(row: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for product in row.get("products") or []:
        active_ingredients = product.get("active_ingredients") or []
        out.append(
            {
                "brand_name": first_str(product, "brand_name"),
                "dosage_form": first_str(product, "dosage_form"),
                "route": first_str(product, "route"),
                "marketing_status": product.get("marketing_status") or [],
                "active_ingredients": [
                    {
                        "name": first_str(ai, "name"),
                        "strength": first_str(ai, "strength"),
                    }
                    for ai in active_ingredients
                ],
            }
        )
    return out
