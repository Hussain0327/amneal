"""The dual-gated live-draft channel: pipeline half (L2).

Pins: drafts stream only in prose mode with a sink attached; the sentinel
refusal is NEVER painted; the truncation retry emits a reset; the gate still
operates on the COMPLETE text; flag-off turns are byte-identical to today.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse, LLMStreamChunk
from regwatch.generate.prose_turn import PROSE_NO_EVIDENCE_SENTINEL
from tests.test_invariants import _meta, _seed_corpus

_QUESTION = "What study design is recommended?"
_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]
_PROSE = "A fasting bioequivalence study is recommended [1]."


class _StreamingStub:
    """Prose synthesizer stub with a REAL incremental stream()."""

    name = "stub-streaming"

    def __init__(self, *stream_texts: str, chunk: int = 8) -> None:
        self._texts = list(stream_texts)
        self._chunk = chunk
        self.stream_calls = 0
        self.complete_calls = 0

    def complete(self, *a: object, **kw: object) -> LLMResponse:
        self.complete_calls += 1
        return LLMResponse(text=self._texts[0], model="stub-streaming")

    def stream(self, *a: object, **kw: object) -> Iterator[LLMStreamChunk]:
        text = self._texts[min(self.stream_calls, len(self._texts) - 1)]
        self.stream_calls += 1
        if text == "<raise-truncated>":
            raise RuntimeError("finish_reason=length")
        if text.startswith("<truncate-then:"):
            # First call: emit a partial delta, then raise truncation.
            if self.stream_calls == 1:
                yield LLMStreamChunk(delta="partial that will be reset ")
                raise RuntimeError("finish_reason=length")
            text = text[len("<truncate-then:") : -1]
        for i in range(0, len(text), self._chunk):
            yield LLMStreamChunk(delta=text[i : i + self._chunk])
        yield LLMStreamChunk(done=True, response=LLMResponse(text=text, model="stub-streaming"))


@pytest.fixture()
def prose_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


def _use(monkeypatch: pytest.MonkeyPatch, provider: _StreamingStub) -> _StreamingStub:
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    return provider


def test_drafts_stream_live_and_gate_runs_on_complete_text(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StreamingStub(_PROSE))
    drafts: list[str] = []
    result = qa_mod.ask(_QUESTION, on_draft=drafts.append)
    assert prov.stream_calls == 1 and prov.complete_calls == 0
    assert "".join(drafts) == _PROSE  # raw model prose, [1] marker included
    assert result.refused is False
    assert "[PSG_020503, p.3]" in result.answer  # gate rendered from FULL text


def test_no_draft_sink_means_buffered_call_and_no_stream(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StreamingStub(_PROSE))
    result = qa_mod.ask(_QUESTION)
    assert prov.stream_calls == 0 and prov.complete_calls == 1
    assert result.refused is False


def test_json_mode_never_streams_even_with_a_draft_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # prose flag OFF: the v5 JSON arm must be structurally unable to stream.
    from tests.conftest import synth_turn_json

    _seed_corpus(_CORPUS)
    turn = synth_turn_json([("A fasting bioequivalence study is recommended", [("PSG_020503", 3)])])
    prov = _use(monkeypatch, _StreamingStub(turn))
    drafts: list[str] = []
    result = qa_mod.ask(_QUESTION, on_draft=drafts.append)
    assert prov.stream_calls == 0 and prov.complete_calls == 1
    assert drafts == []
    assert result.refused is False


def test_sentinel_refusal_is_never_painted_as_a_draft(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StreamingStub(PROSE_NO_EVIDENCE_SENTINEL, chunk=3))
    drafts: list[str] = []
    result = qa_mod.ask(_QUESTION, on_draft=drafts.append)
    assert drafts == []  # held: every prefix of the sentinel is withheld
    assert result.refused is True


def test_truncation_retry_emits_reset_then_restreams(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StreamingStub(f"<truncate-then:{_PROSE}>"))
    drafts: list[str] = []
    resets: list[bool] = []
    result = qa_mod.ask(
        _QUESTION, on_draft=drafts.append, on_draft_reset=lambda: resets.append(True)
    )
    assert prov.stream_calls == 2
    assert resets == [True]
    assert result.refused is False
    # Post-reset deltas reassemble to attempt 2's text exactly.
    reset_marker = drafts.index("partial that will be reset ") + 1
    assert "".join(drafts[reset_marker:]) == _PROSE


def test_query_stream_emits_draft_frames_only_when_dual_gated(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    from tests.conftest import create_user, session_client
    from tests.test_query_stream import _parse_sse, _stream

    monkeypatch.setenv("REGWATCH_LIVE_DRAFT", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StreamingStub(_PROSE))
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, _QUESTION, live_draft=True).text)
        events = [e for e, _ in frames]
        assert "draft" in events
        assert events[-1] == "result"
        result_data = next(d for e, d in frames if e == "result")
        assert json.loads(result_data)["draft_withdrawn"] is None
        # Opt-out request on the same flag-on server: zero draft frames.
        frames2 = _parse_sse(_stream(client, _QUESTION).text)
        assert "draft" not in [e for e, _ in frames2]
    finally:
        client.__exit__(None, None, None)


def test_result_carries_draft_withdrawn_when_a_painted_draft_dies(
    monkeypatch: pytest.MonkeyPatch, prose_mode: None
) -> None:
    """A stub that streams fluent prose which the gate then REFUSES (its one
    citation is fabricated) must stamp draft_withdrawn='refused' on the result
    frame -- and a clean answer turn must stamp nothing."""
    from tests.conftest import create_user, session_client
    from tests.test_query_stream import _parse_sse, _stream

    monkeypatch.setenv("REGWATCH_LIVE_DRAFT", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StreamingStub("A fabricated dose claim [7]."))
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, _QUESTION, live_draft=True).text)
        events = [e for e, _ in frames]
        assert "draft" in events  # the fluent draft painted
        result_data = next(d for e, d in frames if e == "result")
        result = json.loads(result_data)
        assert result["refused"] is True
        assert result["draft_withdrawn"] == "refused"
    finally:
        client.__exit__(None, None, None)
