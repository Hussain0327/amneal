"""Migration 0014: dedupe pre-existing duplicate psg_version rows, then index.

The unique (psg_document_id, content_hash) index cannot build over data from
the pre-constraint era, so 0014 first collapses each duplicate group onto the
row the pipeline already serves as latest (max captured_at, id tiebreak),
repointing be_requirement and alert references and deleting only redundant
duplicates (a loser alert whose (listing, product) already exists on the
keeper IS the INV-4 duplicate the index prevents).

Exercised for real on SQLite by rewinding a head-migrated DB to 0013 (0014's
downgrade only drops the index), seeding the legacy duplicate shape, and
replaying the upgrade. The Postgres variant (opt-in via TEST_DATABASE_URL,
CI's pgvector service) additionally proves the chunk.version_id repoint.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from regwatch.store import db as db_module
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import Alert, BeRequirement, PsgDocument, PsgVersion

TEST_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

DUP_HASH = "dup-hash"


def _rewind_to_0013() -> None:
    command.downgrade(db_module._alembic_config(), "0013_whitepaper_runs")


def _upgrade_to_head() -> None:
    command.upgrade(db_module._alembic_config(), "head")


def _alert(version_id: int, doc_id: int, product_id: int) -> Alert:
    return Alert(
        product_id=product_id,
        active_ingredient="Albuterol Sulfate",
        listing_appl_no="020503",
        listing_psg_type="draft",
        psg_document_id=doc_id,
        psg_version_id=version_id,
        captured_at="2026-01-01T00:00:00+00:00",
        diff_summary=None,
        confidence=0.9,
        rationale="ingredient match",
        source_url="https://example.invalid/PSG_020503.pdf",
    )


def _seed_duplicate_group() -> dict[str, int]:
    """The legacy race shape: two version rows for one (doc, content_hash),
    each with a BE row; alerts on both, one pair colliding, one repointable."""
    with session_scope() as s:
        doc = PsgDocument(
            appl_no="020503",
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            psg_type="draft",
            source_url="https://example.invalid/PSG_020503.pdf",
            content_hash=DUP_HASH,
        )
        s.add(doc)
        s.flush()
        assert doc.id is not None
        loser = PsgVersion(
            psg_document_id=doc.id, content_hash=DUP_HASH, captured_at=datetime(2026, 1, 1)
        )
        keeper = PsgVersion(
            psg_document_id=doc.id, content_hash=DUP_HASH, captured_at=datetime(2026, 1, 2)
        )
        s.add(loser)
        s.add(keeper)
        s.flush()
        assert loser.id is not None and keeper.id is not None
        for version_id in (loser.id, keeper.id):
            s.add(
                BeRequirement(
                    psg_document_id=doc.id,
                    version_id=version_id,
                    study_type="single-dose in-vivo",
                )
            )
        keeper_alert = _alert(keeper.id, doc.id, product_id=1)
        colliding_loser_alert = _alert(loser.id, doc.id, product_id=1)
        repointable_loser_alert = _alert(loser.id, doc.id, product_id=2)
        s.add(keeper_alert)
        s.add(colliding_loser_alert)
        s.add(repointable_loser_alert)
        s.flush()
        assert keeper_alert.id is not None and colliding_loser_alert.id is not None
        return {
            "doc_id": doc.id,
            "loser_id": loser.id,
            "keeper_id": keeper.id,
            "keeper_alert_id": keeper_alert.id,
            "colliding_alert_id": colliding_loser_alert.id,
        }


def _assert_deduped(ids: dict[str, int]) -> None:
    with session_scope() as s:
        versions = list(s.scalars(select(PsgVersion)))
        assert [v.id for v in versions] == [ids["keeper_id"]]
        be_targets = list(s.scalars(select(BeRequirement.version_id)))
        assert be_targets == [ids["keeper_id"], ids["keeper_id"]]
        alerts = list(s.scalars(select(Alert)))
        # The colliding loser alert (same listing+product as the keeper's) is
        # the alert-level duplicate: deleted. The other loser alert repointed.
        assert len(alerts) == 2
        alert_ids = {a.id for a in alerts}
        assert ids["keeper_alert_id"] in alert_ids
        assert ids["colliding_alert_id"] not in alert_ids
        assert all(a.psg_version_id == ids["keeper_id"] for a in alerts)
        assert {a.product_id for a in alerts} == {1, 2}


def _assert_index_enforced(doc_id: int) -> None:
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(PsgVersion(psg_document_id=doc_id, content_hash=DUP_HASH))


def test_0014_dedupes_then_indexes_on_sqlite(capsys: pytest.CaptureFixture[str]) -> None:
    init_db()  # head
    _rewind_to_0013()  # drops only the 0014 index; the legacy shape is legal
    ids = _seed_duplicate_group()
    capsys.readouterr()  # drop bootstrap noise; capture the upgrade alone
    _upgrade_to_head()
    _assert_deduped(ids)
    _assert_index_enforced(ids["doc_id"])
    index_names = {ix["name"] for ix in inspect(db_module.get_engine()).get_indexes("psg_version")}
    assert "uq_psg_version_doc_hash" in index_names
    # The unrecoverable alert delete must announce itself: a duplicate group
    # can be an old-pipeline-recorded revert whose alerts are real history, so
    # the migration prints a manifest of every doomed row for the operator.
    manifest = capsys.readouterr().out
    assert "0014: deleting alert" in manifest
    assert f"id={ids['colliding_alert_id']}" in manifest


def test_0014_keeper_tiebreak_is_max_id_on_equal_captured_at() -> None:
    """Equal captured_at (bulk backfills stamp near-identical times): the
    higher id must win, matching the pipeline's latest-version ordering."""
    init_db()
    _rewind_to_0013()
    ts = datetime(2026, 1, 1)
    with session_scope() as s:
        doc = PsgDocument(
            appl_no="076170",
            active_ingredient="Test",
            normalized_name="test",
            psg_type="final",
            source_url="https://example.invalid/PSG_076170.pdf",
            content_hash=DUP_HASH,
        )
        s.add(doc)
        s.flush()
        assert doc.id is not None
        first = PsgVersion(psg_document_id=doc.id, content_hash=DUP_HASH, captured_at=ts)
        second = PsgVersion(psg_document_id=doc.id, content_hash=DUP_HASH, captured_at=ts)
        s.add(first)
        s.add(second)
        s.flush()
        assert first.id is not None and second.id is not None
        expected_keeper = max(first.id, second.id)
    _upgrade_to_head()
    with session_scope() as s:
        surviving = list(s.scalars(select(PsgVersion.id)))
    assert surviving == [expected_keeper]


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set (postgres integration tests are opt-in)",
)
def test_0014_dedupes_and_repoints_chunks_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as cs

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # K6: init_db asserts provider dim == vector(1536) in Postgres mode.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    engine = db_module.get_engine()
    assert engine.dialect.name == "postgresql", "TEST_DATABASE_URL must be a postgres URL"
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    try:
        init_db()  # fresh bootstrap: create_all (index included) + stamp head
        _rewind_to_0013()
        ids = _seed_duplicate_group()
        # Live-index rows still keyed to the loser version (pre-atomic era).
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO chunk (id, doc_id, version_id, text) "
                    "VALUES (:cid, :doc, :ver, 'body')"
                ),
                {
                    "cid": f"{ids['doc_id']}-{ids['loser_id']}-0",
                    "doc": ids["doc_id"],
                    "ver": ids["loser_id"],
                },
            )
        _upgrade_to_head()
        _assert_deduped(ids)
        _assert_index_enforced(ids["doc_id"])
        with engine.connect() as conn:
            chunk_targets = conn.execute(text("SELECT version_id FROM chunk")).scalars().all()
        assert chunk_targets == [ids["keeper_id"]]
    finally:
        db_module.reset_for_tests()
