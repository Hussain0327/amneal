"""POST /query/stream — the Server-Sent Events twin of POST /query.

These assert the INV-1 emit boundary holds over the wire: no answer text or
citation appears before the single validated ``result`` frame; refusals stream
no prose and never leak a fabricated citation; exactly one audit row is written
(INV-6); the streamed result matches blocking /query (parity); only the three
event names the frontend parses (``status``/``token``/``result``) are emitted
(``token`` needs a streaming provider — covered in test_streaming_synthesis.py)
plus anonymous ``: keep-alive`` comment frames the parser skips; auth /
rate-limit / ownership are enforced BEFORE the stream opens; a mid-stream
failure closes the stream with NO result frame (the client's fallback
trigger); request filters are whitelisted at the boundary; and ask() dispatch
rides a dedicated bounded worker pool with defined behavior at saturation.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import anyio
import httpx
import pytest
from config.settings import get_settings
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import select

from regwatch.api import main as api_main
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
        # The contract: exactly one result, last. `token` is absent HERE only
        # because the shared _stub_llm is complete()-only (no .stream method);
        # token-frame coverage lives in test_streaming_synthesis.py.
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


def test_query_stream_foreign_session_is_404_before_stream() -> None:
    """Chat-session ownership is enforced BEFORE the stream opens: another
    user's session_id gets a real 404 (never confirming the session exists),
    not an opened stream that binds the turn into the victim's history."""
    create_user("a@example.com", "password-for-a")
    create_user("b@example.com", "password-for-b")
    a = login_client("a@example.com", "password-for-a")
    b = login_client("b@example.com", "password-for-b")
    try:
        sid = a.post("/query", json={"question": "Does this exist?"}).json()["session_id"]
        res = b.post(
            "/query/stream",
            json={"question": "And dissolution?", "session_id": sid},
            headers=_ACCEPT,
        )
        assert res.status_code == 404
        assert res.json() == {"detail": "session not found"}  # 404, never 403
        assert "text/event-stream" not in res.headers.get("content-type", "")
    finally:
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)


def test_query_stream_rate_limited_before_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-user rate limit is a real pre-stream 429 — never an opened
    stream (mirrors the buffered /query rate-limit test in test_auth.py)."""
    import config.settings as cs

    create_user()
    client = login_client()
    try:
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
        cs.get_settings.cache_clear()
        assert _stream(client, "First one?").status_code == 200
        res = _stream(client, "Over the limit?")
        assert res.status_code == 429
        assert res.json() == {"detail": "rate limit exceeded"}
        assert "text/event-stream" not in res.headers.get("content-type", "")
    finally:
        client.__exit__(None, None, None)


def _fake_result() -> qa_mod.QAResult:
    """A minimal terminal QAResult for tests that stub ask() itself (plumbing
    tests where the pipeline's internals are not under test)."""
    return qa_mod.QAResult(
        answer=get_settings().refusal_text,
        citations=[],
        refused=True,
        model_name="stub",
        audit_id=1,
        retrieved=[],
        status="refused",
        session_id="session-1",
        turn_id="turn-1",
    )


def test_query_stream_failure_closes_without_result_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing failure contract: when ask() raises mid-stream, the
    stream closes with NO result frame and no token frames — that absence is
    the client's single-fallback trigger — and no exception text leaks."""

    def exploding_ask(**kwargs: Any) -> qa_mod.QAResult:
        kwargs["on_progress"]("Reading the guidance…")
        raise RuntimeError("internal-detail-must-not-leak")

    monkeypatch.setattr(api_main, "ask", exploding_ask)
    create_user()
    client = login_client()
    try:
        res = _stream(client, "Will this fail mid-stream?")
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        frames = _parse_sse(res.text)
        events = [e for e, _ in frames]
        assert "status" in events  # the stream really opened
        assert "result" not in events
        assert "token" not in events
        # No partial JSON or exception detail on the wire.
        assert "RuntimeError" not in res.text
        assert "internal-detail-must-not-leak" not in res.text
    finally:
        client.__exit__(None, None, None)


def test_query_stream_emits_keepalive_comment_during_quiet_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet gap longer than the keep-alive interval (e.g. silent LLM
    retries) produces SSE comment frames so intermediary idle timers never cut
    a stream that is still working; comments carry no event name, so the
    parsed event contract is unchanged."""
    monkeypatch.setattr(api_main, "_SSE_KEEPALIVE_INTERVAL_S", 0.05)

    def quiet_ask(**kwargs: Any) -> qa_mod.QAResult:
        time.sleep(0.3)  # longer than the patched keep-alive interval
        return _fake_result()

    monkeypatch.setattr(api_main, "ask", quiet_ask)
    create_user()
    client = login_client()
    try:
        res = _stream(client, "Anything at all?")
        assert res.status_code == 200
        assert ": keep-alive" in res.text
        frames = _parse_sse(res.text)
        events = [e for e, _ in frames]
        # Comment frames are invisible to the parser; the terminal result stands.
        assert set(events) <= {"status", "result"}
        assert events[-1] == "result"
    finally:
        client.__exit__(None, None, None)


def test_query_stream_builds_result_response_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_query_response does DB I/O (citation recency); the streaming
    path must run it in a worker thread, never on the event loop, or a DB
    stall freezes every concurrent stream on the machine."""
    on_loop: list[bool] = []
    real = api_main._build_query_response

    def probe(result: qa_mod.QAResult) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(result)

    monkeypatch.setattr(api_main, "_build_query_response", probe)
    monkeypatch.setattr(api_main, "ask", lambda **kwargs: _fake_result())
    create_user()
    client = login_client()
    try:
        frames = _parse_sse(_stream(client, "Where does this build?").text)
        assert [e for e, _ in frames][-1] == "result"
        assert on_loop == [False]
    finally:
        client.__exit__(None, None, None)


def test_ask_pool_saturation_sheds_with_defined_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ask() dispatch rides a dedicated bounded pool. At saturation: buffered
    /query sheds a real 503 (not an unbounded queue), the stream closes with
    NO result frame (the client's fallback trigger), and /health — platform
    liveness — never waits behind ask() work."""
    monkeypatch.setattr(api_main, "_ASK_LIMITER", anyio.CapacityLimiter(1))
    entered = threading.Event()
    release = threading.Event()

    def slow_ask(**kwargs: Any) -> qa_mod.QAResult:
        entered.set()
        assert release.wait(timeout=30), "test never released the worker"
        return _fake_result()

    monkeypatch.setattr(api_main, "ask", slow_ask)
    create_user()
    client = login_client()
    first: list[int] = []
    worker = threading.Thread(
        target=lambda: first.append(
            client.post("/query", json={"question": "Slow one?"}).status_code
        )
    )
    try:
        worker.start()
        assert entered.wait(timeout=10), "first query never reached ask()"
        shed = client.post("/query", json={"question": "Busy now?"})
        assert shed.status_code == 503
        frames = _parse_sse(_stream(client, "Busy stream?").text)
        assert "result" not in [e for e, _ in frames]
        assert client.get("/health").status_code == 200
    finally:
        release.set()
        worker.join(timeout=30)
        client.__exit__(None, None, None)
    assert first == [200]


# ---------- boundary filter whitelist (shared QueryRequest model) ----------


def test_query_filters_are_whitelisted_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """version_id (would disable current-version scoping), unknown keys (would
    500 the pgvector store with no audit row), legacy clarify echoes
    (source_url), and non-scalar values are all dropped BEFORE ask() sees the
    filters; known scope keys pass through untouched. Dropped, not 422'd, so
    clarify options persisted by older sessions keep working."""
    seen: dict[str, Any] = {}

    def capture_ask(**kwargs: Any) -> qa_mod.QAResult:
        seen.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(api_main, "ask", capture_ask)
    create_user()
    client = login_client()
    try:
        res = client.post(
            "/query",
            json={
                "question": "What dissolution method is recommended?",
                "filters": {
                    "normalized_name": "albuterol sulfate",
                    "version_id": 17,  # internal-only: never honored from callers
                    "source_url": "http://example/psg.pdf",  # legacy clarify echo
                    "page": 3,  # unknown-to-session junk
                    "dosage_form": ["Gel", "Cream"],  # non-scalar cannot bind
                },
            },
        )
        assert res.status_code == 200
        assert seen["filters"] == {"normalized_name": "albuterol sulfate"}
    finally:
        client.__exit__(None, None, None)


def test_query_request_filters_validator_unit() -> None:
    """The whitelist lives on the shared QueryRequest model, so /query and
    /query/stream cannot drift: unknown keys and non-scalar values drop
    silently (no 422), known scalar keys survive, and None stays None."""
    req = api_main.QueryRequest(
        question="q?",
        k=None,
        filters={
            "normalized_name": "albuterol sulfate",
            "doc_id": 4,
            "version_id": 17,
            "source_url": "http://example/psg.pdf",
            "route": ["Inhalation"],
        },
    )
    assert req.filters == {"normalized_name": "albuterol sulfate", "doc_id": 4}
    assert api_main.QueryRequest(question="q?", k=None).filters is None
