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


def test_fuzzy_emits_all_products_above_threshold() -> None:
    m = match_listings(
        [_listing("Beclomethasone Dipropionate")],
        [
            _product("Beclometasone Dipropionate", prod_id=1),
            _product("Beclomethasone Diproprionate", prod_id=2),
        ],
    )
    assert {x.product["id"] for x in m if x.rationale == "fuzzy"} == {1, 2}


def test_combination_component_match() -> None:
    # Combo PSG; product is only one component.
    m = match_listings(
        [_listing("Hydrocodone Bitartrate; Acetaminophen")],
        [_product("Acetaminophen")],
    )
    assert any(x.rationale in {"combo_component", "stripped"} for x in m)


def test_distinct_electrolytes_do_not_cross_match() -> None:
    # potassium/sodium/calcium chloride all strip to "" -> the empty-key guard
    # refuses the false stripped-exact (0.92) match they used to collapse into.
    m = match_listings([_listing("Potassium Chloride")], [_product("Sodium Chloride")])
    assert m == []


def test_fuzzy_rejects_distinct_near_name_generics() -> None:
    # prednisone vs prednisolone score 90.9 -- below the 92 floor, so the
    # wrong-drug fuzzy match is refused (they share no canonical/stripped key).
    m = match_listings([_listing("Prednisone")], [_product("Prednisolone")])
    assert m == []


def _listing_form(name: str, route: str | None, form: str | None) -> PsgListing:
    from regwatch.common.text_normalize import canonical_name, stripped_name

    return PsgListing(
        appl_no="222222",
        active_ingredient=name,
        normalized_name=canonical_name(name),
        stripped_name=stripped_name(name),
        psg_type="final",
        route=route,
        dosage_form=form,
        rld_or_rs_numbers=[],
        recommended_date=None,
        pdf_url="http://example/PSG_222222.pdf",
        source_url="http://example/index.cfm",
    )


def _product_form(name: str, route: str | None, form: str | None, prod_id: int) -> dict:
    from regwatch.common.text_normalize import canonical_name

    return {
        "id": prod_id,
        "active_ingredient": name,
        "normalized_name": canonical_name(name),
        "route": route,
        "dosage_form": form,
    }


def test_form_route_filters_fanout_to_matching_product() -> None:
    # One PSG (oral tablet) against two same-ingredient products differing only by
    # form/route: only the form-compatible product matches (no fan-out spam).
    listing = _listing_form("Metformin", route="Oral", form="Tablet")
    products = [
        _product_form("Metformin", route="Oral", form="Tablet, Extended Release", prod_id=1),
        _product_form("Metformin", route="Intravenous", form="Injection", prod_id=2),
    ]
    m = match_listings([listing], products)
    assert {x.product["id"] for x in m} == {1}  # prefix-compatible only; injection dropped


def test_null_form_falls_back_to_ingredient_match() -> None:
    # A product with no form/route metadata must NOT be dropped (INV-4: a missing
    # attribute never silences a real match).
    listing = _listing_form("Metformin", route="Oral", form="Tablet")
    products = [_product_form("Metformin", route=None, form=None, prod_id=9)]
    m = match_listings([listing], products)
    assert {x.product["id"] for x in m} == {9}


def test_same_ingredient_two_products_both_alert_when_compatible() -> None:
    # Distinct products that ARE both form-compatible still both match (the
    # per-product fan-out an analyst wants is preserved).
    listing = _listing_form("Metformin", route=None, form=None)
    products = [
        _product_form("Metformin", route="Oral", form="Tablet", prod_id=1),
        _product_form("Metformin", route="Oral", form="Tablet, Extended Release", prod_id=2),
    ]
    m = match_listings([listing], products)
    assert {x.product["id"] for x in m} == {1, 2}


def test_unrelated_ingredient_does_not_match() -> None:
    m = match_listings(
        [_listing("Romidepsin")],
        [_product("Albuterol")],
    )
    assert m == []


def test_empty_watchlist_returns_empty() -> None:
    m = match_listings([_listing("Albuterol")], [])
    assert m == []
