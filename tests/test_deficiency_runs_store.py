"""Store tests for deficiency_run: the CAS state machine + read-time staleness.

The transitions are the safety mechanism that makes the in-process background
runner honest: the analyze timeout abandons (not kills) the worker thread, so
a late ``complete_run`` racing an earlier ``fail_run`` must LOSE -- a timed-out
run may never flip back to complete, and its report is discarded on purpose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from regwatch.store import deficiency_runs as dr
from regwatch.store.models import DeficiencyRun
from tests.conftest import create_user

_SHA = "ab" * 32


def _mk_run(user_id: int, filename: str = "sample.pdf") -> DeficiencyRun:
    return dr.create_run(user_id=user_id, filename=filename, sha256=_SHA)


def test_create_and_get_roundtrip() -> None:
    uid = create_user()
    run = _mk_run(uid)
    assert run.id is not None
    assert run.status == "pending"
    got = dr.get_run(run.id)
    assert got is not None
    assert (got.filename, got.sha256, got.status) == ("sample.pdf", _SHA, "pending")
    assert got.report_json is None
    assert got.error is None
    assert got.started_at is None


def test_claim_running_is_single_shot() -> None:
    uid = create_user()
    run = _mk_run(uid)
    assert run.id is not None
    assert dr.claim_running(run.id) is True
    claimed = dr.get_run(run.id)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.started_at is not None
    # Second claim finds the row no longer pending.
    assert dr.claim_running(run.id) is False


def test_complete_applies_only_from_running() -> None:
    uid = create_user()
    run = _mk_run(uid)
    assert run.id is not None
    # Never claimed: complete must not apply.
    assert (
        dr.complete_run(run.id, report={"faults": []}, fault_count=0, page_count=3, audit_id=None)
        is False
    )
    assert dr.claim_running(run.id) is True
    assert (
        dr.complete_run(run.id, report={"faults": []}, fault_count=0, page_count=3, audit_id=7)
        is True
    )
    got = dr.get_run(run.id)
    assert got is not None
    assert got.status == "complete"
    assert got.fault_count == 0
    assert got.page_count == 3
    assert got.audit_id == 7
    assert got.report_json == {"faults": []}
    assert got.completed_at is not None
    # Terminal: nothing rewrites a completed run.
    assert dr.fail_run(run.id, error="late failure") is False
    assert (
        dr.complete_run(run.id, report={"faults": [{}]}, fault_count=1, page_count=3, audit_id=8)
        is False
    )


def test_timeout_race_discards_late_report() -> None:
    """API timeout writer fails the run first; the orphaned worker thread's
    later complete_run loses and the run stays failed with no report."""
    uid = create_user()
    run = _mk_run(uid)
    assert run.id is not None
    assert dr.claim_running(run.id) is True
    assert dr.fail_run(run.id, error="analysis timed out after 600s") is True
    assert (
        dr.complete_run(run.id, report={"faults": []}, fault_count=0, page_count=1, audit_id=None)
        is False
    )
    got = dr.get_run(run.id)
    assert got is not None
    assert got.status == "failed"
    assert got.error is not None and "timed out" in got.error
    assert got.report_json is None


def test_fail_from_pending_then_claim_loses() -> None:
    uid = create_user()
    run = _mk_run(uid)
    assert run.id is not None
    assert dr.fail_run(run.id, error="boom") is True
    got = dr.get_run(run.id)
    assert got is not None
    assert (got.status, got.error) == ("failed", "boom")
    # Runner arriving after the terminal write exits without doing work.
    assert dr.claim_running(run.id) is False


def test_fail_error_truncated() -> None:
    uid = create_user()
    run = _mk_run(uid)
    assert run.id is not None
    dr.fail_run(run.id, error="x" * 2000)
    got = dr.get_run(run.id)
    assert got is not None
    assert got.error is not None and len(got.error) == 500


def test_list_runs_newest_first_with_total() -> None:
    uid = create_user()
    first = _mk_run(uid, filename="first.pdf")
    second = _mk_run(uid, filename="second.pdf")
    rows, total = dr.list_runs(limit=10)
    assert total >= 2
    ids = [r.id for r in rows]
    assert ids.index(second.id) < ids.index(first.id)


def test_effective_status_passthrough_and_stale_cutoff() -> None:
    now = datetime(2026, 7, 31, 12, 0, 0)

    def run_with(**kw: object) -> DeficiencyRun:
        base: dict[str, object] = {
            "created_by_user_id": 1,
            "filename": "f.pdf",
            "sha256": _SHA,
            "created_at": now,
        }
        base.update(kw)
        return DeficiencyRun(**base)  # type: ignore[arg-type]

    fresh = run_with(status="pending", created_at=now - timedelta(minutes=5))
    assert dr.effective_status(fresh, stale_minutes=20, now=now) == ("pending", None)

    stale_pending = run_with(status="pending", created_at=now - timedelta(minutes=21))
    status, error = dr.effective_status(stale_pending, stale_minutes=20, now=now)
    assert status == "failed"
    assert error is not None and "did not complete" in error

    # started_at is the anchor once running: created long ago, started recently
    # -> still running; started long ago -> failed.
    running_fresh = run_with(
        status="running",
        created_at=now - timedelta(hours=3),
        started_at=now - timedelta(minutes=1),
    )
    assert dr.effective_status(running_fresh, stale_minutes=20, now=now)[0] == "running"
    running_stale = run_with(status="running", started_at=now - timedelta(minutes=30))
    assert dr.effective_status(running_stale, stale_minutes=20, now=now)[0] == "failed"

    # Terminal states pass through untouched no matter how old.
    done = run_with(status="complete", created_at=now - timedelta(days=30))
    assert dr.effective_status(done, stale_minutes=20, now=now) == ("complete", None)
    failed = run_with(status="failed", error="boom", created_at=now - timedelta(days=30))
    assert dr.effective_status(failed, stale_minutes=20, now=now) == ("failed", "boom")


def test_effective_status_handles_aware_and_naive_timestamps() -> None:
    """Fresh in-memory rows carry aware datetimes (datetime.now(UTC)); reloaded
    rows carry naive-UTC. Both must compare without raising."""
    aware_now = datetime.now(UTC)
    aware_run = DeficiencyRun(
        created_by_user_id=1,
        filename="f.pdf",
        sha256=_SHA,
        status="pending",
        created_at=aware_now,
    )
    assert dr.effective_status(aware_run, stale_minutes=20, now=aware_now)[0] == "pending"
    naive_run = DeficiencyRun(
        created_by_user_id=1,
        filename="f.pdf",
        sha256=_SHA,
        status="pending",
        created_at=aware_now.replace(tzinfo=None),
    )
    assert dr.effective_status(naive_run, stale_minutes=20, now=aware_now)[0] == "pending"
