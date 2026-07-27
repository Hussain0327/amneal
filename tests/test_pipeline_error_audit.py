"""INV-6: an unexpected pipeline crash inside ask() still leaves an audit row.

``compute_turn`` (the Go control plane's compute half) already wraps ``ask_core``
in an audited-error boundary, so POST /query is covered while GO_NATIVE_QUERY is
true. ``ask()`` -- the shell behind POST /query/stream (which the Go proxy never
serves natively) and behind POST /query whenever GO_NATIVE_QUERY is false (the
code default and the documented rollback) -- called ``ask_core`` unguarded, so a
raise in retrieve()/rerank/resolve produced ZERO query_log rows for a turn that
actually ran. These pin the boundary on both live surfaces plus the shell itself.

The injected failure is a raise from ``retrieve`` -- the retrieve+rerank hot path
every answerable query passes through, and the site of both a real external
embedding call and a real pgvector SQL call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests.conftest import create_user, session_client
from tests.test_invariants import _meta, _seed_corpus

pytestmark = pytest.mark.invariants


def _boom(*args: Any, **kwargs: Any) -> Any:
    """Stand-in for a pgvector/embedder outage inside retrieve()."""
    raise RuntimeError("simulated pgvector outage")


def _only_query_log_row() -> dict[str, Any]:
    """Snapshot of the single query_log row (dict: the ORM object detaches)."""
    with session_scope() as s:
        rows = list(s.exec(select(QueryLog)))
        assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
        row = rows[0]
        return {
            "id": row.id,
            "mode": row.mode,
            "status": row.status,
            "refused": row.refused,
            "route_json": row.route_json,
            "retrieved_json": row.retrieved_json,
            "citations_json": row.citations_json,
        }


def test_retrieval_error_degrades_to_audited_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """ask() itself: a retrieve() raise becomes a status="error" refusal with a
    durable row, never an escaping exception."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "retrieve", _boom)

    result = qa_mod.ask("What study design is recommended?")

    assert result.refused is True
    assert result.status == "error"
    assert result.reason == "pipeline_error"
    assert result.citations == []
    assert result.answer == qa_mod._SERVICE_UNAVAILABLE_TEXT
    # No internal detail in the user-visible copy.
    assert "RuntimeError" not in result.answer
    assert "pgvector" not in result.answer

    assert result.audit_id is not None and result.audit_id > 0
    row = _only_query_log_row()
    assert row["id"] == result.audit_id
    assert row["mode"] == "qa"
    assert row["status"] == "error"
    assert row["refused"] is True
    assert row["route_json"]["reason"] == "pipeline_error"
    # Nothing was retrieved, so nothing may be recorded as retrieved.
    assert row["retrieved_json"] == []
    assert row["citations_json"] == []


def test_query_stream_retrieval_error_still_audits_and_sends_result_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /query/stream -- the surface the Go proxy always relays. A pipeline
    crash must leave one audit row (INV-6) and a terminal status="error" result
    frame instead of a frameless close with no trace of the turn."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "retrieve", _boom)
    client = session_client(create_user())
    try:
        res = client.post(
            "/query/stream",
            json={"question": "What study design is recommended?"},
            headers={"Accept": "text/event-stream"},
        )
        assert res.status_code == 200
        body = res.text
    finally:
        client.__exit__(None, None, None)

    # INV-6 first: the turn ran (resolution + retrieval were reached), so it
    # must have left a durable row -- before the fix there were zero.
    row = _only_query_log_row()
    assert row["status"] == "error"
    assert row["route_json"]["reason"] == "pipeline_error"

    results = [
        json.loads(block.split("data:", 1)[1].strip())
        for block in body.split("\n\n")
        if block.startswith("event: result")
    ]
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert results[0]["refused"] is True
    assert "RuntimeError" not in body


def test_query_relay_retrieval_error_returns_audited_error_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /query served by Python -- the GO_NATIVE_QUERY=false relay/rollback
    path. A pipeline crash is an audited status="error" turn, not a naked 500
    (nor the unaudited 503 the openai-error handler would have returned)."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "retrieve", _boom)
    client = session_client(create_user())
    try:
        res = client.post("/query", json={"question": "What study design is recommended?"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "error"
        assert body["refused"] is True
        assert body["citations"] == []
    finally:
        client.__exit__(None, None, None)

    row = _only_query_log_row()
    assert row["status"] == "error"
    assert row["route_json"]["reason"] == "pipeline_error"
