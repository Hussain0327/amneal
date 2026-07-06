"""Parse a fixture of the PSG index page (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from regwatch.ingest.psg_crawler import (
    CrawlPageError,
    ensure_psg_index_markup,
    fetch_all_listings,
    filter_listings,
    parse_listings,
)

FIXTURE = Path(__file__).parent / "fixtures" / "psg_index_sample.html"

# An HTTP-200 body that is NOT the PSG index: Akamai challenge/maintenance
# pages (and a full FDA redesign) return 200 with neither drugData rows nor
# the drugTable shell, so status codes alone can never catch them.
CHALLENGE_HTML = (
    "<!doctype html><html><head><title>Access Denied</title></head>"
    "<body><h1>Please verify you are a human</h1></body></html>"
)

# A legitimately-empty letter page, mirroring the live letter-J route: the
# results-table shell renders ("0 record(s) found") with an empty tbody.
EMPTY_LETTER_HTML = (
    "<html><body><p>0 record(s) found for 'J'</p>"
    '<table class="drugTable" id="drugTable" cellspacing="0">'
    "<thead><tr><th>Active Ingredient</th></tr></thead><tbody></tbody></table>"
    "</body></html>"
)


def _load() -> list:
    return parse_listings(FIXTURE.read_text(encoding="utf-8"))


def test_parses_rows() -> None:
    rows = _load()
    assert len(rows) == 4
    names = {r.normalized_name for r in rows}
    assert "albuterol sulfate" in names
    assert "beclomethasone dipropionate" in names
    assert "romidepsin" in names


def test_application_numbers() -> None:
    rows = _load()
    albuterol = next(r for r in rows if r.appl_no == "020503")
    assert albuterol.rld_or_rs_numbers == ["020503", "020983", "021457"]


def test_filter_by_seed_names() -> None:
    rows = _load()
    seeds = filter_listings(rows, normalized_names=["albuterol", "beclomethasone", "romidepsin"])
    assert len(seeds) == 3
    appls = sorted(r.appl_no for r in seeds)
    assert appls == ["020503", "020911", "022393"]


def test_iso_date() -> None:
    rows = _load()
    albuterol = next(r for r in rows if r.appl_no == "020503")
    assert albuterol.recommended_date == "2026-05-21"


def test_dosage_form_and_route() -> None:
    rows = _load()
    albuterol = next(r for r in rows if r.appl_no == "020503")
    assert albuterol.dosage_form == "Aerosol, Metered"
    assert albuterol.route == "Inhalation"
    assert albuterol.psg_type == "draft"


def test_filter_does_not_match_unrelated() -> None:
    rows = _load()
    seeds = filter_listings(rows, normalized_names=["albuterol", "beclomethasone", "romidepsin"])
    names = {r.normalized_name for r in seeds}
    assert "sodium chloride" not in names


def test_markup_check_accepts_page_with_drug_rows() -> None:
    # Must not raise: drugData rows present.
    ensure_psg_index_markup(FIXTURE.read_text(encoding="utf-8"), url="fixture")


def test_markup_check_accepts_legitimately_empty_letter_page() -> None:
    # Zero rows with the results-table shell present is an empty letter, not a
    # crawl failure: the checker must not raise and the parse stays empty.
    assert parse_listings(EMPTY_LETTER_HTML) == []
    ensure_psg_index_markup(EMPTY_LETTER_HTML, url="letter-J")


def test_markup_check_rejects_200_challenge_page() -> None:
    with pytest.raises(CrawlPageError):
        ensure_psg_index_markup(CHALLENGE_HTML, url="letter-A")


def test_fetch_all_listings_fails_loud_on_mid_alphabet_challenge_page() -> None:
    """A 200-status challenge page partway through the A-Z walk must abort the
    WHOLE crawl: unioning only the letters that did parse would silently drop
    every watched product in the missing range."""
    fixture_html = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        letter = request.url.params.get("searchLetter") or ""
        if letter in ("A", "B"):
            return httpx.Response(200, text=fixture_html)
        return httpx.Response(200, text=CHALLENGE_HTML)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(CrawlPageError),
    ):
        fetch_all_listings(client=client)
