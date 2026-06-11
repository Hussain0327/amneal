"""Write-through persistence tests for the White-Paper structured sources.

Schema comes from the Alembic chain (``init_db`` upgrades to head), so these
tests also prove migration 0005 and the SQLModel definitions stay in sync.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ObExclusivity, ObPatent, ObProduct, SplDocument
from regwatch.store.whitepaper_sources import (
    normalize_appl_no,
    persist_ob_exclusivities,
    persist_ob_patents,
    persist_ob_products,
    persist_spl_document,
)

FETCHED_AT = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
SETID = "11111111-2222-3333-4444-555555555555"


def test_normalize_appl_no_accepts_all_input_forms() -> None:
    assert normalize_appl_no("NDA 021446") == "021446"
    assert normalize_appl_no("021446") == "021446"
    assert normalize_appl_no("N021446") == "021446"
    assert normalize_appl_no("anda076204") == "076204"
    assert normalize_appl_no("21446") == "021446"
    with pytest.raises(ValueError, match="unparseable"):
        normalize_appl_no("no digits here")


def test_persist_ob_products_writes_rows_with_freshness() -> None:
    init_db()
    rows = [
        {
            "appl_type": "N",
            "appl_no": "020503",
            "product_no": "001",
            "ingredient": "ALBUTEROL SULFATE",
            "trade_name": "PROAIR HFA",
            "strength": "0.09MG/INH",
            "rld": "RLD",
            "rs": "RS",
            "te_code": "AB",
        }
    ]
    persisted = persist_ob_products(
        "NDA 020503", rows, fetched_at=FETCHED_AT, source_url="https://example.invalid/ob"
    )

    assert len(persisted) == 1
    assert persisted[0].id is not None
    assert persisted[0].appl_no == "020503"
    assert persisted[0].normalized_name == "albuterol sulfate"
    # The returned instance keeps the aware value; SQLite stores it naive.
    assert persisted[0].last_fetched_at == FETCHED_AT
    assert persisted[0].source_url == "https://example.invalid/ob"

    with session_scope() as s:
        stored = s.execute(select(ObProduct).where(ObProduct.appl_no == "020503")).scalars().one()
        assert stored.last_fetched_at == FETCHED_AT.replace(tzinfo=None)


def test_persist_ob_patents_replaces_previous_snapshot() -> None:
    init_db()
    first = [
        {"appl_no": "020503", "product_no": "001", "patent_no": "6868851"},
        {"appl_no": "020503", "product_no": "002", "patent_no": "7105152"},
    ]
    persist_ob_patents("020503", first, fetched_at=FETCHED_AT)
    # An unrelated application's snapshot must survive the replace.
    persist_ob_patents(
        "020911", [{"appl_no": "020911", "patent_no": "5776434"}], fetched_at=FETCHED_AT
    )

    refetch = [{"appl_no": "020503", "product_no": "001", "patent_no": "6868851"}]
    later = datetime(2026, 6, 11, 9, 0, 0, tzinfo=UTC)
    persist_ob_patents("NDA020503", refetch, fetched_at=later)

    with session_scope() as s:
        mine = s.execute(select(ObPatent).where(ObPatent.appl_no == "020503")).scalars().all()
        other = s.execute(select(ObPatent).where(ObPatent.appl_no == "020911")).scalars().all()
        assert [p.patent_no for p in mine] == ["6868851"]
        assert mine[0].last_fetched_at == later.replace(tzinfo=None)
        assert [p.patent_no for p in other] == ["5776434"]


def test_persist_ob_exclusivities_round_trip() -> None:
    init_db()
    rows = [
        {
            "appl_no": "020503",
            "product_no": "001",
            "exclusivity_code": "NCE",
            "exclusivity_date": "Oct 29, 2009",
        }
    ]
    persisted = persist_ob_exclusivities("NDA020503", rows, fetched_at=FETCHED_AT)
    assert persisted[0].exclusivity_code == "NCE"

    with session_scope() as s:
        stored = (
            s.execute(select(ObExclusivity).where(ObExclusivity.appl_no == "020503"))
            .scalars()
            .all()
        )
        assert len(stored) == 1
        assert stored[0].exclusivity_date == "Oct 29, 2009"


def test_persist_ob_products_replace_is_idempotent() -> None:
    init_db()
    rows = [{"appl_no": "020503", "product_no": "001", "ingredient": "ALBUTEROL SULFATE"}]
    persist_ob_products("020503", rows, fetched_at=FETCHED_AT)
    persist_ob_products("020503", rows, fetched_at=FETCHED_AT)

    with session_scope() as s:
        stored = s.execute(select(ObProduct).where(ObProduct.appl_no == "020503")).scalars().all()
        assert len(stored) == 1


def test_persist_spl_document_upserts_by_setid() -> None:
    init_db()
    created = persist_spl_document(
        setid=SETID,
        appl_no="NDA020503",
        title="ALBUTEROL SULFATE AEROSOL, METERED",
        published="Dec 18, 2025",
        source_url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={SETID}",
        fetched_at=FETCHED_AT,
    )
    assert created.id is not None
    assert created.appl_no == "020503"

    later = datetime(2026, 6, 11, 9, 0, 0, tzinfo=UTC)
    updated = persist_spl_document(
        setid=SETID,
        appl_no="020503",
        title="ALBUTEROL SULFATE AEROSOL, METERED [REVISED]",
        published="Jun 10, 2026",
        source_url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={SETID}",
        fetched_at=later,
    )
    assert updated.id == created.id

    with session_scope() as s:
        stored = s.execute(select(SplDocument).where(SplDocument.setid == SETID)).scalars().all()
        assert len(stored) == 1
        assert stored[0].title == "ALBUTEROL SULFATE AEROSOL, METERED [REVISED]"
        assert stored[0].last_fetched_at == later.replace(tzinfo=None)
