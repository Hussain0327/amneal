"""Current approved-label reader over the authoritative Drugs@FDA corpus."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text as sa_text

from regwatch.sources._utils import clean_application_number
from regwatch.store.db import get_engine


@dataclass(frozen=True)
class ApprovedLabelSection:
    code: str
    title: str
    text: str
    page: int


@dataclass(frozen=True)
class ApprovedLabel:
    canonical_id: str
    title: str
    application_number: str
    source_url: str
    source_updated_at: str | None
    fetched_at: datetime
    document_id: int
    version_id: int
    sections: dict[str, ApprovedLabelSection]
    section_codes: tuple[str, ...]


# The legacy white-paper template names these labeling sections by LOINC. The
# source is now the approved Drugs@FDA PDF, so these codes are internal stable
# section keys only; evidence locators are FDA PDF pages, never SPL tokens.
LABEL_SECTION_HEADINGS: dict[str, tuple[str, tuple[re.Pattern[str], ...]]] = {
    "34067-9": (
        "INDICATIONS AND USAGE",
        (re.compile(r"\b(?:1\s+)?INDICATIONS\s+AND\s+USAGE\b", re.I),),
    ),
    "34068-7": (
        "DOSAGE AND ADMINISTRATION",
        (re.compile(r"\b(?:2\s+)?DOSAGE\s+AND\s+ADMINISTRATION\b", re.I),),
    ),
    "43678-2": (
        "DOSAGE FORMS AND STRENGTHS",
        (re.compile(r"\b(?:3\s+)?DOSAGE\s+FORMS?\s+AND\s+STRENGTHS\b", re.I),),
    ),
    "34070-3": (
        "CONTRAINDICATIONS",
        (re.compile(r"\b(?:4\s+)?CONTRAINDICATIONS\b", re.I),),
    ),
    "43685-7": (
        "WARNINGS AND PRECAUTIONS",
        (re.compile(r"\b(?:5\s+)?WARNINGS?\s+AND\s+PRECAUTIONS\b", re.I),),
    ),
    "34084-4": (
        "ADVERSE REACTIONS",
        (re.compile(r"\b(?:6\s+)?ADVERSE\s+REACTIONS\b", re.I),),
    ),
    "34073-7": (
        "DRUG INTERACTIONS",
        (re.compile(r"\b(?:7\s+)?DRUG\s+INTERACTIONS\b", re.I),),
    ),
    "43684-0": (
        "USE IN SPECIFIC POPULATIONS",
        (re.compile(r"\b(?:8\s+)?USE\s+IN\s+SPECIFIC\s+POPULATIONS\b", re.I),),
    ),
    "42228-7": (
        "PREGNANCY",
        (re.compile(r"\b(?:8\.1\s+)?PREGNANCY\b", re.I),),
    ),
    "77290-5": (
        "LACTATION",
        (re.compile(r"\b(?:8\.2\s+)?LACTATION\b", re.I),),
    ),
    "77291-3": (
        "FEMALES AND MALES OF REPRODUCTIVE POTENTIAL",
        (
            re.compile(
                r"\b(?:8\.3\s+)?FEMALES\s+AND\s+MALES\s+OF\s+REPRODUCTIVE\s+POTENTIAL\b",
                re.I,
            ),
        ),
    ),
}

_NEXT_NUMBERED_HEADING = re.compile(
    r"(?:\n|\r)\s*(?:\d{1,2}(?:\.\d{1,2})*)\s+[A-Z][A-Z /,&()-]{3,}",
)


def section_title(code: str) -> str:
    entry = LABEL_SECTION_HEADINGS.get(code)
    return entry[0] if entry else code


def load_latest_approved_label(application_number: str) -> ApprovedLabel | None:
    """Return the newest indexed approved label for one exact application.

    Drugs@FDA exposes every historical labeling action as a separate document.
    ``source_updated_at`` is therefore the regulatory ordering key; the corpus
    version/fetch timestamps only break ties. All text comes from current chunk
    rows, so this read cannot mix document revisions.
    """

    normalized = clean_application_number(application_number)
    if not normalized or normalized[:3] not in {"NDA", "AND", "BLA"}:
        return None
    with get_engine().connect() as conn:
        candidate = (
            conn.execute(
                sa_text(
                    "SELECT DISTINCT d.id, d.canonical_id, d.title, d.application_number, "
                    "d.source_url, v.id AS version_id, v.source_updated_at, v.fetched_at "
                    "FROM fda_document d JOIN fda_document_version v "
                    "ON v.fda_document_id = d.id JOIN chunk c ON c.fda_version_id = v.id "
                    "WHERE d.is_active AND d.document_type = 'approved_label' "
                    "AND upper(regexp_replace(COALESCE(d.application_number, ''), "
                    "'[^A-Za-z0-9]', '', 'g')) = :application_number "
                    "ORDER BY v.source_updated_at DESC NULLS LAST, v.fetched_at DESC, "
                    "v.id DESC, d.id DESC LIMIT 1"
                ),
                {"application_number": normalized},
            )
            .mappings()
            .first()
        )
        if candidate is None:
            return None
        rows = (
            conn.execute(
                sa_text(
                    "SELECT COALESCE(page, 1) AS page, COALESCE(text, '') AS text "
                    "FROM chunk WHERE fda_version_id = :version_id "
                    "ORDER BY page, ordinal, id"
                ),
                {"version_id": candidate["version_id"]},
            )
            .mappings()
            .all()
        )
    pages = _merge_pages(rows)
    sections = _extract_sections(pages)
    return ApprovedLabel(
        canonical_id=str(candidate["canonical_id"]),
        title=str(candidate["title"]),
        application_number=str(candidate["application_number"] or normalized),
        source_url=str(candidate["source_url"]),
        source_updated_at=(
            str(candidate["source_updated_at"]) if candidate["source_updated_at"] else None
        ),
        fetched_at=candidate["fetched_at"],
        document_id=int(candidate["id"]),
        version_id=int(candidate["version_id"]),
        sections=sections,
        section_codes=tuple(code for code in LABEL_SECTION_HEADINGS if code in sections),
    )


def label_from_pages(
    *,
    canonical_id: str,
    title: str,
    application_number: str,
    source_url: str,
    source_updated_at: str | None,
    fetched_at: datetime,
    document_id: int,
    version_id: int,
    pages: list[str],
) -> ApprovedLabel:
    """Construct a label from page text; public for deterministic unit tests."""

    indexed_pages = [(index, text) for index, text in enumerate(pages, start=1)]
    sections = _extract_sections(indexed_pages)
    return ApprovedLabel(
        canonical_id=canonical_id,
        title=title,
        application_number=application_number,
        source_url=source_url,
        source_updated_at=source_updated_at,
        fetched_at=fetched_at,
        document_id=document_id,
        version_id=version_id,
        sections=sections,
        section_codes=tuple(code for code in LABEL_SECTION_HEADINGS if code in sections),
    )


def _merge_pages(rows: Sequence[Any]) -> list[tuple[int, str]]:
    by_page: dict[int, str] = {}
    for row in rows:
        page = int(row["page"])
        text = str(row["text"])
        by_page[page] = _append_without_overlap(by_page.get(page, ""), text)
    return sorted(by_page.items())


def _append_without_overlap(existing: str, incoming: str) -> str:
    if not existing:
        return incoming
    ceiling = min(len(existing), len(incoming), 4_000)
    for size in range(ceiling, 79, -1):
        if existing.endswith(incoming[:size]):
            return existing + incoming[size:]
    return existing + "\n" + incoming


def _extract_sections(pages: list[tuple[int, str]]) -> dict[str, ApprovedLabelSection]:
    out: dict[str, ApprovedLabelSection] = {}
    for code, (title, patterns) in LABEL_SECTION_HEADINGS.items():
        candidates: list[ApprovedLabelSection] = []
        for index, (page, text) in enumerate(pages):
            haystack = text
            if index + 1 < len(pages):
                haystack += "\n" + pages[index + 1][1]
            for pattern in patterns:
                for match in pattern.finditer(haystack):
                    tail = haystack[match.start() :]
                    next_heading = _NEXT_NUMBERED_HEADING.search(tail, match.end() - match.start())
                    body = tail[: next_heading.start() if next_heading else 4_000].strip()
                    if body:
                        candidates.append(
                            ApprovedLabelSection(
                                code=code,
                                title=title,
                                text=body[:4_000],
                                page=page,
                            )
                        )
        if candidates:
            # A table-of-contents hit is short; the body occurrence carries the
            # most text. Deterministic tie-break keeps the later page.
            out[code] = max(candidates, key=lambda item: (len(item.text), item.page))
    return out
