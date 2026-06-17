"""POST /query/stream — the Server-Sent Events twin of POST /query.

These assert the INV-1 emit boundary holds over the wire: no answer text or
citation appears before the single validated ``result`` frame; refusals stream
no prose and never leak a fabricated citation; exactly one audit row is written
(INV-6); the streamed result matches blocking /query (parity); only the two
event names the frontend parses (``status``/``result``) are emitted; and auth /
rate-limit / ownership are enforced BEFORE the stream opens.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from config.settings import get_settings
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import select

from regwatch.generate import grounded_qa as qa_mod
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from tests.conftest import create_user, login_client
from tests.test_invariants import _meta, _seed_corpus, _stub_llm

pytestmark = pytest.mark.invariants

_ACCEPT = {"Accept": "text/event-stream"}


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into ``[(event_name, data_str), ...]`` (one per frame)."""
    frames: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].removeprefix(" "))
        if event is not None:
            frames.append((event, "\n".join(data_lines)))
    return frames


def _stream(client: TestClient, question: str, **body: Any) -> httpx.Response:
    return client.post("/query/stream", json={"question": question, **body}, headers=_ACCEPT)


def _result_payload(frames: list[tuple[str, str]]) -> dict[str, Any]:
    results = [json.loads(d) for e, d in frames if e == "result"]
    assert len(results) == 1, f"expected exactly one result frame, got {len(results)}"
    return results[0]


def _row_count() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(QueryLog)) or 0)


def test_query_stream_streams_progress_then_one_result_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-or-more status frames, then exactly one terminal result frame —
    answer text lives ONLY in the result (INV-1), never in a status frame."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    answer = "A fasting bioequivalence study is recommended [PSG_020503, p.3]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer))
    create_user()
    client = login_client()
    try:
        res = _stream(client, "What study design is recommended?")
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        frames = _parse_sse(res.text)
        events = [e for e, _ in frames]
        # The contract: only `status` and `result`, exactly one result, last.
        assert set(events) <= {"status", "result"}
        assert events.count("result") == 1
        assert events[-1] == "result"
        # Real progress streamed (not just one canned line): the heartbeat plus
        # at least one genuine pipeline phase, each a {"text": ...} payload.
        status_payloads = [json.loads(d) for e, d in frames if e == "status"]
        assert len(status_payloads) >= 2
        assert all(set(p) == {"text"} for p in status_payloads)
        # INV-1: no answer prose and no citation marker in any status frame.
        for p in status_payloads:
            assert "PSG_020503" not in p["text"]
            assert "bioequivalence study is recommended" not in p["text"]
        # The single result frame carries the validated answer + citation.
        result = _result_payload(frames)
        assert result["refused"] is False
        assert "[PSG_020503, p.3]" in result["answer"]
        assert {(c["short_name"], c["page"]) for c in result["citations"]} == {("PSG_020503", 3)}
        assert isinstance(result["audit_id"], int)
        assert result["session_id"] and result["turn_id"]
    finally:
        client.__exit__(None, None, None)


def test_query_stream_matches_blocking_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streamed result frame is content-identical to blocking POST /query."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    answer = "A fasting study is recommended [PSG_020503, p.3]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(answer))
    create_user()
    client = login_client()
    try:
        q = "What study design is recommended?"
        blocking = client.post("/query", json={"question": q})
        assert blocking.status_code == 200
        blocking_json = blocking.json()
        streamed = _result_payload(_parse_sse(_stream(client, q).text))
        # Per-turn identifiers differ; the answer content must not.
        for field in ("answer", "refused", "status"):
            assert streamed[field] == blocking_json[field]
        assert streamed["citations"] == blocking_json["citations"]
    finally:
        client.__exit__(None, None, None)


def test_query_stream_refuses_fabricated_citation_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confident answer whose only citation is fabricated collapses to a
    refusal, and the fabricated marker never appears in ANY frame (INV-1)."""
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    fabricated = "The recommended dose is 100 mg per day [PSG_999999, p.99]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm(fabricated))
    create_user()
    client = login_client()
    try:
        body = _stream(client, "What is the recommended dose?").text
        # The fabricated citation is never emitted, in any frame.
        assert "PSG_999999" not in body
        result = _result_payload(_parse_sse(body))
        assert result["refused"] is True
        assert result["citations"] == []
        assert result["answer"] == get_settings().refusal_text
    finally:
        client.__exit__(None, None, None)


def test_query_stream_writes_exactly_one_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-6: one streamed query writes exactly one query_log row, attributed."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _stub_llm("Yes. [PSG_020503, p.3]")
    )
    user_id = create_user()
    client = login_client()
    try:
        before = _row_count()
        res = _stream(client, "Is a fasting study recommended?")
        assert res.status_code == 200
        assert _row_count() == before + 1
        with session_scope() as s:
            row = s.scalars(select(QueryLog)).all()[-1]
            assert row.mode == "qa"
            assert row.user_id == str(user_id)
    finally:
        client.__exit__(None, None, None)


def test_query_stream_requires_auth() -> None:
    """Unauthenticated callers get a real 401 — never an opened stream."""
    from regwatch.api.main import app

    client = TestClient(app)
    client.__enter__()
    try:
        res = client.post("/query/stream", json={"question": "anything at all?"}, headers=_ACCEPT)
        assert res.status_code == 401
        assert "text/event-stream" not in res.headers.get("content-type", "")
    finally:
        client.__exit__(None, None, None)
