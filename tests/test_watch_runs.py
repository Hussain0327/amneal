"""Durable watch_run ledger: completed runs record a row, aborted runs never do.

INV-4: the ledger is the truthful record of what the cron DID. A clean run and
an errored-but-completed run both record (they happened); a run that RAISED
(broken crawl) records nothing, so the UI can distinguish a quiet day from a
cron that has been dead for a week. A DB hiccup while recording must log, not
turn an already-completed run (durable digest + alerts) into a crash.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlmodel import select

import regwatch.watch.run as run_mod
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import WatchRun
from regwatch.watch.matcher import WatchMatch
from regwatch.watch.runs import latest_watch_run, record_watch_run

APPL_NO = "020503"


def _listing(appl_no: str = APPL_NO, name: str = "Albuterol Sulfate") -> PsgListing:
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


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """One matched listing whose ingest reports `outcome`; no network, no PDFs."""
    listing = _listing()
    match = WatchMatch(
        listing=listing,
        product={"id": 7, "active_ingredient": "Albuterol Sulfate"},
        confidence=1.0,
        rationale="canonical",
    )
    monkeypatch.setattr(run_mod, "fetch_all_listings", lambda: [listing])
    monkeypatch.setattr(run_mod, "list_watchlist", lambda: [match.product])
    monkeypatch.setattr(run_mod, "match_listings", lambda listings, products: [match])
    monkeypatch.setattr(run_mod, "ingest_listing", lambda _listing, *, extract: outcome)


def _ledger_rows() -> list[dict[str, Any]]:
    """All watch_run rows as plain dicts -- materialized INSIDE the session
    (expire_on_commit detaches rows on scope exit; attribute access afterward
    would lazy-load against a closed session)."""
    with session_scope() as s:
        return [
            {
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "listings": r.listings,
                "matched": r.matched,
                "added": r.added,
                "revised": r.revised,
                "unchanged": r.unchanged,
                "errors": r.errors,
                "alerts": r.alerts,
                "digest_date": r.digest_date,
            }
            for r in s.scalars(select(WatchRun))
        ]


def test_clean_run_records_ledger_row(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    _patch_pipeline(monkeypatch, "added")

    result = run_mod.run_watch(extract=False)

    rows = _ledger_rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row["listings"], row["matched"], row["added"], row["errors"]) == (1, 1, 1, 0)
    # No psg_version exists for the stubbed ingest, so build_alerts skipped it
    # (INV-4) -- the ledger must agree with the run's real alert count.
    assert row["alerts"] == len(result.alerts) == 0
    assert row["started_at"] <= row["finished_at"]
    # The digest WAS written on the clean branch; the ledger names the file's
    # own date (parsed from the path), never a separate clock read.
    assert row["digest_date"] == result.digest_path.stem.removeprefix("digest-")


def test_errored_but_completed_run_still_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """An errored-but-completed run is a REAL run (INV-4): the row lands with
    the truthful errors count, and digest_date is None because the
    skip-on-error branch wrote no digest file."""
    init_db()
    _patch_pipeline(monkeypatch, "error")

    result = run_mod.run_watch(extract=False)
    assert result.stats.errors == 1

    rows = _ledger_rows()
    assert len(rows) == 1
    assert rows[0]["errors"] == 1
    assert rows[0]["digest_date"] is None


def test_raising_run_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The zero-listings guard aborts BEFORE any artifact: no digest, no ledger
    row -- the dead-man's-switch owns that failure class, and a row here would
    misreport an aborted run as having happened (INV-4)."""
    init_db()
    monkeypatch.setattr(run_mod, "fetch_all_listings", lambda: [])

    with pytest.raises(RuntimeError, match="0 PSG listings"):
        run_mod.run_watch(extract=False)

    assert _ledger_rows() == []
    assert latest_watch_run() is None


def test_record_failure_does_not_crash_a_completed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest + alerts are already durable when the ledger write runs, so a
    DB hiccup there must log loudly and return the pipeline result -- never
    convert a completed run into a crash (which would page as a failed run)."""
    init_db()
    _patch_pipeline(monkeypatch, "unchanged")

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("ledger db hiccup")

    monkeypatch.setattr(run_mod, "record_watch_run", _boom)

    result = run_mod.run_watch(extract=False)

    assert result.stats.unchanged == 1
    assert result.digest_path.exists()  # the run's real artifacts survived
    assert latest_watch_run() is None  # and the failed write left no row


def test_latest_watch_run_none_when_never_ran() -> None:
    init_db()
    assert latest_watch_run() is None


def test_latest_watch_run_wire_shape_newest_by_finished_at() -> None:
    """Exact wire shape of /watch/latest's `last_run`, and newest-by-finished_at
    ordering (NOT insertion order: the newer run is inserted first)."""
    init_db()
    record_watch_run(
        started_at=datetime(2026, 7, 2, 7, 17, 0),
        finished_at=datetime(2026, 7, 2, 7, 21, 0),
        listings=1795,
        matched=3,
        added=0,
        revised=1,
        unchanged=2,
        errors=0,
        alerts=1,
        digest_date="2026-07-02",
    )
    record_watch_run(
        started_at=datetime(2026, 7, 1, 7, 17, 0),
        finished_at=datetime(2026, 7, 1, 7, 21, 0),
        listings=1794,
        matched=3,
        added=1,
        revised=0,
        unchanged=2,
        errors=1,
        alerts=1,
        digest_date=None,
    )
    assert latest_watch_run() == {
        "started_at": "2026-07-02T07:17:00",
        "finished_at": "2026-07-02T07:21:00",
        "listings": 1795,
        "matched": 3,
        "added": 0,
        "revised": 1,
        "unchanged": 2,
        "errors": 0,
        "alerts": 1,
    }
