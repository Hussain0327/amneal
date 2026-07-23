"""Offline contract tests for the Databricks Gemma Chat Completions provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import regwatch.generate.llm as llm_mod
from regwatch.common import llm_clients
from regwatch.generate.llm import (
    DatabricksProvider,
    LLMMessage,
    LLMUsage,
    current_model_name,
    get_llm_provider,
)


def _choice(
    content: Any = None,
    *,
    finish_reason: str | None = "stop",
    delta: bool = False,
    reasoning: str = "PRIVATE FIELD",
) -> SimpleNamespace:
    payload = SimpleNamespace(content=content, reasoning_content=reasoning)
    key = "delta" if delta else "message"
    return SimpleNamespace(**{key: payload}, finish_reason=finish_reason)


def _response(
    content: Any,
    *,
    finish_reason: str | None = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="db-response",
        object="chat.completion",
        created=123,
        model="served-gemma-revision",
        choices=[_choice(content, finish_reason=finish_reason)],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=5,
        ),
        # If production code dumps the whole object, this private field would
        # reach LLMResponse.raw. The provider must use an allow-list instead.
        model_dump=lambda: {"reasoning_content": "PRIVATE MODEL DUMP"},
    )


class _Completions:
    def __init__(
        self,
        response: Any,
        *,
        stream_events: list[Any] | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.stream_events = stream_events
        self.stream_error = stream_error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if self.stream_error is not None:
                raise self.stream_error
            return iter(self.stream_events or [])
        return self.response


def _client(completions: _Completions) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _provider(
    completions: _Completions,
    *,
    role: str = "synthesizer",
    thinking_enabled: bool = True,
) -> DatabricksProvider:
    return DatabricksProvider(
        model="gemma-endpoint",
        base_url="https://workspace.example/serving-endpoints",
        token="token",
        role=role,
        thinking_enabled=thinking_enabled,
        client=_client(completions),
    )


def test_dedicated_client_cache_keys_every_databricks_transport_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")
    calls: list[dict[str, Any]] = []

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls.append(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    llm_clients.shared_openai_client.cache_clear()
    llm_clients.shared_databricks_openai_client.cache_clear()
    try:
        ordinary = llm_clients.shared_openai_client("openai-key", timeout=30.0, max_retries=2)
        first = llm_clients.shared_databricks_openai_client(
            "https://db.example/serving-endpoints",
            "db-token",
            timeout=45.0,
            max_retries=1,
        )
        cached = llm_clients.shared_databricks_openai_client(
            "https://db.example/serving-endpoints",
            "db-token",
            timeout=45.0,
            max_retries=1,
        )
        rotated = llm_clients.shared_databricks_openai_client(
            "https://db.example/serving-endpoints",
            "rotated-token",
            timeout=45.0,
            max_retries=1,
        )
    finally:
        llm_clients.shared_openai_client.cache_clear()
        llm_clients.shared_databricks_openai_client.cache_clear()

    assert first is cached
    assert first is not rotated
    assert ordinary is not first
    assert calls == [
        {"api_key": "openai-key", "timeout": 30.0, "max_retries": 2},
        {
            "api_key": "db-token",
            "base_url": "https://db.example/serving-endpoints",
            "timeout": 45.0,
            "max_retries": 1,
        },
        {
            "api_key": "rotated-token",
            "base_url": "https://db.example/serving-endpoints",
            "timeout": 45.0,
            "max_retries": 1,
        },
    ]


def test_complete_strips_inline_and_structured_reasoning_from_text_and_raw() -> None:
    response = _response(
        [
            {"type": "reasoning", "text": "PRIVATE CONTENT PART"},
            {
                "type": "text",
                "text": (
                    "<|channel>thought\nPRIVATE INLINE THOUGHT"
                    "<channel|>Grounded answer [PSG_1, p.1]"
                ),
            },
        ]
    )
    completions = _Completions(response)

    result = _provider(completions).complete([LLMMessage("user", "question")])

    assert result.text == "Grounded answer [PSG_1, p.1]"
    assert result.model == "served-gemma-revision"
    assert result.usage == LLMUsage(input_tokens=11, output_tokens=7)
    assert result.raw == {
        "id": "db-response",
        "object": "chat.completion",
        "created": 123,
        "model": "served-gemma-revision",
        "finish_reason": "stop",
    }
    assert "PRIVATE" not in result.text
    assert "PRIVATE" not in repr(result.raw)


@pytest.mark.parametrize("role", ["router", "extractor", "default"])
def test_thinking_is_forced_off_outside_synthesizer(role: str) -> None:
    completions = _Completions(_response("<|channel>thought\n<channel|>answer"))
    provider = _provider(completions, role=role, thinking_enabled=True)

    provider.complete(
        [
            LLMMessage("system", "<|think|>\nBe strict."),
            LLMMessage("user", "question"),
        ]
    )

    assert provider.thinking_enabled is False
    assert completions.calls[-1]["messages"][0] == {
        "role": "system",
        "content": "Be strict.",
    }


def test_synthesizer_thinking_is_opt_in_but_json_mode_disables_it() -> None:
    completions = _Completions(_response('{"ok": true}'))
    provider = _provider(completions)
    messages = [LLMMessage("system", "Be strict."), LLMMessage("user", "question")]

    provider.complete(messages)
    provider.complete(messages, response_format="json")

    prose_call, json_call = completions.calls
    assert prose_call["messages"][0]["content"] == "<|think|>\nBe strict."
    assert "<|think|>" not in json_call["messages"][0]["content"]
    assert json_call["response_format"] == {"type": "json_object"}


def test_stream_buffers_split_thought_delimiters_and_accounts_for_usage() -> None:
    events = [
        SimpleNamespace(
            model="served-gemma-revision",
            choices=[_choice("<|channel>tho", finish_reason=None, delta=True)],
            usage=None,
        ),
        SimpleNamespace(
            model="served-gemma-revision",
            choices=[
                _choice(
                    "ught\nPRIVATE STREAM THOUGHT<channel|>Grounded ",
                    finish_reason=None,
                    delta=True,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            id="db-stream",
            object="chat.completion.chunk",
            model="served-gemma-revision",
            choices=[_choice("answer.", finish_reason="stop", delta=True)],
            usage=None,
        ),
        SimpleNamespace(
            id="db-stream",
            object="chat.completion.chunk",
            model="served-gemma-revision",
            choices=[],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=13),
        ),
    ]
    completions = _Completions(_response("unused"), stream_events=events)

    chunks = list(_provider(completions).stream([LLMMessage("user", "question")]))

    assert [chunk.delta for chunk in chunks if not chunk.done] == ["Grounded answer."]
    terminal = chunks[-1]
    assert terminal.done is True
    assert terminal.response is not None
    assert terminal.response.text == "Grounded answer."
    assert terminal.response.usage == LLMUsage(input_tokens=20, output_tokens=13)
    assert "PRIVATE" not in repr(terminal.response.raw)
    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}


def test_stream_uses_safe_buffered_completion_fallback_before_exposing_text() -> None:
    completions = _Completions(
        _response("<think>PRIVATE FALLBACK THOUGHT</think>Visible fallback."),
        stream_error=NotImplementedError("streaming disabled"),
    )

    chunks = list(_provider(completions).stream([LLMMessage("user", "question")]))

    assert [chunk.delta for chunk in chunks if not chunk.done] == ["Visible fallback."]
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "Visible fallback."
    assert [call.get("stream", False) for call in completions.calls] == [True, False]


def test_complete_rejects_truncated_output() -> None:
    completions = _Completions(_response("partial", finish_reason="length"))

    with pytest.raises(RuntimeError, match="finish_reason=length"):
        _provider(completions).complete([LLMMessage("user", "question")])


def test_factory_builds_role_aware_databricks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm_provider="databricks",
        databricks_llm_base_url="https://workspace.example/serving-endpoints",
        databricks_llm_token="token",
        databricks_llm_model="gemma-endpoint",
        gemma_thinking_enabled=True,
        llm_timeout_s=31.0,
        llm_max_retries=1,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    synthesizer = get_llm_provider(role="synthesizer")
    extractor = get_llm_provider(role="extractor")

    assert isinstance(synthesizer, DatabricksProvider)
    assert synthesizer.thinking_enabled is True
    assert synthesizer.timeout == 31.0
    assert synthesizer.max_retries == 1
    assert isinstance(extractor, DatabricksProvider)
    assert extractor.thinking_enabled is False
    assert current_model_name(role="extractor") == "gemma-endpoint"


@pytest.mark.parametrize(
    ("missing_attr", "expected_env"),
    [
        ("databricks_llm_base_url", "DATABRICKS_LLM_BASE_URL"),
        ("databricks_llm_token", "DATABRICKS_LLM_TOKEN"),
        ("databricks_llm_model", "DATABRICKS_LLM_MODEL"),
    ],
)
def test_factory_fails_loudly_for_missing_databricks_config(
    monkeypatch: pytest.MonkeyPatch,
    missing_attr: str,
    expected_env: str,
) -> None:
    settings = SimpleNamespace(
        llm_provider="databricks",
        databricks_llm_base_url="https://workspace.example/serving-endpoints",
        databricks_llm_token="token",
        databricks_llm_model="gemma-endpoint",
        gemma_thinking_enabled=False,
        llm_timeout_s=31.0,
        llm_max_retries=1,
    )
    setattr(settings, missing_attr, "")
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match=expected_env):
        get_llm_provider()
