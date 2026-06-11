"""DailyMed REST v2 source handler (SPL labeling).

Resolves an FDA application number to the current SPL ``setid``, extracts
LOINC-coded label sections from the SPL XML (stdlib ``xml.etree`` — no new
dependencies), and enumerates label media assets. Sections are surfaced
verbatim with provenance; nothing here interprets a label (INV-3/INV-5).

Application-number format (verified empirically against the live API):
``spls.json?application_number=`` matches the *prefixed, zero-padded* form
(``NDA020503``); bare digits return zero rows. A prefixed input queries
exactly that application; only genuinely bare digits expand to the prefixed
NDA→ANDA→BLA candidates (contract C1).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from config.settings import get_settings

from regwatch.common.text_normalize import canonical_name
from regwatch.sources._utils import (
    APPLICATION_PREFIXES,
    clean_application_number,
    clean_text,
    get_with_retry,
    owned_client,
)
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

DAILYMED_API_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
SPLS_ENDPOINT = f"{DAILYMED_API_BASE}/spls.json"
SPL_XML_URL_TEMPLATE = DAILYMED_API_BASE + "/spls/{setid}.xml"
SPL_MEDIA_URL_TEMPLATE = DAILYMED_API_BASE + "/spls/{setid}/media.json"
SPL_VIEW_URL_TEMPLATE = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"

_HL7_NS = "{urn:hl7-org:v3}"
_PUBLISHED_FORMAT = "%b %d, %Y"  # DailyMed's published_date shape, e.g. "Oct 08, 2019"


@dataclass(frozen=True)
class SetidResolution:
    """The current SPL for an application number, with fetch provenance.

    ``labeler`` is the bracketed labeler suffix from the listing title;
    ``candidate_labelers`` lists every distinct labeler DailyMed returned for
    the number — more than one means repackager relabels were in play and the
    selection is surfaced to the caller rather than silently resolved.
    """

    setid: str
    title: str
    published: str | None
    source_url: str
    fetched_at: datetime
    labeler: str | None = None
    candidate_labelers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SplSection:
    """One LOINC-coded SPL section, verbatim, with fetch provenance."""

    loinc: str
    title: str
    text: str
    source_url: str
    fetched_at: datetime


@dataclass(frozen=True)
class SplMedia:
    """One label media asset (image), enumerated — never interpreted."""

    name: str
    url: str
    mime_type: str


@dataclass(frozen=True)
class SplXmlDocument:
    """A fetched SPL XML document; feed it to the ``parse_spl_*`` helpers."""

    setid: str
    xml: str
    source_url: str
    fetched_at: datetime


def resolve_setid(
    application_number: str,
    *,
    prefer_titles: Sequence[str] = (),
    client: httpx.Client | None = None,
) -> SetidResolution | None:
    """Resolve an application number to its current SPL setid.

    DailyMed lists repackager relabels alongside the sponsor's own SPL, and a
    repackager's relabel is frequently the most recently published — so when
    ``prefer_titles`` (brand / trade / sponsor names) is given, listings whose
    title matches one are preferred, most-recent-first; the most recent overall
    is the fallback. Returns ``None`` only when DailyMed answered successfully
    and listed no SPL for the number ("queried, genuinely absent"). HTTP
    failures propagate so callers can collapse to ``analyst_input_required``.
    """
    listings = _spl_listings(application_number, client)
    best = _select_listing(listings, prefer_titles)
    if best is None:
        return None
    setid = clean_text(best.get("setid"))
    if not setid:
        return None
    title = clean_text(best.get("title"))
    return SetidResolution(
        setid=setid,
        title=title,
        published=clean_text(best.get("published_date")) or None,
        source_url=SPL_VIEW_URL_TEMPLATE.format(setid=setid),
        fetched_at=datetime.now(UTC),
        labeler=_listing_labeler(title),
        candidate_labelers=_distinct_labelers(listings),
    )


def fetch_spl_sections(
    setid: str,
    loinc_codes: Sequence[str],
    *,
    client: httpx.Client | None = None,
) -> dict[str, SplSection]:
    """Fetch the SPL XML once and extract the requested LOINC-coded sections.

    For structure heuristics (PLR/PLLR) that need the full section-code list,
    use :func:`fetch_spl_xml` + :func:`parse_spl_section_codes` on the same
    document instead of re-fetching.
    """
    doc = fetch_spl_xml(setid, client=client)
    return parse_spl_sections(
        doc.xml,
        loinc_codes,
        source_url=doc.source_url,
        fetched_at=doc.fetched_at,
    )


def fetch_spl_xml(setid: str, *, client: httpx.Client | None = None) -> SplXmlDocument:
    """GET ``spls/{setid}.xml`` with retry/backoff and a descriptive UA."""
    with owned_client(client, _dailymed_client) as active_client:
        resp = get_with_retry(active_client, SPL_XML_URL_TEMPLATE.format(setid=setid))
        resp.raise_for_status()
        return SplXmlDocument(
            setid=setid,
            xml=resp.text,
            source_url=SPL_VIEW_URL_TEMPLATE.format(setid=setid),
            fetched_at=datetime.now(UTC),
        )


def parse_spl_sections(
    xml_text: str,
    loinc_codes: Sequence[str],
    *,
    source_url: str,
    fetched_at: datetime,
) -> dict[str, SplSection]:
    """Extract the requested LOINC-coded sections from SPL XML, verbatim.

    First occurrence wins when a code repeats. Section text includes nested
    subsection content but not the section's own title (kept separately).
    """
    wanted = {clean_text(code) for code in loinc_codes}
    out: dict[str, SplSection] = {}
    for section in _safe_fromstring(xml_text).iter(f"{_HL7_NS}section"):
        code = _section_code(section)
        if code is None or code not in wanted or code in out:
            continue
        out[code] = SplSection(
            loinc=code,
            title=_section_title(section),
            text=_section_text(section),
            source_url=source_url,
            fetched_at=fetched_at,
        )
    return out


def parse_spl_section_codes(xml_text: str) -> list[str]:
    """Every LOINC section code in the SPL, document order, de-duplicated.

    This is the input to PLR/PLLR structure heuristics (presence of Highlights
    / pregnancy-and-lactation subsections), which live with the populator —
    this module only reports what the document contains.
    """
    codes: list[str] = []
    for section in _safe_fromstring(xml_text).iter(f"{_HL7_NS}section"):
        code = _section_code(section)
        if code and code not in codes:
            codes.append(code)
    return codes


def fetch_media(setid: str, *, client: httpx.Client | None = None) -> list[SplMedia]:
    """List the SPL's media assets from ``spls/{setid}/media.json``."""
    with owned_client(client, _dailymed_client) as active_client:
        resp = get_with_retry(active_client, SPL_MEDIA_URL_TEMPLATE.format(setid=setid))
        resp.raise_for_status()
        payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    media = data.get("media") if isinstance(data, dict) else None
    out: list[SplMedia] = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        url = clean_text(item.get("url"))
        if not name or not url:
            continue
        out.append(SplMedia(name=name, url=url, mime_type=clean_text(item.get("mime_type"))))
    return out


class DailyMedHandler:
    """SPL listings by application number, as structured source records."""

    source = SourceKind.DAILYMED

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        if not query.application_number:
            return []
        listings = _spl_listings(query.application_number, client)
        app_no = clean_application_number(query.application_number)
        records: list[SourceRecord] = []
        for listing in listings[: query.limit]:
            setid = clean_text(listing.get("setid"))
            if not setid:
                continue
            identifiers = {"setid": setid, "token": f"SPL_{setid}"}
            if app_no:
                identifiers["application_number"] = app_no
            title = clean_text(listing.get("title"))
            records.append(
                SourceRecord(
                    source=SourceKind.DAILYMED,
                    title=f"DailyMed SPL: {title or setid}",
                    source_url=SPL_VIEW_URL_TEMPLATE.format(setid=setid),
                    identifiers=identifiers,
                    fields={
                        "title": title or None,
                        "published": clean_text(listing.get("published_date")) or None,
                        "spl_version": listing.get("spl_version"),
                    },
                    raw=listing,
                )
            )
        return records


# Hard cap on pagination follows: 10 pages * pagesize 100 = 1,000 listings.
_MAX_SPL_PAGES = 10


def _spl_listings(
    application_number: str,
    client: httpx.Client | None,
) -> list[dict[str, Any]]:
    """All SPL listings (every page) for the application number.

    A prefixed input — the populator always sends one (contract C1) — queries
    EXACTLY that application. Candidate expansion (prefixed NDA→ANDA→BLA,
    first with hits wins) happens only for genuinely bare digits: DailyMed
    does not match bare digits (verified empirically), so querying them
    directly would fabricate a "no SPL" answer.
    """
    cleaned = clean_application_number(application_number)
    if cleaned is None:
        return []
    if cleaned.isdigit():
        candidates = [f"{prefix}{cleaned}" for prefix in APPLICATION_PREFIXES]
    else:
        candidates = [cleaned]
    with owned_client(client, _dailymed_client) as active_client:
        for candidate in candidates:
            listings = _paged_listings(active_client, candidate)
            if listings:
                return listings
    return []


def _paged_listings(client: httpx.Client, candidate: str) -> list[dict[str, Any]]:
    """Aggregate every spls.json page for one candidate (hard cap 10 pages).

    DailyMed pages at ``pagesize`` (live ANDA208677 shows total_elements 103,
    so a single 100-row page silently drops listings); the response's own
    pagination metadata (``total_pages`` / ``next_page_url``) drives the loop.
    """
    listings: list[dict[str, Any]] = []
    for page in range(1, _MAX_SPL_PAGES + 1):
        resp = get_with_retry(
            client,
            SPLS_ENDPOINT,
            {"application_number": candidate, "pagesize": 100, "page": page},
        )
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or []
        listings.extend(item for item in data if isinstance(item, dict))
        if not _has_next_page(payload.get("metadata"), page):
            break
    return listings


def _has_next_page(metadata: object, page: int) -> bool:
    """Whether DailyMed's pagination metadata announces a page after ``page``."""
    if not isinstance(metadata, dict):
        return False
    total_pages = metadata.get("total_pages")
    if isinstance(total_pages, int):
        return page < total_pages
    next_url = clean_text(metadata.get("next_page_url"))
    return bool(next_url) and next_url.lower() != "null"


def _select_listing(
    listings: list[dict[str, Any]],
    prefer_titles: Sequence[str],
) -> dict[str, Any] | None:
    """Most recent listing whose title matches a preferred name, else most recent."""
    preferred = [
        listing for listing in listings if _title_matches(listing.get("title"), prefer_titles)
    ]
    return _most_recent(preferred or listings)


def _title_matches(title: object, prefer_titles: Sequence[str]) -> bool:
    canon = canonical_name(clean_text(title))
    if not canon:
        return False
    for want in prefer_titles:
        want_canon = canonical_name(want)
        if want_canon and want_canon in canon:
            return True
    return False


_LABELER_RE = re.compile(r"\[([^\[\]]+)\]\s*$")


def _listing_labeler(title: object) -> str | None:
    """The bracketed labeler suffix DailyMed appends to listing titles."""
    match = _LABELER_RE.search(clean_text(title))
    return clean_text(match.group(1)) if match else None


def _distinct_labelers(listings: list[dict[str, Any]]) -> tuple[str, ...]:
    out: list[str] = []
    for listing in listings:
        labeler = _listing_labeler(listing.get("title"))
        if labeler and labeler not in out:
            out.append(labeler)
    return tuple(out)


def _most_recent(listings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most recently published listing; API order breaks ties/unparsed dates."""
    if not listings:
        return None

    def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[datetime, int]:
        index, listing = item
        published = _parse_published(listing.get("published_date"))
        return (published or datetime.min.replace(tzinfo=UTC), -index)

    return max(enumerate(listings), key=sort_key)[1]


def _parse_published(value: object) -> datetime | None:
    try:
        return datetime.strptime(clean_text(value), _PUBLISHED_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _safe_fromstring(xml_text: str) -> ET.Element:
    """Parse SPL XML, refusing any document that declares a DTD.

    Stdlib ``xml.etree`` is pinned here (no new deps), so XXE/entity-expansion
    is neutralized up front: SPL documents never carry a DOCTYPE, and a payload
    that does is rejected rather than parsed.
    """
    head = xml_text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise ValueError("refusing to parse SPL XML with a DTD/entity declaration")
    return ET.fromstring(xml_text)


def _section_code(section: ET.Element) -> str | None:
    code_el = section.find(f"{_HL7_NS}code")
    if code_el is None:
        return None
    return clean_text(code_el.attrib.get("code")) or None


def _section_title(section: ET.Element) -> str:
    title_el = section.find(f"{_HL7_NS}title")
    if title_el is None:
        return ""
    return clean_text(" ".join(title_el.itertext()))


def _section_text(section: ET.Element) -> str:
    """Verbatim text content of a section (nested subsections included)."""
    skip = {
        f"{_HL7_NS}title",
        f"{_HL7_NS}code",
        f"{_HL7_NS}id",
        f"{_HL7_NS}effectiveTime",
    }
    parts: list[str] = []
    for child in section:
        if child.tag in skip:
            continue
        parts.extend(child.itertext())
    return clean_text(" ".join(parts))


def _dailymed_client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(timeout=s.http_timeout_s, headers={"User-Agent": s.user_agent})
