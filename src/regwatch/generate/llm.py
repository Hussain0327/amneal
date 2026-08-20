"""LLMProvider interface + concrete providers (openai, databricks, echo).

Business logic NEVER hard-codes a model name. It calls `get_llm_provider()`
and uses the protocol below.

`openai` is the 2026-08-20 migration target: gpt-5.6-terra over the OpenAI
Chat Completions API. `databricks` is the incumbent it replaces (gpt-oss-120b
on a serving endpoint, reached over the same OpenAI-compatible SDK); it stays
so the flip is a single LLM_PROVIDER change in either direction. `echo` is the
test provider. The Anthropic path was removed 2026-08-17 and has not returned.
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

    Used by the Databricks provider's no-SSE fallback paths so they still
    honor the stream() contract: deltas, then the terminal chunk carrying the
    fully-validated response.
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


# ---------- Databricks provider ----------
_THOUGHT_CHANNEL_START = re.compile(r"<\|channel>thought(?:\r?\n)?", re.IGNORECASE)
_THINK_TAG_START = re.compile(r"<think>", re.IGNORECASE)


def _drop_private_block(text: str, start: re.Pattern[str], end: str) -> str:
    """Drop every complete or unterminated private block.

    Unterminated thought output is discarded from its opening delimiter to the
    end. Returning a shorter/empty answer is safer than exposing chain of
    thought when a serving engine truncates before the model's closing token.
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


def _visible_answer_text(text: str) -> str:
    """Return only the model's final-answer channel.

    Reasoning models can emit their thought channel inline even when thinking
    is disabled, and some OpenAI-compatible servers expose reasoning in
    ``content`` instead of a separate field. Handle both the native
    thought-channel delimiters and the common ``<think>`` compatibility form.
    A stray closing delimiter is treated conservatively: everything before it
    may have been private reasoning.
    """
    cleaned = _drop_private_block(text, _THOUGHT_CHANNEL_START, "<channel|>")
    if "<channel|>" in cleaned:
        cleaned = cleaned.rsplit("<channel|>", 1)[-1]
    cleaned = _drop_private_block(cleaned, _THINK_TAG_START, "</think>")
    lower = cleaned.lower()
    if "</think>" in lower:
        cleaned = cleaned[lower.rfind("</think>") + len("</think>") :]
    return cleaned.replace("<|think|>", "").strip()


class _StreamScrubber:
    """Incremental twin of ``_visible_answer_text`` for live draft streaming.

    ``push()`` returns ``(visible_delta, retroactive_reset)``. It never emits
    text inside a private reasoning block, never emits a tail that could
    still grow into a delimiter split across wire chunks, and signals
    ``reset=True`` on a stray close-delimiter (everything already emitted may
    have been reasoning; the consumer discards it -- mirroring the buffered
    scrubber's keep-only-after-the-last-close rule). ``flush()`` drops an
    unterminated private block, mirroring the buffered scrubber's
    conservative choice, and releases any held benign tail.
    """

    # Literal delimiter probes a held tail could still be a prefix of. The
    # thought-channel opener regex allows an optional trailing newline, so the
    # literal opener itself is the longest prefix worth holding for.
    _PROBES = ("<|channel>thought", "<think>", "</think>", "<channel|>", "<|think|>")

    def __init__(self) -> None:
        self._buf = ""
        self._closer: str | None = None  # inside a private block when set

    def _hold_len(self) -> int:
        """Chars at the buffer tail that could still become a delimiter."""
        limit = min(max(len(p) for p in self._PROBES), len(self._buf))
        for size in range(limit, 0, -1):
            tail = self._buf[-size:]
            if any(p.startswith(tail) for p in self._PROBES):
                return size
        return 0

    def push(self, chunk: str) -> tuple[str, bool]:
        self._buf += chunk
        visible: list[str] = []
        reset = False
        while True:
            if self._closer is not None:
                close = self._buf.find(self._closer)
                if close < 0:
                    # Keep only enough tail to complete the closer.
                    keep = len(self._closer) - 1
                    self._buf = self._buf[-keep:] if keep else ""
                    return ("".join(visible), reset)
                self._buf = self._buf[close + len(self._closer) :]
                self._closer = None
                continue
            opener_match: re.Match[str] | None = None
            opener_closer = ""
            for start, closer in (
                (_THOUGHT_CHANNEL_START, "<channel|>"),
                (_THINK_TAG_START, "</think>"),
            ):
                m = start.search(self._buf)
                if m and (opener_match is None or m.start() < opener_match.start()):
                    opener_match, opener_closer = m, closer
            # Stray closers: everything before one is suspect (buffered rule).
            stray_at = self._buf.find("<channel|>")
            stray_len = len("<channel|>")
            think_at = self._buf.lower().find("</think>")
            if think_at != -1 and (stray_at == -1 or think_at < stray_at):
                stray_at, stray_len = think_at, len("</think>")
            if opener_match is not None and (stray_at == -1 or opener_match.start() < stray_at):
                visible.append(self._buf[: opener_match.start()])
                self._buf = self._buf[opener_match.end() :]
                self._closer = opener_closer
                continue
            if stray_at != -1:
                visible.clear()
                reset = True
                self._buf = self._buf[stray_at + stray_len :]
                continue
            break
        hold = self._hold_len()
        out = self._buf[: len(self._buf) - hold] if hold else self._buf
        self._buf = self._buf[len(self._buf) - hold :] if hold else ""
        visible.append(out.replace("<|think|>", ""))
        return ("".join(visible), reset)

    def flush(self) -> str:
        if self._closer is not None:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out.replace("<|think|>", "")


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
    for key in (
        "id",
        "object",
        "created",
        "model",
        "system_fingerprint",
        "service_tier",
    ):
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
    """Chat model over a Databricks OpenAI-compatible Chat Completions endpoint.

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
        # The endpoint gets one consolidated system turn. Always remove a
        # caller's accidental control token first; this is what makes
        # router/extractor thinking definitively off rather than
        # prompt-dependent.
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
                content = _visible_answer_text(content)
            request.append({"role": message.role, "content": content})
        return request

    # Appended to a USER turn when JSON mode is requested and no user turn
    # already says "json". Short and true: it repeats what the schema message
    # already instructs, so it cannot pull a model off-task.
    _JSON_USER_DIRECTIVE = "Respond with a single JSON object."

    @staticmethod
    def _ensure_user_json_token(request: list[dict[str, str]]) -> list[dict[str, str]]:
        """Satisfy the endpoint's json_object precondition at the wire seam.

        Databricks rejects `response_format={"type":"json_object"}` with
        400 BAD_REQUEST unless the word "json" appears in the messages, and
        live probing (issue #162) showed a SYSTEM message does not count at any
        casing -- it must be a user turn. `_request_messages` folds every system
        message into one system turn, so the schema instruction that every
        structured caller sends (GUIDANCE_SCHEMA_MESSAGE, TURN_SCHEMA_MESSAGE,
        the deficiency schema message) never reaches a user turn on its own.

        Fixing it here rather than in any one prompt is deliberate: this is the
        single choke point every structured caller passes through (router
        guidance, synthesis, BE extraction, change summary, all deficiency
        structured calls), and it leaves the prompt texts -- and therefore the
        audited prompt-identity hashes -- byte-identical.

        Appends to the LAST user turn rather than adding one, so turn structure
        is unchanged; only a caller that sends no user turn at all gets a new
        one.
        """
        if any("json" in m.get("content", "").lower() for m in request if m["role"] == "user"):
            return request
        for message in reversed(request):
            if message["role"] == "user":
                message["content"] = (
                    f"{message['content']}\n\n{DatabricksProvider._JSON_USER_DIRECTIVE}"
                )
                return request
        request.append({"role": "user", "content": DatabricksProvider._JSON_USER_DIRECTIVE})
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
        request = self._request_messages(messages, allow_thinking=allow_thinking)
        if response_format == "json":
            request = self._ensure_user_json_token(request)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request,
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
            # _visible_answer_text is a PROSE thought-channel scrubber and it
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
            text = _visible_answer_text(candidate)
        model = served or self.model
        return LLMResponse(
            text=text,
            model=model,
            raw=_safe_chat_raw(resp, finish_reason=finish_reason),
            usage=_usage_from(resp, "prompt_tokens", "completion_tokens"),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        """True incremental streaming with an in-adapter reasoning scrubber.

        Deltas are scrubbed by _StreamScrubber, so control/reasoning markup is
        parsed out at this boundary and never reaches a consumer. The D1 check
        binds on the FIRST event that reports ``model`` (owner decision
        2026-08-10: deltas are NOT held waiting for late metadata; a stream
        that never reports raises at the end exactly like complete()). After
        the first yielded delta the buffered fallback is DISABLED -- a re-send
        would paint the whole answer twice.

        The terminal chunk's response is built from the full buffered scrub of
        every raw part, so it stays byte-identical to the pre-streaming
        implementation on every input.
        """
        client = self._client_or_create()
        try:
            events = client.chat.completions.create(
                **self._request_kwargs(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
            )
        except Exception:
            # Endpoint without SSE/stream_options support; nothing yielded yet.
            yield from _buffered_stream(
                self, messages, temperature=temperature, max_tokens=max_tokens
            )
            return
        scrub = _StreamScrubber()
        parts: list[str] = []
        usage = LLMUsage()
        finish_reason: Any = None
        last_event: Any = None
        served: str | None = None
        d1_checked = False
        yielded = False
        saw_choice = False
        iterator = iter(events)
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                break
            except D1ResidencyError:
                raise
            except Exception:
                if yielded:
                    # No re-send after first yield: a fallback would repaint
                    # the full answer after a partial one.
                    raise
                yield from _buffered_stream(
                    self, messages, temperature=temperature, max_tokens=max_tokens
                )
                return
            last_event = event
            reported = getattr(event, "model", None)
            if reported:
                served = reported
                if not d1_checked:
                    # Raises D1ResidencyError pre-yield when the wire reports
                    # early (the G1-recorded common case); a late report binds
                    # here mid-stream instead of holding deltas.
                    self._check_served_model(reported)
                    d1_checked = True
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
            raw = _chat_content_text(getattr(delta, "content", None))
            parts.append(raw)
            visible, reset = scrub.push(raw)
            if reset:
                yielded = True
                yield LLMStreamChunk(reset=True)
            if visible:
                yielded = True
                yield LLMStreamChunk(delta=visible)
        tail = scrub.flush()
        if tail:
            yielded = True
            yield LLMStreamChunk(delta=tail)
        if not d1_checked:
            self._check_served_model(served)  # raises under enforcement; logs otherwise
        if not saw_choice:
            raise RuntimeError("databricks chat stream returned no choices")
        self._raise_for_finish_reason(finish_reason)
        yield LLMStreamChunk(
            done=True,
            response=LLMResponse(
                text=_visible_answer_text("".join(parts)),
                model=served or self.model,
                raw=_safe_chat_raw(last_event, finish_reason=finish_reason),
                usage=usage,
            ),
        )


# ---------- OpenAI provider ----------
class OpenAIProvider:
    """Chat model over the OpenAI Chat Completions API (GPT-5.x).

    Mirrors DatabricksProvider's structure, timeout/retry posture, error
    handling, and logging. It diverges where the OpenAI GPT-5 wire contract
    or the absence of gpt-oss-specific output formatting requires it:

    * ``temperature`` is intentionally not sent. Some GPT-5 models support
      sampling parameters only for specific reasoning configurations (for
      example, reasoning disabled), while reasoning-enabled requests may
      reject them. This provider therefore uses ``reasoning_effort`` as its
      reasoning-control knob and drops the protocol-level ``temperature``
      argument for consistent behavior across supported GPT-5 models.

    * ``max_completion_tokens`` is used instead of the deprecated
      ``max_tokens`` Chat Completions parameter. The limit includes both
      visible output tokens and reasoning tokens.

    * ``reasoning_effort`` is sent using the Chat Completions wire format.
      Supported values depend on the selected model; GPT-5.6 supports
      ``none``, ``low``, ``medium``, ``high``, ``xhigh``, and ``max``.

    * No ``_StreamScrubber`` / ``_visible_answer_text`` processing is
      applied. Those helpers handle gpt-oss/Harmony-style reasoning-channel
      output used by the Databricks provider. This provider preserves
      first-party OpenAI assistant content verbatim.

    * No D1 residency check is performed. Under the 2026-08-20 migration
      decision, OpenAI is treated as an off-perimeter provider. The served
      model is logged for operational visibility rather than residency
      enforcement.

    Chat Completions is retained here for provider-interface compatibility.
    For new OpenAI-native reasoning, tool-calling, or multi-turn workflows,
    OpenAI recommends the Responses API.

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
                    "OPENAI_API_KEY is not set; configure the OpenAI Chat Completions transport"
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
    def _request_messages(messages: list[LLMMessage]) -> list[dict[str, str]]:
        """One consolidated system turn, then the conversation verbatim.

        Same shaping as ``DatabricksProvider._request_messages`` so the
        migration changes the model and nothing else, minus its two
        gpt-oss-only steps: no ``<|think|>`` handling (OpenAI has no such
        control token, and the literal never appears outside this module) and
        no scrub of prior assistant turns, which would silently truncate an
        assistant turn that legitimately quotes ``</think>``.
        """
        system = "\n\n".join(
            message.content for message in messages if message.role == "system"
        ).strip()
        request: list[dict[str, str]] = []
        if system:
            request.append({"role": "system", "content": system})
        for message in messages:
            if message.role == "system":
                continue
            request.append({"role": message.role, "content": message.content})
        return request

    def _request_kwargs(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        response_format: str | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the Chat Completions payload.

        Takes no ``temperature`` parameter at all: the omission is structural
        rather than a conditional a later edit could flip back on.
        """
        request = self._request_messages(messages)
        if response_format == "json":
            # The json_object precondition -- the word "json" must appear in
            # the messages -- is an OpenAI-API rule that the Databricks
            # OpenAI-compatible endpoint inherits, not a Databricks quirk, so
            # this reuses the one choke point rather than copying it. The
            # helper's stricter USER-turn placement (a Databricks-only live
            # finding) is a superset of what OpenAI requires, and reusing it
            # keeps the wire prompt byte-identical across the migration so the
            # model stays the only variable that changed.
            request = DatabricksProvider._ensure_user_json_token(request)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request,
            # max_completion_tokens, NOT max_tokens: the GPT-5 series rejects
            # max_tokens on Chat Completions outright. The budget covers
            # reasoning + visible tokens, so exhausting it arrives as
            # finish_reason="length", which _raise_for_finish_reason fails on.
            "max_completion_tokens": max_tokens,
        }
        # DO NOT add "temperature" here. GPT-5 reasoning models 400 on any
        # non-default value, including an explicit 0.0, which would be a total
        # outage on the very first call rather than a degraded answer.
        if self.reasoning_effort is not None:
            # Valid top-level Chat Completions parameter for the GPT-5 series.
            # Absent key, never a null: a runtime that does not know the
            # parameter must see no parameter at all.
            kwargs["reasoning_effort"] = self.reasoning_effort
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}
        if stream:
            kwargs["stream"] = True
            # Without include_usage the stream carries no usage block at all,
            # and cost_usd then persists NULL with no error and no failing
            # test -- a silent accounting hole, not a visible break.
            kwargs["stream_options"] = {"include_usage": True}
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
    def _first_choice(resp: Any) -> Any:
        choices = getattr(resp, "choices", None)
        if not choices:
            raise RuntimeError("openai chat completion returned no choices")
        return choices[0]

    @staticmethod
    def _raise_for_finish_reason(finish_reason: Any) -> None:
        if finish_reason in ("length", "content_filter"):
            raise RuntimeError(
                f"openai chat completion terminated with finish_reason={finish_reason}"
            )

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
    ) -> LLMResponse:
        # Accepted to satisfy the LLMProvider protocol and dropped on purpose:
        # see the class docstring. Discarding it here makes the drop explicit
        # instead of an omission a reader could mistake for an oversight.
        del temperature
        client = self._client_or_create()
        kwargs = self._request_kwargs(
            messages,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        # The timeout and retry budget live on the client (_client_or_create):
        # the SDK retries max_retries times and then raises APITimeoutError.
        # Deliberately NOT caught here -- it propagates to grounded_qa's
        # provider boundary, which degrades the turn to qa_provider_error
        # exactly as it already does for Databricks.
        resp = client.chat.completions.create(**kwargs)
        served = getattr(resp, "model", None)
        self._log_served(served)
        choice = self._first_choice(resp)
        finish_reason = getattr(choice, "finish_reason", None)
        self._raise_for_finish_reason(finish_reason)
        message = getattr(choice, "message", None)
        candidate = _chat_content_text(getattr(message, "content", None))
        # _extract_json_blob is pure slicing and provider-agnostic: json_object
        # mode makes a fenced or prose-wrapped payload unlikely, not impossible,
        # and slicing can never mint a token the model did not emit. The prose
        # branch only trims whitespace -- no reasoning scrub, see the docstring.
        text = _extract_json_blob(candidate) if response_format == "json" else candidate.strip()
        return LLMResponse(
            text=text,
            model=served or self.model,
            raw=_safe_chat_raw(resp, finish_reason=finish_reason),
            usage=_usage_from(resp, "prompt_tokens", "completion_tokens"),
        )

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Iterator[LLMStreamChunk]:
        """True incremental streaming, yielding the same LLMStreamChunk type
        DatabricksProvider yields so the SSE bridge in api/main.py is untouched.

        No scrubber runs: OpenAI emits no inline reasoning markers, and
        ``delta.reasoning_content`` / ``reasoning`` / ``thinking`` extension
        fields are ignored by ``_chat_content_text``, so private reasoning can
        never reach a delta. After the first yielded delta the buffered
        fallback is DISABLED -- a re-send would repaint the whole answer.
        """
        del temperature  # Never sent; see the class docstring.
        client = self._client_or_create()
        try:
            events = client.chat.completions.create(
                **self._request_kwargs(messages, max_tokens=max_tokens, stream=True)
            )
        except Exception:
            # Nothing yielded yet, so a buffered re-send is safe. Mirrors the
            # Databricks no-SSE fallback; on a permanent error (bad key, bad
            # model) the re-send raises the same error and the turn fails
            # loudly, one extra round-trip later.
            yield from _buffered_stream(self, messages, temperature=0.0, max_tokens=max_tokens)
            return
        parts: list[str] = []
        usage = LLMUsage()
        finish_reason: Any = None
        last_event: Any = None
        served: str | None = None
        logged = False
        yielded = False
        saw_choice = False
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
            last_event = event
            reported = getattr(event, "model", None)
            if reported:
                served = reported
                if not logged:
                    self._log_served(reported)
                    logged = True
            event_usage = _usage_from(event, "prompt_tokens", "completion_tokens")
            if event_usage.input_tokens is not None:
                usage.input_tokens = event_usage.input_tokens
            if event_usage.output_tokens is not None:
                usage.output_tokens = event_usage.output_tokens
            choices = getattr(event, "choices", None) or []
            if not choices:
                # The include_usage terminal event carries usage and no
                # choices; it must not be mistaken for an empty stream.
                continue
            saw_choice = True
            choice = choices[0]
            candidate_finish = getattr(choice, "finish_reason", None)
            if candidate_finish is not None:
                finish_reason = candidate_finish
            delta = getattr(choice, "delta", None)
            raw = _chat_content_text(getattr(delta, "content", None))
            if raw:
                parts.append(raw)
                yielded = True
                yield LLMStreamChunk(delta=raw)
        if not logged:
            self._log_served(served)
        if not saw_choice:
            raise RuntimeError("openai chat stream returned no choices")
        self._raise_for_finish_reason(finish_reason)
        yield LLMStreamChunk(
            done=True,
            response=LLMResponse(
                text="".join(parts).strip(),
                model=served or self.model,
                raw=_safe_chat_raw(last_event, finish_reason=finish_reason),
                usage=usage,
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
            "LLM_PROVIDER=openai or LLM_PROVIDER=databricks (prod), or "
            "LLM_PROVIDER=echo (tests only)."
        )
    if name == "echo":
        return EchoLLMProvider()
    if name == "openai":
        openai_key = getattr(s, "openai_api_key", None)
        openai_model = getattr(s, "openai_llm_model", None)
        # base_url has a real default in Settings; getattr keeps a
        # SimpleNamespace test settings object from becoming an AttributeError
        # at provider build, exactly as on the Databricks branch below.
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
        if (
            role == "synthesizer"
            and bool(getattr(s, "databricks_thinking_enabled", False))
            and bool(getattr(s, "prose_synthesis_enabled", False))
        ):
            # Prose synthesis carries no json response_format, so allow_thinking
            # becomes REACHABLE for the synthesizer for the first time and
            # _visible_answer_text is answer-path load-bearing. Runbook:
            # DATABRICKS_THINKING_ENABLED stays unset through the v6 rollout.
            log.warning("thinking_enabled_with_prose_synthesis")
        return DatabricksProvider(
            model=databricks_model,
            base_url=base_url,
            token=token,
            role=role,
            thinking_enabled=bool(getattr(s, "databricks_thinking_enabled", False)),
            # getattr with defaults throughout: tests construct settings as a
            # SimpleNamespace with a fixed field list, and a bare attribute read
            # would turn a new knob into an AttributeError at provider build.
            reasoning_effort=getattr(s, "databricks_reasoning_effort", None),
            timeout=s.llm_timeout_s,
            max_retries=s.llm_max_retries,
            d1_enforced=bool(getattr(s, "d1_enforced", False)),
            d1_allowed_models=tuple(getattr(s, "d1_allowed_llm_models", ()) or ()),
        )
    raise ValueError(f"unknown LLM provider: {name}")


def assert_llm_runtime_available(name: str | None = None) -> None:
    """Boot-time fail-fast: the configured LLM provider must be fully usable.

    Mirrors process/embedder.assert_embedding_runtime_available. The API's
    answer path generates unconditionally, so its lifespan calls this to
    surface an unset LLM_PROVIDER or missing DATABRICKS_LLM_* at boot, with
    the same remediation message the first generation call would raise.
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
    del role  # Both real providers serve one model for every role.
    s = get_settings()
    provider = (s.llm_provider or "").strip().lower()
    if provider == "echo":
        return "echo"
    if provider == "openai":
        return getattr(s, "openai_llm_model", None) or "unconfigured"
    if provider == "databricks":
        return getattr(s, "databricks_llm_model", None) or "unconfigured"
    return "unconfigured"
