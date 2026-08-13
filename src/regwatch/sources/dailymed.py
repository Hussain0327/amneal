"""Retired DailyMed compatibility types and pure SPL XML parsers.

DailyMed is outside the authoritative FDA corpus policy.  Public acquisition
entry points remain as fail-closed shims so an old caller cannot silently make
an out-of-policy network request.  The pure XML helpers remain for reading
historical data already stored by earlier releases.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from regwatch.sources._utils import (
    clean_text,
)
from regwatch.sources.policy import SourcePolicyError
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

# Kept as empty compatibility exports for downstream imports.  No retired
# endpoint string or credential path remains in the runtime tree.
DAILYMED_API_BASE = ""
SPLS_ENDPOINT = ""
SPL_XML_URL_TEMPLATE = ""
SPL_MEDIA_URL_TEMPLATE = ""
SPL_VIEW_URL_TEMPLATE = ""

_HL7_NS = "{urn:hl7-org:v3}"
_PUBLISHED_FORMAT = "%b %d, %Y"  # DailyMed's published_date shape, e.g. "Oct 08, 2019"


@dataclass(frozen=True)
class SplCandidate:
    """One SPL listing DailyMed returned for the application number.

    Retained on the resolution so the caller can surface the full candidate
    set (repackager relabels included) for analyst review of the selection.
    """

    setid: str
    title: str
    labeler: str | None
    published: str | None


@dataclass(frozen=True)
class SetidResolution:
    """The current SPL for an application number, with fetch provenance.

    ``labeler`` is the bracketed labeler suffix from the listing title;
    ``candidate_labelers`` lists every distinct labeler DailyMed returned for
    the number — more than one means repackager relabels were in play and the
    selection is surfaced to the caller rather than silently resolved.
    ``candidates`` retains every listing (API order) so the selection is
    auditable and overridable, never a silent pick.
    """

    setid: str
    title: str
    published: str | None
    source_url: str
    fetched_at: datetime
    labeler: str | None = None
    candidate_labelers: tuple[str, ...] = ()
    candidates: tuple[SplCandidate, ...] = ()


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
    prefer_labelers: Sequence[str] = (),
    client: httpx.Client | None = None,
) -> SetidResolution | None:
    """Fail closed: new label acquisition is owned by Drugs@FDA."""
    del application_number, prefer_titles, prefer_labelers, client
    raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


def fetch_spl_sections(
    setid: str,
    loinc_codes: Sequence[str],
    *,
    client: httpx.Client | None = None,
) -> dict[str, SplSection]:
    """Fail closed: use approved Drugs@FDA labeling from the corpus."""
    del setid, loinc_codes, client
    raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


def fetch_spl_xml(setid: str, *, client: httpx.Client | None = None) -> SplXmlDocument:
    """Fail closed: use approved Drugs@FDA labeling from the corpus."""
    del setid, client
    raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


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
    """Fail closed: media outside the approved source universe is not fetched."""
    del setid, client
    raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


class DailyMedHandler:
    """Fail-closed compatibility handler for the retired source."""

    source = SourceKind.DAILYMED

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        del query, client
        raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


# Hard cap on pagination follows: 10 pages * pagesize 100 = 1,000 listings.
_MAX_SPL_PAGES = 10


def _spl_listings(
    application_number: str,
    client: httpx.Client | None,
) -> list[dict[str, Any]]:
    """Fail closed even for callers that reached the former private helper."""
    del application_number, client
    raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


def _paged_listings(client: httpx.Client, candidate: str) -> list[dict[str, Any]]:
    """Fail closed even for callers that reached the former private helper."""
    del client, candidate
    raise SourcePolicyError("DailyMed is outside the authoritative FDA corpus policy")


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
    prefer_labelers: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Tiered pick: title-matched, else labeler-matched, else most recent overall."""
    preferred = [
        listing for listing in listings if _title_matches(listing.get("title"), prefer_titles)
    ]
    if not preferred:
        preferred = [
            listing
            for listing in listings
            if _labeler_matches(_listing_labeler(listing.get("title")), prefer_labelers)
        ]
    return _most_recent(preferred or listings)


# Containment shorter than this proves nothing ("HFA"/"INC" would wave through
# half the catalog) -- mirrors the populator's name-verification floor.
_MIN_PREFER_CONTAINMENT_CHARS = 4
_MATCH_NORM_RE = re.compile(r"[^a-z0-9]+")


def _match_normalized(value: object) -> str:
    """Lowercased, punctuation-to-space, whitespace-collapsed match key.

    DailyMed titles/labelers and Drugs@FDA / Orange Book names disagree on
    punctuation ("PROVENTIL-HFA" vs "PROVENTIL HFA ...", "MERCK SHARP & DOHME
    CORP." vs "MERCK SHARP DOHME CORP") -- canonical_name preserves hyphens, so
    a punctuation variant silently missed its own brand and the most-recent
    repackager relabel won the pick.
    """
    return " ".join(_MATCH_NORM_RE.sub(" ", clean_text(value).lower()).split())


def _normalized_contains(candidate: str, want: str) -> bool:
    """Bidirectional normalized containment with the minimum-length floor."""
    if not candidate or not want:
        return False
    contained = want if want in candidate else candidate if candidate in want else None
    return contained is not None and len(contained) >= _MIN_PREFER_CONTAINMENT_CHARS


def _title_matches(title: object, prefer_titles: Sequence[str]) -> bool:
    haystack = _match_normalized(title)
    if not haystack:
        return False
    return any(_normalized_contains(haystack, _match_normalized(want)) for want in prefer_titles)


def _labeler_matches(labeler: str | None, prefer_labelers: Sequence[str]) -> bool:
    got = _match_normalized(labeler)
    if not got:
        return False
    return any(_normalized_contains(got, _match_normalized(want)) for want in prefer_labelers)


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


def _candidates(listings: list[dict[str, Any]]) -> tuple[SplCandidate, ...]:
    out: list[SplCandidate] = []
    for listing in listings:
        setid = clean_text(listing.get("setid"))
        if not setid:
            continue
        title = clean_text(listing.get("title"))
        out.append(
            SplCandidate(
                setid=setid,
                title=title,
                labeler=_listing_labeler(title),
                published=clean_text(listing.get("published_date")) or None,
            )
        )
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
    return ET.fromstring(xml_text)  # noqa: S314 - DTD/entity refused above


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
