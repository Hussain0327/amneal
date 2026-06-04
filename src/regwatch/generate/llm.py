"""LLMProvider interface + concrete providers (openai, anthropic, echo).

Business logic NEVER hard-codes a model name. It calls `get_llm_provider()`
and uses the protocol below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from config.settings import get_settings


@dataclass
class LLMMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMResponse:
    text: str
    model: str
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,  # "json" to request JSON-only
    ) -> LLMResponse: ...


# ---------- echo provider ----------
class EchoLLMProvider:
    """For tests. Returns a deterministic string derived from the last user message.

    If response_format == 'json', returns valid JSON: {"echo": "<last user msg>"}.
    """

    name = "echo"

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if response_format == "json":
            return LLMResponse(
                text=json.dumps({"echo": last_user}),
                model="echo",
            )
        return LLMResponse(text=f"ECHO: {last_user}", model="echo")


# ---------- openai provider ----------
class OpenAIProvider:
    """OpenAI provider.

    Defaults to the Responses API (`client.responses.create`), the native surface
    for GPT-5.x models. `mode="chat"` keeps the legacy Chat Completions path. The
    `complete()` interface is identical either way, so the rest of the app is
    agnostic. `client` is injectable for tests.
    """

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str | None,
        *,
        mode: str = "responses",
        client: Any = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.mode = mode
        self._client = client

    def _client_or_create(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        if self.mode == "chat":
            return self._complete_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
        return self._complete_responses(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def _complete_responses(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        response_format: str | None,
    ) -> LLMResponse:
        import openai

        client = self._client_or_create()
        instructions = "\n\n".join(m.content for m in messages if m.role == "system")
        input_items = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if response_format == "json":
            # Responses API JSON object mode (compatibility bridge for the extractor).
            kwargs["text"] = {"format": {"type": "json_object"}}

        try:
            resp = client.responses.create(temperature=temperature, **kwargs)
        except openai.BadRequestError as exc:
            # Reasoning models (e.g. gpt-5-nano) reject `temperature`; retry without it.
            if "temperature" in str(exc).lower():
                resp = client.responses.create(**kwargs)
            else:
                raise
        text = (getattr(resp, "output_text", "") or "").strip()
        raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
        return LLMResponse(text=text, model=getattr(resp, "model", self.model), raw=raw)

    def _complete_chat(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        response_format: str | None,
    ) -> LLMResponse:
        client = self._client_or_create()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        return LLMResponse(text=text, model=self.model, raw=resp.model_dump())


# ---------- anthropic provider ----------
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key)
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        convo = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        prompt_suffix = ""
        if response_format == "json":
            prompt_suffix = "\n\nReturn ONLY a valid JSON object. No prose, no markdown fences."
            if convo:
                convo[-1] = {**convo[-1], "content": convo[-1]["content"] + prompt_suffix}
        resp = client.messages.create(
            model=self.model,
            system=system or None,  # type: ignore[arg-type]
            messages=convo,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = "".join(
            block.text  # type: ignore[union-attr]
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return LLMResponse(text=text, model=self.model, raw=resp.model_dump())


def _model_for_role(s: Any, role: str) -> str:
    """Resolve the model for a call's purpose, falling back to llm_model.

    role ∈ {"router", "synthesizer", "extractor", "default"}.
    """
    by_role = {
        "router": s.router_model,
        "synthesizer": s.synthesizer_model,
        "extractor": s.extractor_model,
    }
    return by_role.get(role) or s.llm_model


def get_llm_provider(name: str | None = None, *, role: str = "default") -> LLMProvider:
    s = get_settings()
    name = (name or s.llm_provider).lower()
    if name == "echo":
        return EchoLLMProvider()
    model = _model_for_role(s, role)
    if name == "openai":
        if not s.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not set; configure or use LLM_PROVIDER=echo")
        return OpenAIProvider(model=model, api_key=s.openai_api_key, mode=s.openai_api_mode)
    if name == "anthropic":
        if not s.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set; configure or use LLM_PROVIDER=echo")
        return AnthropicProvider(model=model, api_key=s.anthropic_api_key)
    raise ValueError(f"unknown LLM provider: {name}")


def current_model_name(role: str = "default") -> str:
    s = get_settings()
    if s.llm_provider == "echo":
        return "echo"
    return _model_for_role(s, role)
