"""Retired DailyMed acquisition fails closed; pure historical SPL parsing remains."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from regwatch.sources.dailymed import (
    DailyMedHandler,
    fetch_media,
    fetch_spl_sections,
    fetch_spl_xml,
    parse_spl_section_codes,
    parse_spl_sections,
    resolve_setid,
)
from regwatch.sources.policy import SourcePolicyError
from regwatch.sources.router import route_sources
from regwatch.sources.types import SourceKind, SourceQuery

SETID = "11111111-2222-3333-4444-555555555555"
SPL_XML = (Path(__file__).parent / "fixtures" / "spl_sample.xml").read_text()


def test_all_dailymed_acquisition_entry_points_fail_closed() -> None:
    with pytest.raises(SourcePolicyError):
        resolve_setid("NDA020503")
    with pytest.raises(SourcePolicyError):
        fetch_spl_xml(SETID)
    with pytest.raises(SourcePolicyError):
        fetch_spl_sections(SETID, ["34067-9"])
    with pytest.raises(SourcePolicyError):
        fetch_media(SETID)
    with pytest.raises(SourcePolicyError):
        DailyMedHandler().search(SourceQuery(application_number="NDA020503"))


def test_parse_spl_sections_extracts_requested_loinc_sections() -> None:
    fetched_at = datetime.now(UTC)
    sections = parse_spl_sections(
        SPL_XML,
        ["34067-9", "42228-7"],
        source_url="historical://stored-spl",
        fetched_at=fetched_at,
    )

    assert set(sections) == {"34067-9", "42228-7"}
    assert sections["34067-9"].title
    assert sections["34067-9"].text
    assert sections["34067-9"].source_url == "historical://stored-spl"


def test_parse_spl_section_codes_preserves_order_and_deduplicates() -> None:
    codes = parse_spl_section_codes(SPL_XML)
    assert codes
    assert codes == list(dict.fromkeys(codes))


def test_spl_parser_refuses_dtd_or_entity_declarations() -> None:
    malicious = '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>'
    with pytest.raises(ValueError, match="DTD/entity"):
        parse_spl_section_codes(malicious)


def test_dailymed_is_not_registered_or_requestable() -> None:
    routed = route_sources(SourceQuery(query_text="find the approved label"))
    assert SourceKind.DAILYMED not in routed
    assert SourceKind.DRUGSFDA in routed
    with pytest.raises(ValueError, match="outside the authoritative FDA policy"):
        route_sources(
            SourceQuery(query_text="legacy label"),
            requested=[SourceKind.DAILYMED],
        )
