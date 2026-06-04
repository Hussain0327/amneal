"""Seed selection: pin by appl_no; exact name match (no substring leak)."""

from __future__ import annotations

from regwatch.ingest.psg_crawler import PsgListing, filter_listings


def _listing(appl_no: str, normalized: str, stripped: str) -> PsgListing:
    return PsgListing(
        appl_no=appl_no,
        active_ingredient=normalized,
        normalized_name=normalized,
        stripped_name=stripped,
        psg_type="draft",
        route="Inhalation",
        dosage_form="Aerosol, Metered",
        rld_or_rs_numbers=[],
        recommended_date=None,
        pdf_url=f"http://example/PSG_{appl_no}.pdf",
        source_url="http://example/",
    )


ALBUTEROL = _listing("020503", "albuterol sulfate", "albuterol")
LEVALBUTEROL = _listing("021730", "levalbuterol tartrate", "levalbuterol")
BECLO = _listing("020911", "beclomethasone dipropionate", "beclomethasone dipropionate")
ROWS = [ALBUTEROL, LEVALBUTEROL, BECLO]


def test_pin_by_appl_no_is_exact() -> None:
    out = filter_listings(ROWS, appl_numbers=["020503", "020911"])
    assert {r.appl_no for r in out} == {"020503", "020911"}


def test_name_match_no_longer_pulls_substring() -> None:
    # "albuterol" must match albuterol (via stripped name) but NOT levalbuterol.
    out = filter_listings(ROWS, normalized_names=["albuterol"])
    assert {r.appl_no for r in out} == {"020503"}
    assert all(r.appl_no != "021730" for r in out)


def test_exact_canonical_name_match() -> None:
    out = filter_listings(ROWS, normalized_names=["levalbuterol tartrate"])
    assert {r.appl_no for r in out} == {"021730"}


def test_empty_filters_returns_all() -> None:
    assert filter_listings(ROWS) == ROWS
