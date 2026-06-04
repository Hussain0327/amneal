"""REMS@FDA source handler.

REMS does not currently have an openFDA drug endpoint, so this handler parses
FDA REMS search-result HTML into structured rows. It is deliberately
conservative: if the page shape changes, it returns no rows instead of
inventing fields.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx
from config.settings import get_settings
from selectolax.parser import HTMLParser

from regwatch.common.text_normalize import canonical_name
from regwatch.sources._utils import clean_application_number, clean_text
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

REMS_INDEX_URL = "https://www.accessdata.fda.gov/scripts/cder/rems/index.cfm"


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
        terms = _terms(query)
        out: list[SourceRecord] = []
        for row in rows:
            haystack = canonical_name(" ".join(str(v) for v in row.values()))
            if terms and not any(term in haystack for term in terms):
                continue
            out.append(_record(row))
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


def _fetch_rems_html(client: httpx.Client | None) -> str:
    s = get_settings()
    owned = client is None
    active_client = client or httpx.Client(
        timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent}
    )
    try:
        resp = active_client.get(REMS_INDEX_URL)
        resp.raise_for_status()
        return resp.text
    finally:
        if owned:
            active_client.close()


def _record(row: dict[str, str]) -> SourceRecord:
    title = row.get("drug_name") or row.get("rems_name") or row.get("col_1") or "REMS record"
    source_url = next((v for k, v in row.items() if k.endswith("_url")), REMS_INDEX_URL)
    identifiers = {
        k: v
        for k, v in {
            "application_number": row.get("application_number") or row.get("application_no"),
        }.items()
        if v
    }
    return SourceRecord(
        source=SourceKind.REMS,
        title=f"REMS: {title}",
        source_url=source_url,
        identifiers=identifiers,
        fields=_public_fields(row),
        raw=row,
    )


def _terms(query: SourceQuery) -> list[str]:
    terms = []
    for value in (query.active_ingredient, query.brand_name):
        if value:
            terms.append(canonical_name(value))
    app_no = clean_application_number(query.application_number)
    if app_no:
        terms.append(app_no.lower())
    return terms


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
