"""INV-4 crash recovery: a version committed but never alerted must be re-surfaced.

The silent-miss bug: ``ingest_listing`` commits the ``psg_version`` row and THEN
writes chunks/BE in separate, non-atomic stores. If that second step crashes, the
version is durably committed but no alert was ever built. Because the content_hash
now matches forever, every later run classifies the listing as ``unchanged`` and
NEVER re-enters the alert path -- a permanent silent miss (regulatory change the
analyst is never paged about).

The fix derives missed alerts straight from the durable tables (a matched
version whose ``psg_version_id`` has no row in ``alert``) and feeds them back
through ``build_alerts``. These tests fail if that re-surfacing regresses.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

import regwatch.watch.run as run_mod
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import Alert as AlertRow
from regwatch.store.models import PsgDocument, PsgVersion
from regwatch.watch.alerts import latest_digest_records, pairs_without_alert
from regwatch.watch.matcher import WatchMatch

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


def _match(appl_no: str = APPL_NO, product_id: int = 7) -> WatchMatch:
    return WatchMatch(
        listing=_listing(appl_no),
        product={"id": product_id, "active_ingredient": "Albuterol Sulfate"},
        confidence=1.0,
        rationale="canonical",
    )


def _commit_version_but_no_alert(appl_no: str = APPL_NO) -> int:
    """Persist a psg_document + latest psg_version with NO chunks and NO alert.

    This is exactly the durable state left behind when chunk extraction crashed
    AFTER the version row committed: the row exists, but nothing alerted on it.
    """
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
        v = PsgVersion(
            psg_document_id=doc.id,
            content_hash="x",
            recommended_date="2026-05-21",
            diff_summary="init",
        )
        s.add(v)
        s.flush()
        assert v.id is not None
        return v.id


def test_pairs_without_alert_flags_committed_unalerted_version() -> None:
    version_id = _commit_version_but_no_alert()
    assert pairs_without_alert([(APPL_NO, 7)]) == {(APPL_NO, 7)}
    # And it really has no alert row yet (the precondition we recover from).
    with session_scope() as s:
        rows = list(s.scalars(select(AlertRow)))
    assert all(r.psg_version_id != version_id for r in rows)


def test_pairs_without_alert_ignores_already_alerted_and_unknown() -> None:
    """No miss for an already-alerted (version, product); unknown appl_no never flagged."""
    from regwatch.watch.alerts import build_alerts, write_digest

    _commit_version_but_no_alert()
    write_digest(build_alerts([_match()]))  # durable alert now exists for product 7
    assert pairs_without_alert([(APPL_NO, 7)]) == set()
    # An appl_no with no psg_document/version has nothing to alert on.
    assert pairs_without_alert([("999999", 7)]) == set()


def test_pairs_without_alert_is_per_product_not_per_version() -> None:
    """A version alerted for product 7 must STILL flag product 8 as missed.

    This is the silent-miss regression: a per-VERSION check (any alert row on
    the version) would treat a newly-watched second product as satisfied and
    never page its analyst, even though no alert exists for THAT product.
    """
    from regwatch.watch.alerts import build_alerts, write_digest

    _commit_version_but_no_alert()
    write_digest(build_alerts([_match(product_id=7)]))  # alerted for product 7 only
    assert pairs_without_alert([(APPL_NO, 7)]) == set()  # 7 satisfied
    assert pairs_without_alert([(APPL_NO, 8)]) == {(APPL_NO, 8)}  # 8 still missed


def test_run_watch_resurfaces_missed_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: the listing ingests as 'unchanged' (post-crash) yet an alert
    IS produced and is durable in the `alert` table (INV-4)."""
    _commit_version_but_no_alert()

    listing = _listing()
    match = _match()

    monkeypatch.setattr(run_mod, "fetch_all_listings", lambda: [listing])
    monkeypatch.setattr(run_mod, "list_watchlist", lambda: [match.product])
    monkeypatch.setattr(run_mod, "match_listings", lambda listings, products: [match])

    # Simulate the steady state AFTER the crash: the hash already matches, so
    # ingest reports "unchanged" -- the exact path that used to swallow the alert.
    def _fake_ingest(_listing: PsgListing, *, extract: bool) -> str:
        return "unchanged"

    monkeypatch.setattr(run_mod, "ingest_listing", _fake_ingest)

    result = run_mod.run_watch(extract=False)

    # An alert IS produced despite the "unchanged" classification ...
    assert len(result.alerts) == 1
    assert result.alerts[0].listing_appl_no == APPL_NO
    assert result.stats.unchanged == 1 and result.stats.errors == 0
    # ... and it is DURABLE (survives a redeploy -- read back from the `alert`
    # table, not the ephemeral JSONL digest).
    records = latest_digest_records()
    assert len(records) == 1
    assert records[0]["listing_appl_no"] == APPL_NO


def test_run_watch_no_missed_alert_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely clean unchanged run (already-alerted version) emits NO alert."""
    from regwatch.watch.alerts import build_alerts, write_digest

    _commit_version_but_no_alert()
    write_digest(build_alerts([_match()]))  # version already alerted

    listing = _listing()
    match = _match()
    monkeypatch.setattr(run_mod, "fetch_all_listings", lambda: [listing])
    monkeypatch.setattr(run_mod, "list_watchlist", lambda: [match.product])
    monkeypatch.setattr(run_mod, "match_listings", lambda listings, products: [match])
    monkeypatch.setattr(run_mod, "ingest_listing", lambda _l, *, extract: "unchanged")

    result = run_mod.run_watch(extract=False)
    assert result.alerts == []  # no re-emit; idempotent
    # The durable feed still has exactly the one original alert.
    assert len(latest_digest_records()) == 1


def test_run_watch_alerts_newly_watched_second_product(monkeypatch: pytest.MonkeyPatch) -> None:
    """A product added to the watchlist AFTER a version was alerted for another
    product gets its own alert on the next run, even though the listing is
    'unchanged' (the per-product silent-miss fix), without re-emitting for the
    already-alerted product."""
    from regwatch.watch.alerts import build_alerts, write_digest

    _commit_version_but_no_alert()
    write_digest(build_alerts([_match(product_id=7)]))  # product 7 already alerted

    listing = _listing()
    match7 = _match(product_id=7)
    match8 = _match(product_id=8)  # same listing, newly-watched second product
    monkeypatch.setattr(run_mod, "fetch_all_listings", lambda: [listing])
    monkeypatch.setattr(run_mod, "list_watchlist", lambda: [match7.product, match8.product])
    monkeypatch.setattr(run_mod, "match_listings", lambda listings, products: [match7, match8])
    monkeypatch.setattr(run_mod, "ingest_listing", lambda _l, *, extract: "unchanged")

    result = run_mod.run_watch(extract=False)

    # Exactly one NEW alert this run -- for product 8 only; product 7 is a no-op.
    assert len(result.alerts) == 1
    assert result.alerts[0].product_id == 8
    # Durable feed now carries both per-product alerts for the one version.
    records = latest_digest_records()
    assert {r["product_id"] for r in records} == {7, 8}
    assert len({r["psg_version_id"] for r in records}) == 1  # same version, two products
