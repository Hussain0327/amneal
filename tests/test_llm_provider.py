"""OpenAI provider: Responses API surface, JSON mode, role→model, temp retry."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

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


class _Event:
    """One streamed Responses-API event (duck-typed: type + optional attrs)."""

    def __init__(self, type: str, **attrs: object) -> None:
        self.type = type
        for key, value in attrs.items():
            setattr(self, key, value)


class _Responses:
    def __init__(self, reject_temperature: bool = False, message_only: bool = False) -> None:
        self.calls: list[dict] = []
        self.reject_temperature = reject_temperature
        self.message_only = message_only

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_temperature and "temperature" in kwargs:
            req = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise openai.BadRequestError(
                "Unsupported parameter: 'temperature' is not supported with this model.",
                response=httpx.Response(400, request=req),
                body=None if self.message_only else {"param": "temperature"},
            )
        if kwargs.get("stream"):
            return iter(
                [
                    _Event("response.output_text.delta", delta="po"),
                    _Event("response.output_text.delta", delta="ng"),
                    _Event("response.completed", response=_Resp()),
                ]
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
    def __init__(self, reject_temperature: bool = False, message_only: bool = False) -> None:
        self.responses = _Responses(reject_temperature, message_only)
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


def test_temperature_retry_uses_structured_param_not_message() -> None:
    fake = _FakeClient(reject_temperature=True, message_only=True)
    with pytest.raises(openai.BadRequestError):
        _provider(fake).complete([LLMMessage("user", "hi")], temperature=0.0)
    assert len(fake.responses.calls) == 1


def test_stream_temperature_retry_on_reasoning_model() -> None:
    fake = _FakeClient(reject_temperature=True)
    chunks = list(_provider(fake).stream([LLMMessage("user", "hi")], temperature=0.0))
    # First call carried temperature (rejected), second omitted it; both streamed.
    assert len(fake.responses.calls) == 2
    assert "temperature" in fake.responses.calls[0]
    assert "temperature" not in fake.responses.calls[1]
    assert all(call["stream"] is True for call in fake.responses.calls)
    # The retried stream still delivers deltas then the terminal validated chunk.
    assert "".join(c.delta for c in chunks if not c.done) == "pong"
    assert chunks[-1].done is True
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "pong"


def test_stream_temperature_retry_uses_structured_param_not_message() -> None:
    fake = _FakeClient(reject_temperature=True, message_only=True)
    with pytest.raises(openai.BadRequestError):
        list(_provider(fake).stream([LLMMessage("user", "hi")], temperature=0.0))
    assert len(fake.responses.calls) == 1


def test_chat_mode_uses_chat_completions() -> None:
    fake = _FakeClient()
    # In chat mode, the responses surface must be untouched; chat.create raises.
    try:
        _provider(fake, mode="chat").complete([LLMMessage("user", "hi")])
    except AssertionError as e:
        assert "Chat Completions" in str(e)
    assert fake.responses.calls == []


# ---------- terminal failure / truncation must raise, never a fake completion ----------


def _event(type_: str, **attrs: Any) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **attrs)


def _stream_provider(events: list[SimpleNamespace]) -> OpenAIProvider:
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: iter(events)))
    return OpenAIProvider(model="gpt-5.4-nano", api_key="x", mode="responses", client=client)


def test_stream_failed_event_raises_after_deltas() -> None:
    """response.failed raises (the caller degrades to an audited refusal) instead
    of a done chunk quietly built from the partial deltas already yielded."""
    events = [
        _event("response.output_text.delta", delta="A fasting "),
        _event("response.failed", response=SimpleNamespace(error="server_error: boom")),
    ]
    it = _stream_provider(events).stream([LLMMessage("user", "q")])
    assert next(it).delta == "A fasting "
    with pytest.raises(RuntimeError, match=r"response\.failed"):
        next(it)


def test_stream_incomplete_event_raises() -> None:
    """max_output_tokens truncation is a provider failure, not a completion: a
    silently truncated answer would be validated and cited as if complete."""
    events = [
        _event("response.output_text.delta", delta="Dissolution uses app"),
        _event(
            "response.incomplete",
            response=SimpleNamespace(
                incomplete_details=SimpleNamespace(reason="max_output_tokens")
            ),
        ),
    ]
    it = _stream_provider(events).stream([LLMMessage("user", "q")])
    assert next(it).delta == "Dissolution uses app"
    with pytest.raises(RuntimeError, match=r"response\.incomplete"):
        next(it)


def test_stream_completed_event_still_yields_terminal_chunk() -> None:
    """The failure check must not swallow a normal stream: deltas then a
    response.completed event still produce the validated done chunk."""
    events = [
        _event("response.output_text.delta", delta="pong"),
        _event("response.completed", response=_Resp(text="pong")),
    ]
    chunks = list(_stream_provider(events).stream([LLMMessage("user", "q")]))
    assert [c.delta for c in chunks if not c.done] == ["pong"]
    assert chunks[-1].done and chunks[-1].response is not None
    assert chunks[-1].response.text == "pong"


def test_complete_incomplete_status_raises() -> None:
    """Buffered path parity: a resp.status == 'incomplete' (truncation) raises
    instead of returning the partial output_text as a normal LLMResponse."""
    resp = SimpleNamespace(
        output_text="Dissolution uses app",
        model="gpt-5.4-nano",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: resp))
    p = OpenAIProvider(model="gpt-5.4-nano", api_key="x", mode="responses", client=client)
    with pytest.raises(RuntimeError, match="incomplete"):
        p.complete([LLMMessage("user", "q")])


def _set_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ROUTER_MODEL", "router-m")
    monkeypatch.setenv("SYNTHESIZER_MODEL", "synth-m")
    monkeypatch.setenv("EXTRACTOR_MODEL", "extract-m")
    monkeypatch.setenv("LLM_MODEL", "legacy-m")
    import config.settings as cs

    cs.get_settings.cache_clear()


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
    import config.settings as cs

    cs.get_settings.cache_clear()
    assert cast(OpenAIProvider, get_llm_provider(role="synthesizer")).model == "legacy-m"
