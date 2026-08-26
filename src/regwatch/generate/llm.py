"""LLMProvider interface + concrete providers (OpenAI and test echo).

Business logic NEVER hard-codes a model name. It calls `get_llm_provider()`
and uses the protocol below.

`openai` uses gpt-5.6-luna over the Responses API. RegWatch owns conversation
state, so every request is stateless (`store=False`) and sends the applicable
transcript explicitly. `echo` is the test provider.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

from config.settings import get_settings

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
    # True = retroactive invalidation: a late close-delimiter revealed that
    # earlier deltas may have been private reasoning. Consumers discard every
    # delta received so far and start over; the terminal response is
    # unaffected (it is always built from the full buffered scrub).
    reset: bool = False


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

    Used when a provider cannot establish a stream so it still honors the
    stream() contract: deltas, then the terminal chunk carrying the fully
    validated response.
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
        is_guidance = any(
            m.role == "system" and "[REGWATCH_QUERY_GUIDANCE_V1]" in m.content for m in messages
        )
        is_route = any(
            m.role == "system"
            and ("[REGWATCH_ROUTE_V2]" in m.content or "[REGWATCH_ROUTE_V1]" in m.content)
            for m in messages
        )
        # F19: the v6 prose synthesizer is keyed on its system sentinel, like
        # the guidance branch -- NOT on marker-shape + response_format, which
        # would misfire on the deficiency chat_completion seam. v7 gets its
        # OWN sentinel (checked first, below) rather than piggybacking on
        # is_prose_qa: the two literals never overlap, so which check runs
        # first never matters for correctness, but v7 is checked first to
        # match B.10.2's stated order.
        is_v7_qa = any(
            m.role == "system" and "[REGWATCH_GROUNDED_QA_V7]" in m.content for m in messages
        )
        is_prose_qa = any(
            m.role == "system" and "[REGWATCH_GROUNDED_QA_V6]" in m.content for m in messages
        )
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
            if is_route:
                # Deterministic local/contract behavior for the advisory route
                # schema. Lazy imports avoid the route -> llm module cycle.
                try:
                    from regwatch.retrieve.scope import detect_explicit_corpus_policy

                    context = json.loads(last_user)
                    question = str(context["untrusted_question"])
                    trusted_product = str(context.get("trusted_product_context") or "").strip()
                    recent_turns = list(context.get("recent_turns") or [])
                    allowed_policies = set(context.get("allowed_corpus_policies") or [])
                    lowered = question.lower()
                    prior = recent_turns[-1] if recent_turns else None
                    standalone = question
                    product_hint: str | None = None
                    corpus_policy_hint: str | None = None
                    social_opening = bool(
                        re.search(r"\b(?:hello|hi|hey)\b", lowered)
                        or "can you help" in lowered
                        or "help me" in lowered
                    )
                    asks_for_evidence = bool(
                        re.search(
                            r"\b(?:what|which|when|where|how|define|requires?|recommends?)\b",
                            lowered,
                        )
                    )
                    if social_opening and not asks_for_evidence:
                        mode = "converse"
                        scope_hint = "unknown"
                    elif detect_explicit_corpus_policy(question) is not None:
                        mode = "lookup"
                        scope_hint = "corpus"
                        corpus_policy_hint = "inhalation_psg"
                        if corpus_policy_hint not in allowed_policies:
                            return LLMResponse(text="{}", model="echo", usage=usage)
                    elif (
                        isinstance(prior, dict)
                        and prior.get("scope_audited") is True
                        and prior.get("scope_kind") in {"product", "corpus"}
                    ):
                        mode = "lookup"
                        scope_hint = "inherit"
                        if trusted_product and prior.get("scope_kind") == "product":
                            standalone = f"{trusted_product}: {question}"
                    elif trusted_product or "beclomethasone" in lowered:
                        mode = "lookup"
                        scope_hint = "product"
                        product_hint = trusted_product or "beclomethasone dipropionate"
                    else:
                        mode = "lookup_clarify"
                        scope_hint = "unknown"
                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                    return LLMResponse(text="{}", model="echo", usage=usage)
                return LLMResponse(
                    text=json.dumps(
                        {
                            "standalone_question": standalone,
                            "mode": mode,
                            "scope_hint": scope_hint,
                            "product_hint": product_hint,
                            "corpus_policy_hint": corpus_policy_hint,
                        }
                    ),
                    model="echo",
                    usage=usage,
                )
            if is_guidance:
                # Test/local parity for the constrained guidance planner. The
                # user message is the exact JSON context built by guidance.py;
                # echo selects only values the application supplied, just like a
                # compliant live router response.
                try:
                    context = json.loads(last_user)
                    steps = list(context.get("allowed_next_steps") or [])
                    options = list(context.get("available_options") or [])
                    next_step = str(steps[0])
                    option_ids = [str(row["id"]) for row in options[:3]]
                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                    return LLMResponse(text="{}", model="echo", usage=usage)
                return LLMResponse(
                    text=json.dumps({"next_step": next_step, "option_ids": option_ids}),
                    model="echo",
                    usage=usage,
                )
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
        if is_v7_qa:
            # v7 selective citation. Own sentinel, own shapes -- FORCE_REFUSAL
            # is a conversational decline (two clean, uncited sentences), NOT
            # the shared NO_EVIDENCE token (B.10.1.8: v7 has no sentinel).
            if self._flag("REGWATCH_ECHO_FORCE_MALFORMED"):
                return LLMResponse(
                    text="an unterminated prose fragment with no terminal punctuation at all",
                    model="echo",
                    usage=usage,
                )
            if self._force_refusal():
                return LLMResponse(
                    text=(
                        "ECHO has nothing on that question in these passages. "
                        "Want me to try a different phrasing?"
                    ),
                    model="echo",
                    usage=usage,
                )
            if marker is not None:
                return LLMResponse(
                    text=(
                        "ECHO grounded test answer [1]. Let me know if you want the "
                        "dissolution details as well."
                    ),
                    model="echo",
                    usage=usage,
                )
            return LLMResponse(
                text="ECHO prose synthesis without passages.", model="echo", usage=usage
            )
        if is_prose_qa:
            # v6 prose synthesis. The lazy import avoids a module cycle
            # (prose_turn -> turn_gate -> turn_schema -> this module); by call
            # time everything is loaded. The cited shape reuses the v5 echo
            # claim text so the RENDERED answer is byte-identical across the
            # format flip, and [1] resolves to the prompt's first passage --
            # the same passage the json branch scrapes.
            if self._flag("REGWATCH_ECHO_FORCE_MALFORMED"):
                # Unterminated on purpose: the parser drops the tail, parses
                # zero sentences, and the caller serves malformed_structure --
                # the prose analogue of "not json at all {".
                return LLMResponse(
                    text="an unterminated prose fragment with no terminal punctuation at all",
                    model="echo",
                    usage=usage,
                )
            if self._force_refusal():
                from regwatch.generate.prose_turn import PROSE_NO_EVIDENCE_SENTINEL

                return LLMResponse(text=PROSE_NO_EVIDENCE_SENTINEL, model="echo", usage=usage)
            if marker is not None:
                return LLMResponse(text="ECHO grounded test answer [1].", model="echo", usage=usage)
            return LLMResponse(
                text="ECHO prose synthesis without passages.", model="echo", usage=usage
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


_JSON_USER_DIRECTIVE = "Respond with a single JSON object."


def _ensure_user_json_token(request: list[dict[str, str]]) -> list[dict[str, str]]:
    """Ensure JSON mode is named in a user Item, as required by the API."""
    if any(
        "json" in message.get("content", "").lower()
        for message in request
        if message["role"] == "user"
    ):
        return request
    for message in reversed(request):
        if message["role"] == "user":
            message["content"] = f"{message['content']}\n\n{_JSON_USER_DIRECTIVE}"
            return request
    request.append({"role": "user", "content": _JSON_USER_DIRECTIVE})
    return request


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


# ---------- OpenAI provider ----------
class OpenAIProvider:
    """GPT-5 generation over the OpenAI Responses API.

    System messages become top-level ``instructions``. The remaining transcript
    is sent as ``input`` Items, keeping application state inside RegWatch. The
    provider never sends ``previous_response_id`` or a Conversations API ID and
    always sets ``store=False``.

    The client and every connection input are injectable so offline tests
    never touch a developer's environment or the ``get_settings`` cache.
    """

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        role: str = "default",
        reasoning_effort: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        client: Any = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.role = role
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client

    def _client_or_create(self) -> Any:
        """Return the shared SDK client, refusing loudly without a key.

        get_llm_provider already rejects an unset OPENAI_API_KEY, so this is
        the second gate for directly-constructed providers: without it the SDK
        would raise its own ``OpenAIError`` at import-adjacent construction
        time with a message that says nothing about which env var to set.
        """
        if self._client is None:
            if not (self.api_key or "").strip():
                raise RuntimeError(
                    "OPENAI_API_KEY is not set; configure the OpenAI Responses transport"
                )
            s = get_settings()
            timeout = self.timeout if self.timeout is not None else s.llm_timeout_s
            max_retries = self.max_retries if self.max_retries is not None else s.llm_max_retries
            from regwatch.common.llm_clients import shared_openai_api_client

            self._client = shared_openai_api_client(
                self.base_url,
                self.api_key,
                timeout=timeout,
                max_retries=max_retries,
            )
        return self._client

    @staticmethod
    def _request_input(
        messages: list[LLMMessage],
    ) -> tuple[str, list[dict[str, str]]]:
        """Return top-level instructions and the RegWatch-owned transcript."""
        instructions = "\n\n".join(
            message.content for message in messages if message.role == "system"
        ).strip()
        request: list[dict[str, str]] = []
        for message in messages:
            if message.role == "system":
                continue
            request.append({"role": message.role, "content": message.content})
        return instructions, request

    def _request_kwargs(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        response_format: str | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build a stateless Responses API payload."""
        instructions, request = self._request_input(messages)
        if response_format == "json":
            request = _ensure_user_json_token(request)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": request,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if self.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        if response_format == "json":
            kwargs["text"] = {"format": {"type": "json_object"}}
        if stream:
            kwargs["stream"] = True
        return kwargs

    def _log_served(self, served: str | None) -> None:
        """Record which model actually answered. Ops visibility, not a gate.

        OPENAI_LLM_MODEL can name an alias that resolves to a dated snapshot,
        so the response is the only place the answering model appears. There
        is deliberately no residency check on this path (see the class
        docstring); model names only -- this line must never carry prompt
        content.
        """
        _log_served_model_once(self.model, (served or "").strip() or "<unreported>")

    @staticmethod
    def _require_completed(resp: Any) -> None:
        status = getattr(resp, "status", None)
        if status != "completed":
            raise RuntimeError(f"openai response terminated with status={status}")

    @staticmethod
    def _safe_raw(resp: Any) -> dict[str, Any]:
        """Allow-list non-content Responses metadata for audit logging."""
        raw: dict[str, Any] = {}
        for key in ("id", "object", "created_at", "model", "status", "service_tier"):
            value = getattr(resp, key, None)
            if value is not None and isinstance(value, (str, int, float, bool)):
                raw[key] = value
        return raw

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        """Create a completed, stateless OpenAI response."""
        del temperature
        client = self._client_or_create()
        kwargs = self._request_kwargs(
            messages,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        resp = client.responses.create(**kwargs)
        served = getattr(resp, "model", None)
        self._log_served(served)
        self._require_completed(resp)
        candidate = getattr(resp, "output_text", None)
        if not isinstance(candidate, str):
            raise RuntimeError("openai response returned no output_text")
        text = _extract_json_blob(candidate) if response_format == "json" else candidate.strip()
        return LLMResponse(
            text=text,
            model=served or self.model,
            raw=self._safe_raw(resp),
            usage=_usage_from(resp, "input_tokens", "output_tokens"),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        """Consume typed Responses events and emit visible text deltas."""
        del temperature
        client = self._client_or_create()
        try:
            events = client.responses.create(
                **self._request_kwargs(messages, max_tokens=max_tokens, stream=True)
            )
        except Exception:
            yield from _buffered_stream(self, messages, temperature=0.0, max_tokens=max_tokens)
            return
        parts: list[str] = []
        yielded = False
        terminal: Any = None
        iterator = iter(events)
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                break
            except Exception:
                if yielded:
                    raise
                yield from _buffered_stream(self, messages, temperature=0.0, max_tokens=max_tokens)
                return
            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    parts.append(delta)
                    yielded = True
                    yield LLMStreamChunk(delta=delta)
                continue
            if event_type == "response.completed":
                terminal = getattr(event, "response", None)
                break
            if event_type in ("response.incomplete", "response.failed", "error"):
                raise RuntimeError(f"openai stream terminated with event={event_type}")
        if terminal is None:
            raise RuntimeError("openai stream ended without response.completed")
        self._require_completed(terminal)
        served = getattr(terminal, "model", None)
        self._log_served(served)
        output_text = getattr(terminal, "output_text", None)
        text = output_text if isinstance(output_text, str) else "".join(parts)
        yield LLMStreamChunk(
            done=True,
            response=LLMResponse(
                text=text.strip(),
                model=served or self.model,
                raw=self._safe_raw(terminal),
                usage=_usage_from(terminal, "input_tokens", "output_tokens"),
            ),
        )


def get_llm_provider(name: str | None = None, *, role: str = "default") -> LLMProvider:
    """Build the configured LLM provider, refusing when none is configured.

    LLM_PROVIDER deliberately has no default: a process whose generation
    provider is implicit is the same configuration hazard as an implicit
    embedding provider (see the 2026-08-14 backfill postmortem), so an unset
    value refuses loudly here instead of guessing.
    """
    s = get_settings()
    name = (name or s.llm_provider or "").strip().lower()
    if not name:
        raise RuntimeError(
            "LLM_PROVIDER is not set and has no default. Set "
            "LLM_PROVIDER=openai, or LLM_PROVIDER=echo (tests only)."
        )
    if name == "echo":
        return EchoLLMProvider()
    if name == "openai":
        openai_key = getattr(s, "openai_api_key", None)
        openai_model = getattr(s, "openai_llm_model", None)
        # base_url has a real default in Settings; getattr keeps a
        # SimpleNamespace test settings object from becoming an AttributeError
        # at provider build.
        openai_base_url = getattr(s, "openai_base_url", None) or "https://api.openai.com/v1"
        missing = [
            env_name
            for env_name, value in (
                ("OPENAI_API_KEY", openai_key),
                ("OPENAI_LLM_MODEL", openai_model),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} not set; configure the OpenAI LLM provider")
        # Narrowing for mypy only; the `missing` check above is the real gate.
        assert isinstance(openai_key, str)  # noqa: S101
        assert isinstance(openai_model, str)  # noqa: S101
        openai_timeout = getattr(s, "openai_timeout_s", None)
        openai_retries = getattr(s, "openai_max_retries", None)
        return OpenAIProvider(
            model=openai_model,
            api_key=openai_key,
            base_url=openai_base_url,
            role=role,
            reasoning_effort=getattr(s, "openai_reasoning_effort", None),
            # Explicit None checks, not `or`: 0.0 / 0 are meaningful values a
            # truthiness fallback would silently replace with the generic
            # llm_* budget.
            timeout=openai_timeout if openai_timeout is not None else s.llm_timeout_s,
            max_retries=openai_retries if openai_retries is not None else s.llm_max_retries,
        )
    if name == "databricks":
        raise ValueError("unknown LLM provider: databricks")
    raise ValueError(f"unknown LLM provider: {name}")


def assert_llm_runtime_available(name: str | None = None) -> None:
    """Boot-time fail-fast: the configured LLM provider must be fully usable.

    Mirrors process/embedder.assert_embedding_runtime_available. The API's
    answer path generates unconditionally, so its lifespan calls this to
    surface an unset LLM_PROVIDER or missing OpenAI configuration at boot.
    Provider construction is credential validation only -- the HTTP client is
    created lazily on first request -- so a configured-but-unreachable
    endpoint still boots and degrades per turn, which the contract suite's
    dead_provider flavor pins.
    """
    get_llm_provider(name)


def current_model_name(role: str = "default") -> str:
    """Best-effort model label for audit rows; never raises.

    Audit stamping must not be the thing that fails a turn, so a
    misconfigured provider yields the honest label "unconfigured" here and
    the turn itself fails at get_llm_provider with the real remediation.
    """
    del role
    s = get_settings()
    provider = (s.llm_provider or "").strip().lower()
    if provider == "echo":
        return "echo"
    if provider == "openai":
        return getattr(s, "openai_llm_model", None) or "unconfigured"
    return "unconfigured"
