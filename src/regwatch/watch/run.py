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
from pathlib import Path

from regwatch.common.logging import get_logger
from regwatch.ingest.pipeline import IngestStats, ingest_listing
from regwatch.ingest.psg_crawler import PsgListing, fetch_all_listings
from regwatch.watch.alerts import (
    Alert,
    appl_nos_without_alert,
    build_alerts,
    digest_path,
    write_digest,
)
from regwatch.watch.matcher import WatchMatch, match_listings
from regwatch.watch.watchlist import list_watchlist

log = get_logger(__name__)

_CHANGED_OUTCOMES = {"added", "revised"}


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
    listings = fetch_all_listings()
    products = list_watchlist()
    matches = match_listings(listings, products)
    matched = _matched_listings(matches)

    stats, outcomes = _ingest_matched(matched, extract=extract)
    changed = [m for m in matches if outcomes.get(m.listing.appl_no) in _CHANGED_OUTCOMES]
    # INV-4 crash recovery: a prior run can commit a psg_version row and then
    # crash before its chunks/BE land (separate non-atomic stores), so this run
    # reads the unchanged content_hash and classifies it "unchanged" -- never
    # re-entering the alert path. Re-surface any matched version that is durably
    # committed but has no alert row (LEFT JOIN alert on psg_version_id) so the
    # missed alert is finally produced. build_alerts re-verifies each version and
    # _persist_alerts is idempotent, so already-alerted versions stay no-ops and
    # this never double-emits.
    changed_appl_nos = {m.listing.appl_no for m in changed}
    # `matched` is list[PsgListing] (de-duped by appl_no); `changed`/`matches` are
    # list[WatchMatch]. Only check listings this run did NOT already alert on.
    missed_appl_nos = appl_nos_without_alert(
        [li.appl_no for li in matched if li.appl_no not in changed_appl_nos]
    )
    to_alert = changed + [
        m
        for m in matches
        if m.listing.appl_no in missed_appl_nos and m.listing.appl_no not in changed_appl_nos
    ]
    alerts = build_alerts(to_alert)

    # A clean run writes its digest (even when empty: that empty file is the
    # truthful "ran, no changes" record and stops `/watch/latest` re-surfacing a
    # stale digest as current). But an ERRORED run with no alerts must NOT stamp an
    # empty all-clear digest — that asserts a clean no-change day (INV-4: never
    # report a run state that did not happen) and would clobber an earlier
    # same-day digest. The non-zero exit code already signals the failure.
    if stats.errors and not alerts:
        out_path = digest_path()
        log.info("watch_digest_skipped_on_error", errors=stats.errors, path=str(out_path))
    else:
        out_path = write_digest(alerts)

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
    return WatchRunResult(
        listings=len(listings),
        matched=len(matched),
        stats=stats,
        alerts=alerts,
        digest_path=out_path,
    )
