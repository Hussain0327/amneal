"""White-Paper API + CLI contract — auth, audit rows, 422, docx, CLI smoke."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from typer.testing import CliRunner

import regwatch.api.main as api_main
from regwatch.api.main import app
from regwatch.cli import app as cli_app
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources
from tests.conftest import create_user, login_client

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


def _built_result(auth_client: TestClient) -> dict[str, Any]:
    """One successful POST /whitepaper — the exact payload the docx call sends."""
    r = auth_client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
    assert r.status_code == 200, r.text
    return r.json()


def _forbid_repopulate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docx endpoint must never re-populate: no fetches, no LLM, no audit.

    Any call into build_whitepaper (the only door to the live sources and the
    synthesizer) fails the test outright.
    """

    def boom(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("docx endpoint re-populated (live fetch/LLM path was invoked)")

    monkeypatch.setattr(api_main, "build_whitepaper", boom)


def test_whitepaper_requires_auth() -> None:
    client = TestClient(app)
    client.__enter__()
    try:
        r = client.post("/whitepaper", json={"rld_name": RLD_NAME, "application_number": APPL_NO})
        assert r.status_code == 401
        r2 = client.post("/whitepaper/docx", json={"result": {}})
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


def test_whitepaper_docx_renders_from_result_without_repopulating(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract C2: render FROM the reviewed result — zero re-populate."""
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 200, r.text
    assert "wordprocessingml.document" in r.headers["content-type"]
    assert f"whitepaper_{APPL_NO}.docx" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"

    # Exactly one lightweight docx_render audit row rides on top of the
    # populate row — never a second populate.
    rows = _whitepaper_audit_rows()
    assert [row["status"] for row in rows] == ["populated", "docx_rendered"]
    render_row = rows[1]
    assert render_row["route_json"]["reason"] == "docx_render"
    assert render_row["route_json"]["source_audit_id"] == result["audit_id"]
    assert APPL_NO in render_row["query_text"]
    assert render_row["user_id"] == rows[0]["user_id"]


def test_whitepaper_docx_rejects_foreign_audit_id(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's audit_id never renders — uniform 422, no docx audit row."""
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    create_user("other@example.com", "other-password-123")
    other = login_client("other@example.com", "other-password-123")
    try:
        r = other.post("/whitepaper/docx", json={"result": result})
    finally:
        other.__exit__(None, None, None)
    assert r.status_code == 422
    assert "white-paper runs" in r.json()["detail"]
    assert [row["status"] for row in _whitepaper_audit_rows()] == ["populated"]


def test_whitepaper_docx_rejects_fabricated_audit_id(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    result["audit_id"] = result["audit_id"] + 9999
    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 422
    assert [row["status"] for row in _whitepaper_audit_rows()] == ["populated"]


def test_whitepaper_docx_rejects_non_whitepaper_audit_row(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-owned audit row of another mode (qa) never authorizes a render."""
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    user_id = str(auth_client.get("/auth/me").json()["user"]["id"])
    with session_scope() as s:
        row = QueryLog(
            mode="qa",
            query_text="q",
            answer_text="a",
            refused=False,
            status="answer",
            model_name="stub",
            user_id=user_id,
        )
        s.add(row)
        s.flush()
        assert row.id is not None
        qa_audit_id = row.id
    result["audit_id"] = qa_audit_id
    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 422


def test_whitepaper_docx_rejects_resolution_failed_audit_row(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed-resolution audit row never authorizes a render of a pasted result."""
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    failed = auth_client.post(
        "/whitepaper", json={"rld_name": "ibuprofen", "application_number": APPL_NO}
    )
    assert failed.status_code == 422
    _forbid_repopulate(monkeypatch)

    with session_scope() as s:
        rows = s.scalars(select(QueryLog).where(QueryLog.mode == "whitepaper")).all()
        failed_ids = [r.id for r in rows if r.status == "resolution_failed" and r.id is not None]
    assert len(failed_ids) == 1
    result["audit_id"] = failed_ids[0]
    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: result.pop("audit_id"),
        lambda result: result.update(audit_id="42"),
        lambda result: result.pop("sections"),
        lambda result: result.update(sections=[]),
        lambda result: result.update(sections=[{"title": "x", "cells": [{"id": "y"}]}]),
        lambda result: result.update(spine={}),
        lambda result: result["sections"][0]["cells"][0].update(value=123),
        lambda result: result["sections"][0]["cells"][0].pop("evidence"),
        lambda result: result["sections"][0]["cells"][0].update(evidence=["not-an-object"]),
    ],
)
def test_whitepaper_docx_rejects_malformed_result(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    mutate(result)
    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 422
    assert "exact JSON object" in r.json()["detail"]


@pytest.mark.parametrize(
    "appl_no",
    [
        '020503"\r\nX-Evil: injected',
        "020503\r\nSet-Cookie: x=1",
        '0205"03',
        "../../etc/passwd",
        "",
        "   ",
    ],
)
def test_whitepaper_docx_rejects_header_unsafe_application_number(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    appl_no: str,
) -> None:
    """spine.application_number lands in the Content-Disposition filename (and
    the audit row): anything that is not application-number-shaped is a 422 —
    CR/LF or quotes never reach the response header."""
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    result["spine"]["application_number"] = appl_no
    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 422
    assert "application_number" in r.json()["detail"]
    # No docx audit row was written for the rejected render.
    assert [row["status"] for row in _whitepaper_audit_rows()] == ["populated"]


def test_whitepaper_docx_accepts_prefixed_application_number(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strict pattern still admits every shape /whitepaper can return."""
    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    result["spine"]["application_number"] = f"NDA{APPL_NO}"
    r = auth_client.post("/whitepaper/docx", json={"result": result})
    assert r.status_code == 200, r.text
    assert f"whitepaper_NDA{APPL_NO}.docx" in r.headers["content-disposition"]


def test_whitepaper_docx_rate_limited(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract C2(e): the docx render keeps drawing from the /query budget."""
    import config.settings as cs

    install_fake_sources(monkeypatch)
    result = _built_result(auth_client)
    _forbid_repopulate(monkeypatch)

    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    cs.get_settings.cache_clear()
    try:
        assert auth_client.post("/whitepaper/docx", json={"result": result}).status_code == 200
        assert auth_client.post("/whitepaper/docx", json={"result": result}).status_code == 429
    finally:
        cs.get_settings.cache_clear()


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
    assert {"/query", "/whitepaper"} <= paths
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
