"""The synthesizer output cap is the operator knob on ONE structured call.

Background: an open-weight reasoning model draws its thought and its answer from
the same max_tokens budget, so the cap tuned for gpt-5.4-nano (900, all answer)
truncated a reasoning model mid-thought -- finish_reason=length, which the
provider raises and grounded_qa degrades into an audited refusal. The cap became
a setting so an operator has headroom without a deploy.

What the structured-turn contract changed: the synthesizer no longer returns
prose, it returns ONE JSON object. Truncated JSON is UNPARSEABLE where truncated
prose merely lost a sentence, so the envelope needs headroom the old cap never
budgeted -- the default moved 900 -> 1600 -> 3000, and `_complete_structured`
clamps to SYNTH_MAX_TOKENS_CEILING (6000) with a single 2x truncation retry
underneath it. The ceiling now lives in config.settings so the field validator
can refuse a budget at or above it: that range used to boot clean while
silently clamping the first call AND silently disabling the retry.

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
from config.settings import SYNTH_MAX_TOKENS_CEILING, Settings, get_settings
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
    """3000, sized off the SCHEMA CEILING rather than observed p95.

    Under the old prose contract a short cap cost a sentence. Under the turn
    contract it costs the whole turn -- a truncated object fails
    GroundedTurn.model_validate and the user gets the service-unavailable
    refusal. Because undershooting costs the whole turn, the budget must cover
    the worst LEGAL payload, not the median one: 20 claims x 250 chars x 2
    cites is 2,317 output tokens on o200k_harmony, plus a ~500-token reasoning
    residual, rounded up for JSON escaping.

    Moving this default down again silently reintroduces the outage. Note the
    previous 1600 did not even cover the OLD 10-claim cap (1,927 + 500).
    """
    assert _settings().synthesizer_max_tokens == 3000


@pytest.mark.parametrize("value", [0, -1, 100_000])
def test_synthesizer_cap_rejects_out_of_range(value: int) -> None:
    """A cap of 0 would truncate every completion to nothing and degrade every
    turn to the empty_completion refusal -- the app looks alive, answers none."""
    with pytest.raises(ValidationError, match="SYNTHESIZER_MAX_TOKENS"):
        _settings(synthesizer_max_tokens=value)


@pytest.mark.parametrize("value", [SYNTH_MAX_TOKENS_CEILING, SYNTH_MAX_TOKENS_CEILING + 1, 32_000])
def test_settings_reject_a_cap_at_or_above_the_ceiling(value: int) -> None:
    """The gap this closes: 4001..32768 used to boot clean and fail twice, silently.

    The first call was clamped down to the ceiling with no log, AND the
    truncation retry stopped working, because min(capped * 2, CEILING) can no
    longer exceed a budget that already sits at the ceiling. Both are invisible
    in production. Refusing at boot is the only place this is observable.
    """
    with pytest.raises(ValidationError, match="SYNTHESIZER_MAX_TOKENS"):
        _settings(synthesizer_max_tokens=value)


def test_the_ceiling_is_single_sourced() -> None:
    """Two copies of this number is the bug the validator coupling fixes.

    The runtime clamp and the boot-time bound must be the same constant; if the
    module ever re-declares its own, the validator can pass a value the clamp
    then silently lowers.
    """
    assert qa_mod._SYNTH_MAX_TOKENS_CEILING is SYNTH_MAX_TOKENS_CEILING
    assert _settings().synthesizer_max_tokens < SYNTH_MAX_TOKENS_CEILING


def test_reasoning_effort_defaults_to_medium_and_rejects_typos() -> None:
    """A typo'd effort level must fail at boot, not 400 every request."""
    assert _settings().openai_reasoning_effort == "medium"
    assert _settings(openai_reasoning_effort="HIGH").openai_reasoning_effort == "high"
    with pytest.raises(ValidationError, match="OPENAI_REASONING_EFFORT"):
        _settings(openai_reasoning_effort="ultra")


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_reasoning_effort_uses_the_medium_default(value: str) -> None:
    """Blank deployment variables preserve the checked-in reasoning level."""
    assert _settings(openai_reasoning_effort=value).openai_reasoning_effort == "medium"


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


def _route(result: qa_mod.QAResult) -> dict[str, Any]:
    """The persisted route_json for a turn -- the audit row, not the return value."""
    from regwatch.store.db import session_scope
    from regwatch.store.models import QueryLog

    with session_scope() as session:
        row = session.get(QueryLog, result.audit_id)
        assert row is not None
        return dict(row.route_json)


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


def test_synthesis_defaults_to_3000(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing configured the wire value is the new structured default."""
    provider = _CapturingLLM(_ANSWER_TURN)
    result = _drive(monkeypatch, provider)

    assert not result.refused
    assert provider.max_tokens == [3000]


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
    assert provider.max_tokens == [3000]
    assert provider.stream_max_tokens == []
    # Byte-identical to the rendered answer: the replay is the gate's output,
    # never the model's draft (which said none of "[PSG_020503, p.3]").
    assert "".join(seen) == result.answer
    assert "[PSG_020503, p.3]" in result.answer


# ---------- the truncation retry under the ceiling ----------


def test_truncation_retry_doubles_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """One 2x retry rescues the first overrun of the JSON envelope.

    Delete the retry and this fails twice over: the call list collapses to
    [3000] and the turn degrades to the audited provider_error refusal.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [3000, 6000]
    assert provider.response_formats == ["json", "json"]
    assert not result.refused


def test_truncation_retry_is_clamped_to_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """2x never runs away past the ceiling, however high the operator set the knob.

    4000 is chosen because 2x it (8000) overshoots the 6000 ceiling, which is
    what makes the clamp observable. At the 3000 default the retry lands
    exactly ON the ceiling and would prove nothing.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    with _cap(monkeypatch, 4000):
        result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [4000, SYNTH_MAX_TOKENS_CEILING]  # not 8000
    assert not result.refused


def test_complete_structured_clamps_an_out_of_band_budget() -> None:
    """Defense in depth for a budget Settings can no longer produce.

    Since the validator bound the setting to the ceiling, `_cap(32_000)` fails
    at construction -- so this drives `_complete_structured` DIRECTLY. The
    clamp must stay regardless: it is the last thing between a programmatic
    caller and an unbudgeted per-turn bill.
    """
    provider = _CapturingLLM(_ANSWER_TURN)
    qa_mod._complete_structured(provider, [], max_tokens=32_000)

    assert provider.max_tokens == [SYNTH_MAX_TOKENS_CEILING]


def test_no_retry_once_the_budget_is_already_at_the_ceiling() -> None:
    """At the ceiling a truncation is REAL and must surface, not loop.

    The retry budget min(capped * 2, CEILING) cannot exceed a budget already at
    the ceiling, so re-issuing would send a BYTE-IDENTICAL request at
    temperature 0.0 -- double the spend, same truncation. The error propagates
    and ask() degrades it to the audited provider_error refusal.

    Driven directly for the same reason as above: Settings will no longer
    construct a budget at the ceiling.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        qa_mod._complete_structured(provider, [], max_tokens=SYNTH_MAX_TOKENS_CEILING)

    assert provider.max_tokens == [SYNTH_MAX_TOKENS_CEILING]  # no second attempt


def test_a_truncation_at_the_ceiling_still_reaches_the_audited_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end half of the case above, at the highest configurable budget.

    It serves the service-unavailable copy rather than settings.refusal_text: a
    truncation says nothing about whether the corpus covers the question.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=2)
    with _cap(monkeypatch, SYNTH_MAX_TOKENS_CEILING - 1):
        result = _drive(monkeypatch, provider)

    assert provider.max_tokens == [SYNTH_MAX_TOKENS_CEILING - 1, SYNTH_MAX_TOKENS_CEILING]
    assert result.refused
    assert result.reason == "provider_error"
    assert result.answer == qa_mod._SERVICE_UNAVAILABLE_TEXT
    assert result.citations == []


def test_a_clean_synthesis_records_its_budget_and_no_retry_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the turn was allowed to spend belongs in the audit row.

    Without it, "is our malformed_structure rate a token-cap hit?" cannot be
    answered from the DB at all -- the budget lived only in process config, so
    a row from before a cap change is indistinguishable from one after it.
    """
    provider = _CapturingLLM(_ANSWER_TURN)
    result = _drive(monkeypatch, provider)

    synthesis = _route(result)["synthesis"]
    assert synthesis["max_output_tokens"] == 3000
    assert synthesis["first_budget"] == 3000
    assert "synthesis_retried" not in synthesis


def test_a_truncation_retry_is_recorded_in_route_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry left NOTHING durable -- one structlog line and no more.

    It is the difference between "the budget is fine" and "the budget is
    binding on every long turn", and it was invisible in the DB.
    """
    provider = _CapturingLLM(_ANSWER_TURN, fail_times=1)
    result = _drive(monkeypatch, provider)

    synthesis = _route(result)["synthesis"]
    assert synthesis["synthesis_retried"] is True
    assert synthesis["first_budget"] == 3000
    assert synthesis["retry_budget"] == 6000


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

    assert provider.max_tokens == [3000]  # no second attempt
    assert result.refused
    assert result.reason == "provider_error"
    assert result.citations == []
