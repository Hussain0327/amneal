"""Contract tests for POST /resolve — deterministic product resolution.

/resolve reuses the SAME spine resolution as /whitepaper (populator._build_context)
but writes NO audit row and returns no answer text: it lets a surface pin a
canonical product without running a populate. These tests pin that contract —
the bare-spine shape, the refuse-over-guess 422, auth, and (the load-bearing
invariant) that it logs ZERO QueryLog rows on success AND failure, unlike
/whitepaper which logs one either way.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import select

from regwatch.api.main import app
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources


def _query_log_count() -> int:
    """Total QueryLog rows — same convention as test_resolution_hardening.py."""
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)


def test_resolve_success_returns_bare_spine(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    r = auth_client.post(
        "/resolve", json={"rld_name": RLD_NAME, "application_number": APPL_NO}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The response IS the spine (not wrapped in {"spine": ...}, unlike /whitepaper).
    assert body["application_number"] == APPL_NO
    assert body["application_type"] == "NDA"
    assert body["normalized_name"] == "albuterol sulfate"
    # Entity resolution, not an LLM turn: no audit id, no answer, no cells.
    assert "audit_id" not in body
    assert "answer" not in body
    assert "sections" not in body


def test_resolve_422_on_name_number_mismatch(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    r = auth_client.post(
        "/resolve", json={"rld_name": "ibuprofen", "application_number": APPL_NO}
    )
    assert r.status_code == 422
    # The resolver's own detail is surfaced verbatim (refuse over guess).
    assert "ibuprofen" in r.json()["detail"]


def test_resolve_requires_auth() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/resolve", json={"rld_name": RLD_NAME, "application_number": APPL_NO}
        )
    assert r.status_code == 401


def test_resolve_success_writes_no_audit_row(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    before = _query_log_count()
    r = auth_client.post(
        "/resolve", json={"rld_name": RLD_NAME, "application_number": APPL_NO}
    )
    assert r.status_code == 200, r.text
    assert _query_log_count() == before


def test_resolve_failure_writes_no_audit_row(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_sources(monkeypatch)
    before = _query_log_count()
    r = auth_client.post(
        "/resolve", json={"rld_name": "ibuprofen", "application_number": APPL_NO}
    )
    assert r.status_code == 422
    # Unlike /whitepaper (which logs a status="resolution_failed" row), /resolve
    # writes nothing on failure either.
    assert _query_log_count() == before


def test_resolve_rate_limited(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/resolve draws on the same per-user query budget as /whitepaper."""
    import config.settings as cs

    install_fake_sources(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    cs.get_settings.cache_clear()
    try:
        body = {"rld_name": RLD_NAME, "application_number": APPL_NO}
        assert auth_client.post("/resolve", json=body).status_code == 200
        assert auth_client.post("/resolve", json=body).status_code == 429
    finally:
        cs.get_settings.cache_clear()
