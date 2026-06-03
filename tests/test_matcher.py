"""Matcher: normalized + fuzzy match of PSG listings against watchlist."""

from __future__ import annotations

from regwatch.ingest.psg_crawler import PsgListing
from regwatch.watch.matcher import match_listings


def _listing(name: str, appl_no: str = "111111") -> PsgListing:
    from regwatch.common.text_normalize import canonical_name, stripped_name

    return PsgListing(
        appl_no=appl_no,
        active_ingredient=name,
        normalized_name=canonical_name(name),
        stripped_name=stripped_name(name),
        psg_type="draft",
        route=None,
        dosage_form=None,
        rld_or_rs_numbers=[],
        recommended_date=None,
        pdf_url=f"http://example/PSG_{appl_no}.pdf",
        source_url="http://example/index.cfm",
    )


def _product(name: str, prod_id: int = 1) -> dict:
    from regwatch.common.text_normalize import canonical_name

    return {
        "id": prod_id,
        "active_ingredient": name,
        "normalized_name": canonical_name(name),
    }


def test_canonical_exact_match() -> None:
    m = match_listings([_listing("Albuterol Sulfate")], [_product("Albuterol Sulfate")])
    assert len(m) == 1
    assert m[0].confidence == 1.0
    assert m[0].rationale == "canonical"


def test_stripped_match_when_salt_differs() -> None:
    # Listing has the salt form; product is salt-stripped.
    m = match_listings([_listing("Albuterol Sulfate")], [_product("Albuterol")])
    assert len(m) == 1
    assert m[0].rationale in {"canonical", "stripped"}


def test_fuzzy_handles_minor_typos() -> None:
    m = match_listings(
        [_listing("Beclomethasone Dipropionate")],
        [_product("Beclometasone Dipropionate")],  # missing 'h'
    )
    assert len(m) == 1
    assert m[0].rationale in {"fuzzy", "stripped"}
    assert m[0].confidence > 0.85


def test_combination_component_match() -> None:
    # Combo PSG; product is only one component.
    m = match_listings(
        [_listing("Hydrocodone Bitartrate; Acetaminophen")],
        [_product("Acetaminophen")],
    )
    assert any(x.rationale in {"combo_component", "stripped"} for x in m)


def test_unrelated_ingredient_does_not_match() -> None:
    m = match_listings(
        [_listing("Romidepsin")],
        [_product("Albuterol")],
    )
    assert m == []


def test_empty_watchlist_returns_empty() -> None:
    m = match_listings([_listing("Albuterol")], [])
    assert m == []
