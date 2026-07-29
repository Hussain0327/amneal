"""Real token streaming with INV-1 reconciliation.

The synthesizer can stream provisional answer tokens for a live "typing" effect,
but the RECORDED answer is still the fully-validated one. These tests pin the two
load-bearing properties: (1) an ungrounded streamed answer still collapses to a
refusal on the record, and (2) the refusal sentinel is NEVER painted as a
streaming answer — the sentinel guard holds every token.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from config.settings import get_settings

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import EchoLLMProvider, LLMMessage, LLMResponse, LLMStreamChunk
from tests.conftest import create_user, session_client
from tests.test_invariants import _meta, _seed_corpus
from tests.test_query_stream import _parse_sse, _result_payload, _stream

pytestmark = pytest.mark.invariants


def _streaming_llm(text: str, *, chunks: list[str] | None = None) -> Any:
    """A stub provider with BOTH complete() and stream(); stream() yields the given
    chunks (default: two halves) then a terminal validated chunk."""

    class _LLM:
        name = "stub-stream"

        def complete(self, *a: object, **kw: object) -> LLMResponse:
            return LLMResponse(text=text, model="stub-stream")

        def stream(self, *a: object, **kw: object) -> Iterator[LLMStreamChunk]:
            parts = (
                chunks if chunks is not None else [text[: len(text) // 2], text[len(text) // 2 :]]
            )
            for part in parts:
                if part:
                    yield LLMStreamChunk(delta=part)
            yield LLMStreamChunk(done=True, response=LLMResponse(text=text, model="stub-stream"))

    return _LLM()


# ---------- provider stream()/complete() parity ----------


def test_echo_stream_text_matches_complete() -> None:
    prov = EchoLLMProvider()
    msgs = [LLMMessage(role="user", content="hello there world")]
    complete_text = prov.complete(msgs).text
    chunks = list(prov.stream(msgs))
    assert chunks[-1].done and chunks[-1].response is not None
    assert chunks[-1].response.text == complete_text
    assert "".join(c.delta for c in chunks if not c.done) == complete_text


# ---------- the sentinel guard (unit, no retrieval dependency) ----------


def test_stream_synthesis_holds_the_refusal_sentinel() -> None:
    """Streaming the refusal sentinel char-by-char emits NOTHING — a refusal is
    never painted as a provisional answer."""
    refusal = get_settings().refusal_text
    prov = _streaming_llm(refusal, chunks=list(refusal))
    emitted: list[str] = []
    resp = qa_mod._stream_synthesis(
        prov,
        [LLMMessage(role="user", content="q")],
        on_emit=emitted.append,
        refusal_text=refusal,
        max_tokens=900,
    )
    assert emitted == []
    assert resp.text == refusal


def test_stream_synthesis_holds_whitespace_prefixed_refusal() -> None:
    """A refusal preceded by leading whitespace (a '\\n' first delta) must ALSO
    hold: the guard compares whitespace-normalized, exactly as the authoritative
    path strips before its sentinel check -- otherwise the whole refusal (plus
    any uncited trailing prose) paints as a provisional answer, then vanishes."""
    refusal = get_settings().refusal_text
    trailing = " However, sponsors typically run a fed study."
    text = "\n" + refusal + trailing
    prov = _streaming_llm(text, chunks=["\n", *list(refusal), trailing])
    emitted: list[str] = []
    resp = qa_mod._stream_synthesis(
        prov,
        [LLMMessage(role="user", content="q")],
        on_emit=emitted.append,
        refusal_text=refusal,
        max_tokens=900,
    )
    assert emitted == []  # nothing painted -- not the refusal, not the trailing prose
    # The raw text still reaches the caller, whose .strip() sentinel check refuses.
    assert resp.text == text


def test_stream_synthesis_streams_a_real_answer() -> None:
    """A real answer is released in full (the held prefix is flushed, then live)."""
    answer = "A fasting study is recommended [PSG_020503, p.3]."
    prov = _streaming_llm(
        answer, chunks=["A fasting ", "study is ", "recommended [PSG_020503, p.3]."]
    )
    emitted: list[str] = []
    resp = qa_mod._stream_synthesis(
        prov,
        [LLMMessage(role="user", content="q")],
        on_emit=emitted.append,
        refusal_text=get_settings().refusal_text,
        max_tokens=900,
    )
    assert "".join(emitted) == answer
    assert resp.text == answer


def _headless_stream_llm(parts: list[str], *, terminal: LLMStreamChunk | None = None) -> Any:
    """A stream that never delivers a usable terminal response: it either ends
    right after the deltas (transport truncation, no done=True chunk at all) or
    ends with the given terminal chunk (e.g. done=True but response=None)."""

    class _LLM:
        name = "stub-headless"

        def stream(self, *a: object, **kw: object) -> Iterator[LLMStreamChunk]:
            for part in parts:
                yield LLMStreamChunk(delta=part)
            if terminal is not None:
                yield terminal

    return _LLM()


def test_stream_synthesis_survives_missing_terminal_chunk() -> None:
    """A stream truncated before its done=True chunk (a known provider transport
    mode) still returns the FULL accumulated text, so the caller's INV-1
    pipeline validates the buffered answer instead of crashing on None."""
    parts = ["A fasting ", "study is recommended [PSG_020503, p.3]."]
    emitted: list[str] = []
    resp = qa_mod._stream_synthesis(
        _headless_stream_llm(parts),
        [LLMMessage(role="user", content="q")],
        on_emit=emitted.append,
        refusal_text=get_settings().refusal_text,
        max_tokens=900,
    )
    assert resp.text == "".join(parts)
    assert "".join(emitted) == "".join(parts)


def test_stream_synthesis_survives_null_terminal_response() -> None:
    """done=True with response=None is the same transport failure in another
    shape -- fall back to the accumulated buffer, never return None."""
    parts = ["A fasting ", "study is recommended [PSG_020503, p.3]."]
    emitted: list[str] = []
    resp = qa_mod._stream_synthesis(
        _headless_stream_llm(parts, terminal=LLMStreamChunk(done=True, response=None)),
        [LLMMessage(role="user", content="q")],
        on_emit=emitted.append,
        refusal_text=get_settings().refusal_text,
        max_tokens=900,
    )
    assert resp.text == "".join(parts)
    assert "".join(emitted) == "".join(parts)


# ---------- INV-1 reconciliation through ask() ----------


def test_streaming_ungrounded_answer_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provisional tokens may stream, but an answer whose only citation is
    fabricated still collapses to a refusal on the RECORDED answer (INV-1)."""
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    fabricated = "The recommended dose is 100 mg per day [PSG_999999, p.99]."
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _streaming_llm(fabricated))
    tokens: list[str] = []
    result = qa_mod.ask("What is the recommended dose?", on_token=tokens.append)
    assert result.refused
    assert result.answer == get_settings().refusal_text
    assert result.citations == []
    assert "PSG_999999" not in result.answer  # the record never carries it


# ---------- the SSE token frame over the wire ----------


def test_query_stream_emits_token_frames_then_validated_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streaming provider produces `token` frames (provisional deltas), all
    strictly before the single terminal validated `result` frame."""
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    answer = "A fasting bioequivalence study is recommended [PSG_020503, p.3]."
    monkeypatch.setattr(
        qa_mod,
        "get_llm_provider",
        lambda *a, **k: _streaming_llm(
            answer,
            chunks=["A fasting ", "bioequivalence study ", "is recommended [PSG_020503, p.3]."],
        ),
    )
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, "What study design is recommended?").text)
        events = [e for e, _ in frames]
        assert "token" in events
        assert events.count("result") == 1
        assert events[-1] == "result"
        # Every token frame precedes the result frame.
        assert max(i for i, e in enumerate(events) if e == "token") < events.index("result")
        token_payloads = [json.loads(d) for e, d in frames if e == "token"]
        assert all(set(p) == {"delta"} for p in token_payloads)
        result = _result_payload(frames)
        assert result["refused"] is False
        assert "[PSG_020503, p.3]" in result["answer"]
    finally:
        client.__exit__(None, None, None)


def test_query_stream_refusal_streams_zero_token_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E over /query/stream: a provider that STREAMS the refusal sentinel
    (char by char) produces ZERO `token` frames. This pins the WIRING above
    _stream_synthesis -- ask() passing on_token/refusal_text and main.py's
    token frames -- so a refactor cannot paint the refusal as a provisional
    answer that types out and then vanishes."""
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    refusal = get_settings().refusal_text
    monkeypatch.setattr(
        qa_mod, "get_llm_provider", lambda *a, **k: _streaming_llm(refusal, chunks=list(refusal))
    )
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, "What is the recommended dose?").text)
        events = [e for e, _ in frames]
        assert "token" not in events  # the sentinel guard held end-to-end
        result = _result_payload(frames)
        assert result["refused"] is True
        assert result["answer"] == refusal
        # Belt and braces: no fragment of the sentinel appears before the result
        # frame under ANY event name (char-split deltas would reassemble here).
        pre_result = "".join(d for e, d in frames if e != "result")
        assert refusal not in pre_result
        assert refusal[:15] not in pre_result
    finally:
        client.__exit__(None, None, None)
