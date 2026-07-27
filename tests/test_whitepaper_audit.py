"""INV-6: every /whitepaper build -- populated, refused, OR errored -- audits.

``build_whitepaper`` audited only the two typed failures it names in its
docstring (unresolved spine, build deadline). Everything after the fetch phase
(``_build_sections`` / ``_status_counts`` / the success-path ``log_query``) ran
outside any boundary, so an unexpected error there escaped as a naked 500 with
ZERO mode="whitepaper" rows -- while the Requirements cell's inner ``ask()``
had already written its own mode="qa" row, leaving an orphan turn with no
owning operation in the ledger.

These tests lock the 009cc41 boundary shape for the three escapes:

- a mid-build failure still leaves one status="error" row (reason
  "build_error") before it re-raises;
- an audit-write failure on the resolution path can no longer REPLACE the
  ``SpineResolutionError`` the route maps to 422 (it degraded to a 500);
- a failed success-path audit write is attempted again as a status="error" row
  and then re-raises -- deliberately NOT degraded into a returned paper, since
  a white paper with no audit row (or one citing an id that references no row)
  breaks no-audit-no-answer (grounded_qa._persist_turn).
"""

from __future__ import annotations

from typing import Any, NoReturn

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from regwatch.whitepaper import populator
from regwatch.whitepaper.populator import SpineResolutionError, build_whitepaper
from tests._whitepaper_stub import APPL_NO, RLD_NAME, install_fake_sources
from tests.conftest import AuthedClient

pytestmark = pytest.mark.invariants


def _whitepaper_audit_rows() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(select(QueryLog).where(QueryLog.mode == "whitepaper"))
        return [
            {
                "status": r.status,
                "refused": r.refused,
                "answer_text": r.answer_text,
                "route_json": dict(r.route_json),
            }
            for r in rows
        ]


def _recording_failing_log_query(calls: list[dict[str, Any]]) -> Any:
    """A ``log_query`` stand-in that records its kwargs then fails like a down DB."""

    def failing(**kwargs: Any) -> NoReturn:
        calls.append(kwargs)
        raise OperationalError("INSERT INTO query_log", {}, Exception("db down"))

    return failing


def test_mid_build_error_writes_error_audit_row_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure AFTER the fetch phase must still leave exactly one audit row."""
    install_fake_sources(monkeypatch)

    def boom(_ctx: Any) -> NoReturn:
        raise RuntimeError("simulated mid-build failure")

    monkeypatch.setattr(populator, "_build_sections", boom)

    # Re-raised, not degraded: WhitepaperResponse has no refusal shape, and a
    # fabricated empty paper would be persisted as a finalizable run (INV-3).
    with pytest.raises(RuntimeError, match="simulated mid-build failure"):
        build_whitepaper(RLD_NAME, APPL_NO)

    rows = _whitepaper_audit_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["refused"] is True
    assert rows[0]["route_json"]["reason"] == "build_error"
    assert rows[0]["route_json"]["error_type"] == "RuntimeError"
    # No invented content in the audit answer.
    assert "could not be built" in rows[0]["answer_text"]


def test_resolution_audit_write_failure_still_raises_spine_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB blip during the resolution audit write must not mask the 422."""
    install_fake_sources(monkeypatch)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(populator, "log_query", _recording_failing_log_query(calls))

    with pytest.raises(SpineResolutionError):
        build_whitepaper("ibuprofen", APPL_NO)

    # Exactly one audit ATTEMPT: the resolution row. A second attempt would mean
    # the catch-all boundary re-caught the re-raise and double-audited the 422.
    assert [c["status"] for c in calls] == ["resolution_failed"]


def test_resolution_audit_write_failure_still_returns_422(
    auth_client: AuthedClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP twin of the above: the route keeps its 422, not a 500."""
    install_fake_sources(monkeypatch)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(populator, "log_query", _recording_failing_log_query(calls))

    r = auth_client.post(
        "/whitepaper", json={"rld_name": "ibuprofen", "application_number": APPL_NO}
    )
    assert r.status_code == 422, r.text
    assert "ibuprofen" in r.json()["detail"]


def test_success_audit_write_failure_attempts_error_row_then_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed success-path audit write is audited (best effort), then raises.

    Pins the no-audit-no-answer choice against a future "just make it safe"
    regression: ``_log_query_safe`` returns no id, so a paper shipped through it
    would carry a ``source_audit_id`` referencing no row.
    """
    install_fake_sources(monkeypatch)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(populator, "log_query", _recording_failing_log_query(calls))

    with pytest.raises(OperationalError):
        build_whitepaper(RLD_NAME, APPL_NO)

    assert [c["status"] for c in calls] == ["populated", "error"]
    assert calls[1]["refused"] is True
    assert calls[1]["route_json"]["reason"] == "build_error"
    assert calls[1]["route_json"]["error_type"] == "OperationalError"
