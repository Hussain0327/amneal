"""API contract for /deficiency: upload guards, 202 + background scheduling,
read-time staleness in list/detail, and verbatim report passthrough.

The real detection pipeline never runs here: the runner seam is monkeypatched
at its use site (api_main.run_deficiency_analysis), which also proves the
endpoint's only coupling to the pipeline is that one callable.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import config.settings as cs
import pytest
from sqlalchemy import update
from sqlmodel import col

import regwatch.api.main as api_main
from regwatch.store import deficiency_runs as dr
from regwatch.store.db import session_scope
from regwatch.store.models import DeficiencyRun
from tests.conftest import create_user, session_client

PDF_BYTES = b"%PDF-1.4\n% deficiency api test stub\n1 0 obj\nendobj\ntrailer\n%%EOF\n"


def test_analyze_requires_auth() -> None:
    from fastapi.testclient import TestClient

    with TestClient(api_main.app) as client:
        r = client.post(
            "/deficiency/analyze",
            files={"file": ("s.pdf", PDF_BYTES, "application/pdf")},
        )
        assert r.status_code == 401


def test_analyze_rejects_non_pdf() -> None:
    client = session_client(create_user())
    r = client.post(
        "/deficiency/analyze",
        files={"file": ("notes.pdf", b"MZ this is not a pdf", "application/pdf")},
    )
    assert r.status_code == 400
    assert "not a PDF" in r.json()["detail"]


def test_analyze_rejects_empty_upload() -> None:
    client = session_client(create_user())
    r = client.post("/deficiency/analyze", files={"file": ("empty.pdf", b"", "application/pdf")})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_analyze_rejects_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the cap instead of shipping 50 MB through the test client.
    monkeypatch.setattr(api_main, "_DEFICIENCY_MAX_PDF_BYTES", 1024)
    client = session_client(create_user())
    r = client.post(
        "/deficiency/analyze",
        files={"file": ("big.pdf", b"%PDF" + b"0" * 4096, "application/pdf")},
    )
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]


def test_analyze_schedules_background_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, str, bool]] = []

    def fake_runner(run_id: int, tmp_path: str) -> None:
        calls.append((run_id, tmp_path, os.path.exists(tmp_path)))

    monkeypatch.setattr(api_main, "run_deficiency_analysis", fake_runner)
    client = session_client(create_user())
    r = client.post(
        "/deficiency/analyze",
        files={"file": ("submission.pdf", PDF_BYTES, "application/pdf")},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    run_id = body["run_id"]
    # TestClient executes background tasks before handing back the response.
    assert calls, "background task never invoked the runner"
    called_id, tmp_path, existed_during_run = calls[0]
    assert called_id == run_id
    assert existed_during_run is True
    assert not os.path.exists(tmp_path), "temp PDF must be deleted after the run"
    row = dr.get_run(run_id)
    assert row is not None
    assert row.filename == "submission.pdf"
    assert row.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert row.created_by_user_id == client.user_id


def test_background_timeout_marks_failed_and_leaves_orphan_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fail_after abandons the worker thread: the run reads failed immediately,
    and the temp file is deliberately NOT unlinked under the still-running
    orphan (see _deficiency_background)."""
    uid = create_user()
    run = dr.create_run(user_id=uid, filename="slow.pdf", sha256="cd" * 32)
    assert run.id is not None

    def slow_runner(run_id: int, tmp_path: str) -> None:
        time.sleep(1.0)

    monkeypatch.setattr(api_main, "run_deficiency_analysis", slow_runner)
    monkeypatch.setenv("DEFICIENCY_ANALYZE_TIMEOUT_S", "0.2")
    cs.get_settings.cache_clear()
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="deficiency-test-")
    os.close(fd)
    try:
        anyio.run(api_main._deficiency_background, run.id, tmp_path)
        got = dr.get_run(run.id)
        assert got is not None
        assert got.status == "failed"
        assert got.error is not None and "timed out" in got.error
        assert os.path.exists(tmp_path), "abandoned thread still owns the file"
    finally:
        cs.get_settings.cache_clear()
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _complete_payload(run_id: int) -> dict[str, Any]:
    return {
        "job_id": str(run_id),
        "faults": [],
        "analysis_seconds": 1.2,
    }


def test_list_and_detail_contract() -> None:
    client = session_client(create_user())
    uid = client.user_id
    done = dr.create_run(user_id=uid, filename="done.pdf", sha256="aa" * 32)
    assert done.id is not None
    assert dr.claim_running(done.id)
    payload = _complete_payload(done.id)
    assert dr.complete_run(done.id, report=payload, fault_count=0, page_count=2, audit_id=None)
    waiting = dr.create_run(user_id=uid, filename="waiting.pdf", sha256="bb" * 32)
    assert waiting.id is not None

    r = client.get("/deficiency/runs")
    assert r.status_code == 200
    body = r.json()
    assert {"count", "total", "limit", "offset", "runs"} <= set(body)
    runs = body["runs"]
    ids = [row["id"] for row in runs]
    assert ids.index(waiting.id) < ids.index(done.id), "newest first"
    by_id = {row["id"]: row for row in runs}
    assert by_id[done.id]["status"] == "complete"
    assert by_id[done.id]["fault_count"] == 0
    assert by_id[waiting.id]["status"] == "pending"
    assert "report" not in by_id[done.id], "list rows never carry the payload"

    detail = client.get(f"/deficiency/runs/{done.id}").json()
    assert detail["status"] == "complete"
    assert detail["report"] == payload, "stored report must pass through verbatim"
    pending_detail = client.get(f"/deficiency/runs/{waiting.id}").json()
    assert pending_detail["status"] == "pending"
    assert pending_detail["report"] is None

    assert client.get("/deficiency/runs/99999999").status_code == 404


def test_detail_reinterprets_stale_pending_as_failed() -> None:
    client = session_client(create_user())
    run = dr.create_run(user_id=client.user_id, filename="stuck.pdf", sha256="ee" * 32)
    assert run.id is not None
    stranded_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
    with session_scope() as s:
        s.execute(
            update(DeficiencyRun)
            .where(col(DeficiencyRun.id) == run.id)
            .values(created_at=stranded_at)
        )
    detail = client.get(f"/deficiency/runs/{run.id}").json()
    assert detail["status"] == "failed"
    assert detail["error"] is not None and "did not complete" in detail["error"]
    assert detail["report"] is None
    # The database row itself is never rewritten by a read.
    raw = dr.get_run(run.id)
    assert raw is not None and raw.status == "pending"
