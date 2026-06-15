"""Rules-first FDA source routing and handler execution."""

from __future__ import annotations

import re
from collections.abc import Iterable

import httpx

from regwatch.common.logging import get_logger
from regwatch.sources.dailymed import DailyMedHandler
from regwatch.sources.drugsfda import DrugsFdaHandler
from regwatch.sources.ndc import NdcHandler
from regwatch.sources.orange_book import OrangeBookHandler
from regwatch.sources.psg import PsgHandler
from regwatch.sources.rems import RemsHandler
from regwatch.sources.shortages import ShortagesHandler
from regwatch.sources.types import SourceHandler, SourceKind, SourceQuery, SourceRecord

NDC_PATTERN = re.compile(r"\b\d{4,5}-\d{3,4}(?:-\d{1,2})?\b")
APP_PATTERN = re.compile(r"\b(?:NDA|ANDA|BLA)?\s*\d{5,6}\b", re.IGNORECASE)
RS_PATTERN = re.compile(r"\brs\b", re.IGNORECASE)
SPL_PATTERN = re.compile(r"\bspl\b", re.IGNORECASE)
# Word-boundary matched: a bare "rld"/"te code" substring would fire on "world",
# "worldwide", "integrate code", spuriously routing to ORANGE_BOOK.
RLD_PATTERN = re.compile(r"\brld\b", re.IGNORECASE)
TE_CODE_PATTERN = re.compile(r"\bte code\b", re.IGNORECASE)
log = get_logger(__name__)


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
    # DailyMed routes only on an explicit labeling cue AND a structured
    # application number: DailyMedHandler queries spls.json by application
    # number alone, so without one the cue would exclusive-route the query to
    # a guaranteed-empty handler. A labeling-cue query with no application
    # number falls through to the other cues / default triple instead.
    if query.application_number and (
        "dailymed" in text or "structured product label" in text or SPL_PATTERN.search(text)
    ):
        routed.append(SourceKind.DAILYMED)
    if (
        "orange book" in text
        or "therapeutic equivalence" in text
        or TE_CODE_PATTERN.search(text)
        or RLD_PATTERN.search(text)
        or "reference standard" in text
        or _mentions_reference_standard(text, query)
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
        try:
            records.extend(_handler_for(source).search(query, client=client))
        except Exception as exc:
            log.warning(
                "source_search_failed",
                source=source.value,
                error_type=type(exc).__name__,
                error=str(exc),
            )
    return routed, records


_HANDLERS: dict[SourceKind, SourceHandler] = {
    SourceKind.PSG: PsgHandler(),
    SourceKind.ORANGE_BOOK: OrangeBookHandler(),
    SourceKind.DRUGSFDA: DrugsFdaHandler(),
    SourceKind.SHORTAGE: ShortagesHandler(),
    SourceKind.NDC: NdcHandler(),
    SourceKind.REMS: RemsHandler(),
    SourceKind.DAILYMED: DailyMedHandler(),
}


def _handler_for(source: SourceKind) -> SourceHandler:
    return _HANDLERS[source]


def _mentions_reference_standard(text: str, query: SourceQuery) -> bool:
    if not RS_PATTERN.search(text):
        return False
    return bool(
        query.active_ingredient
        or query.brand_name
        or query.application_number
        or "orange book" in text
        or "reference standard" in text
        or "therapeutic equivalence" in text
        or RLD_PATTERN.search(text)
        or TE_CODE_PATTERN.search(text)
    )


def _dedupe(sources: Iterable[SourceKind]) -> list[SourceKind]:
    out: list[SourceKind] = []
    seen: set[SourceKind] = set()
    for source in sources:
        if source in seen:
            continue
        seen.add(source)
        out.append(source)
    return out
