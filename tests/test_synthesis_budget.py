"""The synthesizer output cap is one operator knob that reaches BOTH twins.

Background: an open-weight reasoning model draws its thought and its answer from
the same max_tokens budget, so the cap tuned for gpt-5.4-nano (900, all answer)
truncates a reasoning model mid-thought -- finish_reason=length, which the
provider raises and grounded_qa degrades into an audited refusal. The cap became
a setting so an operator has headroom without a deploy. The default must NOT
move: LLM_PROVIDER=openai is the live rollback path and its answer length, the
eval baselines, the dossier and the white paper all ride on 900.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from config.settings import Settings, get_settings
from pydantic import ValidationError

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse, LLMStreamChunk
from tests.test_invariants import _meta, _seed_corpus

pytestmark = pytest.mark.invariants


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "database_url": "postgresql://u@h/db"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------- Settings ----------


def test_synthesizer_cap_default_is_the_historical_constant() -> None:
    """Moving this default silently changes the OpenAI rollback path."""
    assert _settings().synthesizer_max_tokens == 900


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


# ---------- the cap reaches both synthesis twins ----------


class _CapturingLLM:
    """Records the max_tokens each synthesis path asked for."""

    name = "stub-cap"

    def __init__(self, text: str) -> None:
        self.text = text
        self.complete_max_tokens: list[Any] = []
        self.stream_max_tokens: list[Any] = []

    def complete(self, *a: object, **kw: Any) -> LLMResponse:
        self.complete_max_tokens.append(kw.get("max_tokens"))
        return LLMResponse(text=self.text, model="stub-cap")

    def stream(self, *a: object, **kw: Any) -> Iterator[LLMStreamChunk]:
        self.stream_max_tokens.append(kw.get("max_tokens"))
        yield LLMStreamChunk(delta=self.text)
        yield LLMStreamChunk(done=True, response=LLMResponse(text=self.text, model="stub-cap"))


def _drive_both_twins(monkeypatch: pytest.MonkeyPatch) -> _CapturingLLM:
    _seed_corpus([("Fasting BE study with 36 subjects.", _meta(1, 3, "PSG_020503"))])
    provider = _CapturingLLM("A fasting study is recommended [PSG_020503, p.3].")
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    qa_mod.ask("What study design is recommended?")  # buffered branch
    qa_mod.ask("What study design is recommended?", on_token=lambda _t: None)  # stream branch
    return provider


def test_both_twins_use_the_configured_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """One read per turn, handed to whichever twin runs.

    This is the test that fails if someone re-hardcodes the cap in one of the
    two paths: answer length would then depend on whether the client streamed.
    """
    monkeypatch.setenv("SYNTHESIZER_MAX_TOKENS", "2600")
    import config.settings as cs

    cs.get_settings.cache_clear()
    try:
        assert get_settings().synthesizer_max_tokens == 2600
        provider = _drive_both_twins(monkeypatch)
    finally:
        monkeypatch.delenv("SYNTHESIZER_MAX_TOKENS", raising=False)
        cs.get_settings.cache_clear()

    assert provider.complete_max_tokens == [2600]
    assert provider.stream_max_tokens == [2600]


def test_both_twins_default_to_900(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing configured, the wire value is byte-identical to the
    pre-change constant -- LLM_PROVIDER=openai must not notice this commit."""
    provider = _drive_both_twins(monkeypatch)

    assert provider.complete_max_tokens == [900]
    assert provider.stream_max_tokens == [900]
