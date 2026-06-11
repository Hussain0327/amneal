"""Write-through persistence tests for the White-Paper structured sources.

Schema comes from the Alembic chain (``init_db`` upgrades to head), so these
tests also prove migrations 0005/0006 and the SQLModel definitions stay in
sync.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from regwatch.sources import orange_book
from regwatch.sources.orange_book import OrangeBookRows
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import ObExclusivity, ObPatent, ObProduct, SplDocument
from regwatch.store.whitepaper_sources import (
    ObSnapshot,
    SplSnapshot,
    normalize_appl_no,
    normalize_appl_type,
    persist_ob_exclusivities,
    persist_ob_patents,
    persist_ob_products,
    persist_spl_document,
    persist_whitepaper_snapshot,
)
from regwatch.whitepaper.populator import build_whitepaper
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources

FETCHED_AT = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
SETID = "11111111-2222-3333-4444-555555555555"


def _ob_snapshot(**overrides: object) -> ObSnapshot:
    base: dict[str, object] = {
        "application_number": "020503",
        "appl_type": "NDA",
        "product_rows": [
            {"appl_no": "020503", "product_no": "001", "ingredient": "ALBUTEROL SULFATE"}
        ],
        "patent_rows": [{"appl_no": "020503", "patent_no": "RE37410"}],
        "exclusivity_rows": [{"appl_no": "020503", "exclusivity_code": "NCE"}],
        "products_fetched_at": FETCHED_AT,
        "patents_fetched_at": FETCHED_AT,
        "exclusivities_fetched_at": FETCHED_AT,
        "source_url": "https://example.invalid/ob",
    }
    base.update(overrides)
    return ObSnapshot(**base)  # type: ignore[arg-type]


def test_normalize_appl_no_accepts_all_input_forms() -> None:
    assert normalize_appl_no("NDA 021446") == "021446"
    assert normalize_appl_no("021446") == "021446"
    assert normalize_appl_no("N021446") == "021446"
    assert normalize_appl_no("anda076204") == "076204"
    assert normalize_appl_no("21446") == "021446"
    with pytest.raises(ValueError, match="unparseable"):
        normalize_appl_no("no digits here")


def test_normalize_appl_type_accepts_letters_and_prefixes() -> None:
    assert normalize_appl_type("N") == "NDA"
    assert normalize_appl_type("a") == "ANDA"
    assert normalize_appl_type("BLA") == "BLA"
    assert normalize_appl_type(" nda ") == "NDA"
    with pytest.raises(ValueError, match="unparseable"):
        normalize_appl_type("X")


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
        "NDA 020503",
        rows,
        appl_type="NDA",
        fetched_at=FETCHED_AT,
        source_url="https://example.invalid/ob",
    )

    assert len(persisted) == 1
    assert persisted[0].id is not None
    assert persisted[0].appl_no == "020503"
    assert persisted[0].appl_type == "NDA"  # the OB letter normalizes to the full prefix
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
    persist_ob_patents("020503", first, appl_type="NDA", fetched_at=FETCHED_AT)
    # An unrelated application's snapshot must survive the replace.
    persist_ob_patents(
        "020911",
        [{"appl_no": "020911", "patent_no": "5776434"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )

    refetch = [{"appl_no": "020503", "product_no": "001", "patent_no": "6868851"}]
    later = datetime(2026, 6, 11, 9, 0, 0, tzinfo=UTC)
    persist_ob_patents("NDA020503", refetch, appl_type="NDA", fetched_at=later)

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
    persisted = persist_ob_exclusivities("NDA020503", rows, appl_type="NDA", fetched_at=FETCHED_AT)
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
    persist_ob_products("020503", rows, appl_type="NDA", fetched_at=FETCHED_AT)
    persist_ob_products("020503", rows, appl_type="NDA", fetched_at=FETCHED_AT)

    with session_scope() as s:
        stored = s.execute(select(ObProduct).where(ObProduct.appl_no == "020503")).scalars().all()
        assert len(stored) == 1


# --------------------------- A4: typed replace-snapshots ---------------------------
def test_replace_snapshot_never_wipes_the_other_type_sharing_digits() -> None:
    # NDA 020503 and ANDA 020503 are DIFFERENT applications. Persisting one
    # type's snapshot previously deleted by appl_no alone and wiped the other's
    # durable patent/exclusivity provenance rows.
    init_db()
    persist_ob_patents(
        "020503",
        [{"appl_no": "020503", "patent_no": "1111111"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )
    persist_ob_exclusivities(
        "020503",
        [{"appl_no": "020503", "exclusivity_code": "NCE"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )
    persist_ob_products(
        "020503",
        [{"appl_no": "020503", "product_no": "001", "ingredient": "ALBUTEROL SULFATE"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )

    # The digit-colliding ANDA replaces ITS snapshot only.
    persist_ob_patents(
        "020503",
        [{"appl_no": "020503", "patent_no": "2222222"}],
        appl_type="ANDA",
        fetched_at=FETCHED_AT,
    )
    persist_ob_exclusivities(
        "020503",
        [{"appl_no": "020503", "exclusivity_code": "PE"}],
        appl_type="ANDA",
        fetched_at=FETCHED_AT,
    )
    persist_ob_products(
        "020503",
        [{"appl_no": "020503", "product_no": "001", "ingredient": "ALBUTEROL SULFATE"}],
        appl_type="ANDA",
        fetched_at=FETCHED_AT,
    )

    with session_scope() as s:
        patents = s.execute(select(ObPatent).where(ObPatent.appl_no == "020503")).scalars().all()
        excl = (
            s.execute(select(ObExclusivity).where(ObExclusivity.appl_no == "020503"))
            .scalars()
            .all()
        )
        products = s.execute(select(ObProduct).where(ObProduct.appl_no == "020503")).scalars().all()
        assert sorted(p.patent_no for p in patents) == ["1111111", "2222222"]
        assert sorted(e.exclusivity_code for e in excl) == ["NCE", "PE"]
        assert sorted(p.appl_type or "" for p in products) == ["ANDA", "NDA"]


def test_replace_snapshot_retires_legacy_untyped_rows() -> None:
    # Rows persisted before the appl_type column existed are NULL-typed; the
    # first typed replace for their number retires them (no stale evidence).
    init_db()
    with session_scope() as s:
        s.add(ObPatent(appl_no="020503", patent_no="9999999", last_fetched_at=FETCHED_AT))
        s.add(ObExclusivity(appl_no="020503", exclusivity_code="OLD", last_fetched_at=FETCHED_AT))

    persist_ob_patents(
        "020503",
        [{"appl_no": "020503", "patent_no": "1111111"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )
    persist_ob_exclusivities(
        "020503",
        [{"appl_no": "020503", "exclusivity_code": "NCE"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )

    with session_scope() as s:
        patents = s.execute(select(ObPatent).where(ObPatent.appl_no == "020503")).scalars().all()
        excl = (
            s.execute(select(ObExclusivity).where(ObExclusivity.appl_no == "020503"))
            .scalars()
            .all()
        )
        assert [p.patent_no for p in patents] == ["1111111"]
        assert [e.exclusivity_code for e in excl] == ["NCE"]


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


# --------------------------- A9: one atomic snapshot transaction ---------------------------
def test_persist_whitepaper_snapshot_round_trip() -> None:
    init_db()
    persist_whitepaper_snapshot(
        ob=_ob_snapshot(),
        spl=SplSnapshot(
            setid=SETID,
            appl_no="020503",
            title="ALBUTEROL",
            published="Jun 10, 2026",
            source_url="https://example.invalid/spl",
            fetched_at=FETCHED_AT,
        ),
    )
    with session_scope() as s:
        assert len(s.execute(select(ObProduct)).scalars().all()) == 1
        assert len(s.execute(select(ObPatent)).scalars().all()) == 1
        assert len(s.execute(select(ObExclusivity)).scalars().all()) == 1
        assert len(s.execute(select(SplDocument)).scalars().all()) == 1


def test_persist_whitepaper_snapshot_rolls_back_atomically_on_mid_failure() -> None:
    # The four replace-snapshots previously ran as four independent committed
    # transactions: a mid-run failure left the products replaced but the rest
    # stale (half a snapshot as durable evidence). One transaction now: the
    # bad SPL appl_no fails AFTER the OB replaces, and everything rolls back.
    init_db()
    persist_whitepaper_snapshot(ob=_ob_snapshot())

    new_rows = _ob_snapshot(
        product_rows=[
            {"appl_no": "020503", "product_no": "002", "ingredient": "ALBUTEROL SULFATE"}
        ],
        patent_rows=[{"appl_no": "020503", "patent_no": "7777777"}],
    )
    bad_spl = SplSnapshot(
        setid=SETID,
        appl_no="no digits at all",  # normalize_appl_no raises inside the transaction
        title=None,
        published=None,
        source_url=None,
        fetched_at=FETCHED_AT,
    )
    with pytest.raises(ValueError, match="unparseable"):
        persist_whitepaper_snapshot(ob=new_rows, spl=bad_spl)

    with session_scope() as s:
        products = s.execute(select(ObProduct)).scalars().all()
        patents = s.execute(select(ObPatent)).scalars().all()
        # The previous snapshot survives intact — nothing was half-replaced.
        assert [p.product_no for p in products] == ["001"]
        assert [p.patent_no for p in patents] == ["RE37410"]
        assert s.execute(select(SplDocument)).scalars().all() == []


# --------------------------- missing ZIP member never wipes the snapshot ---------------------------
def test_missing_zip_member_retains_previous_durable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # patent.txt/exclusivity.txt absent from the ZIP degrade to empty rows;
    # treating that as "queried and absent" REPLACE-deleted the application's
    # previously persisted ObPatent/ObExclusivity provenance rows — exactly
    # the wipe the replace-on-success rule forbids (INV-5).
    install_fake_sources(monkeypatch)
    persist_ob_patents(
        APPL_NO,
        [{"appl_no": APPL_NO, "patent_no": "RE37410"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )
    persist_ob_exclusivities(
        APPL_NO,
        [{"appl_no": APPL_NO, "exclusivity_code": "NCE"}],
        appl_type="NDA",
        fetched_at=FETCHED_AT,
    )

    def degraded(application_number: str, *, client: object = None) -> OrangeBookRows:
        return OrangeBookRows(rows=[], fetched_at=FETCHED_AT, member_missing=True)

    monkeypatch.setattr(orange_book, "patent_rows", degraded)
    monkeypatch.setattr(orange_book, "exclusivity_rows", degraded)
    result = build_whitepaper(RLD_NAME, APPL_NO)

    with session_scope() as s:
        patents = s.execute(select(ObPatent).where(ObPatent.appl_no == APPL_NO)).scalars().all()
        excl = (
            s.execute(select(ObExclusivity).where(ObExclusivity.appl_no == APPL_NO)).scalars().all()
        )
        assert [p.patent_no for p in patents] == ["RE37410"]
        assert [e.exclusivity_code for e in excl] == ["NCE"]

    # The cells and warnings say "file unavailable", never "queried and absent".
    cells = {c["id"]: c for section in result["sections"] for c in section["cells"]}
    assert "unavailable in this Orange Book download" in (cells["patents"]["note"] or "")
    assert "unavailable in this Orange Book download" in (cells["first_to_market"]["note"] or "")
    assert any("patent.txt unavailable" in w for w in result["spine"]["warnings"])
    assert any("exclusivity.txt unavailable" in w for w in result["spine"]["warnings"])


# --------------------------- A10: per-rowset freshness ---------------------------
def test_snapshot_rows_carry_their_own_rowset_timestamps() -> None:
    init_db()
    products_at = datetime(2026, 6, 9, 8, 0, 0, tzinfo=UTC)
    patents_at = datetime(2026, 6, 10, 9, 0, 0, tzinfo=UTC)
    excl_at = datetime(2026, 6, 11, 10, 0, 0, tzinfo=UTC)
    persist_whitepaper_snapshot(
        ob=_ob_snapshot(
            products_fetched_at=products_at,
            patents_fetched_at=patents_at,
            exclusivities_fetched_at=excl_at,
        )
    )
    with session_scope() as s:
        product = s.execute(select(ObProduct)).scalars().one()
        patent = s.execute(select(ObPatent)).scalars().one()
        excl = s.execute(select(ObExclusivity)).scalars().one()
        assert product.last_fetched_at == products_at.replace(tzinfo=None)
        assert patent.last_fetched_at == patents_at.replace(tzinfo=None)
        assert excl.last_fetched_at == excl_at.replace(tzinfo=None)
