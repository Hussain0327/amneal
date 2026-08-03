"""LLMProvider interface + concrete providers (openai, databricks, anthropic, echo).

Business logic NEVER hard-codes a model name. It calls `get_llm_provider()`
and uses the protocol below.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

from config.settings import d1_model_rejection, get_settings

from regwatch.common.logging import get_logger
from regwatch.common.structured_json import extract_json_blob as _extract_json_blob

log = get_logger(__name__)


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


def _buffered_stream(
    provider: LLMProvider,
    messages: list[LLMMessage],
    *,
    temperature: float,
    max_tokens: int,
) -> Iterator[LLMStreamChunk]:
    """Degrade streaming to one buffered chunk built from complete().

    Shared by providers/modes with no real token streaming (OpenAI chat mode,
    Anthropic) so they still honor the stream() contract: deltas, then the
    terminal chunk carrying the fully-validated response.
    """
    resp = provider.complete(messages, temperature=temperature, max_tokens=max_tokens)
    if resp.text:
        yield LLMStreamChunk(delta=resp.text)
    yield LLMStreamChunk(done=True, response=resp)


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


# Matches the passage headers _format_passages writes: "[<short_name>, p.<n>]".
_ECHO_CITATION_RE = re.compile(r"\[([A-Za-z0-9_]+),\s*p\.\s*(\d+)\]")


# ---------- echo provider ----------
class EchoLLMProvider:
    """For tests. Returns a deterministic string derived from the last user message.

    If response_format == 'json', returns valid JSON: {"echo": "<last user msg>"}.

    REGWATCH_ECHO_FORCE_REFUSAL (truthy) flips completions to a decline so
    wire-level suites (tests_contract) can reach the synthesis-time decline
    path. REGWATCH_ECHO_FORCE_MALFORMED (truthy) returns unparseable text in
    json mode so the same suites can drive the malformed_structure branch over
    the real wire. Echo is a test-grade provider already fenced from prod by the
    REGWATCH_ALLOW_TEST_PROVIDERS boot guard, so neither knob can reach a real
    deployment.
    """

    name = "echo"

    @staticmethod
    def _flag(name: str) -> bool:
        value = os.environ.get(name, "").strip().lower()
        return value not in ("", "0", "false")

    @staticmethod
    def _force_refusal() -> bool:
        return EchoLLMProvider._flag("REGWATCH_ECHO_FORCE_REFUSAL")

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
        # Scrape the prompt's FIRST passage marker BEFORE the json branch: it is
        # what discriminates the synthesizer (whose user prompt carries passage
        # headers) from the extractor / change detector (which carry none) now
        # that BOTH ask for json. Get this wrong and the wire suite fails, not a
        # unit test.
        marker = _ECHO_CITATION_RE.search(last_user)
        if response_format == "json":
            if self._flag("REGWATCH_ECHO_FORCE_MALFORMED"):
                return LLMResponse(text="not json at all {", model="echo", usage=usage)
            if marker is None:
                # Non-QA json callers keep the historical shape verbatim.
                return LLMResponse(text=json.dumps({"echo": last_user}), model="echo", usage=usage)
            if self._force_refusal():
                return LLMResponse(
                    text=json.dumps({"turn_type": "NO_EVIDENCE", "claims": [], "unsupported": []}),
                    model="echo",
                    usage=usage,
                )
            return LLMResponse(
                text=json.dumps(
                    {
                        "turn_type": "ANSWER",
                        "claims": [
                            {
                                "text": "ECHO grounded test answer",
                                "cites": [
                                    {
                                        "short_name": marker.group(1),
                                        "page": int(marker.group(2)),
                                    }
                                ],
                            }
                        ],
                        "unsupported": [],
                    }
                ),
                model="echo",
                usage=usage,
            )
        if self._force_refusal():
            return LLMResponse(text=get_settings().refusal_text, model="echo", usage=usage)
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
        # stream() carries no response_format, so this is the PROSE shape; the
        # synthesizer no longer streams from the provider.
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

    def _responses_kwargs(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        """Responses-API request kwargs, shared by complete() and stream().

        ``response_format`` stays a complete()-only concern: streaming is the
        synthesizer's prose path and never requests JSON mode.
        """
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
        return kwargs

    def _create_responses(
        self, kwargs: dict[str, Any], *, temperature: float, stream: bool = False
    ) -> Any:
        """One responses.create call with the reasoning-model temperature retry.

        Single home for the quirk workaround so complete() and stream() can
        never drift: reasoning models (e.g. gpt-5-nano) reject ``temperature``
        via a structured BadRequestError param; retry once without it. Any
        other BadRequestError re-raises unchanged.
        """
        import openai

        client = self._client_or_create()
        if stream:
            kwargs = {**kwargs, "stream": True}
        try:
            return client.responses.create(temperature=temperature, **kwargs)
        except openai.BadRequestError as exc:
            if getattr(exc, "param", None) == "temperature":
                return client.responses.create(**kwargs)
            raise

    def _complete_responses(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        response_format: str | None,
    ) -> LLMResponse:
        kwargs = self._responses_kwargs(
            messages, max_tokens=max_tokens, response_format=response_format
        )
        resp = self._create_responses(kwargs, temperature=temperature)
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
            yield from _buffered_stream(
                self, messages, temperature=temperature, max_tokens=max_tokens
            )
            return

        kwargs = self._responses_kwargs(messages, max_tokens=max_tokens)
        events = self._create_responses(kwargs, temperature=temperature, stream=True)
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


# ---------- Databricks Gemma provider ----------
_GEMMA_THOUGHT_START = re.compile(r"<\|channel>thought(?:\r?\n)?", re.IGNORECASE)
_GEMMA_XML_THOUGHT_START = re.compile(r"<think>", re.IGNORECASE)


def _drop_private_block(text: str, start: re.Pattern[str], end: str) -> str:
    """Drop every complete or unterminated private block.

    Unterminated thought output is discarded from its opening delimiter to the
    end. Returning a shorter/empty answer is safer than exposing chain of
    thought when a serving engine truncates before Gemma's closing token.
    """
    visible: list[str] = []
    cursor = 0
    while match := start.search(text, cursor):
        visible.append(text[cursor : match.start()])
        close = text.find(end, match.end())
        if close < 0:
            return "".join(visible)
        cursor = close + len(end)
    visible.append(text[cursor:])
    return "".join(visible)


def _visible_gemma_text(text: str) -> str:
    """Return only Gemma's final-answer channel.

    Gemma 4 can emit its thought channel inline even when thinking is disabled,
    and some OpenAI-compatible servers expose reasoning in ``content`` instead
    of a separate field. Handle both Gemma's native delimiters and the common
    ``<think>`` compatibility form. A stray closing delimiter is treated
    conservatively: everything before it may have been private reasoning.
    """
    cleaned = _drop_private_block(text, _GEMMA_THOUGHT_START, "<channel|>")
    if "<channel|>" in cleaned:
        cleaned = cleaned.rsplit("<channel|>", 1)[-1]
    cleaned = _drop_private_block(cleaned, _GEMMA_XML_THOUGHT_START, "</think>")
    lower = cleaned.lower()
    if "</think>" in lower:
        cleaned = cleaned[lower.rfind("</think>") + len("</think>") :]
    return cleaned.replace("<|think|>", "").strip()


def _chat_content_text(content: Any) -> str:
    """Extract visible candidate text without accepting reasoning content parts."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            item_type = item.get("type")
            value = item.get("text")
        else:
            item_type = getattr(item, "type", None)
            value = getattr(item, "text", None)
        if item_type in ("reasoning", "reasoning_content", "thinking", "analysis"):
            continue
        if item_type not in (None, "text", "output_text") or not isinstance(value, str):
            continue
        parts.append(value)
    return "".join(parts)


def _safe_chat_raw(resp: Any, *, finish_reason: Any = None) -> dict[str, Any]:
    """Allow-list non-content response metadata for audit/debugging.

    Deliberately do not call ``model_dump()``: OpenAI-compatible runtimes may
    place private reasoning in extension fields such as ``reasoning_content``,
    ``reasoning`` or ``thinking``. An allow-list makes new extension fields
    private by default.
    """
    raw: dict[str, Any] = {}
    for key in ("id", "object", "created", "model", "system_fingerprint", "service_tier"):
        value = getattr(resp, key, None)
        if value is not None and isinstance(value, (str, int, float, bool)):
            raw[key] = value
    if isinstance(finish_reason, str):
        raw["finish_reason"] = finish_reason
    return raw


class D1ResidencyError(RuntimeError):
    """The model that served a response is outside the D1 perimeter.

    A dedicated type because stream()'s SSE fallback catches Exception and
    re-sends the request as a buffered completion -- correct for "endpoint has
    no SSE", catastrophic for a residency violation, where the re-send would
    hand the analyst question to the very endpoint the guard fences off. The
    fallback re-raises this type instead.
    """


@lru_cache(maxsize=8)
def _log_served_model_once(endpoint: str, served: str) -> None:
    """Announce which model actually answered, once per (endpoint, served) pair.

    The configured endpoint name can be a Unity Catalog alias, so without this
    line nothing in the logs says what is really serving: the audit row records
    the alias (grounded_qa stamps current_model_name), and query_log.model_name
    only carries the served id on turns that produce an answer. Cached rather
    than logged per call because the API process is long-lived and this would
    otherwise be one line per Ask. Keyed on the PAIR so a mid-process alias
    repoint -- the event this exists to surface -- logs again instead of being
    swallowed by a "already logged" flag.
    """
    log.info("llm_served_model", endpoint=endpoint, served=served)


class DatabricksProvider:
    """Gemma over a Databricks OpenAI-compatible Chat Completions endpoint.

    The client and all connection inputs are injectable for deterministic
    tests. Thinking is opt-in *and* synthesizer-only. Generated thought content
    is never returned in ``text`` or ``raw``.
    """

    name = "databricks"

    def __init__(
        self,
        model: str,
        base_url: str,
        token: str,
        *,
        role: str = "default",
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        d1_enforced: bool = False,
        d1_allowed_models: tuple[str, ...] = (),
        client: Any = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.token = token
        self.role = role
        self.thinking_enabled = bool(thinking_enabled and role == "synthesizer")
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_retries = max_retries
        # Injected, not read from get_settings() at request time: this provider
        # is 100% constructor-injected so the offline tests never touch a
        # developer's .env or the get_settings lru_cache. Defaults are INERT so
        # an unarmed deployment (today's prod) behaves exactly as before.
        self.d1_enforced = d1_enforced
        self.d1_allowed_models = d1_allowed_models
        self._client = client

    def _client_or_create(self) -> Any:
        if self._client is None:
            from regwatch.common.llm_clients import shared_databricks_openai_client

            s = get_settings()
            timeout = self.timeout if self.timeout is not None else s.llm_timeout_s
            max_retries = self.max_retries if self.max_retries is not None else s.llm_max_retries
            self._client = shared_databricks_openai_client(
                self.base_url,
                self.token,
                timeout=timeout,
                max_retries=max_retries,
            )
        return self._client

    def _request_messages(
        self,
        messages: list[LLMMessage],
        *,
        allow_thinking: bool,
    ) -> list[dict[str, str]]:
        # Gemma expects one consolidated system turn. Always remove a caller's
        # accidental control token first; this is what makes router/extractor
        # thinking definitively off rather than prompt-dependent.
        system = "\n\n".join(
            message.content.replace("<|think|>", "")
            for message in messages
            if message.role == "system"
        ).strip()
        if allow_thinking:
            system = f"<|think|>\n{system}" if system else "<|think|>"

        request: list[dict[str, str]] = []
        if system:
            request.append({"role": "system", "content": system})
        for message in messages:
            if message.role == "system":
                continue
            content = message.content
            # Do not feed a prior assistant thought channel back into a later
            # turn. User text is left byte-for-byte intact.
            if message.role == "assistant":
                content = _visible_gemma_text(content)
            request.append({"role": message.role, "content": content})
        return request

    def _request_kwargs(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
        response_format: str | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        # JSON is used by extraction/routing and must never spend or expose a
        # thought channel, even if a synthesizer provider is reused manually.
        allow_thinking = self.thinking_enabled and response_format != "json"
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._request_messages(messages, allow_thinking=allow_thinking),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.reasoning_effort is not None:
            # Deliberately NOT gated the way `allow_thinking` is. A reasoning
            # model draws thought and answer from one max_tokens budget on every
            # role, so the JSON callers (extractor at 1500, change detector at
            # 400) are exactly where an unbounded thinking spend turns into
            # finish_reason="length" -- and JSON mode gives no answer-quality
            # reason to think longer. Absent key, never a null: an endpoint that
            # does not know the parameter must see no parameter.
            kwargs["reasoning_effort"] = self.reasoning_effort
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    def _check_served_model(self, served: str | None) -> None:
        """Fail closed when the model that ANSWERED is outside the D1 perimeter.

        DATABRICKS_LLM_MODEL can be a Unity Catalog alias, repointable with no
        deploy, so the Settings boot check inspects a name that says nothing
        about what is serving. The response is the only place the truth appears.
        Detective, not preventive: the question has already been disclosed by
        the time this runs. What it buys is that the ANSWER is never served and
        that every subsequent turn fails loudly instead of quietly migrating the
        corpus off-perimeter.

        Takes the RAW reported value, before any `or self.model` substitution:
        the alias is necessarily allowlisted (the boot check requires it), so
        checking the collapsed value would let an endpoint that reports no model
        launder itself through the guard.
        """
        name = (served or "").strip()
        _log_served_model_once(self.model, name or "<unreported>")
        if not self.d1_enforced:
            return
        if not name:
            raise D1ResidencyError(
                f"D1_ENFORCED: endpoint {self.model!r} reported no served model; "
                "residency cannot be verified"
            )
        why = d1_model_rejection(name, self.d1_allowed_models)
        if why is not None:
            # The served name belongs in the message: it is the only per-turn
            # record of who answered (the audit row stamps the alias), and it
            # reaches qa_provider_error and Sentry from grounded_qa's boundary.
            # Model names only -- that log line must never carry prompt content.
            raise D1ResidencyError(
                f"D1_ENFORCED: served model {name!r} behind endpoint {self.model!r} {why}"
            )

    @staticmethod
    def _first_choice(resp: Any) -> Any:
        choices = getattr(resp, "choices", None)
        if not choices:
            raise RuntimeError("databricks chat completion returned no choices")
        return choices[0]

    @staticmethod
    def _raise_for_finish_reason(finish_reason: Any) -> None:
        if finish_reason in ("length", "content_filter"):
            raise RuntimeError(
                f"databricks chat completion terminated with finish_reason={finish_reason}"
            )

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        client = self._client_or_create()
        kwargs = self._request_kwargs(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        resp = client.chat.completions.create(**kwargs)
        # Residency first, before the shape/truncation checks: a truncated
        # answer from an off-perimeter model is still an off-perimeter
        # disclosure, and if this ran second, a deployment where EVERY response
        # truncates would never emit the served-model line ops needs to see.
        served = getattr(resp, "model", None)
        self._check_served_model(served)
        choice = self._first_choice(resp)
        finish_reason = getattr(choice, "finish_reason", None)
        self._raise_for_finish_reason(finish_reason)
        message = getattr(choice, "message", None)
        candidate = _chat_content_text(getattr(message, "content", None))
        if response_format == "json":
            # _visible_gemma_text is a PROSE thought-channel scrubber and it
            # destroys JSON: it returns only what follows the LAST "</think>",
            # drops everything from a "<|channel>thought" opener, and deletes
            # "<|think|>" inline. A JSON payload quoting any of those tokens
            # (a user can ask "how do I read the </think> markers in the SPL?")
            # would come back unparseable -- a per-question hard error rather
            # than the lost prefix it used to be. Slice out the JSON blob
            # instead; the thought channel is unreachable in json mode anyway
            # (_request_kwargs forces allow_thinking False).
            text = _extract_json_blob(candidate)
        else:
            text = _visible_gemma_text(candidate)
        model = served or self.model
        return LLMResponse(
            text=text,
            model=model,
            raw=_safe_chat_raw(resp, finish_reason=finish_reason),
            usage=_usage_from(resp, "prompt_tokens", "completion_tokens"),
        )

    def _complete_stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """Consume a stream into a private buffer, then expose only final-answer text.

        Buffering is intentional: Gemma's opening/closing thought delimiters can
        be split across arbitrary SSE chunks. Emitting candidate content before
        seeing the closing delimiter could leak reasoning that cannot be
        retracted.

        The D1 residency check runs HERE, on the raw reported value BEFORE the
        `or self.model` substitution (the alias is allowlisted by construction,
        so the collapsed value would launder a no-report stream) and BEFORE the
        shape/truncation raises, mirroring complete(): a truncated answer from
        an off-perimeter model is still an off-perimeter disclosure, and the
        exact deployment this guard targets -- an alias repointed to a reasoning
        model -- truncates on EVERY turn, so a check placed after the
        finish_reason raise would never fire for it.
        """
        client = self._client_or_create()
        events = client.chat.completions.create(
            **self._request_kwargs(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
        )
        parts: list[str] = []
        usage = LLMUsage()
        finish_reason: Any = None
        last_event: Any = None
        served: str | None = None
        saw_choice = False
        for event in events:
            last_event = event
            served = getattr(event, "model", None) or served
            event_usage = _usage_from(event, "prompt_tokens", "completion_tokens")
            if event_usage.input_tokens is not None:
                usage.input_tokens = event_usage.input_tokens
            if event_usage.output_tokens is not None:
                usage.output_tokens = event_usage.output_tokens
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            saw_choice = True
            choice = choices[0]
            candidate_finish = getattr(choice, "finish_reason", None)
            if candidate_finish is not None:
                finish_reason = candidate_finish
            delta = getattr(choice, "delta", None)
            # Deliberately ignore delta.reasoning_content / reasoning / thinking.
            parts.append(_chat_content_text(getattr(delta, "content", None)))

        self._check_served_model(served)
        if not saw_choice:
            raise RuntimeError("databricks chat stream returned no choices")
        self._raise_for_finish_reason(finish_reason)
        return LLMResponse(
            text=_visible_gemma_text("".join(parts)),
            model=served or self.model,
            raw=_safe_chat_raw(last_event, finish_reason=finish_reason),
            usage=usage,
        )

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        try:
            resp = self._complete_stream(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except D1ResidencyError:
            # Never falls back: the fallback below would re-send the analyst
            # question to the very endpoint the guard exists to fence off.
            # Nothing has been yielded yet, so nothing is painted then
            # retracted -- the raise reaches grounded_qa's audited boundary.
            raise
        except Exception:
            # Some custom Databricks endpoints do not implement SSE or
            # stream_options. Since no candidate text has been yielded yet, a
            # normal completion is a safe, duplicate-free user-visible fallback.
            # complete() runs the same residency check, so falling back never
            # skips the guard.
            yield from _buffered_stream(
                self,
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return
        if resp.text:
            yield LLMStreamChunk(delta=resp.text)
        yield LLMStreamChunk(done=True, response=resp)


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
        yield from _buffered_stream(self, messages, temperature=temperature, max_tokens=max_tokens)


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
    if name == "databricks":
        base_url = getattr(s, "databricks_llm_base_url", None)
        token = getattr(s, "databricks_llm_token", None)
        databricks_model = getattr(s, "databricks_llm_model", None)
        missing = [
            env_name
            for env_name, value in (
                ("DATABRICKS_LLM_BASE_URL", base_url),
                ("DATABRICKS_LLM_TOKEN", token),
                ("DATABRICKS_LLM_MODEL", databricks_model),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} not set; configure Databricks LLM serving")
        # Narrowing for mypy only; the `missing` check above is the real gate.
        assert isinstance(base_url, str)  # noqa: S101
        assert isinstance(token, str)  # noqa: S101
        assert isinstance(databricks_model, str)  # noqa: S101
        return DatabricksProvider(
            model=databricks_model,
            base_url=base_url,
            token=token,
            role=role,
            thinking_enabled=bool(getattr(s, "gemma_thinking_enabled", False)),
            # getattr with defaults throughout: tests construct settings as a
            # SimpleNamespace with a fixed field list, and a bare attribute read
            # would turn a new knob into an AttributeError at provider build.
            reasoning_effort=getattr(s, "databricks_reasoning_effort", None),
            timeout=s.llm_timeout_s,
            max_retries=s.llm_max_retries,
            d1_enforced=bool(getattr(s, "d1_enforced", False)),
            d1_allowed_models=tuple(getattr(s, "d1_allowed_llm_models", ()) or ()),
        )
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
    provider = s.llm_provider.lower()
    if provider == "echo":
        return "echo"
    if provider == "databricks":
        return getattr(s, "databricks_llm_model", None) or _model_for_role(s, role)
    return _model_for_role(s, role)
