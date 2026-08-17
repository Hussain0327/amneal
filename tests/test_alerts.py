"""Alerts: INV-4 — never emit an alert for a version not actually in the DB."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import select

from regwatch.common.text_normalize import canonical_name
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.watch.alerts import build_alerts, latest_digest_records, write_digest
from regwatch.watch.matcher import WatchMatch


def _listing(appl_no: str = "020503", name: str = "Albuterol Sulfate") -> PsgListing:
    from regwatch.common.text_normalize import stripped_name

    return PsgListing(
        appl_no=appl_no,
        active_ingredient=name,
        normalized_name=canonical_name(name),
        stripped_name=stripped_name(name),
        psg_type="final",
        route="Inhalation",
        dosage_form="Aerosol, Metered",
        rld_or_rs_numbers=[appl_no],
        recommended_date="2026-05-21",
        pdf_url=f"http://example/PSG_{appl_no}.pdf",
        source_url="http://example/index.cfm",
    )


def _match() -> WatchMatch:
    return WatchMatch(
        listing=_listing(),
        product={"id": 7, "active_ingredient": "Albuterol Sulfate"},
        confidence=1.0,
        rationale="canonical",
    )


def _persist_version(appl_no: str = "020503", diff: str | None = "init") -> None:
    init_db()
    with session_scope() as s:
        doc = PsgDocument(
            appl_no=appl_no,
            active_ingredient="Albuterol Sulfate",
            normalized_name="albuterol sulfate",
            dosage_form="Aerosol, Metered",
            route="Inhalation",
            psg_type="final",
            recommended_date="2026-05-21",
            source_url=f"http://example/PSG_{appl_no}.pdf",
            content_hash="x",
        )
        s.add(doc)
        s.flush()
        s.add(
            PsgVersion(
                psg_document_id=doc.id,
                content_hash="x",
                recommended_date="2026-05-21",
                diff_summary=diff,
            )
        )


def test_alert_skipped_when_no_version_exists() -> None:
    """INV-4: a match whose underlying PSG was never fetched yields NO alert."""
    init_db()  # no PsgDocument / PsgVersion rows
    alerts = build_alerts([_match()])
    assert alerts == []


def test_alert_emitted_only_for_existing_version() -> None:
    _persist_version()
    alerts = build_alerts([_match()])
    assert len(alerts) == 1
    assert alerts[0].listing_appl_no == "020503"
    assert alerts[0].psg_version_id > 0


def test_digest_round_trip(tmp_path: Path) -> None:
    _persist_version()
    alerts = build_alerts([_match()])
    write_digest(alerts)
    records = latest_digest_records()
    assert records
    assert records[0]["listing_appl_no"] == "020503"


def test_empty_digest_round_trip(tmp_path: Path) -> None:
    """A no-change watch run persists no alerts and writes an EMPTY JSONL digest.

    With no rows in the durable `alert` table, GET /watch/latest reads zero; the
    empty file is retained only as the truthful "ran, no changes" artifact.
    """
    init_db()
    path = write_digest([])
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""
    assert latest_digest_records() == []


def test_write_digest_is_idempotent_on_rerun() -> None:
    """Durable + idempotent: persisting the same alert twice yields ONE row.

    Exercises the (psg_version_id, listing_appl_no, product_id) unique key /
    ON CONFLICT DO NOTHING path directly — re-running watch never duplicates.
    """
    _persist_version()
    alerts = build_alerts([_match()])
    assert len(alerts) == 1
    write_digest(alerts)
    write_digest(alerts)
    records = latest_digest_records()
    assert len(records) == 1
    assert records[0]["psg_version_id"] == alerts[0].psg_version_id


def test_since_boundary_equality_is_inclusive() -> None:
    """`since` == the alert's exact captured_at KEEPS the row (at/after contract).

    Stored captured_at strings are timezone-NAIVE isoformat (PsgVersion's
    captured_at column has no timezone), so a tz-aware since.isoformat() ends
    in +00:00 and sorts strictly AFTER its exact stored equal -- the compare
    must normalize `since` to the stored naive shape or the boundary row (and
    any same-version alert sharing that captured_at) is silently dropped.
    """
    _persist_version()
    alerts = build_alerts([_match()])
    write_digest(alerts)
    # The writer's format is naive; a cursor client echoes it back as UTC.
    stored = datetime.fromisoformat(alerts[0].captured_at)
    assert stored.tzinfo is None, "premise: the writer stores naive isoformat"
    since = stored.replace(tzinfo=UTC)
    records = latest_digest_records(since=since)
    assert [r["psg_version_id"] for r in records] == [alerts[0].psg_version_id]


def test_change_kind_new_for_first_version() -> None:
    """An alert on a document's only/first version carries change_kind="new"."""
    _persist_version()
    write_digest(build_alerts([_match()]))
    records = latest_digest_records()
    assert len(records) == 1
    assert records[0]["change_kind"] == "new"


def test_change_kind_revised_when_prior_version_exists() -> None:
    """A prior psg_version row for the same document makes the alert "revised"
    -- derived structurally from the DB, NOT from diff prose, because prod
    revisions can degrade to the "Initial version ingested" marker when the
    cron runner's prior parsed text is gone."""
    _persist_version()
    with session_scope() as s:
        doc = s.scalars(select(PsgDocument).where(PsgDocument.appl_no == "020503")).first()
        assert doc is not None and doc.id is not None
        s.add(
            PsgVersion(
                psg_document_id=doc.id,
                content_hash="y",
                recommended_date="2026-05-21",
                # The degraded prod prose: kind must NOT be inferred from it.
                diff_summary="Initial version ingested. Begins: ...",
            )
        )
    alerts = build_alerts([_match()])  # picks the LATEST version (the revision)
    write_digest(alerts)
    records = latest_digest_records()
    assert len(records) == 1
    assert records[0]["change_kind"] == "revised"


# ---------------------------------------------------------------------------
# Postgres-only: prod's _persist_alerts branch resolves ON CONFLICT by
# CONSTRAINT NAME ("uq_alert_version_listing_product"), structurally different
# from the SQLite branch (column list) every other test exercises. A constraint
# rename/typo would keep SQLite tests green and kill the daily cron in prod,
# so execute the real statement under CI's pgvector service. Gating + fixture
# mirror tests/test_postgres_bootstrap.py (fixture kept LOCAL: conftest.py is
# owned by the shared suite).

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture()
def pg_alerts_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the app at the test Postgres and wipe its public schema."""
    import config.settings as cs

    from regwatch.store import db as db_module

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # init_db asserts provider dim == vector(1536) in Postgres mode, so the
    # bootstrap needs the 1536-dim provider. No API key is required -- the
    # assert reads `.dim` without instantiating a client.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "echo")
    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()
    db_module.reset_for_tests()
    engine = db_module.get_engine()
    assert engine.dialect.name == "postgresql", "TEST_DATABASE_URL must be a postgres URL"
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield
    db_module.reset_for_tests()


def test_persist_alerts_postgres_on_conflict_do_nothing(pg_alerts_db: None) -> None:
    """The pg_insert branch upserts by constraint name and stays idempotent."""
    from regwatch.watch.alerts import _persist_alerts

    _persist_version()
    alerts = build_alerts([_match()])
    assert len(alerts) == 1
    assert _persist_alerts(alerts) == 1  # first insert lands
    assert _persist_alerts(alerts) == 0  # rerun hits the NAMED constraint: no dup
    assert len(latest_digest_records()) == 1
