"""LLMProvider interface + concrete providers (openai, anthropic, echo).

Business logic NEVER hard-codes a model name. It calls `get_llm_provider()`
and uses the protocol below.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from config.settings import get_settings


@dataclass
class LLMMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMUsage:
    """Token usage for one completion. None = the provider didn't report it."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    raw: dict[str, Any] = field(default_factory=dict)
    # H3: token accounting rides on the existing return object rather than a
    # (text, usage) tuple or provider-level last_usage state — every existing
    # caller keeps working unchanged, there is no mutable per-provider state to
    # race on, and only the call sites that care (the synthesizer audit path)
    # read it.
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass
class LLMStreamChunk:
    """One step of a streaming completion.

    Text arrives as ``delta`` chunks with ``done=False``; the FINAL chunk carries
    ``done=True`` and the fully-assembled ``response`` (the same LLMResponse that
    ``complete()`` would have returned). Callers stream the deltas cosmetically
    but run all validation on the final ``response`` — never on partial deltas.
    """

    delta: str = ""
    done: bool = False
    response: LLMResponse | None = None


def _usage_from(resp: Any, input_attr: str, output_attr: str) -> LLMUsage:
    """Extract token usage defensively — absent/odd shapes yield None, never a guess."""

    def _as_int(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    u = getattr(resp, "usage", None)
    if u is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=_as_int(getattr(u, input_attr, None)),
        output_tokens=_as_int(getattr(u, output_attr, None)),
    )


def estimate_cost_usd(model: str, usage: LLMUsage) -> float | None:
    """USD cost from the settings price table; unknown model/usage -> None (never a guess)."""
    prices = get_settings().price_for_model(model)
    if prices is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    return (
        usage.input_tokens * prices["input"] + usage.output_tokens * prices["output"]
    ) / 1_000_000


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

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        """Yield answer text deltas, then a terminal chunk (``done=True``) whose
        ``response`` is the fully-assembled LLMResponse. Only the synthesizer uses
        this; other callers keep using ``complete()`` and feature-detect via
        ``hasattr(provider, "stream")``."""
        ...


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
        usage = LLMUsage(input_tokens=0, output_tokens=0)  # stub: zeros, never None
        if response_format == "json":
            return LLMResponse(
                text=json.dumps({"echo": last_user}),
                model="echo",
                usage=usage,
            )
        return LLMResponse(text=f"ECHO: {last_user}", model="echo", usage=usage)

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        # Deterministic two-chunk stream of the same text complete() returns, so
        # tests exercise real delta accumulation + the terminal validated chunk.
        resp = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        mid = len(resp.text) // 2
        for part in (resp.text[:mid], resp.text[mid:]):
            if part:
                yield LLMStreamChunk(delta=part)
        yield LLMStreamChunk(done=True, response=resp)


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
            from regwatch.common.llm_clients import shared_openai_client

            s = get_settings()
            self._client = shared_openai_client(
                self.api_key, timeout=s.llm_timeout_s, max_retries=s.llm_max_retries
            )
        return self._client

    @staticmethod
    def _split_messages(
        messages: list[LLMMessage],
    ) -> tuple[str, list[dict[str, str]]]:
        """Responses API shape: system messages -> ``instructions``, the rest ->
        ``input`` items. Shared by complete() and stream() so the two prompt
        assemblies can never drift."""
        instructions = "\n\n".join(m.content for m in messages if m.role == "system")
        input_items = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        return instructions, input_items

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
        instructions, input_items = self._split_messages(messages)
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
            if getattr(exc, "param", None) == "temperature":
                resp = client.responses.create(**kwargs)
            else:
                raise
        # A failed or incomplete (max_output_tokens truncation) response must
        # raise so the caller degrades to an audited refusal: a silently
        # truncated answer would pass citation validation and ship as if
        # complete, which is worse than a refusal. stream() applies the same
        # rule via its terminal-event check, so the two paths agree.
        status = getattr(resp, "status", None)
        if status in ("failed", "incomplete"):
            detail = getattr(resp, "error", None) or getattr(resp, "incomplete_details", None)
            raise RuntimeError(f"openai response status {status}: {detail}")
        text = (getattr(resp, "output_text", "") or "").strip()
        raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
        return LLMResponse(
            text=text,
            model=getattr(resp, "model", self.model),
            raw=raw,
            usage=_usage_from(resp, "input_tokens", "output_tokens"),
        )

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
        return LLMResponse(
            text=text,
            model=self.model,
            raw=resp.model_dump(),
            usage=_usage_from(resp, "prompt_tokens", "completion_tokens"),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        # Real token streaming is the Responses path (the prod synthesizer). In
        # chat mode we degrade to one buffered chunk so the caller still works.
        if self.mode == "chat":
            resp = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
            if resp.text:
                yield LLMStreamChunk(delta=resp.text)
            yield LLMStreamChunk(done=True, response=resp)
            return

        import openai

        client = self._client_or_create()
        instructions, input_items = self._split_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }
        if instructions:
            kwargs["instructions"] = instructions
        try:
            events = client.responses.create(temperature=temperature, stream=True, **kwargs)
        except openai.BadRequestError as exc:
            # Reasoning models (e.g. gpt-5-nano) reject `temperature`; retry without it.
            if getattr(exc, "param", None) == "temperature":
                events = client.responses.create(stream=True, **kwargs)
            else:
                raise
        parts: list[str] = []
        final: Any = None
        for event in events:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                if delta:
                    parts.append(delta)
                    yield LLMStreamChunk(delta=delta)
            elif etype == "response.completed":
                final = getattr(event, "response", None)
            elif etype in ("response.failed", "response.incomplete", "error"):
                # A failed or truncated (max_output_tokens) stream must raise --
                # matching the buffered path, whose caller degrades to an
                # audited refusal -- never launder the partial deltas into a
                # normal-looking terminal chunk that would be validated and
                # cited as if generation finished.
                ev_resp = getattr(event, "response", None)
                detail = (
                    getattr(ev_resp, "error", None)
                    or getattr(ev_resp, "incomplete_details", None)
                    or getattr(event, "message", None)
                )
                raise RuntimeError(f"openai stream terminal event {etype}: {detail}")
        text = "".join(parts).strip()
        model = getattr(final, "model", self.model) if final is not None else self.model
        usage = (
            _usage_from(final, "input_tokens", "output_tokens") if final is not None else LLMUsage()
        )
        raw = final.model_dump() if (final is not None and hasattr(final, "model_dump")) else {}
        yield LLMStreamChunk(
            done=True,
            response=LLMResponse(text=text, model=model, raw=raw, usage=usage),
        )


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
        from regwatch.common.llm_clients import shared_anthropic_client

        s = get_settings()
        client = shared_anthropic_client(
            self.api_key, timeout=s.llm_timeout_s, max_retries=s.llm_max_retries
        )
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
        return LLMResponse(
            text=text,
            model=self.model,
            raw=resp.model_dump(),
            usage=_usage_from(resp, "input_tokens", "output_tokens"),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        # Anthropic is the fallback provider; serve streaming as one buffered
        # chunk (prod uses OpenAI for real token-by-token). Keeps the Protocol
        # total so callers can feature-detect uniformly.
        resp = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        if resp.text:
            yield LLMStreamChunk(delta=resp.text)
        yield LLMStreamChunk(done=True, response=resp)


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
