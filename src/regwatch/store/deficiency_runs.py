"""CRUD + state machine for deficiency_run rows.

State transitions are compare-and-set UPDATEs (``WHERE status IN ...``), never
blind writes: the analyze timeout cancels the *await*, not the worker thread,
so a timed-out run is marked failed while the orphaned thread may still finish
minutes later -- its ``complete_run`` must then find ``status='failed'`` and
no-op instead of resurrecting the run. Every transition function returns
whether it actually applied, and callers log the losing side.

Rows stranded in pending/running by a process restart are reinterpreted at
read time by ``effective_status`` (see the model docstring); nothing rewrites
them in the database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from config.settings import get_settings
from sqlalchemy import update
from sqlmodel import col, select

from regwatch.common.logging import get_logger
from regwatch.store.db import session_scope
from regwatch.store.models import DeficiencyRun

log = get_logger(__name__)

_STALE_ERROR = "analysis did not complete (process restarted or timed out)"


def create_run(
    *, user_id: int, filename: str, sha256: str, source: str | None = None
) -> DeficiencyRun:
    """Create a pending run. ``source`` is None for a PDF upload and "studio"
    for a Compliance Studio check; it decides who may read the row back."""
    run = DeficiencyRun(
        created_by_user_id=user_id,
        filename=filename,
        sha256=sha256,
        status="pending",
        source=source,
    )
    with session_scope() as s:
        s.add(run)
        s.flush()
        s.refresh(run)
        s.expunge(run)
    return run


def claim_running(run_id: int) -> bool:
    """pending -> running. False when the row is missing or already terminal
    (e.g. the timeout fired before the limiter granted a slot)."""
    with session_scope() as s:
        # Applied-or-not via RETURNING, not cursor rowcount (psycopg v3 can
        # report -1 -- same reasoning as watch/alerts.py).
        row = s.execute(
            update(DeficiencyRun)
            .where(col(DeficiencyRun.id) == run_id, col(DeficiencyRun.status) == "pending")
            .values(status="running", started_at=datetime.now(UTC))
            .returning(col(DeficiencyRun.id))
        ).scalar_one_or_none()
        applied = row is not None
    if not applied:
        log.warning("deficiency_run_claim_lost", run_id=run_id)
    return applied


def complete_run(
    run_id: int,
    *,
    report: dict[str, Any],
    fault_count: int,
    page_count: int | None,
    audit_id: int | None,
) -> bool:
    """running -> complete. Loses (returns False) when the timeout already
    failed the run -- the late thread's report is then discarded on purpose."""
    with session_scope() as s:
        row = s.execute(
            update(DeficiencyRun)
            .where(col(DeficiencyRun.id) == run_id, col(DeficiencyRun.status) == "running")
            .values(
                status="complete",
                completed_at=datetime.now(UTC),
                report_json=report,
                fault_count=fault_count,
                page_count=page_count,
                audit_id=audit_id,
                error=None,
            )
            .returning(col(DeficiencyRun.id))
        ).scalar_one_or_none()
        applied = row is not None
    if not applied:
        log.warning("deficiency_run_complete_lost", run_id=run_id)
    return applied


def fail_run(run_id: int, *, error: str, audit_id: int | None = None) -> bool:
    """pending|running -> failed. Idempotent-by-outcome: a second failure
    writer finds a terminal row and no-ops."""
    values: dict[str, Any] = {
        "status": "failed",
        "completed_at": datetime.now(UTC),
        "error": error[:500],
    }
    if audit_id is not None:
        values["audit_id"] = audit_id
    with session_scope() as s:
        row = s.execute(
            update(DeficiencyRun)
            .where(
                col(DeficiencyRun.id) == run_id,
                col(DeficiencyRun.status).in_(("pending", "running")),
            )
            .values(**values)
            .returning(col(DeficiencyRun.id))
        ).scalar_one_or_none()
        applied = row is not None
    if not applied:
        log.warning("deficiency_run_fail_lost", run_id=run_id)
    return applied


def get_run(run_id: int) -> DeficiencyRun | None:
    with session_scope() as s:
        run = s.get(DeficiencyRun, run_id)
        if run is not None:
            s.expunge(run)
        return run


def list_runs(*, limit: int = 50, offset: int = 0) -> tuple[list[DeficiencyRun], int]:
    """Org-shared listing of UPLOAD runs, newest first (same product decision
    as white-paper runs: any authenticated analyst sees every run).

    Studio checks live in this table too and are deliberately excluded: they
    are private to the analyst who ran them, so surfacing them in an org-shared
    list would leak one analyst's draft into everyone else's history.
    """
    from sqlalchemy import func

    uploads_only = col(DeficiencyRun.source).is_(None)
    with session_scope() as s:
        total = int(
            s.execute(
                select(func.count()).select_from(DeficiencyRun).where(uploads_only)  # type: ignore[arg-type]
            ).scalar_one()
        )
        rows = (
            s.execute(
                select(DeficiencyRun)
                .where(uploads_only)
                .order_by(col(DeficiencyRun.created_at).desc(), col(DeficiencyRun.id).desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        for row in rows:
            s.expunge(row)
    return list(rows), total


def _naive_utc(ts: datetime) -> datetime:
    """Timestamps persist naive-UTC but in-memory (unsaved/refreshed) instances
    may carry aware values from ``datetime.now(UTC)`` -- normalize before math."""
    return ts if ts.tzinfo is None else ts.astimezone(UTC).replace(tzinfo=None)


def effective_status(
    run: DeficiencyRun,
    *,
    stale_minutes: int | None = None,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """The status a reader should trust: terminal states verbatim; a
    pending/running row past the stale cutoff reads as failed (the background
    task cannot have survived that long -- the analyze timeout is shorter)."""
    if run.status not in ("pending", "running"):
        return run.status, run.error
    limit = (
        stale_minutes if stale_minutes is not None else get_settings().deficiency_run_stale_minutes
    )
    anchor = run.started_at or run.created_at
    now_naive = _naive_utc(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    if now_naive - _naive_utc(anchor) > timedelta(minutes=limit):
        return "failed", _STALE_ERROR
    return run.status, run.error
