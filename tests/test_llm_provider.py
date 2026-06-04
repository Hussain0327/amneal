"""OpenAI provider: Responses API surface, JSON mode, role→model, temp retry."""

from __future__ import annotations

from typing import cast

import httpx
import openai
import pytest

from regwatch.generate.llm import (
    LLMMessage,
    OpenAIProvider,
    current_model_name,
    get_llm_provider,
)


class _Resp:
    def __init__(self, text: str = "pong", model: str = "gpt-5.4-nano") -> None:
        self.output_text = text
        self.model = model

    def model_dump(self) -> dict:
        return {"model": self.model, "output_text": self.output_text}


class _Responses:
    def __init__(self, reject_temperature: bool = False) -> None:
        self.calls: list[dict] = []
        self.reject_temperature = reject_temperature

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_temperature and "temperature" in kwargs:
            req = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise openai.BadRequestError(
                "Unsupported parameter: 'temperature' is not supported with this model.",
                response=httpx.Response(400, request=req),
                body=None,
            )
        return _Resp()


class _Completions:
    def __init__(self) -> None:
        self.called = False

    def create(self, **kwargs):
        self.called = True
        raise AssertionError("Chat Completions must not be called in responses mode")


class _Chat:
    def __init__(self) -> None:
        self.completions = _Completions()


class _FakeClient:
    def __init__(self, reject_temperature: bool = False) -> None:
        self.responses = _Responses(reject_temperature)
        self.chat = _Chat()


def _provider(client: _FakeClient, mode: str = "responses") -> OpenAIProvider:
    return OpenAIProvider(model="gpt-5.4-nano", api_key="x", mode=mode, client=client)


def test_responses_mode_calls_responses_not_chat() -> None:
    fake = _FakeClient()
    r = _provider(fake).complete([LLMMessage("user", "hi")])
    assert len(fake.responses.calls) == 1
    assert fake.chat.completions.called is False
    assert r.text == "pong"
    assert r.model == "gpt-5.4-nano"


def test_system_message_becomes_instructions() -> None:
    fake = _FakeClient()
    _provider(fake).complete([LLMMessage("system", "be strict"), LLMMessage("user", "q")])
    call = fake.responses.calls[-1]
    assert call["instructions"] == "be strict"
    assert call["input"] == [{"role": "user", "content": "q"}]
    assert call["max_output_tokens"] == 1024  # default max_tokens mapped


def test_json_mode_sets_text_format() -> None:
    fake = _FakeClient()
    _provider(fake).complete([LLMMessage("user", "give json")], response_format="json")
    assert fake.responses.calls[-1]["text"] == {"format": {"type": "json_object"}}


def test_temperature_retry_on_reasoning_model() -> None:
    fake = _FakeClient(reject_temperature=True)
    r = _provider(fake).complete([LLMMessage("user", "hi")], temperature=0.0)
    # First call carried temperature (rejected), second omitted it and succeeded.
    assert len(fake.responses.calls) == 2
    assert "temperature" in fake.responses.calls[0]
    assert "temperature" not in fake.responses.calls[1]
    assert r.text == "pong"


def test_chat_mode_uses_chat_completions() -> None:
    fake = _FakeClient()
    # In chat mode, the responses surface must be untouched; chat.create raises.
    try:
        _provider(fake, mode="chat").complete([LLMMessage("user", "hi")])
    except AssertionError as e:
        assert "Chat Completions" in str(e)
    assert fake.responses.calls == []


def _set_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ROUTER_MODEL", "router-m")
    monkeypatch.setenv("SYNTHESIZER_MODEL", "synth-m")
    monkeypatch.setenv("EXTRACTOR_MODEL", "extract-m")
    monkeypatch.setenv("LLM_MODEL", "legacy-m")


def test_role_selects_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_openai(monkeypatch)
    assert cast(OpenAIProvider, get_llm_provider(role="router")).model == "router-m"
    assert cast(OpenAIProvider, get_llm_provider(role="synthesizer")).model == "synth-m"
    assert cast(OpenAIProvider, get_llm_provider(role="extractor")).model == "extract-m"
    assert cast(OpenAIProvider, get_llm_provider()).model == "legacy-m"  # default → llm_model
    assert current_model_name(role="extractor") == "extract-m"


def test_role_falls_back_to_llm_model_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_openai(monkeypatch)
    monkeypatch.setenv("SYNTHESIZER_MODEL", "")  # empty → fall back to llm_model
    assert cast(OpenAIProvider, get_llm_provider(role="synthesizer")).model == "legacy-m"
