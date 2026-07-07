"""Store tests for durable White-Paper runs + the attributed analyst overlay.

The compliance spine of Phase 2 (INV-3/INV-5): the generated layer is inserted
once and never mutated -- every overlay edit lives in whitepaper_input -- and
``sections_sha256`` must stay verifiable against the STORED sections forever,
including when the populate result carried datetime objects inside evidence
(the canonical round-trip must serialize them exactly as the fingerprint did).
Runs against the per-test tmp SQLite from conftest, never the ambient DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

import regwatch.store.whitepaper_runs as wr
from regwatch.store.db import get_engine, init_db, session_scope
from regwatch.store.models import WhitepaperInput, WhitepaperRun
from regwatch.whitepaper.populator import result_fingerprint
from tests.conftest import create_user

APPL_NO = "020503"
AUDIT_ID = 1234
# Real CELL_SPECS ids: an analyst cell and a populated/auto cell.
ANALYST_CELL = "rd_center"
AUTO_CELL = "product_name"

FETCHED = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _evidence(source: str, *, fetched_at: Any) -> dict[str, Any]:
    return {
        "source": source,
        "locator": f"products.txt (appl {APPL_NO})",
        "source_url": "https://example/ob",
        "fetched_at": fetched_at,
        "page": None,
        "section": None,
        "snippet": "ALBUTEROL SULFATE",
    }


def _result(*, appl_no: str = APPL_NO, ingredient: str = "ALBUTEROL SULFATE") -> dict[str, Any]:
    """A build_whitepaper-shaped payload whose evidence carries REAL datetime
    objects (the JSON-serialization hazard create_run must neutralize)."""
    sections = [
        {
            "title": "Proposed Generic Product",
            "cells": [
                {
                    "id": AUTO_CELL,
                    "label": "Product Name",
                    "mode": "auto",
                    "status": "populated",
                    "value": ingredient,
                    "evidence": [_evidence("Orange Book", fetched_at=FETCHED)],
                    "note": None,
                },
                {
                    "id": ANALYST_CELL,
                    "label": "R&D Center",
                    "mode": "manual",
                    "status": "analyst_input_required",
                    "value": None,
                    "evidence": [],
                    "note": None,
                },
                {
                    "id": "drug_shortage",
                    "label": "Drug Shortage",
                    "mode": "auto",
                    "status": "verified_absent",
                    "value": "No",
                    "evidence": [_evidence("openFDA shortages", fetched_at=FETCHED)],
                    "note": None,
                },
            ],
        }
    ]
    return {
        "spine": {
            "application_number": appl_no,
            "application_type": "NDA",
            "ingredient": ingredient,
            "normalized_name": ingredient.lower(),
            "product_numbers": ["001"],
            "setid": "abc-def-123",
            "spl_candidates": [],
            "warnings": [],
        },
        "sections": sections,
        "warnings": ["orange book fetch degraded"],
        "audit_id": AUDIT_ID,
    }


def _create(user_id: int, **kwargs: Any) -> int:
    return wr.create_run(user_id=user_id, rld_name_input="Proventil HFA", result=_result(**kwargs))


# ---------------------------------------------------------------------------
# create / get round-trip + the fingerprint-across-storage invariant
# ---------------------------------------------------------------------------


def test_create_and_get_run_round_trip() -> None:
    uid = create_user()
    run_id = _create(uid)

    detail = wr.get_run(run_id)
    assert detail is not None
    assert detail.id == run_id
    assert detail.rld_name_input == "Proventil HFA"
    assert detail.application_number == APPL_NO
    assert detail.application_type == "NDA"
    assert detail.ingredient == "ALBUTEROL SULFATE"
    assert detail.normalized_name == "albuterol sulfate"
    assert detail.source_audit_id == AUDIT_ID
    assert detail.status == "draft"
    assert detail.created_by_user_id == uid
    assert detail.created_by == "Test Analyst"
    assert detail.finalized_at is None and detail.finalized_by_user_id is None
    assert detail.warnings == ["orange book fetch degraded"]
    assert detail.spine["setid"] == "abc-def-123"
    # Counts describe the immutable generated layer.
    assert (detail.populated_count, detail.analyst_input_count, detail.verified_absent_count) == (
        1,
        1,
        1,
    )
    # The datetime evidence landed as the fingerprint's isoformat string.
    stored_evidence = detail.sections[0]["cells"][0]["evidence"][0]
    assert stored_evidence["fetched_at"] == FETCHED.isoformat()
    assert detail.inputs == []


def test_fingerprint_survives_storage_with_datetime_evidence() -> None:
    """MANDATORY invariant: the sha256 computed over the in-memory sections
    (datetime objects included) must verify against the sections as reloaded
    from the DB -- otherwise finalize/render could never re-verify (INV-3)."""
    uid = create_user()
    result = _result()
    fingerprint_before = result_fingerprint(result["sections"])

    run_id = wr.create_run(user_id=uid, rld_name_input="Proventil HFA", result=result)
    detail = wr.get_run(run_id)
    assert detail is not None
    assert detail.sections_sha256 == fingerprint_before
    assert result_fingerprint(detail.sections) == detail.sections_sha256


def test_create_run_refuses_missing_audit_id() -> None:
    uid = create_user()
    result = _result()
    del result["audit_id"]
    with pytest.raises(ValueError, match="audit_id"):
        wr.create_run(user_id=uid, rld_name_input="Proventil HFA", result=result)


def test_get_run_missing_returns_none() -> None:
    init_db()
    assert wr.get_run(99999) is None


# ---------------------------------------------------------------------------
# list_runs: org-shared, ordering, filters, pagination, payload discipline
# ---------------------------------------------------------------------------


def test_list_runs_is_org_shared_and_newest_updated_first() -> None:
    uid_a = create_user()
    uid_b = create_user("other@example.com", display_name="Other Analyst")
    run_a = _create(uid_a)
    run_b = _create(uid_b, appl_no="021457", ingredient="FLUTICASONE")

    summaries, total = wr.list_runs(limit=10, offset=0)
    assert total == 2
    # No user filter: both analysts' runs are visible, with author attribution.
    assert {s.created_by for s in summaries} == {"Test Analyst", "Other Analyst"}
    # Newest updated_at first: touching run_a's overlay moves it to the top.
    wr.upsert_input(run_id=run_a, cell_id=ANALYST_CELL, value="Amneal NY", user_id=uid_b)
    summaries, _ = wr.list_runs(limit=10, offset=0)
    assert [s.id for s in summaries] == [run_a, run_b]
    assert summaries[0].inputs_count == 1
    assert summaries[1].inputs_count == 0


def test_list_runs_filters_and_pagination() -> None:
    uid = create_user()
    run_a = _create(uid)
    run_b = _create(uid, appl_no="021457", ingredient="FLUTICASONE")
    wr.finalize_run(run_id=run_b, user_id=uid)

    # Filter accepts any normalize_appl_no form (six digits stored).
    by_appl, total = wr.list_runs(limit=10, offset=0, application_number="NDA 020503")
    assert total == 1 and [s.id for s in by_appl] == [run_a]

    by_name, total = wr.list_runs(limit=10, offset=0, normalized_name="fluticasone")
    assert total == 1 and [s.id for s in by_name] == [run_b]

    by_status, total = wr.list_runs(limit=10, offset=0, status="final")
    assert total == 1 and [s.id for s in by_status] == [run_b]

    # Pagination: total stays the same-filter COUNT, not the page size.
    page, total = wr.list_runs(limit=1, offset=0)
    assert len(page) == 1 and total == 2
    page2, _ = wr.list_runs(limit=1, offset=1)
    assert page2[0].id != page[0].id


def test_list_runs_summary_query_never_selects_json_payloads() -> None:
    """The list query must select explicit columns: the JSON payloads are the
    large immutable blobs and belong only to get_run."""
    uid = create_user()
    _create(uid)
    statements: list[str] = []
    engine = get_engine()

    def _capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        summaries, total = wr.list_runs(limit=10, offset=0)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert total == 1 and len(summaries) == 1
    selects = [
        s for s in statements if "whitepaper_run" in s and s.lstrip().upper().startswith("SELECT")
    ]
    assert selects, "expected at least one SELECT over whitepaper_run"
    for stmt in selects:
        for payload in ("sections_json", "spine_json", "warnings_json"):
            assert payload not in stmt, f"summary query selected {payload}"


# ---------------------------------------------------------------------------
# Overlay upsert/clear semantics
# ---------------------------------------------------------------------------


def test_upsert_input_round_trip_updates_in_place() -> None:
    uid = create_user()
    other = create_user("other@example.com", display_name="Other Analyst")
    run_id = _create(uid)

    view = wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="Amneal NY", user_id=uid)
    assert view is not None
    assert (view.cell_id, view.value, view.author) == (ANALYST_CELL, "Amneal NY", "Test Analyst")

    # Second upsert REPLACES: one row per (run, cell), author + time travel.
    view2 = wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="Amneal NJ", user_id=other)
    assert view2 is not None and view2.value == "Amneal NJ" and view2.author == "Other Analyst"

    detail = wr.get_run(run_id)
    assert detail is not None
    assert len(detail.inputs) == 1
    assert detail.inputs[0].value == "Amneal NJ"
    assert detail.inputs[0].author_user_id == other


def test_unique_run_cell_enforced_at_db_level() -> None:
    uid = create_user()
    run_id = _create(uid)
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(WhitepaperInput(run_id=run_id, cell_id=ANALYST_CELL, value="a", author_user_id=uid))
        s.add(WhitepaperInput(run_id=run_id, cell_id=ANALYST_CELL, value="b", author_user_id=uid))


def test_upsert_input_strips_control_chars_keeps_newline_tab() -> None:
    uid = create_user()
    run_id = _create(uid)
    view = wr.upsert_input(
        run_id=run_id,
        cell_id=ANALYST_CELL,
        value="  line1\x00\x07\r\nline2\tend\x1b  ",
        user_id=uid,
    )
    assert view is not None
    assert view.value == "line1\nline2\tend"


def test_upsert_input_cap_applies_after_cleaning() -> None:
    uid = create_user()
    run_id = _create(uid)
    # Control characters beyond the cap are stripped BEFORE the length check.
    ok = "a" * wr.MAX_INPUT_CHARS + "\x00\x00"
    view = wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value=ok, user_id=uid)
    assert view is not None and len(view.value) == wr.MAX_INPUT_CHARS

    with pytest.raises(wr.InputTooLongError):
        wr.upsert_input(
            run_id=run_id, cell_id=ANALYST_CELL, value="a" * (wr.MAX_INPUT_CHARS + 1), user_id=uid
        )


def test_upsert_empty_after_cleaning_clears_the_row() -> None:
    uid = create_user()
    run_id = _create(uid)
    wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="keep", user_id=uid)

    cleared = wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value=" \x00\r ", user_id=uid)
    assert cleared is None
    detail = wr.get_run(run_id)
    assert detail is not None and detail.inputs == []


def test_concurrent_duplicate_insert_raises_typed_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writers race the same EMPTY cell: the loser's insert trips
    uq_whitepaper_input_run_cell and must surface as ConcurrentEditError
    (API: 409), never a naked IntegrityError 500. Simulated by pre-inserting
    the winner's row and blinding the existing-row lookup (the stale read)."""
    from sqlmodel import col

    uid = create_user()
    run_id = _create(uid)
    with session_scope() as s:
        s.add(
            WhitepaperInput(run_id=run_id, cell_id=ANALYST_CELL, value="first", author_user_id=uid)
        )

    def stale_select(model: Any) -> Any:
        # The race window: the lookup misses the row another writer committed.
        # Wraps the module-level `select` import (the same object wr.select is
        # bound to) rather than wr.select, which strict mypy rejects as a
        # non-exported attribute.
        return select(model).where(col(WhitepaperInput.id) < 0)

    monkeypatch.setattr(wr, "select", stale_select)
    with pytest.raises(wr.ConcurrentEditError):
        wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="second", user_id=uid)
    monkeypatch.undo()

    # The winner's value stands untouched; the loser's transaction rolled back.
    detail = wr.get_run(run_id)
    assert detail is not None
    assert [(iv.cell_id, iv.value) for iv in detail.inputs] == [(ANALYST_CELL, "first")]


def test_upsert_input_rejects_unknown_cell_and_missing_run() -> None:
    uid = create_user()
    run_id = _create(uid)
    with pytest.raises(wr.InvalidCellError):
        wr.upsert_input(run_id=run_id, cell_id="not_a_cell", value="x", user_id=uid)
    with pytest.raises(wr.RunNotFoundError):
        wr.upsert_input(run_id=99999, cell_id=ANALYST_CELL, value="x", user_id=uid)


def test_clear_input_semantics() -> None:
    uid = create_user()
    run_id = _create(uid)
    wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="v", user_id=uid)
    before = wr.get_run(run_id)
    assert before is not None

    assert wr.clear_input(run_id=run_id, cell_id=ANALYST_CELL, user_id=uid) is True
    after = wr.get_run(run_id)
    assert after is not None and after.inputs == []
    assert after.updated_at > before.updated_at

    # No-op clear: nothing existed, nothing changed, updated_at untouched.
    assert wr.clear_input(run_id=run_id, cell_id=ANALYST_CELL, user_id=uid) is False
    noop = wr.get_run(run_id)
    assert noop is not None and noop.updated_at == after.updated_at


def test_updated_at_bumps_on_overlay_mutation() -> None:
    uid = create_user()
    run_id = _create(uid)
    created = wr.get_run(run_id)
    assert created is not None
    wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="v", user_id=uid)
    touched = wr.get_run(run_id)
    assert touched is not None
    assert touched.updated_at > created.updated_at
    assert touched.created_at == created.created_at


# ---------------------------------------------------------------------------
# Finalize / reopen lifecycle
# ---------------------------------------------------------------------------


def test_finalize_freezes_edits_and_reopen_unfreezes() -> None:
    uid = create_user()
    run_id = _create(uid)
    wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="kept", user_id=uid)

    wr.finalize_run(run_id=run_id, user_id=uid)
    detail = wr.get_run(run_id)
    assert detail is not None
    assert detail.status == "final"
    assert detail.finalized_at is not None
    assert detail.finalized_by_user_id == uid
    assert detail.finalized_by == "Test Analyst"

    with pytest.raises(wr.RunFinalizedError):
        wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="edit", user_id=uid)
    with pytest.raises(wr.RunFinalizedError):
        wr.clear_input(run_id=run_id, cell_id=ANALYST_CELL, user_id=uid)
    with pytest.raises(wr.RunFinalizedError):
        wr.finalize_run(run_id=run_id, user_id=uid)

    # The overlay survived the frozen edit attempts untouched.
    frozen = wr.get_run(run_id)
    assert frozen is not None and frozen.inputs[0].value == "kept"

    wr.reopen_run(run_id=run_id, user_id=uid)
    reopened = wr.get_run(run_id)
    assert reopened is not None
    assert reopened.status == "draft"
    assert reopened.finalized_at is None
    assert reopened.finalized_by_user_id is None and reopened.finalized_by is None
    # Editable again.
    assert wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="edit", user_id=uid)


def test_reopen_draft_raises() -> None:
    uid = create_user()
    run_id = _create(uid)
    with pytest.raises(wr.RunNotFinalError):
        wr.reopen_run(run_id=run_id, user_id=uid)


def test_generated_layer_and_fingerprint_stable_across_overlay_and_lifecycle() -> None:
    """INV-3: overlay edits and status flips never touch sections_json -- the
    stored fingerprint verifies before AND after every mutation path."""
    uid = create_user()
    run_id = _create(uid)
    original = wr.get_run(run_id)
    assert original is not None

    wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="human text", user_id=uid)
    wr.clear_input(run_id=run_id, cell_id=ANALYST_CELL, user_id=uid)
    wr.finalize_run(run_id=run_id, user_id=uid)
    wr.reopen_run(run_id=run_id, user_id=uid)

    after = wr.get_run(run_id)
    assert after is not None
    assert after.sections == original.sections
    assert after.sections_sha256 == original.sections_sha256
    assert result_fingerprint(after.sections) == after.sections_sha256
    # The analyst cell's GENERATED value stays None even while an overlay
    # value exists (the human answer is visibly human, INV-3).
    analyst_cells = [c for s in after.sections for c in s["cells"] if c["id"] == ANALYST_CELL]
    assert analyst_cells[0]["value"] is None


def test_finalize_fingerprint_mismatch_raises_and_stays_draft() -> None:
    uid = create_user()
    run_id = _create(uid)
    # Simulate stored-data corruption: tamper the generated layer directly
    # (the store module itself exposes no way to do this).
    with session_scope() as s:
        run = s.scalars(select(WhitepaperRun).where(WhitepaperRun.id == run_id)).one()
        tampered = [dict(sec) for sec in run.sections_json]
        tampered[0] = {**tampered[0], "title": "TAMPERED"}
        run.sections_json = tampered
        s.add(run)

    with pytest.raises(wr.IntegrityMismatchError):
        wr.finalize_run(run_id=run_id, user_id=uid)
    detail = wr.get_run(run_id)
    assert detail is not None and detail.status == "draft" and detail.finalized_at is None


def test_finalize_and_reopen_missing_run_raise() -> None:
    uid = create_user()
    with pytest.raises(wr.RunNotFoundError):
        wr.finalize_run(run_id=99999, user_id=uid)
    with pytest.raises(wr.RunNotFoundError):
        wr.reopen_run(run_id=99999, user_id=uid)


# ---------------------------------------------------------------------------
# Delete rules
# ---------------------------------------------------------------------------


def test_delete_run_is_creator_only_drafts_only() -> None:
    uid = create_user()
    other = create_user("other@example.com", display_name="Other Analyst")
    run_id = _create(uid)
    wr.upsert_input(run_id=run_id, cell_id=ANALYST_CELL, value="v", user_id=other)

    with pytest.raises(wr.RunNotOwnedError):
        wr.delete_run(run_id=run_id, user_id=other)
    assert wr.get_run(run_id) is not None

    wr.finalize_run(run_id=run_id, user_id=uid)
    with pytest.raises(wr.RunFinalizedError):
        wr.delete_run(run_id=run_id, user_id=uid)
    wr.reopen_run(run_id=run_id, user_id=uid)

    with pytest.raises(wr.RunNotFoundError):
        wr.delete_run(run_id=99999, user_id=uid)

    wr.delete_run(run_id=run_id, user_id=uid)
    assert wr.get_run(run_id) is None
    # The overlay rows went with it (explicit delete, same transaction).
    with session_scope() as s:
        leftovers = s.scalars(select(WhitepaperInput).where(WhitepaperInput.run_id == run_id)).all()
    assert leftovers == []


# ---------------------------------------------------------------------------
# Migration 0013: upgrade/downgrade round-trip + models/migration convergence
# ---------------------------------------------------------------------------


def _alembic_cfg(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


_RUN_INDEXES = {
    "ix_whitepaper_run_created_at",
    "ix_whitepaper_run_created_by_user_id",
    "ix_whitepaper_run_application_number",
    "ix_whitepaper_run_normalized_name",
    "ix_whitepaper_run_source_audit_id",
    "ix_whitepaper_run_status",
}


def test_migration_0013_upgrade_and_downgrade_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "mig-roundtrip.db"
    cfg = _alembic_cfg(db)
    command.upgrade(cfg, "0013_whitepaper_runs")

    engine = sa.create_engine(f"sqlite:///{db.as_posix()}")
    inspector = sa.inspect(engine)
    assert {"whitepaper_run", "whitepaper_input"} <= set(inspector.get_table_names())
    assert {ix["name"] for ix in inspector.get_indexes("whitepaper_run")} >= _RUN_INDEXES
    assert "ix_whitepaper_input_run_id" in {
        ix["name"] for ix in inspector.get_indexes("whitepaper_input")
    }
    assert any(
        uq["name"] == "uq_whitepaper_input_run_cell"
        and set(uq["column_names"]) == {"run_id", "cell_id"}
        for uq in inspector.get_unique_constraints("whitepaper_input")
    )
    engine.dispose()

    command.downgrade(cfg, "0012_watch_runs")
    engine = sa.create_engine(f"sqlite:///{db.as_posix()}")
    tables = set(sa.inspect(engine).get_table_names())
    assert "whitepaper_run" not in tables and "whitepaper_input" not in tables
    engine.dispose()


def test_models_and_migration_0013_produce_identical_schemas(tmp_path: Path) -> None:
    """Fresh-boot convergence: a FRESH Postgres never replays 0013 (bootstrap =
    create_all + stamp head), so models.py and the migration must agree."""
    mig_db = tmp_path / "from-migrations.db"
    command.upgrade(_alembic_cfg(mig_db), "head")
    mig_engine = sa.create_engine(f"sqlite:///{mig_db.as_posix()}")

    boot_db = tmp_path / "from-create-all.db"
    boot_engine = sa.create_engine(f"sqlite:///{boot_db.as_posix()}")
    SQLModel.metadata.create_all(boot_engine)

    def _shape(engine: sa.Engine, table: str) -> dict[str, Any]:
        inspector = sa.inspect(engine)
        return {
            "columns": {(c["name"], c["nullable"]) for c in inspector.get_columns(table)},
            "indexes": {
                (ix["name"], tuple(ix["column_names"])) for ix in inspector.get_indexes(table)
            },
            "uniques": {
                (uq["name"], tuple(uq["column_names"]))
                for uq in inspector.get_unique_constraints(table)
            },
            "fks": {
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table)
            },
        }

    for table in ("whitepaper_run", "whitepaper_input"):
        assert _shape(mig_engine, table) == _shape(boot_engine, table), table

    # The named CHECK constraint exists on both bootstrap paths.
    for engine in (mig_engine, boot_engine):
        with engine.connect() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'whitepaper_run'")
            ).scalar_one()
        assert "ck_whitepaper_run_status" in ddl

    mig_engine.dispose()
    boot_engine.dispose()
