"""DailyMed source handler tests (mocked HTTP — no live calls).

The application-number format was verified empirically against the live API:
``spls.json?application_number=`` matches only the prefixed, zero-padded form
(``NDA020503``); bare digits return zero rows. These tests pin that contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from regwatch.sources import router as router_mod
from regwatch.sources.dailymed import (
    SPL_MEDIA_URL_TEMPLATE,
    SPL_XML_URL_TEMPLATE,
    SPLS_ENDPOINT,
    DailyMedHandler,
    SplCandidate,
    fetch_media,
    fetch_spl_sections,
    parse_spl_section_codes,
    parse_spl_sections,
    resolve_setid,
)
from regwatch.sources.router import route_sources
from regwatch.sources.types import SourceKind, SourceQuery

SETID = "11111111-2222-3333-4444-555555555555"
SPL_XML = (Path(__file__).parent / "fixtures" / "spl_sample.xml").read_text()


def _listing(setid: str, published: str, title: str = "ALBUTEROL SULFATE") -> dict[str, object]:
    return {"setid": setid, "published_date": published, "title": title, "spl_version": 3}


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("regwatch.sources._utils.time.sleep", lambda _seconds: None)


def test_resolve_setid_picks_most_recent_published() -> None:
    payload = {
        "data": [
            _listing("older-setid", "Jan 02, 2020"),
            _listing(SETID, "Dec 18, 2025"),
            _listing("middle-setid", "Mar 05, 2023"),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT, params__contains={"application_number": "NDA020503"}).mock(
            return_value=httpx.Response(200, json=payload)
        )
        resolution = resolve_setid("NDA 020503")

    assert resolution is not None
    assert resolution.setid == SETID
    assert resolution.published == "Dec 18, 2025"
    assert SETID in resolution.source_url
    assert resolution.fetched_at.tzinfo is not None


def test_resolve_setid_prefers_sponsor_title_over_recent_repackager() -> None:
    """Repackager relabels are often the most recently published listing — when
    the caller supplies the resolved brand/sponsor, the sponsor's own label wins
    and the repackager set stays visible via candidate_labelers."""
    payload = {
        "data": [
            _listing(
                "repackager-setid",
                "Jan 16, 2026",
                "ALBUTEROL SULFATE AEROSOL, METERED [PREFERRED PHARMACEUTICALS INC.]",
            ),
            _listing(
                SETID,
                "Oct 08, 2019",
                "PROVENTIL HFA (ALBUTEROL SULFATE) AEROSOL [MERCK SHARP & DOHME CORP.]",
            ),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid("NDA020503", prefer_titles=["PROVENTIL HFA"])

    assert resolution is not None
    assert resolution.setid == SETID
    assert resolution.labeler == "MERCK SHARP & DOHME CORP."
    assert set(resolution.candidate_labelers) == {
        "PREFERRED PHARMACEUTICALS INC.",
        "MERCK SHARP & DOHME CORP.",
    }


def test_resolve_setid_falls_back_to_most_recent_without_title_match() -> None:
    payload = {
        "data": [
            _listing("older-setid", "Jan 02, 2020"),
            _listing(SETID, "Dec 18, 2025"),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid(
            "NDA020503",
            prefer_titles=["NO SUCH BRAND"],
            prefer_labelers=["NO SUCH COMPANY"],
        )

    assert resolution is not None
    assert resolution.setid == SETID


def test_resolve_setid_matches_punctuation_variant_brand_title() -> None:
    """Live NDA 020503 regression: the Orange Book trade name is hyphenated
    ("PROVENTIL-HFA") while DailyMed titles it "PROVENTIL HFA ..." -- the old
    canonical containment kept the hyphen, silently missed the brand, and the
    most-recent REPACKAGER relabel won the pick."""
    payload = {
        "data": [
            _listing(
                "repackager-setid",
                "Jan 16, 2026",
                "ALBUTEROL SULFATE AEROSOL, METERED [PREFERRED PHARMACEUTICALS INC.]",
            ),
            _listing(
                SETID,
                "Oct 08, 2019",
                "PROVENTIL HFA (ALBUTEROL SULFATE) AEROSOL, METERED [MERCK SHARP & DOHME CORP.]",
            ),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid("NDA020503", prefer_titles=["PROVENTIL-HFA"])

    assert resolution is not None
    assert resolution.setid == SETID
    assert resolution.labeler == "MERCK SHARP & DOHME CORP."


def test_resolve_setid_labeler_tier_picks_sponsor_over_recent_repackager() -> None:
    """With no brand/trade title match at all, the second tier matches the
    bracketed LABELER against the Drugs@FDA sponsor / OB applicant names -- the
    sponsor's own label beats a more recently published repackager relabel."""
    payload = {
        "data": [
            _listing(
                "repackager-setid",
                "Jan 16, 2026",
                "ALBUTEROL SULFATE AEROSOL, METERED [PREFERRED PHARMACEUTICALS INC.]",
            ),
            _listing(
                SETID,
                "Oct 08, 2019",
                "ALBUTEROL SULFATE AEROSOL, METERED [KINDEVA DRUG DELIVERY L.P.]",
            ),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid(
            "NDA020503",
            prefer_titles=["NO SUCH BRAND"],
            prefer_labelers=["Kindeva Drug Delivery L.P."],
        )

    assert resolution is not None
    assert resolution.setid == SETID
    assert resolution.labeler == "KINDEVA DRUG DELIVERY L.P."


def test_resolve_setid_title_tier_outranks_labeler_tier() -> None:
    """A title-matched listing wins even when a labeler-matched one is newer --
    the brand/trade title is the stronger identity signal."""
    payload = {
        "data": [
            _listing(
                "labeler-setid",
                "Jan 16, 2026",
                "ALBUTEROL SULFATE AEROSOL, METERED [KINDEVA DRUG DELIVERY L.P.]",
            ),
            _listing(
                SETID,
                "Oct 08, 2019",
                "PROVENTIL HFA (ALBUTEROL SULFATE) AEROSOL, METERED [MERCK SHARP & DOHME CORP.]",
            ),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid(
            "NDA020503",
            prefer_titles=["PROVENTIL-HFA"],
            prefer_labelers=["Kindeva Drug Delivery L.P."],
        )

    assert resolution is not None
    assert resolution.setid == SETID


def test_resolve_setid_short_prefer_tokens_never_match() -> None:
    """Sub-4-char normalized tokens ("HFA", "INC.") prove nothing -- without the
    floor they would pin the OLDER listing here; with it the pick falls back to
    the most recent overall."""
    payload = {
        "data": [
            _listing(
                SETID,
                "Jan 16, 2026",
                "ALBUTEROL SULFATE AEROSOL, METERED [KINDEVA DRUG DELIVERY L.P.]",
            ),
            _listing(
                "older-setid",
                "Oct 08, 2019",
                "PROVENTIL HFA (ALBUTEROL SULFATE) AEROSOL, METERED [PREFERRED PHARMACEUTICALS INC.]",
            ),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid("NDA020503", prefer_titles=["HFA"], prefer_labelers=["INC."])

    assert resolution is not None
    assert resolution.setid == SETID


def test_resolve_setid_retains_candidate_listings() -> None:
    """The chosen resolution keeps the full candidate set (setid/title/labeler/
    published) so the caller can surface the selection for analyst review."""
    repack_title = "ALBUTEROL SULFATE AEROSOL, METERED [PREFERRED PHARMACEUTICALS INC.]"
    sponsor_title = "PROVENTIL HFA (ALBUTEROL SULFATE) AEROSOL, METERED [MERCK SHARP & DOHME CORP.]"
    payload = {
        "data": [
            _listing("repackager-setid", "Jan 16, 2026", repack_title),
            _listing(SETID, "Oct 08, 2019", sponsor_title),
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid("NDA020503", prefer_titles=["PROVENTIL HFA"])

    assert resolution is not None
    assert resolution.candidates == (
        SplCandidate(
            setid="repackager-setid",
            title=repack_title,
            labeler="PREFERRED PHARMACEUTICALS INC.",
            published="Jan 16, 2026",
        ),
        SplCandidate(
            setid=SETID,
            title=sponsor_title,
            labeler="MERCK SHARP & DOHME CORP.",
            published="Oct 08, 2019",
        ),
    )


def test_resolve_setid_never_queries_bare_digits() -> None:
    """Bare digits return zero rows on the live API — querying them would
    fabricate a false "no SPL" (INV-5), so only prefixed candidates go out."""
    empty: dict[str, list[object]] = {"data": []}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=empty))
        assert resolve_setid("020503") is None

    queried = [call.request.url.params["application_number"] for call in route.calls]
    assert queried == ["NDA020503", "ANDA020503", "BLA020503"]


def test_resolve_setid_prefixed_input_queries_exactly_that_application() -> None:
    """Contract C1: a prefixed input (the populator always sends one) queries
    EXACTLY that application — never the NDA→ANDA→BLA expansion, which could
    silently return another application's SPL."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json={"data": []}))
        assert resolve_setid("ANDA208677") is None

    queried = [call.request.url.params["application_number"] for call in route.calls]
    assert queried == ["ANDA208677"]


def test_resolve_setid_single_letter_prefix_queries_its_application() -> None:
    """'N020503' (the UI placeholder format) is NDA020503 — never degraded to
    bare digits, never the three-way expansion."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json={"data": []}))
        assert resolve_setid("N020503") is None

    queried = [call.request.url.params["application_number"] for call in route.calls]
    assert queried == ["NDA020503"]


def test_spl_listings_follow_pagination_metadata() -> None:
    """Live ANDA208677 shows total_elements 103 — page 2 must be fetched and
    aggregated; a single pagesize-100 request silently drops listings."""
    page1 = {
        "metadata": {"total_elements": 103, "elements_per_page": 100, "total_pages": 2},
        "data": [_listing(f"setid-{i}", "Jan 02, 2020") for i in range(100)],
    }
    page2 = {
        "metadata": {"total_elements": 103, "elements_per_page": 100, "total_pages": 2},
        "data": [
            _listing("setid-100", "Jan 03, 2020"),
            _listing("setid-101", "Jan 04, 2020"),
            _listing(SETID, "Dec 18, 2025"),
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=page1 if page == "1" else page2)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(side_effect=respond)
        resolution = resolve_setid("ANDA208677")

    assert route.call_count == 2
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2"]
    assert resolution is not None
    assert resolution.setid == SETID  # the most recent listing lives on page 2


def test_spl_listings_follow_next_page_url_metadata() -> None:
    """Some responses advertise pagination via next_page_url, not total_pages."""
    page1 = {
        "metadata": {"next_page_url": f"{SPLS_ENDPOINT}?application_number=NDA020503&page=2"},
        "data": [_listing("setid-0", "Jan 02, 2020")],
    }
    page2 = {
        "metadata": {"next_page_url": None},
        "data": [_listing(SETID, "Dec 18, 2025")],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=page1 if page == "1" else page2)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(side_effect=respond)
        resolution = resolve_setid("NDA020503")

    assert route.call_count == 2
    assert resolution is not None
    assert resolution.setid == SETID


def test_spl_listings_pagination_hard_cap_at_ten_pages() -> None:
    """A runaway (or lying) pagination signal stops at the 10-page hard cap."""
    payload = {
        "metadata": {"total_pages": 99},
        "data": [_listing("setid-x", "Jan 02, 2020")],
    }
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        resolution = resolve_setid("NDA020503")

    assert route.call_count == 10
    assert resolution is not None


def test_resolve_setid_none_only_after_successful_empty_query() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json={"data": []}))
        assert resolve_setid("ANDA076204") is None


def test_resolve_setid_http_failure_propagates() -> None:
    """A failed query must never read as "no SPL" — the populator collapses
    the cell to analyst_input_required instead (absence-handling rule)."""
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            resolve_setid("NDA020503")


def test_fetch_spl_sections_extracts_loinc_sections() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPL_XML_URL_TEMPLATE.format(setid=SETID)).mock(
            return_value=httpx.Response(200, text=SPL_XML)
        )
        sections = fetch_spl_sections(SETID, ["34067-9", "42228-7"])

    assert set(sections) == {"34067-9", "42228-7"}
    indications = sections["34067-9"]
    assert indications.title == "1 INDICATIONS AND USAGE"
    assert "treatment or prevention of bronchospasm" in indications.text
    assert SETID in indications.source_url
    # Nested PLLR subsection content rides along with its parent section.
    pregnancy = sections["42228-7"]
    assert "Pregnancy Exposure Registry" in pregnancy.text
    assert "drug-associated risk" in pregnancy.text


def test_parse_spl_section_codes_document_order_deduplicated() -> None:
    assert parse_spl_section_codes(SPL_XML) == ["34067-9", "42228-7", "77290-5", "34089-3"]


def test_parse_spl_sections_ignores_unrequested_codes() -> None:
    sections = parse_spl_sections(
        SPL_XML,
        ["34089-3"],
        source_url="https://example.invalid",
        fetched_at=datetime.now(UTC),
    )
    assert list(sections) == ["34089-3"]
    assert "albuterol sulfate, USP" in sections["34089-3"].text


def test_spl_xml_with_dtd_is_refused() -> None:
    payload = '<!DOCTYPE document [<!ENTITY x "y">]><document/>'
    with pytest.raises(ValueError, match="DTD"):
        parse_spl_section_codes(payload)


def test_fetch_media_lists_label_assets() -> None:
    # Payload shape verified against the live media.json endpoint.
    payload = {
        "data": {
            "spl_version": 6,
            "media": [
                {
                    "mime_type": "image/jpeg",
                    "url": f"https://dailymed.nlm.nih.gov/dailymed/image.cfm?setid={SETID}&name=carton.jpg",
                    "name": "carton.jpg",
                },
                {"mime_type": "image/jpeg", "url": "", "name": "missing-url-dropped.jpg"},
            ],
        }
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPL_MEDIA_URL_TEMPLATE.format(setid=SETID)).mock(
            return_value=httpx.Response(200, json=payload)
        )
        media = fetch_media(SETID)

    assert len(media) == 1
    assert media[0].name == "carton.jpg"
    assert media[0].mime_type == "image/jpeg"
    assert "image.cfm" in media[0].url


def test_handler_returns_records_with_spl_token() -> None:
    payload = {"data": [_listing(SETID, "Dec 18, 2025")]}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        records = DailyMedHandler().search(SourceQuery(application_number="NDA 020503"))

    assert len(records) == 1
    record = records[0]
    assert record.source == SourceKind.DAILYMED
    assert record.identifiers["setid"] == SETID
    assert record.identifiers["token"] == f"SPL_{SETID}"
    assert record.identifiers["application_number"] == "NDA020503"
    assert record.fields["published"] == "Dec 18, 2025"


def test_handler_without_application_number_does_not_query() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(SPLS_ENDPOINT).mock(return_value=httpx.Response(500))
        assert DailyMedHandler().search(SourceQuery(query_text="albuterol")) == []
    assert route.call_count == 0


def test_dailymed_registered_and_routed_with_application_number() -> None:
    assert isinstance(router_mod._HANDLERS[SourceKind.DAILYMED], DailyMedHandler)
    assert SourceKind.DAILYMED in route_sources(
        SourceQuery(
            query_text="What does the SPL labeling on DailyMed say?",
            application_number="NDA020503",
        )
    )
    # The no-cue fallback triple is untouched.
    assert route_sources(SourceQuery(query_text="hello")) == [
        SourceKind.DRUGSFDA,
        SourceKind.ORANGE_BOOK,
        SourceKind.PSG,
    ]


def test_labeling_cue_without_application_number_keeps_default_routing() -> None:
    """Regression (B5): 'metformin spl' used to exclusive-route to DailyMed,
    whose handler returns [] without an application number — a guaranteed
    zero-result query. Without an application number the labeling cue must
    behave exactly as pre-whitepaper: other cues, else the default triple."""
    assert route_sources(SourceQuery(query_text="metformin spl")) == [
        SourceKind.DRUGSFDA,
        SourceKind.ORANGE_BOOK,
        SourceKind.PSG,
    ]
    # Other cues still win on their own merits.
    assert route_sources(SourceQuery(query_text="rems spl for metformin")) == [SourceKind.REMS]
