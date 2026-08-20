"""Offline contract tests for the OpenAI Chat Completions provider.

Migration target: gpt-5.6-terra generation via OpenAI Chat Completions
(config/settings.py openai_* fields). Reasoning models on that family reject
a non-default `temperature` with a hard 400 on the very first call, and
reject `max_tokens` in favor of `max_completion_tokens`. `reasoning_effort`
is a valid top-level Chat Completions parameter for the series, and
streaming usage requires `stream_options={"include_usage": True}` or usage
is silently absent from the stream and `cost_usd` persists NULL with no
error and no failing test. These tests pin that wire shape offline, with no
network calls and no OPENAI_API_KEY required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import regwatch.generate.llm as llm_mod
from regwatch.generate.llm import LLMMessage, LLMUsage, OpenAIProvider, get_llm_provider


def _choice(
    content: Any,
    *,
    finish_reason: str | None = "stop",
    delta: bool = False,
) -> SimpleNamespace:
    payload = SimpleNamespace(content=content)
    key = "delta" if delta else "message"
    return SimpleNamespace(**{key: payload}, finish_reason=finish_reason)


def _response(
    content: Any,
    *,
    finish_reason: str | None = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
    model: str | None = "gpt-5.6-terra",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="oa-response",
        object="chat.completion",
        created=123,
        model=model,
        choices=[_choice(content, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class _Completions:
    """Fake `client.chat.completions` -- records every kwargs dict it receives."""

    def __init__(self, response: Any, *, stream_events: list[Any] | None = None) -> None:
        self.response = response
        self.stream_events = stream_events
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(self.stream_events or [])
        return self.response


def _client(completions: _Completions) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _provider(
    completions: _Completions,
    *,
    reasoning_effort: str | None = None,
) -> OpenAIProvider:
    return OpenAIProvider(
        model="gpt-5.6-terra",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        reasoning_effort=reasoning_effort,
        client=_client(completions),
    )


def _stream_event(content: str, *, finish_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="oa-stream",
        object="chat.completion.chunk",
        model="gpt-5.6-terra",
        choices=[_choice(content, finish_reason=finish_reason, delta=True)],
        usage=None,
    )


def test_request_never_sends_temperature() -> None:
    """The outage-on-first-call bug: sending temperature=0.0 unconditionally
    (as DatabricksProvider._request_kwargs does) is a hard 400 on a reasoning
    model's very first call. The key must be absent, not merely defaulted."""
    completions = _Completions(_response("answer"))

    _provider(completions).complete([LLMMessage("user", "question")])

    assert "temperature" not in completions.calls[-1]


def test_request_uses_max_completion_tokens_not_max_tokens() -> None:
    completions = _Completions(_response("answer"))

    _provider(completions).complete([LLMMessage("user", "question")], max_tokens=512)

    call = completions.calls[-1]
    assert call["max_completion_tokens"] == 512
    assert "max_tokens" not in call


def test_reasoning_effort_is_sent_when_configured() -> None:
    completions = _Completions(_response("answer"))

    _provider(completions, reasoning_effort="low").complete([LLMMessage("user", "question")])

    assert completions.calls[-1]["reasoning_effort"] == "low"


def test_reasoning_effort_is_omitted_entirely_when_unset() -> None:
    """Absent key, never a null: an endpoint unaware of the parameter must not
    see `reasoning_effort: None` and 400 every call."""
    completions = _Completions(_response("answer"))

    _provider(completions, reasoning_effort=None).complete([LLMMessage("user", "question")])

    assert "reasoning_effort" not in completions.calls[-1]


def test_streaming_requests_usage_via_stream_options() -> None:
    """Without this, usage is absent from the stream and cost_usd persists
    NULL silently with no error and no failing test."""
    completions = _Completions(
        _response("unused"),
        stream_events=[_stream_event("answer", finish_reason="stop")],
    )

    list(_provider(completions).stream([LLMMessage("user", "question")]))

    call = completions.calls[-1]
    assert call["stream"] is True
    assert call["stream_options"] == {"include_usage": True}


def test_complete_reads_prompt_and_completion_token_usage() -> None:
    """Pins the Chat Completions attribute names (prompt_tokens /
    completion_tokens): switching to the Responses API would silently break
    this, along with streaming and structured output."""
    completions = _Completions(_response("answer", prompt_tokens=20, completion_tokens=9))

    result = _provider(completions).complete([LLMMessage("user", "question")])

    assert result.usage == LLMUsage(input_tokens=20, output_tokens=9)
    assert result.text == "answer"
    assert result.model == "gpt-5.6-terra"


def test_get_llm_provider_openai_by_explicit_name_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today get_llm_provider raises ValueError('unknown LLM provider: openai')
    (llm.py:983-1044, confirmed by live probe). This is the test that catches
    a regression back to that state once the branch exists. The explicit name
    argument must win over settings.llm_provider, exactly like the "databricks"
    and "echo" branches already do.
    """
    settings = SimpleNamespace(
        llm_provider="databricks",  # deliberately NOT openai: the explicit name must win
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        openai_llm_model="gpt-5.6-terra",
        openai_reasoning_effort="low",
        openai_timeout_s=45.0,
        openai_max_retries=2,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    provider = get_llm_provider("openai")

    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai"


def test_factory_builds_openai_provider_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        openai_llm_model="gpt-5.6-terra",
        openai_reasoning_effort="low",
        openai_timeout_s=45.0,
        openai_max_retries=2,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    provider = get_llm_provider(role="synthesizer")

    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-5.6-terra"
    assert provider.reasoning_effort == "low"
