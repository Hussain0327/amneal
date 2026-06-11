"""White-Paper API + CLI contract — auth, audit rows, 422, docx, CLI smoke."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from typer.testing import CliRunner

from regwatch.api.main import app
from regwatch.cli import app as cli_app
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources

runner = CliRunner()


def _whitepaper_audit_rows() -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(QueryLog).where(QueryLog.mode == "whitepaper"))
        return [{"user_id": r.user_id, "status": r.status, "refused": r.refused} for r in rows]


def test_whitepaper_requires_auth() -> None:
    client = TestClient(app)
    client.__enter__()
    try:
        r = client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
        assert r.status_code == 401
        r2 = client.post(
            "/whitepaper/docx", json={"rld_name": RLD_NAME, "application_number": APPL_NO}
        )
        assert r2.status_code == 401
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


def test_whitepaper_docx_endpoint(auth_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_sources(monkeypatch)
    r = auth_client.post(
        "/whitepaper/docx", json={"rld_name": RLD_NAME, "application_number": APPL_NO}
    )
    assert r.status_code == 200, r.text
    assert "wordprocessingml.document" in r.headers["content-type"]
    assert f"whitepaper_{APPL_NO}.docx" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"


def test_no_draft_or_submit_endpoints() -> None:
    # The system surfaces and cites; it never authors/files (INV-3). No route may
    # be named to imply submission drafting.
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    for path in paths:
        lowered = path.lower()
        assert "draft" not in lowered
        assert "submit" not in lowered
        assert "file-anda" not in lowered


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
