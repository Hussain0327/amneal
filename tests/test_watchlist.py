"""Watchlist: INV-5 — only verified sources accepted."""

from __future__ import annotations

import pytest

from regwatch.store.db import init_db, session_scope
from regwatch.store.models import Product
from regwatch.watch.watchlist import (
    ALLOWED_SOURCES,
    WatchlistEntry,
    add_manual_product,
    list_watchlist,
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
