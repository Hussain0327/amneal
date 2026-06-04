"""Rules-first FDA source routing and handler execution."""

from __future__ import annotations

import re
from collections.abc import Iterable

import httpx

from regwatch.sources.drugsfda import DrugsFdaHandler
from regwatch.sources.ndc import NdcHandler
from regwatch.sources.orange_book import OrangeBookHandler
from regwatch.sources.psg import PsgHandler
from regwatch.sources.rems import RemsHandler
from regwatch.sources.shortages import ShortagesHandler
from regwatch.sources.types import SourceHandler, SourceKind, SourceQuery, SourceRecord

NDC_PATTERN = re.compile(r"\b\d{4,5}-\d{3,4}(?:-\d{1,2})?\b")
APP_PATTERN = re.compile(r"\b(?:NDA|ANDA|BLA)?\s*\d{5,6}\b", re.IGNORECASE)


def route_sources(
    query: SourceQuery,
    *,
    requested: Iterable[SourceKind] | None = None,
) -> list[SourceKind]:
    if requested:
        return _dedupe(requested)

    text = " ".join(
        value
        for value in (
            query.query_text,
            query.active_ingredient or "",
            query.brand_name or "",
            query.application_number or "",
            query.ndc or "",
        )
        if value
    ).lower()

    routed: list[SourceKind] = []
    if query.ndc or "ndc" in text or NDC_PATTERN.search(text):
        routed.append(SourceKind.NDC)
    if "shortage" in text or "availability" in text:
        routed.append(SourceKind.SHORTAGE)
    if "rems" in text or "risk evaluation" in text or "mitigation strategy" in text:
        routed.append(SourceKind.REMS)
    if (
        "orange book" in text
        or "therapeutic equivalence" in text
        or "te code" in text
        or "rld" in text
        or "reference standard" in text
        or "rs" in text.split()
    ):
        routed.append(SourceKind.ORANGE_BOOK)
    if (
        query.application_number
        or APP_PATTERN.search(text)
        or "drugs@fda" in text
        or "approval" in text
        or "sponsor" in text
        or "applicant" in text
    ):
        routed.append(SourceKind.DRUGSFDA)
    if (
        "psg" in text
        or "product-specific guidance" in text
        or "bioequivalence" in text
        or "be study" in text
        or "dissolution" in text
    ):
        routed.append(SourceKind.PSG)

    if routed:
        return _dedupe(routed)

    # Default to structured approval/product identity plus local PSG evidence.
    return [SourceKind.DRUGSFDA, SourceKind.ORANGE_BOOK, SourceKind.PSG]


def search_sources(
    query: SourceQuery,
    *,
    sources: Iterable[SourceKind] | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[SourceKind], list[SourceRecord]]:
    routed = route_sources(query, requested=sources)
    records: list[SourceRecord] = []
    for source in routed:
        records.extend(_handler_for(source).search(query, client=client))
    return routed, records


def _handler_for(source: SourceKind) -> SourceHandler:
    handlers: dict[SourceKind, SourceHandler] = {
        SourceKind.PSG: PsgHandler(),
        SourceKind.ORANGE_BOOK: OrangeBookHandler(),
        SourceKind.DRUGSFDA: DrugsFdaHandler(),
        SourceKind.SHORTAGE: ShortagesHandler(),
        SourceKind.NDC: NdcHandler(),
        SourceKind.REMS: RemsHandler(),
    }
    return handlers[source]


def _dedupe(sources: Iterable[SourceKind]) -> list[SourceKind]:
    out: list[SourceKind] = []
    seen: set[SourceKind] = set()
    for source in sources:
        if source in seen:
            continue
        seen.add(source)
        out.append(source)
    return out
