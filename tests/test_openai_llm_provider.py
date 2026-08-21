"""Offline contract tests for the OpenAI Responses provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import regwatch.generate.llm as llm_mod
from regwatch.generate.llm import LLMMessage, LLMUsage, OpenAIProvider, get_llm_provider


def _response(
    text: str,
    *,
    status: str = "completed",
    input_tokens: int = 11,
    output_tokens: int = 7,
    model: str = "gpt-5.6-terra",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_test",
        object="response",
        status=status,
        model=model,
        output_text=text,
        output=[],
        incomplete_details=None,
        error=None,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def _event(event_type: str, **values: Any) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, **values)


class _Responses:
    """Records ``client.responses.create`` requests and replays fixtures."""

    def __init__(self, response: Any, *, stream_events: list[Any] | None = None) -> None:
        self.response = response
        self.stream_events = stream_events
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(self.stream_events or [])
        return self.response


def _provider(
    responses: _Responses,
    *,
    reasoning_effort: str | None = "medium",
) -> OpenAIProvider:
    return OpenAIProvider(
        model="gpt-5.6-terra",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        reasoning_effort=reasoning_effort,
        client=SimpleNamespace(responses=responses),
    )


def test_request_uses_responses_shape_and_regwatch_owned_state() -> None:
    responses = _Responses(_response("answer"))
    messages = [
        LLMMessage("system", "System one."),
        LLMMessage("system", "System two."),
        LLMMessage("user", "question"),
        LLMMessage("assistant", "earlier answer"),
        LLMMessage("user", "follow-up"),
    ]

    _provider(responses).complete(messages, max_tokens=512)

    call = responses.calls[-1]
    assert call == {
        "model": "gpt-5.6-terra",
        "instructions": "System one.\n\nSystem two.",
        "input": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "follow-up"},
        ],
        "max_output_tokens": 512,
        "reasoning": {"effort": "medium"},
        "store": False,
    }
    assert "messages" not in call
    assert "max_completion_tokens" not in call
    assert "previous_response_id" not in call
    assert "conversation" not in call
    assert "temperature" not in call


def test_reasoning_is_omitted_when_unset() -> None:
    responses = _Responses(_response("answer"))

    _provider(responses, reasoning_effort=None).complete([LLMMessage("user", "question")])

    assert "reasoning" not in responses.calls[-1]


def test_json_output_uses_responses_text_format() -> None:
    responses = _Responses(_response('{"answer": true}'))

    _provider(responses).complete(
        [LLMMessage("user", "Return a JSON answer.")],
        response_format="json",
    )

    call = responses.calls[-1]
    assert call["text"] == {"format": {"type": "json_object"}}
    assert "response_format" not in call


def test_complete_reads_output_text_and_responses_usage() -> None:
    responses = _Responses(_response("answer", input_tokens=20, output_tokens=9))

    result = _provider(responses).complete([LLMMessage("user", "question")])

    assert result.text == "answer"
    assert result.model == "gpt-5.6-terra"
    assert result.usage == LLMUsage(input_tokens=20, output_tokens=9)


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
def test_non_completed_response_fails_closed(status: str) -> None:
    responses = _Responses(_response("partial", status=status))

    with pytest.raises(RuntimeError, match=status):
        _provider(responses).complete([LLMMessage("user", "question")])


def test_stream_consumes_typed_responses_events() -> None:
    terminal = _response("answer", input_tokens=12, output_tokens=3)
    responses = _Responses(
        terminal,
        stream_events=[
            _event("response.created", response=_response("")),
            _event("response.output_text.delta", delta="ans"),
            _event("response.output_text.delta", delta="wer"),
            _event("response.completed", response=terminal),
        ],
    )

    chunks = list(_provider(responses).stream([LLMMessage("user", "question")]))

    assert responses.calls[-1]["stream"] is True
    assert [chunk.delta for chunk in chunks[:-1]] == ["ans", "wer"]
    assert chunks[-1].done is True
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "answer"
    assert chunks[-1].response.usage == LLMUsage(
        input_tokens=12,
        output_tokens=3,
    )


@pytest.mark.parametrize(
    "event",
    [
        _event("response.incomplete", response=_response("", status="incomplete")),
        _event("response.failed", response=_response("", status="failed")),
        _event("error", message="simulated error"),
    ],
)
def test_terminal_stream_error_fails_closed(event: SimpleNamespace) -> None:
    responses = _Responses(_response("unused"), stream_events=[event])

    with pytest.raises(RuntimeError):
        list(_provider(responses).stream([LLMMessage("user", "question")]))


def test_factory_builds_openai_provider_with_medium_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        openai_llm_model="gpt-5.6-terra",
        openai_reasoning_effort="medium",
        openai_timeout_s=45.0,
        openai_max_retries=2,
        llm_timeout_s=60.0,
        llm_max_retries=3,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    provider = get_llm_provider(role="synthesizer")

    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-5.6-terra"
    assert provider.reasoning_effort == "medium"


def test_databricks_provider_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm_provider="databricks",
        databricks_llm_base_url="https://workspace.example",
        databricks_llm_token="token",
        databricks_llm_model="workspace.default.regwatch",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="unknown LLM provider: databricks"):
        get_llm_provider()
