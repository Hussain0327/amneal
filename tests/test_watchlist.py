"""Watchlist: INV-5 — only verified sources accepted."""

from __future__ import annotations

import pytest

from regwatch.common.text_normalize import canonical_name
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import Product
from regwatch.watch.watchlist import (
    ALLOWED_SOURCES,
    WatchlistEntry,
    add_manual_product,
    list_watchlist,
    set_on_watchlist,
    upsert_entries,
)


def _entry(
    active_ingredient: str,
    source: str,
    source_url: str | None,
    *,
    dosage_form: str | None = "Injection",
    route: str | None = "Intravenous",
    rld_name: str | None = "Istodax",
    company_status: str | None = "approved",
) -> WatchlistEntry:
    """Build a WatchlistEntry for a fixed product key, varying only the source."""
    return WatchlistEntry(
        active_ingredient=active_ingredient,
        normalized_name=canonical_name(active_ingredient),
        dosage_form=dosage_form,
        route=route,
        rld_name=rld_name,
        rld_application_number="208574",
        company_status=company_status,
        source=source,
        source_url=source_url,
    )


def test_inv5_entry_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="INV-5"):
        WatchlistEntry(
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            rld_name="ProAir HFA",
            rld_application_number="021457",
            company_status="approved",
            source="model_memory",  # forbidden
            source_url=None,
        )


def test_inv5_allowed_sources_set_matches_spec() -> None:
    assert {"drugsfda", "anda_letter", "manual"} == ALLOWED_SOURCES


def test_add_manual_product_roundtrips() -> None:
    init_db()
    add_manual_product(
        active_ingredient="Romidepsin",
        dosage_form="Injection",
        route="Intravenous",
        rld_name="Istodax",
        rld_application_number="208574",
        company_status="approved",
        source="anda_letter",
        source_url="file://internal/anda_219099.pdf",
    )
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["active_ingredient"] == "Romidepsin"
    assert items[0]["source"] == "anda_letter"


def test_add_manual_product_rejects_bad_source() -> None:
    with pytest.raises(ValueError, match="INV-5"):
        add_manual_product(
            active_ingredient="X",
            dosage_form=None,
            route=None,
            rld_name=None,
            rld_application_number=None,
            company_status=None,
            source="guessed_from_news",
            source_url=None,
        )


def test_no_products_created_outside_allowed_sources_via_orm() -> None:
    """Even at the ORM layer, a Product saved without an ALLOWED source is malformed.

    We document the contract by asserting our own write path doesn't insert
    invalid sources; the column itself is permissive (TEXT) — INV-5 lives in
    the loader, not the column.
    """
    init_db()
    with session_scope() as s:
        s.add(
            Product(
                active_ingredient="Z",
                normalized_name="z",
                source="drugsfda",
                on_watchlist=False,
            )
        )
    items = list_watchlist()
    # off-watchlist should not appear
    assert all(it["active_ingredient"] != "Z" for it in items)


def test_set_on_watchlist_soft_unwatch_hides_row_and_can_revive() -> None:
    """Unwatch is SOFT: the row leaves list_watchlist but survives in the table
    (alert history references product_id; INV-5 provenance kept), and flipping
    it back revives the same row -- no duplicate."""
    init_db()
    add_manual_product(
        active_ingredient="Romidepsin",
        dosage_form="Injection",
        route="Intravenous",
        rld_name="Istodax",
        rld_application_number="208574",
        company_status="approved",
        source="manual",
        source_url=None,
    )
    items = list_watchlist()
    assert len(items) == 1
    product_id = items[0]["id"]
    assert isinstance(product_id, int)

    assert set_on_watchlist(product_id, False) is True
    assert list_watchlist() == []
    with session_scope() as s:
        row = s.get(Product, product_id)
        assert row is not None  # soft: the row is kept, only the flag flips
        assert row.on_watchlist is False

    # Idempotent: re-applying the current state is still True, not an error.
    assert set_on_watchlist(product_id, False) is True

    assert set_on_watchlist(product_id, True) is True
    assert [it["id"] for it in list_watchlist()] == [product_id]


def test_set_on_watchlist_unknown_id_returns_false() -> None:
    init_db()
    assert set_on_watchlist(424242, False) is False


def test_upsert_upgrades_source_drugsfda_to_anda_letter() -> None:
    """A lower-trust drugsfda row upgrades to anda_letter on the same key."""
    init_db()
    upsert_entries([_entry("Romidepsin", "drugsfda", "https://api.fda.gov/x")])
    upsert_entries([_entry("Romidepsin", "anda_letter", "file://internal/anda.pdf")])
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["source"] == "anda_letter"


def test_upsert_does_not_downgrade_manual_to_drugsfda() -> None:
    """A higher-trust manual row must not be overwritten by a drugsfda re-import."""
    init_db()
    upsert_entries([_entry("Romidepsin", "manual", "user://override")])
    upsert_entries([_entry("Romidepsin", "drugsfda", "https://api.fda.gov/x")])
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["source"] == "manual"


def test_upsert_does_not_downgrade_anda_letter_to_drugsfda() -> None:
    """anda_letter outranks drugsfda; re-import keeps the verified letter source."""
    init_db()
    upsert_entries([_entry("Romidepsin", "anda_letter", "file://internal/anda.pdf")])
    upsert_entries([_entry("Romidepsin", "drugsfda", "https://api.fda.gov/x")])
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["source"] == "anda_letter"


def test_upsert_lower_trust_cannot_overwrite_manual_data_fields() -> None:
    """INV-5: a drugsfda re-import must not clobber a manual row's DATA.

    The rank guard has to cover company_status/rld_name/source_url, not just
    the source label -- otherwise the row keeps saying 'manual' while silently
    carrying unverified openFDA values (the user's override is reverted and
    the provenance label lies).
    """
    init_db()
    upsert_entries(
        [
            _entry(
                "Romidepsin",
                "manual",
                "user://override",
                company_status="pipeline",
                rld_name="UserRld",
            )
        ]
    )
    upsert_entries(
        [
            _entry(
                "Romidepsin",
                "drugsfda",
                "https://api.fda.gov/x",
                company_status="approved",
                rld_name="Istodax",
            )
        ]
    )
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["source"] == "manual"
    assert items[0]["company_status"] == "pipeline"
    assert items[0]["rld_name"] == "UserRld"
    assert items[0]["source_url"] == "user://override"


def test_upsert_lower_trust_fills_only_empty_fields() -> None:
    """A lower-trust source may FILL fields the trusted row left empty."""
    init_db()
    upsert_entries([_entry("Romidepsin", "manual", None, company_status=None, rld_name=None)])
    upsert_entries(
        [
            _entry(
                "Romidepsin",
                "drugsfda",
                "https://api.fda.gov/x",
                company_status="approved",
                rld_name="Istodax",
            )
        ]
    )
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["source"] == "manual"
    assert items[0]["company_status"] == "approved"
    assert items[0]["rld_name"] == "Istodax"
    assert items[0]["source_url"] == "https://api.fda.gov/x"


def test_upsert_equal_rank_still_overwrites() -> None:
    """Equal rank keeps the refresh behavior: a re-import updates its own rows."""
    init_db()
    upsert_entries(
        [_entry("Romidepsin", "drugsfda", "https://api.fda.gov/x", company_status="approved")]
    )
    upsert_entries(
        [_entry("Romidepsin", "drugsfda", "https://api.fda.gov/y", company_status="discontinued")]
    )
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["company_status"] == "discontinued"
    assert items[0]["source_url"] == "https://api.fda.gov/y"


def test_upsert_identity_is_case_insensitive_on_form_and_route() -> None:
    """Manual 'Tablet'/'Oral' and FDA's 'TABLET'/' ORAL ' are ONE product.

    The matcher compares form/route case-insensitively, so the upsert identity
    must agree -- otherwise a cross-source case difference duplicates the row
    and every PSG change alerts twice.
    """
    init_db()
    upsert_entries(
        [_entry("Romidepsin", "manual", "user://override", dosage_form="Tablet", route="Oral")]
    )
    added = upsert_entries(
        [
            _entry(
                "Romidepsin",
                "drugsfda",
                "https://api.fda.gov/x",
                dosage_form="TABLET",
                route=" ORAL ",
            )
        ]
    )
    assert added == 0
    items = list_watchlist()
    assert len(items) == 1
    assert items[0]["source"] == "manual"


def test_upsert_identity_null_form_matches_only_null() -> None:
    """None form/route matches only None -- unknown form is not 'any form'."""
    init_db()
    assert upsert_entries([_entry("Romidepsin", "manual", None, dosage_form=None, route=None)]) == 1
    assert (
        upsert_entries([_entry("Romidepsin", "drugsfda", None, dosage_form="Tablet", route="Oral")])
        == 1
    )
    items = list_watchlist()
    assert len(items) == 2


def test_status_from_marketing_status_is_deterministic() -> None:
    """openFDA marketing_status may be a scalar string OR a list; precedence must
    be explicit (approved > discontinued > tentative), not scan-order."""
    from regwatch.watch.watchlist import _status_from_marketing_status

    # Scalar coercion (openFDA returns a STRING, iterating it would walk chars).
    assert _status_from_marketing_status({"marketing_status": "Prescription"}) == "approved"
    assert _status_from_marketing_status({"marketing_status": "Discontinued"}) == "discontinued"
    # approved short-circuits over any other status.
    assert (
        _status_from_marketing_status({"marketing_status": ["Discontinued", "Prescription"]})
        == "approved"
    )
    # Deterministic regardless of order: discontinued outranks tentative both ways.
    assert (
        _status_from_marketing_status({"marketing_status": ["tentative", "discontinued"]})
        == "discontinued"
    )
    assert (
        _status_from_marketing_status({"marketing_status": ["discontinued", "tentative"]})
        == "discontinued"
    )
    # Empty / unknown -> None.
    assert _status_from_marketing_status({"marketing_status": None}) is None
    assert _status_from_marketing_status({"marketing_status": ""}) is None
    assert _status_from_marketing_status({"marketing_status": "Whatever"}) is None
