"""The synthesizer output cap is the operator knob on ONE structured call.

Background: an open-weight reasoning model draws its thought and its answer from
the same max_tokens budget, so the cap tuned for gpt-5.4-nano (900, all answer)
truncated a reasoning model mid-thought -- finish_reason=length, which the
provider raises and grounded_qa degrades into an audited refusal. The cap became
a setting so an operator has headroom without a deploy.

What the structured-turn contract changed: the synthesizer no longer returns
prose, it returns ONE JSON object. Truncated JSON is UNPARSEABLE where truncated
prose merely lost a sentence, so the envelope needs headroom the old cap never
budgeted -- the default moved 900 -> 1600, and `_complete_structured` clamps to
a 4000 ceiling with a single 2x truncation retry underneath it.

And the twin is gone. There is exactly ONE synthesis call path: a buffered
`complete(..., response_format="json")`. `provider.stream()` carries no
response_format on the Protocol or in any implementation, so a streaming twin
would hand unstructured PROSE back to a structured caller. The live "typing"
effect now replays the RENDERED, gated answer after the audit write, which is
why these tests assert `stream` is never touched even when a token sink is
supplied.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from config.settings import Settings, get_settings
from pydantic import ValidationError

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import D1ResidencyError, LLMResponse, LLMStreamChunk
from tests.conftest import synth_turn_json
from tests.test_invariants import _meta, _seed_corpus

pytestmark = pytest.mark.invariants


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "database_url": "postgresql://u@h/db"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------- Settings ----------


def test_synthesizer_cap_default_is_the_structured_budget() -> None:
    """1600, not the historical 900: the JSON envelope has to fit.

    Under the old prose contract a short cap cost a sentence. Under the turn
    contract it costs the whole turn -- a truncated object fails
    GroundedTurn.model_validate and the user gets the service-unavailable
    refusal. Moving this default down again silently reintroduces that.
    """
    assert _settings().synthesizer_max_tokens == 1600


@pytest.mark.parametrize("value", [0, -1, 100_000])
def test_synthesizer_cap_rejects_out_of_range(value: int) -> None:
    """A cap of 0 would truncate every completion to nothing and degrade every
    turn to the empty_completion refusal -- the app looks alive, answers none."""
    with pytest.raises(ValidationError, match="SYNTHESIZER_MAX_TOKENS"):
        _settings(synthesizer_max_tokens=value)


def test_reasoning_effort_defaults_to_low_and_rejects_typos() -> None:
    """A typo'd effort level must fail at boot, not 400 every request."""
    assert _settings().databricks_reasoning_effort == "low"
    assert _settings(databricks_reasoning_effort="HIGH").databricks_reasoning_effort == "high"
    with pytest.raises(ValidationError, match="DATABRICKS_REASONING_EFFORT"):
        _settings(databricks_reasoning_effort="ultra")


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_reasoning_effort_means_send_no_parameter(value: str) -> None:
    """Unset is a real configuration: some endpoints reject the parameter."""
    assert _settings(databricks_reasoning_effort=value).databricks_reasoning_effort is None


# ---------- the cap on the wire ----------


class _CapturingLLM:
    """Records what each synthesis call asked for, and whether it streamed.

    ``fail_times`` makes the first N ``complete`` calls raise, standing in for
    the truncation error only a truncation-aware provider raises (Databricks
    inspects finish_reason).
    """

    name = "stub-cap"

    def __init__(self, text: str, *, fail_times: int = 0, error: Exception | None = None) -> None:
        self.text = text
        self.fail_times = fail_times
        self.error: Exception = error if error is not None else RuntimeError("finish_reason=length")
        self.max_tokens: list[Any] = []
        self.response_formats: list[Any] = []
        self.stream_max_tokens: list[Any] = []

    def complete(self, *a: object, **kw: Any) -> LLMResponse:
        self.max_tokens.append(kw.get("max_tokens"))
        self.response_formats.append(kw.get("response_format"))
        if len(self.max_tokens) <= self.fail_times:
            raise self.error
        return LLMResponse(text=self.text, model="stub-cap")

    def stream(self, *a: object, **kw: Any) -> Iterator[LLMStreamChunk]:
        # Recorded, never raised: an exception here would be swallowed by ask()'s
        # provider-error branch and read as an ordinary refusal instead of the
        # contract breach it is. The assertions below check the list is empty.
        self.stream_max_tokens.append(kw.get("max_tokens"))
        yield LLMStreamChunk(done=True, response=LLMResponse(text=self.text, model="stub-cap"))


# One admitted claim, cited to the seeded passage: the happy path, so a cap
# assertion is made on a turn that actually rendered rather than on one that
# died in the gate.
_ANSWER_TURN = synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])

_QUESTION = "What study design is recommended?"


@contextmanager
def _cap(monkeypatch: pytest.MonkeyPatch, value: int) -> Iterator[None]:
    """Bind SYNTHESIZER_MAX_TOKENS for one turn and drop the settings memo.

    The finally is load-bearing: get_settings is lru_cached, so a hand-set cap
    left memoized would silently re-tune whatever test ran next.
    """
    import config.settings as cs

    monkeypatch.setenv("SYNTHESIZER_MAX_TOKENS", str(value))
    cs.get_settings.cache_clear()
    try:
        assert get_settings().synthesizer_max_tokens == value
        yield
    finally:
        monkeypatch.delenv("SYNTHESIZER_MAX_TOKENS", raising=False)
        cs.get_settings.cache_clear()


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    provider: _CapturingLLM,
    *,
    on_token: Any = None,
) -> qa_mod.QAResult:
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    return qa_mod.ask(_QUESTION, on_token=on_token)


def test_synthesis_is_one_buffered_json_call_at_the_configured_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator knob reaches the wire, once, on the structured surface.

    This fails if someone re-hardcodes the cap, reads it twice, or reintroduces
    a path that asks for prose (response_format missing).
    """
    provider = _CapturingLLM(_ANSWER_TURN)
    with _cap(monkeypatch, 2600):
        result = _drive(monkeypatch, provider)

    assert not result.refused
    assert provider.max_tokens == [2600]
    assert provider.response_formats == ["json"]
    assert provider.stream_max_tokens == []


def test_synthesis_defaults_to_1600(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing configured the wire value is the new structured default."""
    provider = _CapturingLLM(_ANSWER_TURN)
    result = _drive(monkeypatch, provider)

    assert not result.refused
    assert provider.max_tokens == [1600]


def test_a_token_sink_does_not_change_the_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-1 at the transport boundary: nothing the user reads is a model token.

    Supplying on_token used to select a second, streaming synthesis twin. It no
    longer selects anything -- the same buffered json call runs, and the sink
    receives the RENDERED, gated, already-audited answer. If a streaming twin
    ever comes back, stream() drops response_format and the reader sees
    provisional prose the gate never admitted.
    """
    seen: list[str] = []
    provider = _CapturingLLM(_ANSWER_TURN)
    result = _drive(monkeypatch, provider, on_token=seen.append)

    assert not result.refused
    assert provider.max_tokens == [1600]
    assert provider.stream_max_tokens == []
    # Byte-identical to the rendered answer: the replay is the gate's output,
    # never the model's draft (which said none of "[PSG_020503, p.3]").
    assert "".join(seen) == result.answer
    assert "[PSG_020503, p.3]" in result.answer


# ---------- the truncation retry under the ceiling ----------


def test_truncation_retry_doubles_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """One 2x retry rescues the first overrun of the JSON envelope.

    Delete the retry and this fails twice over: the call list collapses to
    [1600] and the turn degrades to the audited provider_error refusal.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [1600, 3200]
    assert provider.response_formats == ["json", "json"]
    assert not result.refused


def test_truncation_retry_is_clamped_to_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """2x never runs away past 4000, however high the operator set the knob."""
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    with _cap(monkeypatch, 3000):
        result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [3000, 4000]  # not 6000
    assert not result.refused


def test_first_call_is_clamped_to_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings validator allows up to 32768; the synthesis path does not.

    An operator raising the knob mid-incident must not be able to buy a
    per-turn bill (or a provider 400) the code never budgeted for.
    """
    provider = _CapturingLLM(_ANSWER_TURN)
    with _cap(monkeypatch, 32_000):
        result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [4000]
    assert not result.refused


def test_no_retry_once_the_cap_is_already_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the ceiling a truncation is REAL and must surface, not loop.

    Retrying the same 4000 would double the spend and still truncate. The turn
    degrades to the audited provider_error refusal, and it serves the
    service-unavailable copy rather than settings.refusal_text: a truncation
    says nothing about whether the corpus covers the question.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    with _cap(monkeypatch, 4000):
        result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [4000]
    assert result.refused
    assert result.reason == "provider_error"
    assert result.answer == qa_mod._SERVICE_UNAVAILABLE_TEXT
    assert result.citations == []


def test_residency_error_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1: a residency violation must fail the turn, not re-hit the endpoint.

    D1ResidencyError subclasses RuntimeError, so without the explicit re-raise
    ahead of the truncation branch the guard would be tripped a SECOND time
    against the very endpoint it fences off -- and at 2x the tokens.
    """
    provider = _CapturingLLM(
        _ANSWER_TURN, fail_times=1, error=D1ResidencyError("query data may not leave")
    )
    result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [1600]  # no second attempt
    assert result.refused
    assert result.reason == "provider_error"
    assert result.citations == []
