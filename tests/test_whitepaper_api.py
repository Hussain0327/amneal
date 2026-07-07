"""White-Paper API + CLI contract - auth, audit rows, 422, run persistence, CLI smoke.

The durable-run surface (GET/POST/DELETE /whitepaper/runs...) is covered in
test_whitepaper_runs_api.py; this module owns POST /whitepaper itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from typer.testing import CliRunner

from regwatch.api.main import app
from regwatch.cli import app as cli_app
from regwatch.store import whitepaper_runs as run_store
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources

runner = CliRunner()


def _whitepaper_audit_rows() -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(QueryLog).where(QueryLog.mode == "whitepaper"))
        return [
            {
                "user_id": r.user_id,
                "status": r.status,
                "refused": r.refused,
                "query_text": r.query_text,
                "route_json": dict(r.route_json),
            }
            for r in rows
        ]


def test_whitepaper_requires_auth() -> None:
    client = TestClient(app)
    client.__enter__()
    try:
        r = client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
        assert r.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_whitepaper_success_contract_and_audit(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    r = auth_client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spine"]["application_number"] == APPL_NO
    assert body["spine"]["application_type"] == "NDA"
    assert isinstance(body["audit_id"], int)
    assert len(body["sections"]) == 4
    # The cell wire shape is exactly as contracted.
    cell = body["sections"][0]["cells"][0]
    assert set(cell) == {"id", "label", "mode", "status", "value", "evidence", "note"}

    rows = _whitepaper_audit_rows()
    assert len(rows) == 1
    assert rows[0]["user_id"] is not None
    assert rows[0]["status"] == "populated"


def test_whitepaper_response_carries_persisted_run_id(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful populate is durable: run_id lands in the response and the
    run is immediately readable (org-shared) with the same audit linkage."""
    install_fake_sources(monkeypatch)
    r = auth_client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["run_id"], int)

    detail = auth_client.get(f"/whitepaper/runs/{body['run_id']}")
    assert detail.status_code == 200, detail.text
    stored = detail.json()
    assert stored["source_audit_id"] == body["audit_id"]
    assert stored["status"] == "draft"
    assert stored["application_number"] == APPL_NO


def test_whitepaper_persist_failure_degrades_with_warning(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing durability must not lose the expensive populate: run_id null +
    an explicit warning, the result and its audit row still ship."""
    install_fake_sources(monkeypatch)

    def boom(**kwargs: Any) -> NoReturn:
        raise RuntimeError("db down")

    # Patch the store module itself: api.main binds it as `run_store` and
    # resolves the attribute at call time, so the failure injects cleanly.
    monkeypatch.setattr(run_store, "create_run", boom)
    r = auth_client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] is None
    assert any("Saving this run failed" in w for w in body["warnings"])
    assert isinstance(body["audit_id"], int)
    # The populate audit row exists; no run was persisted.
    assert [row["status"] for row in _whitepaper_audit_rows()] == ["populated"]
    listing = auth_client.get("/whitepaper/runs")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0


def test_whitepaper_422_on_mismatch_writes_audit(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    r = auth_client.post(
        "/whitepaper", json={"rld_name": "ibuprofen", "application_number": APPL_NO}
    )
    assert r.status_code == 422
    assert "ibuprofen" in r.json()["detail"]
    # The resolution failure still leaves exactly one audit row (INV-6).
    rows = _whitepaper_audit_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "resolution_failed"
    assert rows[0]["refused"] is True
    # A failed resolution never persists a run.
    assert auth_client.get("/whitepaper/runs").json()["total"] == 0


def _all_route_paths(routes: Iterable[Any]) -> set[str]:
    """Every route path reachable from ``routes``, recursing through routers.

    FastAPI 0.137 / Starlette 1.3 stopped flattening ``include_router`` into
    ``app.routes``: an included router now appears as an opaque ``_IncludedRouter``
    wrapper (no ``.path``) that exposes its real routes via ``original_router``.
    Recurse so this INV-3 check still inspects /query, /whitepaper, … instead of
    passing vacuously over just the wrapper objects.
    """
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        original = getattr(route, "original_router", None)
        sub = getattr(original, "routes", None)
        if sub is not None:
            paths |= _all_route_paths(sub)
    return paths


def test_no_draft_or_submit_endpoints() -> None:
    # The system surfaces and cites; it never authors/files (INV-3). No route may
    # be named to imply submission drafting.
    paths = _all_route_paths(app.routes)
    # Guard against vacuous success: if route introspection ever stops surfacing
    # the real endpoints (as the FastAPI 0.137 upgrade did), fail loudly rather
    # than silently asserting over an empty set.
    assert {"/query", "/whitepaper", "/whitepaper/runs"} <= paths
    for path in paths:
        lowered = path.lower()
        assert "draft" not in lowered
        assert "submit" not in lowered
        assert "file-anda" not in lowered


def test_legacy_client_echo_docx_endpoint_removed() -> None:
    """The client-echo render path is gone: the only docx route is the saved-run
    one, so a tampered echoed payload has no endpoint to reach."""
    paths = _all_route_paths(app.routes)
    assert "/whitepaper/docx" not in paths
    assert "/whitepaper/runs/{run_id}/docx" in paths


def test_cli_whitepaper_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    install_fake_sources(monkeypatch)
    json_out = tmp_path / "wp.json"
    docx_out = tmp_path / "wp.docx"
    result = runner.invoke(
        cli_app,
        [
            "whitepaper",
            "--appl",
            APPL_NO,
            "--rld",
            RLD_NAME,
            "--json",
            str(json_out),
            "--docx",
            str(docx_out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Proposed Generic Product" in result.output
    assert json_out.exists()
    assert docx_out.read_bytes()[:2] == b"PK"


def test_cli_whitepaper_resolution_failure_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    result = runner.invoke(cli_app, ["whitepaper", "--appl", APPL_NO, "--rld", "ibuprofen"])
    assert result.exit_code == 2
