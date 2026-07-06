"""Durable watch-run ledger: persist + read one row per COMPLETED Watch run.

WHY: the JSONL digest lives on the GitHub cron runner's ephemeral disk, so
before this ledger the API had no way to tell a quiet day (recent run, zero
alerts) from a cron that has been dead for a week -- both looked like an empty
feed. GET /watch/latest surfaces the newest row as ``last_run`` so the UI can
show run freshness next to the alerts.

Recording is deliberately narrow (INV-4 -- never report a run state that did
not happen):
  * a run that COMPLETES records a row, INCLUDING completed-with-errors runs
    (CLI exit 2): an errored-but-completed run is a real run and its errors
    count is the truthful record of it;
  * a run that RAISES (e.g. the zero-listings crawl guard) records NOTHING --
    the cron's dead-man's-switch owns that failure class, and a ledger row
    would misreport an aborted run as having happened.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc
from sqlmodel import select

from regwatch.store.db import session_scope
from regwatch.store.models import WatchRun


def record_watch_run(
    *,
    started_at: datetime,
    finished_at: datetime,
    listings: int,
    matched: int,
    added: int,
    revised: int,
    unchanged: int,
    errors: int,
    alerts: int,
    digest_date: str | None,
) -> None:
    """Persist one completed run.

    Own ``session_scope`` on purpose: the caller (run.py) wraps this call in a
    log-loudly-but-do-not-crash guard because the digest + alert rows are
    already durable by the time it runs, so a DB hiccup here must fail alone
    -- never inside a shared transaction it could poison.
    """
    with session_scope() as s:
        s.add(
            WatchRun(
                started_at=started_at,
                finished_at=finished_at,
                listings=listings,
                matched=matched,
                added=added,
                revised=revised,
                unchanged=unchanged,
                errors=errors,
                alerts=alerts,
                digest_date=digest_date,
            )
        )


def latest_watch_run() -> dict[str, Any] | None:
    """The newest completed run in the wire shape ``/watch/latest`` embeds as
    ``last_run``; None when no run has ever completed (the truthful "never ran"
    signal, distinct from "ran and found nothing").

    Newest by finished_at with an id tie-break so a same-timestamp collision is
    deterministic (mirrors alerts.py's latest-version ordering). Materialized
    INSIDE the session: expire_on_commit detaches the row on scope exit, so
    reading attributes afterward would lazy-load against a closed session.
    """
    with session_scope() as s:
        row = s.scalars(
            select(WatchRun)
            .order_by(desc(WatchRun.finished_at), desc(WatchRun.id))  # type: ignore[arg-type]
            .limit(1)
        ).first()
        if row is None:
            return None
        return {
            # isoformat of the round-tripped naive-UTC DateTime columns -- the
            # same string shape the alert feed's captured_at uses on the wire.
            "started_at": row.started_at.isoformat(),
            "finished_at": row.finished_at.isoformat(),
            "listings": row.listings,
            "matched": row.matched,
            "added": row.added,
            "revised": row.revised,
            "unchanged": row.unchanged,
            "errors": row.errors,
            "alerts": row.alerts,
        }
