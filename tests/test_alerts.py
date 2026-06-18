"""Alerts: INV-4 — never emit an alert for a version not actually in the DB."""

from __future__ import annotations

from pathlib import Path

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
