"""Rules-first FDA source routing and handler execution."""

from __future__ import annotations

import re
from collections.abc import Iterable

import httpx

from regwatch.common.logging import get_logger
from regwatch.sources.action_packages import ActionPackageHandler
from regwatch.sources.be_guidance import FdaBeGuidanceHandler
from regwatch.sources.drugsfda import DrugsFdaHandler
from regwatch.sources.orange_book import OrangeBookHandler
from regwatch.sources.psg import PsgHandler
from regwatch.sources.types import SourceHandler, SourceKind, SourceQuery, SourceRecord

APP_PATTERN = re.compile(r"\b(?:NDA|ANDA|BLA)?\s*\d{5,6}\b", re.IGNORECASE)
RS_PATTERN = re.compile(r"\brs\b", re.IGNORECASE)
# Word-boundary matched: a bare "rld"/"te code" substring would fire on "world",
# "worldwide", "integrate code", spuriously routing to ORANGE_BOOK.
RLD_PATTERN = re.compile(r"\brld\b", re.IGNORECASE)
TE_CODE_PATTERN = re.compile(r"\bte code\b", re.IGNORECASE)
log = get_logger(__name__)

_APPROVED_KINDS = frozenset(
    {
        SourceKind.DRUGSFDA,
        SourceKind.ACTION_PACKAGE,
        SourceKind.PSG,
        SourceKind.FDA_BE_GUIDANCE,
        SourceKind.ORANGE_BOOK,
    }
)


def route_sources(
    query: SourceQuery,
    *,
    requested: Iterable[SourceKind] | None = None,
) -> list[SourceKind]:
    if requested:
        explicitly_routed = _dedupe(requested)
        rejected = [source.value for source in explicitly_routed if source not in _APPROVED_KINDS]
        if rejected:
            raise ValueError(f"sources outside the authoritative FDA policy: {rejected}")
        return explicitly_routed

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
        or "label" in text
        or "sponsor" in text
        or "applicant" in text
    ):
        routed.append(SourceKind.DRUGSFDA)
    if any(
        term in text
        for term in (
            "action package",
            "clinical review",
            "statistical review",
            "clinical pharmacology review",
            "cmc review",
            "quality review",
            "integrated review",
            "multidisciplinary review",
        )
    ):
        routed.append(SourceKind.ACTION_PACKAGE)
    if (
        "psg" in text
        or "product-specific guidance" in text
        or "bioequivalence" in text
        or "be study" in text
        or "dissolution" in text
    ):
        routed.append(SourceKind.PSG)
        routed.append(SourceKind.FDA_BE_GUIDANCE)

    if routed:
        return _dedupe(routed)

    # Default to structured approval/product identity plus local PSG evidence.
    return [
        SourceKind.DRUGSFDA,
        SourceKind.ORANGE_BOOK,
        SourceKind.PSG,
        SourceKind.FDA_BE_GUIDANCE,
    ]


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
    SourceKind.ACTION_PACKAGE: ActionPackageHandler(),
    SourceKind.FDA_BE_GUIDANCE: FdaBeGuidanceHandler(),
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
