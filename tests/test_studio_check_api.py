"""API contract for /studio/check: input bounds, 202 + background scheduling,
private-to-creator reads, and isolation from the /deficiency surface.

Studio checks and PDF uploads share one table, so half of this file exists to
prove they do not share visibility: a Studio run is readable only by the
analyst who made it, and it must not appear in -- or be reachable through --
the org-shared /deficiency routes.

The detection pipeline never runs here. The runner seam is monkeypatched at its
use site (api_main.run_studio_check), which also proves the endpoint's only
coupling to the pipeline is that one callable.
"""

from __future__ import annotations

from typing import Any

import anyio
import config.settings as cs
import pytest

import regwatch.api.main as api_main
from regwatch.store import deficiency_runs as dr
from tests.conftest import create_user, session_client


def _blocks(*texts: str) -> list[dict[str, Any]]:
    return [{"id": f"b{i}", "type": "p", "text": text} for i, text in enumerate(texts)]


def _body(*texts: str) -> dict[str, Any]:
    return {"name": "Spec.docx", "blocks": _blocks(*texts)}


def _stub_runner(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, int]]:
    calls: list[tuple[int, str, int]] = []

    def fake(run_id: int, name: str, blocks: list[dict[str, Any]]) -> None:
        calls.append((run_id, name, len(blocks)))

    monkeypatch.setattr(api_main, "run_studio_check", fake)
    return calls


def test_check_requires_auth() -> None:
    from fastapi.testclient import TestClient

    with TestClient(api_main.app) as client:
        assert client.post("/studio/check", json=_body("Assay 98.2 percent.")).status_code == 401


def test_check_rejects_a_document_with_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty document runs the whole pipeline and reports a clean result.
    # Refusing it at the boundary is the only way that outcome stays honest.
    _stub_runner(monkeypatch)
    client = session_client(create_user())

    r = client.post("/studio/check", json={"name": "Empty.docx", "blocks": []})

    assert r.status_code == 400
    assert "no text" in r.json()["detail"]


def test_check_rejects_whitespace_only_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runner(monkeypatch)
    client = session_client(create_user())

    r = client.post("/studio/check", json=_body("   ", "\n"))

    assert r.status_code == 400


def test_check_rejects_too_many_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main, "_STUDIO_MAX_BLOCKS", 3)
    _stub_runner(monkeypatch)
    client = session_client(create_user())

    r = client.post("/studio/check", json=_body("a", "b", "c", "d"))

    assert r.status_code == 400
    assert "blocks" in r.json()["detail"]


def test_check_rejects_an_oversize_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main, "_STUDIO_MAX_CHARS", 32)
    _stub_runner(monkeypatch)
    client = session_client(create_user())

    r = client.post("/studio/check", json=_body("x" * 64))

    assert r.status_code == 400
    assert "too large" in r.json()["detail"]


def test_check_schedules_the_background_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_runner(monkeypatch)
    client = session_client(create_user())

    r = client.post("/studio/check", json=_body("Assay is not less than 95.0 percent."))

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    # TestClient executes background tasks before handing back the response.
    assert calls == [(body["run_id"], "Spec.docx", 1)]
    row = dr.get_run(body["run_id"])
    assert row is not None
    assert row.source == "studio"
    assert row.filename == "Spec.docx"
    assert row.created_by_user_id == client.user_id


def test_the_same_document_hashes_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    # sha256 identifies the checked content, exactly as it does for an upload;
    # a per-request value would make two checks of one draft incomparable.
    _stub_runner(monkeypatch)
    client = session_client(create_user())

    first = client.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]
    second = client.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]
    third = client.post("/studio/check", json=_body("Assay 92.4 percent.")).json()["run_id"]

    hashes = []
    for run_id in (first, second, third):
        row = dr.get_run(run_id)
        assert row is not None
        hashes.append(row.sha256)

    assert hashes[0] == hashes[1]
    assert hashes[0] != hashes[2]


def test_a_check_run_is_private_to_its_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runner(monkeypatch)
    owner = session_client(create_user("owner@example.com"))
    run_id = owner.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]
    other = session_client(create_user("other@example.com"))

    assert owner.get(f"/studio/check/{run_id}").status_code == 200
    assert other.get(f"/studio/check/{run_id}").status_code == 404


def test_a_studio_run_is_not_reachable_through_the_deficiency_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both routes read one table. Without this guard the private rule above is
    # bypassed by asking the org-shared endpoint for the same id.
    _stub_runner(monkeypatch)
    client = session_client(create_user())
    run_id = client.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]

    assert client.get(f"/deficiency/runs/{run_id}").status_code == 404


def test_a_studio_run_is_absent_from_the_deficiency_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_runner(monkeypatch)
    client = session_client(create_user())
    upload = dr.create_run(user_id=client.user_id, filename="submission.pdf", sha256="aa" * 32)
    studio_id = client.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]

    ids = [row["id"] for row in client.get("/deficiency/runs").json()["runs"]]

    assert upload.id in ids
    assert studio_id not in ids


def test_an_upload_run_is_not_reachable_through_the_studio_route() -> None:
    client = session_client(create_user())
    upload = dr.create_run(user_id=client.user_id, filename="submission.pdf", sha256="aa" * 32)
    assert upload.id is not None

    assert client.get(f"/studio/check/{upload.id}").status_code == 404


def test_detail_passes_the_stored_report_through_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_runner(monkeypatch)
    client = session_client(create_user())
    run_id = client.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]
    payload = {"job_id": str(run_id), "faults": [], "analysis_seconds": 1.2}
    assert dr.claim_running(run_id)
    assert dr.complete_run(run_id, report=payload, fault_count=0, page_count=1, audit_id=None)

    detail = client.get(f"/studio/check/{run_id}").json()

    assert detail["status"] == "complete"
    assert detail["report"] == payload


def test_a_pending_run_carries_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runner(monkeypatch)
    client = session_client(create_user())
    run_id = client.post("/studio/check", json=_body("Assay 98.2 percent.")).json()["run_id"]

    detail = client.get(f"/studio/check/{run_id}").json()

    assert detail["status"] == "pending"
    assert detail["report"] is None


def test_background_timeout_marks_the_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """fail_after abandons the worker thread; the run reads failed immediately
    and the abandoned thread's complete_run loses the compare-and-set."""
    uid = create_user()
    run = dr.create_run(user_id=uid, filename="Slow.docx", sha256="cd" * 32, source="studio")
    assert run.id is not None

    def slow(run_id: int, name: str, blocks: list[dict[str, Any]]) -> None:
        import time

        time.sleep(1.0)

    monkeypatch.setattr(api_main, "run_studio_check", slow)
    monkeypatch.setenv("DEFICIENCY_ANALYZE_TIMEOUT_S", "0.2")
    cs.get_settings.cache_clear()
    try:
        anyio.run(api_main._studio_check_background, run.id, "Slow.docx", _blocks("slow"))
        got = dr.get_run(run.id)
        assert got is not None
        assert got.status == "failed"
        assert got.error is not None and "timed out" in got.error
    finally:
        cs.get_settings.cache_clear()


def test_an_unreachable_model_fails_the_run_and_serves_no_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of the audit-before-complete ordering: a run that could
    # not reach the model is failed and its report stays null, rather than
    # completing empty and reading as a clean document.
    import regwatch.deficiency.runner as runner

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Databricks serving endpoint unreachable")

    monkeypatch.setattr(runner, "run_detection", unreachable)
    uid = create_user()
    run = dr.create_run(user_id=uid, filename="Spec.docx", sha256="ef" * 32, source="studio")
    assert run.id is not None

    runner.run_studio_check(run.id, "Spec.docx", _blocks("Assay is not less than 95.0 percent."))

    got = dr.get_run(run.id)
    assert got is not None
    assert got.status == "failed"
    assert got.error is not None and "RuntimeError" in got.error
    assert got.report_json is None
