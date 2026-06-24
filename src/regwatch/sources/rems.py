"""REMS@FDA source handler.

REMS does not currently have an openFDA drug endpoint, so this handler parses
FDA REMS search-result HTML into structured rows. It is deliberately
conservative: if the page shape changes, it returns no rows instead of
inventing fields.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

import httpx
from config.settings import get_settings
from selectolax.parser import HTMLParser

from regwatch.common.text_normalize import canonical_name
from regwatch.sources._utils import (
    application_number_candidates,
    clean_application_number,
    clean_text,
    get_with_retry,
    owned_client,
)
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

REMS_INDEX_URL = "https://www.accessdata.fda.gov/scripts/cder/rems/index.cfm"

# How the live index embeds application numbers in row text: "NDA #022549".
_APP_NO_IN_TEXT_RE = re.compile(r"\b(?:NDA|ANDA|BLA)\s*#?\s*\d{5,6}\b", re.IGNORECASE)


class RemsHandler:
    source = SourceKind.REMS

    def __init__(self, *, html: str | None = None) -> None:
        self._html = html

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        html = self._html or _fetch_rems_html(client)
        rows = parse_rems_rows(html)
        name_terms = _name_terms(query)
        query_app_nos = set(application_number_candidates(query.application_number))
        out: list[SourceRecord] = []
        for row in rows:
            row_app_nos = _row_application_numbers(row)
            if not _matches(row, row_app_nos, name_terms, query_app_nos):
                continue
            out.append(_record(row, row_app_nos))
            if len(out) >= query.limit:
                break
        return out


def parse_rems_rows(html: str) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    rows: list[dict[str, str]] = []
    for table in tree.css("table"):
        headers = [_field_name(th.text() or "") for th in table.css("th")]
        for tr in table.css("tr"):
            cells = tr.css("td")
            if not cells:
                continue
            row: dict[str, str] = {}
            for idx, cell in enumerate(cells):
                key = headers[idx] if idx < len(headers) and headers[idx] else f"col_{idx + 1}"
                row[key] = clean_text(cell.text())
                link = cell.css_first("a[href]")
                if link is not None:
                    href = link.attributes.get("href")
                    if href:
                        row[f"{key}_url"] = urljoin(REMS_INDEX_URL, href)
            if any(v for v in row.values()):
                rows.append(row)
    return rows


def fetch_rems_index_html(client: httpx.Client | None = None) -> str:
    """Fetch the REMS index page HTML once.

    Callers that need a tri-state absence signal parse this with
    :func:`parse_rems_rows` themselves: zero TOTAL parsed rows means the scrape
    degraded (the parser deliberately invents nothing), which must never read
    as "queried, genuinely absent" (INV-5).
    """
    return _fetch_rems_html(client)


def _fetch_rems_html(client: httpx.Client | None) -> str:
    s = get_settings()
    with owned_client(
        client,
        lambda: httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent}),
    ) as active_client:
        resp = get_with_retry(active_client, REMS_INDEX_URL)
        resp.raise_for_status()
        return resp.text


def _record(row: dict[str, str], app_nos: list[str]) -> SourceRecord:
    """One parsed index row as a SourceRecord.

    Identifiers contract: ``application_number`` is the FIRST typed number
    extracted from the row's text (rows without an NDA/ANDA/BLA-prefixed
    value carry NO application-number identifier — bare digits cannot name an
    application type, INV-5); ``application_numbers`` lists ALL extracted
    numbers, comma-joined, when the row carries more than one.
    """
    title = row.get("drug_name") or row.get("rems_name") or row.get("col_1") or "REMS record"
    source_url = next((v for k, v in row.items() if k.endswith("_url")), REMS_INDEX_URL)
    identifiers: dict[str, str] = {}
    if app_nos:
        identifiers["application_number"] = app_nos[0]
        if len(app_nos) > 1:
            identifiers["application_numbers"] = ", ".join(app_nos)
    return SourceRecord(
        source=SourceKind.REMS,
        title=f"REMS: {title}",
        source_url=source_url,
        identifiers=identifiers,
        fields=_public_fields(row),
        raw=row,
    )


def _row_application_numbers(row: dict[str, str]) -> list[str]:
    """Cleaned application numbers extracted from the row's visible text.

    The live index embeds them in free text ("NDA #022549"), so structured
    identifiers come from pattern extraction — never raw column trust.
    """
    out: list[str] = []
    for key, value in row.items():
        if key.endswith("_url"):
            continue
        for match in _APP_NO_IN_TEXT_RE.finditer(value):
            cleaned = clean_application_number(match.group(0))
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _matches(
    row: dict[str, str],
    row_app_nos: list[str],
    name_terms: list[str],
    query_app_nos: set[str],
) -> bool:
    """Whether a parsed index row matches the query.

    Application numbers compare exactly against the row's extracted, cleaned
    identifiers — a raw substring like ``nda022549`` can never match the
    index's literal ``NDA #022549`` text, so substring matching is banned
    here. Name terms still match canonicalized row text. No filter at all
    matches everything (a browse query).
    """
    if not name_terms and not query_app_nos:
        return True
    if query_app_nos and query_app_nos.intersection(row_app_nos):
        return True
    if name_terms:
        haystack = canonical_name(" ".join(str(v) for v in row.values()))
        return any(term in haystack for term in name_terms)
    return False


def _name_terms(query: SourceQuery) -> list[str]:
    return [canonical_name(value) for value in (query.active_ingredient, query.brand_name) if value]


def _field_name(value: str) -> str:
    cleaned = clean_text(value).lower()
    for old, new in {
        " ": "_",
        "/": "_",
        "-": "_",
        ".": "",
        "#": "number",
    }.items():
        cleaned = cleaned.replace(old, new)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def _public_fields(row: dict[str, str]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.endswith("_url")}
