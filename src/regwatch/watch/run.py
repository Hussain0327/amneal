"""The Watch pipeline: crawl → match → ingest matched → alert → digest.

Composition rules (INV-4 — never report a change that did not happen):
  - Only listings that match the watchlist are ingested.
  - Alerts are built ONLY for matches whose listing this run actually ingested
    as "added" or "revised". An unchanged PSG never produces an alert, so
    re-running twice in a row yields an empty second digest, not duplicates.
  - `build_alerts` independently re-verifies every version against the DB.

A clean no-change run still writes a (possibly empty) digest: the empty file is
the truthful record of the latest run, and `GET /watch/latest` then reports zero
alerts instead of re-surfacing a stale digest as current. An ERRORED run that
produced no alerts is the exception — it does NOT overwrite with an empty digest,
since an all-clear file would misrepresent a failed run as a quiet day (INV-4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from regwatch.common.logging import get_logger
from regwatch.ingest.pipeline import IngestStats, ingest_listing
from regwatch.ingest.psg_crawler import PsgListing, fetch_all_listings
from regwatch.watch.alerts import (
    Alert,
    build_alerts,
    digest_path,
    pairs_without_alert,
    write_digest,
)
from regwatch.watch.matcher import WatchMatch, match_listings
from regwatch.watch.runs import record_watch_run
from regwatch.watch.watchlist import list_watchlist

log = get_logger(__name__)

_CHANGED_OUTCOMES = {"added", "revised"}


def _product_id(m: WatchMatch) -> int | None:
    """The match's watchlist product id, or None when it carries no int id."""
    pid = m.product.get("id")
    return pid if isinstance(pid, int) else None


@dataclass
class WatchRunResult:
    listings: int
    matched: int
    stats: IngestStats
    alerts: list[Alert]
    digest_path: Path


def _matched_listings(matches: list[WatchMatch]) -> list[PsgListing]:
    """De-dupe matched listings by appl_no (the psg_document identity key)."""
    seen: set[str] = set()
    out: list[PsgListing] = []
    for m in matches:
        if m.listing.appl_no in seen:
            continue
        seen.add(m.listing.appl_no)
        out.append(m.listing)
    return out


def _ingest_matched(
    listings: list[PsgListing], *, extract: bool
) -> tuple[IngestStats, dict[str, str]]:
    """Ingest matched listings, returning stats AND the per-appl_no outcome."""
    stats = IngestStats()
    outcomes: dict[str, str] = {}
    for listing in listings:
        stats.scanned += 1
        outcome = ingest_listing(listing, extract=extract)
        outcomes[listing.appl_no] = outcome
        if outcome == "added":
            stats.added += 1
        elif outcome == "revised":
            stats.revised += 1
        elif outcome == "unchanged":
            stats.unchanged += 1
        else:
            stats.errors += 1
    return stats, outcomes


def run_watch(*, extract: bool = True) -> WatchRunResult:
    """Run one full Watch cycle and write the daily digest."""
    # Captured BEFORE the crawl so the durable ledger row reflects the whole
    # run (the crawl is the slow part), not just the post-match work.
    started_at = datetime.now(UTC)
    listings = fetch_all_listings()
    # A working crawl structurally cannot return zero listings: even letters
    # with no drugs fall back to the ~70-row default slice, so an empty catalog
    # means the crawl itself broke (200-status challenge page, redesign). Fail
    # BEFORE any digest write -- an empty all-clear digest plus exit 0 would
    # misreport a dead crawl as a quiet day from then on (INV-4) and keep
    # pinging the cron's dead-man's-switch as success.
    if not listings:
        raise RuntimeError("watch crawl returned 0 PSG listings; aborting before digest write")
    products = list_watchlist()
    matches = match_listings(listings, products)
    matched = _matched_listings(matches)

    stats, outcomes = _ingest_matched(matched, extract=extract)
    changed = [m for m in matches if outcomes.get(m.listing.appl_no) in _CHANGED_OUTCOMES]
    # INV-4 crash recovery, PER-PRODUCT: a prior run can commit a psg_version row
    # and then crash before its chunks/BE land (separate non-atomic stores), so
    # this run reads the unchanged content_hash and classifies it "unchanged" --
    # never re-entering the alert path. The same gap appears whenever a NEW
    # product is added to the watchlist after a version was already alerted for a
    # different product (the version stays "unchanged" forever). Re-surface any
    # matched (appl_no, product_id) pair whose committed latest version has no
    # alert row FOR THAT PRODUCT. `changed` already fans per product for listings
    # that changed this run, so only check the rest. build_alerts re-verifies each
    # version and _persist_alerts is idempotent, so already-alerted pairs stay
    # no-ops and this never double-emits.
    changed_keys = {(m.listing.appl_no, _product_id(m)) for m in changed}
    candidate_pairs = [
        (m.listing.appl_no, pid)
        for m in matches
        if (pid := _product_id(m)) is not None and (m.listing.appl_no, pid) not in changed_keys
    ]
    missed_pairs = pairs_without_alert(candidate_pairs)
    to_alert = list(changed)
    for m in matches:
        pid = _product_id(m)
        if pid is not None and (m.listing.appl_no, pid) in missed_pairs:
            to_alert.append(m)
    alerts = build_alerts(to_alert)

    # A clean run writes its digest (even when empty: that empty file is the
    # truthful "ran, no changes" record and stops `/watch/latest` re-surfacing a
    # stale digest as current). But an ERRORED run with no alerts must NOT stamp an
    # empty all-clear digest — that asserts a clean no-change day (INV-4: never
    # report a run state that did not happen) and would clobber an earlier
    # same-day digest. The non-zero exit code already signals the failure.
    if stats.errors and not alerts:
        out_path = digest_path()
        # No digest was written on this branch, so the ledger must not name
        # one (INV-4: never claim an artifact that does not exist).
        digest_date = None
        log.info("watch_digest_skipped_on_error", errors=stats.errors, path=str(out_path))
    else:
        out_path = write_digest(alerts)
        # The date embedded in the file actually written (digest-YYYY-MM-DD),
        # parsed from the path rather than re-read from the clock so a run
        # spanning midnight still records the digest it really wrote.
        digest_date = out_path.stem.removeprefix("digest-")

    log.info(
        "watch_run_done",
        listings=len(listings),
        matched=len(matched),
        added=stats.added,
        revised=stats.revised,
        unchanged=stats.unchanged,
        errors=stats.errors,
        alerts=len(alerts),
        digest=str(out_path),
    )
    # Durable ledger row -- the truthful "a run completed" record (INV-4).
    # Errored-but-completed runs (CLI exit 2) record too: they really ran, and
    # their errors count is the honest state. Runs that RAISE (the zero-listings
    # guard above, or a crash mid-pipeline) never reach this line, so an aborted
    # run records nothing -- the cron's dead-man's-switch owns that class. The
    # digest + alert rows are already durable here, so a DB hiccup while
    # recording logs loudly instead of turning a completed run into a crash.
    try:
        record_watch_run(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            listings=len(listings),
            matched=len(matched),
            added=stats.added,
            revised=stats.revised,
            unchanged=stats.unchanged,
            errors=stats.errors,
            alerts=len(alerts),
            digest_date=digest_date,
        )
    except Exception:
        log.error("watch_run_record_failed", exc_info=True)
    return WatchRunResult(
        listings=len(listings),
        matched=len(matched),
        stats=stats,
        alerts=alerts,
        digest_path=out_path,
    )
