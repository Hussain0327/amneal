"""The synthesis call shape and the token-replay boundary.

The synthesizer is BUFFERED json mode (one ``complete(response_format="json")``
call, never ``provider.stream()``), and the user-visible "typing" effect is a
REPLAY of the rendered answer that fires after the audit row is committed and
only on an answer/summary turn. These tests pin what that boundary guarantees:

  1. Nothing is streamed from the model, so no draft the gate later retracts can
     reach a reader (the old provisional-token path could paint one).
  2. Every replayed byte is the gated, rendered, AUDITED answer -- byte for byte,
     with no whitespace-free token torn across two frames.
  3. Every decline shape (model NO_EVIDENCE, unknown citation, material drop,
     unparseable payload, failed audit write) replays ZERO tokens, so INV-1 and
     INV-6 hold on the wire as well as on the record.

Historical note: this file used to test ``grounded_qa._stream_synthesis`` and its
refusal-sentinel guard. Both are gone -- the model no longer writes prose, so
there is no sentinel to hold back and no provisional token to guard. The safety
property they protected (a refusal/retraction is never painted as an answer) is
now structural and is pinned here at its new home, the replay.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from config.settings import get_settings

# The real audit writer, imported from its own module: grounded_qa re-exports
# nothing (mypy strict / no_implicit_reexport), so the monkeypatched tests below
# wrap THIS rather than reading it back off qa_mod.
from regwatch.common.audit import log_query as real_log_query
from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate import turn_gate as tg
from regwatch.generate.llm import EchoLLMProvider, LLMMessage, LLMResponse
from tests.conftest import create_user, session_client, synth_turn_json
from tests.test_invariants import _meta, _seed_corpus
from tests.test_query_stream import _parse_sse, _result_payload, _stream

pytestmark = pytest.mark.invariants

_QUESTION = "What study design is recommended?"
_CORPUS = [("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))]


def _claim(text: str, cites: list[tuple[str, int]]) -> tuple[str, list[tuple[str, int]]]:
    return (text, cites)


def _turn(
    claims: list[tuple[str, list[tuple[str, int]]]],
    *,
    turn_type: str = "ANSWER",
    unsupported: list[str] | None = None,
) -> str:
    """One conformant structured completion, as the synthesizer must now emit.

    Built through the ONE shared payload seam (tests/conftest.synth_turn_json)
    so a synthesis-format change is one edit, not one per stub module (F10).
    """
    return synth_turn_json(claims, turn_type=turn_type, unsupported=tuple(unsupported or ()))


# A two-claim grounded turn: long enough once rendered (marker per sentence plus
# the Sources trailer) that the ~60-char replay really chunks.
_GROUNDED_TURN = _turn(
    [
        _claim("A fasting bioequivalence study is recommended", [("PSG_020503", 3)]),
        _claim("The study enrolls 36 healthy subjects", [("PSG_020503", 3)]),
    ]
)


class _StructuredLLM:
    """A synthesizer stub speaking the STRUCTURED contract.

    ``stream`` deliberately RAISES. stream() carries no ``response_format`` on
    the Protocol or in any implementation, so a synthesizer that reached for it
    would silently get PROSE back from a structured caller; the raise turns that
    regression into a test failure instead of a malformed-structure error rate.
    """

    name = "stub-structured"

    def __init__(self, *texts: str) -> None:
        # More than one text = one per successive call (for the retry path).
        self._texts = list(texts)
        self.calls: list[dict[str, object]] = []

    def complete(self, *a: object, **kw: object) -> LLMResponse:
        self.calls.append(dict(kw))
        text = self._texts[min(len(self.calls) - 1, len(self._texts) - 1)]
        if text == _RAISE_TRUNCATED:
            # The shape only truncation-aware providers raise (Databricks
            # inspects finish_reason); _complete_structured retries once at 2x.
            raise RuntimeError("completion truncated (finish_reason=length)")
        return LLMResponse(text=text, model="stub-structured")

    def stream(self, *a: object, **kw: object) -> object:
        raise AssertionError("the synthesizer must not call provider.stream()")


_RAISE_TRUNCATED = "<raise-truncated>"


def _use(monkeypatch: pytest.MonkeyPatch, provider: _StructuredLLM) -> _StructuredLLM:
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    return provider


# ---------- provider stream()/complete() parity ----------


def test_echo_stream_text_matches_complete() -> None:
    """The provider-level stream() contract still holds.

    No longer on the QA path (synthesis is buffered json), but stream() remains
    on LLMProvider and on five implementations, so its contract -- deltas that
    reassemble into the terminal validated response -- still needs a pin.
    """
    prov = EchoLLMProvider()
    msgs = [LLMMessage(role="user", content="hello there world")]
    complete_text = prov.complete(msgs).text
    chunks = list(prov.stream(msgs))
    assert chunks[-1].done and chunks[-1].response is not None
    assert chunks[-1].response.text == complete_text
    assert "".join(c.delta for c in chunks if not c.done) == complete_text


# ---------- the synthesis call is buffered json, never a provider stream ----------


def test_synthesis_is_one_buffered_json_call_never_provider_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly one complete(response_format="json") call, at the configured budget.

    The stub's stream() raises, so reaching for it fails the test rather than
    degrading a structured caller to prose. The token budget is read from
    settings (SYNTHESIZER_MAX_TOKENS, raised from the old prose-era 900 because
    a JSON envelope needs headroom), never hardcoded at the call site.
    """
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StructuredLLM(_GROUNDED_TURN))
    result = qa_mod.ask(_QUESTION)
    assert result.refused is False
    assert len(prov.calls) == 1
    assert prov.calls[0]["response_format"] == "json"
    assert prov.calls[0]["max_tokens"] == get_settings().synthesizer_max_tokens
    assert prov.calls[0]["temperature"] == 0.0


def test_truncated_synthesis_retries_once_at_double_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport truncation is survivable on the buffered path too.

    The stream-era twin of this test accumulated deltas when a provider never
    sent its terminal chunk. Truncation is now handled by ONE 2x retry inside
    _complete_structured -- still buffered, still json -- and the replayed
    answer is the retry's gated output.
    """
    _seed_corpus(_CORPUS)
    s = get_settings()
    prov = _use(monkeypatch, _StructuredLLM(_RAISE_TRUNCATED, _GROUNDED_TURN))
    tokens: list[str] = []
    result = qa_mod.ask(_QUESTION, on_token=tokens.append)
    assert result.refused is False
    assert [c["max_tokens"] for c in prov.calls] == [
        s.synthesizer_max_tokens,
        min(s.synthesizer_max_tokens * 2, qa_mod._SYNTH_MAX_TOKENS_CEILING),
    ]
    assert all(c["response_format"] == "json" for c in prov.calls)
    assert "".join(tokens) == result.answer


# ---------- the replay: exactly the gated answer, only after the audit ----------


def test_replay_emits_exactly_the_rendered_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token sink receives the RENDERED answer byte for byte -- nothing the
    gate did not admit, nothing the renderer did not write, nothing dropped."""
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StructuredLLM(_GROUNDED_TURN))
    tokens: list[str] = []
    result = qa_mod.ask(_QUESTION, on_token=tokens.append)
    assert result.refused is False
    assert "".join(tokens) == result.answer
    assert len(tokens) >= 2, "the replay really chunks; a single frame is not a typing effect"
    # Chunk boundaries land on whitespace only: no whitespace-free token (and so
    # no half of a "[PSG_020503," / "p.3]" marker word) is torn across frames.
    assert [t for chunk in tokens for t in chunk.split()] == result.answer.split()
    # What was replayed is what was recorded: the marker and its Sources trailer.
    assert "[PSG_020503, p.3]" in result.answer
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}


def test_prose_flag_replay_emits_exactly_the_rendered_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-on twin of the replay pin: the v6 prose chain replays the GATED render.

    The model-facing [n] markers never reach the sink -- the replayed bytes
    carry canonical [SHORT_NAME, p.N] markers plus the Sources trailer, chunked
    on whitespace so no marker is torn across frames -- and the synthesis call
    itself carries NO response_format (prose, not json_object).
    """
    _seed_corpus(_CORPUS)
    monkeypatch.setenv("REGWATCH_PROSE_SYNTHESIS", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    prov = _use(
        monkeypatch,
        _StructuredLLM(
            "A fasting bioequivalence study is recommended [1]. "
            "The study enrolls thirty-six healthy subjects [1]."
        ),
    )
    tokens: list[str] = []
    result = qa_mod.ask(_QUESTION, on_token=tokens.append)
    assert result.refused is False
    assert prov.calls[0]["response_format"] is None
    assert "".join(tokens) == result.answer
    assert len(tokens) >= 2, "the replay really chunks; a single frame is not a typing effect"
    assert [t for chunk in tokens for t in chunk.split()] == result.answer.split()
    assert "[1]" not in result.answer
    assert "[PSG_020503, p.3]" in result.answer
    assert {(c.short_name, c.page) for c in result.citations} == {("PSG_020503", 3)}


def test_tokens_are_replayed_only_after_the_audit_row_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6 ordering: not one user-visible byte precedes the audit write.

    The pre-change path streamed provisional tokens DURING synthesis, so a
    complete answer could reach a reader with no audit row anywhere. The replay
    inverts that ordering, and this test is what holds it inverted.
    """
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StructuredLLM(_GROUNDED_TURN))
    events: list[str] = []

    def _traced(**kwargs: Any) -> int:
        events.append("audit")
        return int(real_log_query(**kwargs))

    monkeypatch.setattr(qa_mod, "log_query", _traced)
    result = qa_mod.ask(_QUESTION, on_token=lambda _d: events.append("token"))
    assert result.refused is False
    assert events.count("audit") == 1  # INV-6: exactly one row per turn
    assert events[0] == "audit"
    assert "token" in events


def test_a_failed_audit_write_replays_zero_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-audit-no-answer: when the strict audit write fails, the validated
    answer is NOT replayed -- the turn degrades to the fixed-copy service error
    and the sink sees nothing at all."""
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StructuredLLM(_GROUNDED_TURN))
    calls = {"n": 0}

    def _flaky(**kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:  # the answer turn's strict write
            raise RuntimeError("audit db down")
        return int(real_log_query(**kwargs))

    monkeypatch.setattr(qa_mod, "log_query", _flaky)
    tokens: list[str] = []
    result = qa_mod.ask(_QUESTION, on_token=tokens.append)
    assert tokens == []
    assert result.status == "error"
    assert result.refused is True
    assert result.answer == qa_mod._SERVICE_UNAVAILABLE_TEXT
    assert result.citations == []
    assert "PSG_020503" not in result.answer


# ---------- every decline shape replays nothing ----------

# Expected-answer keys, resolved at TEST time (settings.refusal_text is
# env-resolved per test by the conftest fixture, so it must not be baked into a
# module-level table).
_REFUSAL = "refusal_text"
_MATERIAL = "material_drop_text"
_SERVICE = "service_unavailable_text"

_DECLINES: list[tuple[str, str, str, str]] = [
    # (id, completion text, expected-answer key, expected status)
    ("model_no_evidence", _turn([], turn_type="NO_EVIDENCE"), _REFUSAL, "refused"),
    (
        "unknown_citation",
        _turn([_claim("The recommended dose is 100 mg per day", [("PSG_999999", 99)])]),
        _REFUSAL,
        "refused",
    ),
    (
        "material_drop",
        _turn(
            [
                _claim("A fasting bioequivalence study is recommended", [("PSG_020503", 3)]),
                _claim("A fed study is not required for this product", [("PSG_999999", 9)]),
            ]
        ),
        _MATERIAL,
        "refused",
    ),
    (
        "truncated_json",
        '{"turn_type": "ANSWER", "claims": [{"text": "A biowaiver is not granted unless',
        _SERVICE,
        "error",
    ),
    (
        # The exact pre-change output shape: prose plus a trailing bibliography.
        # It is now a MACHINE fault (the contract is JSON), so it must serve the
        # service copy -- never the refusal string, which would record an untested
        # claim about corpus coverage in the audit row.
        "prose_instead_of_json",
        "A fasting study is recommended.\n\nSources: [PSG_020503, p.3]",
        _SERVICE,
        "error",
    ),
]


@pytest.mark.parametrize(
    ("completion", "expected_key", "expected_status"),
    [(c, a, s) for _id, c, a, s in _DECLINES],
    ids=[i for i, _c, _a, _s in _DECLINES],
)
def test_declined_turns_replay_zero_tokens(
    monkeypatch: pytest.MonkeyPatch,
    completion: str,
    expected_key: str,
    expected_status: str,
) -> None:
    """Every non-answer verdict emits NOTHING to the token sink.

    The replay is gated on status in ("answer", "summary"), which is what makes
    "a retracted or declined draft can never be painted" structural rather than
    a sentinel string comparison that a new refusal wording could slip past.
    """
    expected_answer = {
        _REFUSAL: get_settings().refusal_text,
        _MATERIAL: tg.MATERIAL_DROP_TEXT,
        _SERVICE: qa_mod._SERVICE_UNAVAILABLE_TEXT,
    }[expected_key]
    _seed_corpus(_CORPUS)
    prov = _use(monkeypatch, _StructuredLLM(completion))
    tokens: list[str] = []
    result = qa_mod.ask(_QUESTION, on_token=tokens.append)
    # The turn must reach synthesis: without this the test would still pass if
    # retrieval started declining before the gate, and would then be asserting
    # nothing about the replay at all.
    assert prov.calls, "the turn declined before synthesis -- the replay gate was never exercised"
    assert tokens == []
    assert result.refused is True
    assert result.status == expected_status
    assert result.answer == expected_answer
    assert result.citations == []


def test_a_fabricated_citation_never_reaches_the_record_or_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-1 end to end through ask(): a claim whose only citation names a
    passage that was never retrieved is dropped, the turn collapses to a
    refusal, and neither the claim text nor the fabricated marker appears in the
    recorded answer or in a single replayed byte."""
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    _use(
        monkeypatch,
        _StructuredLLM(
            _turn([_claim("The recommended dose is 100 mg per day", [("PSG_999999", 99)])])
        ),
    )
    tokens: list[str] = []
    result = qa_mod.ask("What is the recommended dose?", on_token=tokens.append)
    assert result.refused is True
    assert result.answer == get_settings().refusal_text
    assert result.citations == []
    assert tokens == []
    assert "PSG_999999" not in result.answer  # the record never carries it
    assert "100 mg" not in result.answer  # nor the model's claim text


# ---------- the SSE token frame over the wire ----------


def test_query_stream_emits_token_frames_then_validated_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`token` frames carry the validated answer's replay -- all strictly before
    the single terminal `result` frame, and reassembling into exactly it."""
    _seed_corpus(_CORPUS)
    _use(monkeypatch, _StructuredLLM(_GROUNDED_TURN))
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, _QUESTION).text)
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
        # The wire twin of the replay-fidelity assert: what typed out IS the
        # gated, audited answer, not a draft that merely resembles it.
        assert "".join(p["delta"] for p in token_payloads) == result["answer"]
    finally:
        client.__exit__(None, None, None)


def test_query_stream_refusal_streams_zero_token_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E over /query/stream: a model that declines produces ZERO `token`
    frames. This pins the WIRING above the replay -- ask() threading on_token
    into _persist_turn and main.py's token frames -- so a refactor cannot paint
    a refusal as an answer that types out and then vanishes."""
    _seed_corpus([("Bioequivalence requires a fasting study.", _meta(3, 1, "PSG_333333"))])
    refusal = get_settings().refusal_text
    prov = _use(monkeypatch, _StructuredLLM(_turn([], turn_type="NO_EVIDENCE")))
    client = session_client(create_user())
    try:
        frames = _parse_sse(_stream(client, "What is the recommended dose?").text)
        events = [e for e, _ in frames]
        assert prov.calls, "the turn declined before synthesis -- nothing about tokens was tested"
        assert "token" not in events
        result = _result_payload(frames)
        assert result["refused"] is True
        assert result["answer"] == refusal
        # Belt and braces: no fragment of the refusal appears before the result
        # frame under ANY event name (char-split deltas would reassemble here).
        pre_result = "".join(d for e, d in frames if e != "result")
        assert refusal not in pre_result
        assert refusal[:15] not in pre_result
    finally:
        client.__exit__(None, None, None)
