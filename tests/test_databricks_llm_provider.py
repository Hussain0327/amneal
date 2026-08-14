"""Offline contract tests for the Databricks Chat Completions provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import regwatch.generate.llm as llm_mod
from regwatch.common import llm_clients
from regwatch.generate.llm import (
    D1ResidencyError,
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
    model: str | None = "served-model-revision",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="db-response",
        object="chat.completion",
        created=123,
        model=model,
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
    reasoning_effort: str | None = None,
    d1_enforced: bool = False,
    d1_allowed_models: tuple[str, ...] = (),
) -> DatabricksProvider:
    # The new params default UNARMED so every pre-existing case is unchanged:
    # _response() reports "served-model-revision", which is in no allowlist, so
    # a default-armed guard would fail nearly every test in this file.
    return DatabricksProvider(
        model="llm-endpoint",
        base_url="https://workspace.example/serving-endpoints",
        token="token",
        role=role,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        d1_enforced=d1_enforced,
        d1_allowed_models=d1_allowed_models,
        client=_client(completions),
    )


@pytest.fixture(autouse=True)
def _reset_served_model_log() -> Any:
    """The served-model notice is an lru_cache keyed on (endpoint, served).

    Without clearing it, whether a line is emitted depends on which test ran
    first in the process -- the log-once assertions below would be order
    dependent.
    """
    llm_mod._log_served_model_once.cache_clear()
    yield
    llm_mod._log_served_model_once.cache_clear()


class _LogRecorder:
    """Captures structlog-style calls on the llm module logger."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))


def _stream_event(
    content: str,
    *,
    finish_reason: str | None = None,
    model: str | None = "served-model-revision",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="db-stream",
        object="chat.completion.chunk",
        model=model,
        choices=[_choice(content, finish_reason=finish_reason, delta=True)],
        usage=None,
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
    assert result.model == "served-model-revision"
    assert result.usage == LLMUsage(input_tokens=11, output_tokens=7)
    assert result.raw == {
        "id": "db-response",
        "object": "chat.completion",
        "created": 123,
        "model": "served-model-revision",
        "finish_reason": "stop",
    }
    assert "PRIVATE" not in result.text
    assert "PRIVATE" not in repr(result.raw)


def test_prose_call_with_thinking_scrubs_thought_before_the_prose_gate() -> None:
    """v6 prose is the first non-json synthesizer call, so allow_thinking is
    REACHABLE for the answer path and the scrub is answer-path load-bearing:
    what leaves complete() is exactly what prose_turn.parse will read, so a
    surviving thought token would become a parser kill (or worse, a sentence).
    """
    response = _response(
        [
            {
                "type": "text",
                "text": (
                    "<|channel>thought\nPRIVATE PLANNING"
                    "<channel|>A fasting study is described [1]."
                ),
            }
        ]
    )
    completions = _Completions(response)

    result = _provider(completions).complete([LLMMessage("user", "question")])

    # Thinking WAS requested on the wire (prose carries no json response_format,
    # so the synthesizer-only gate opens)...
    assert completions.calls[0]["messages"][0]["content"].startswith("<|think|>")
    assert "response_format" not in completions.calls[0]
    # ...and none of it survives into the visible answer channel.
    assert result.text == "A fasting study is described [1]."
    assert "PRIVATE" not in result.text


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
            model="served-model-revision",
            choices=[_choice("<|channel>tho", finish_reason=None, delta=True)],
            usage=None,
        ),
        SimpleNamespace(
            model="served-model-revision",
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
            model="served-model-revision",
            choices=[_choice("answer.", finish_reason="stop", delta=True)],
            usage=None,
        ),
        SimpleNamespace(
            id="db-stream",
            object="chat.completion.chunk",
            model="served-model-revision",
            choices=[],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=13),
        ),
    ]
    completions = _Completions(_response("unused"), stream_events=events)

    chunks = list(_provider(completions).stream([LLMMessage("user", "question")]))

    # True incremental streaming: each wire event that yields visible text
    # produces its own delta (the "<|channel>tho" prefix of event 1 is held
    # back as a possible delimiter start and folds into event 2's delta).
    assert [chunk.delta for chunk in chunks if not chunk.done] == ["Grounded ", "answer."]
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


def test_stream_rejects_truncated_output() -> None:
    """The streaming twin of the truncation guard.

    A reasoning model that spends the whole budget thinking finishes with
    finish_reason=length on the SSE path too, and that must raise rather than
    ship a silently truncated answer for citation validation.
    """
    completions = _Completions(
        _response("partial", finish_reason="length"),
        stream_events=[_stream_event("partial", finish_reason="length")],
    )

    with pytest.raises(RuntimeError, match="finish_reason=length"):
        list(_provider(completions).stream([LLMMessage("user", "question")]))

    # Incremental streaming (2026-08-10): the "partial" delta is visible and
    # yielded to the consumer BEFORE the truncation check runs, so the
    # buffered-fallback re-send is correctly skipped -- resending would paint
    # the whole answer a second time after a partial one. One upstream call.
    assert [call.get("stream", False) for call in completions.calls] == [True]


# ---------- reasoning budget (DATABRICKS_REASONING_EFFORT) ----------


def test_reasoning_effort_is_sent_on_both_the_buffered_and_streaming_request() -> None:
    """Thought and answer share one max_tokens budget on a reasoning model.

    Without a bound, the model spends the budget thinking, finishes with
    finish_reason=length and the provider raises -- every substantive Ask
    degrades to an audited refusal. The streaming request needs it just as much
    as the buffered one: streaming is the default UI path.
    """
    completions = _Completions(
        _response("answer"),
        stream_events=[_stream_event("answer", finish_reason="stop")],
    )
    provider = _provider(completions, reasoning_effort="low")

    provider.complete([LLMMessage("user", "question")])
    list(provider.stream([LLMMessage("user", "question")]))

    buffered, streamed = completions.calls
    assert buffered["reasoning_effort"] == "low"
    assert streamed["stream"] is True
    assert streamed["reasoning_effort"] == "low"


def test_reasoning_effort_is_omitted_entirely_when_unset() -> None:
    """Absent key, never a null: an endpoint that does not know the parameter
    must not be handed `reasoning_effort: None` and 400 every synthesis."""
    completions = _Completions(_response("answer"))

    _provider(completions, reasoning_effort=None).complete([LLMMessage("user", "question")])

    assert "reasoning_effort" not in completions.calls[-1]


@pytest.mark.parametrize("role", ["router", "extractor", "default", "synthesizer"])
def test_reasoning_effort_survives_json_mode_and_every_role(role: str) -> None:
    """Unlike thinking, the effort bound is NOT synthesizer-only.

    The JSON callers (extractor at 1500 tokens, change detector at 400) are
    exactly where an unbounded thinking spend overruns the budget, and JSON mode
    gives no answer-quality reason to think longer.
    """
    completions = _Completions(_response('{"ok": true}'))
    provider = _provider(completions, role=role, reasoning_effort="medium")

    provider.complete([LLMMessage("user", "question")], response_format="json")

    assert completions.calls[-1]["reasoning_effort"] == "medium"
    assert completions.calls[-1]["response_format"] == {"type": "json_object"}


# ---------- D1 served-model runtime check ----------

_ALIAS_ONLY = ("llm-endpoint",)


def test_served_model_outside_the_allowlist_is_refused_when_enforced() -> None:
    """The hole the Settings boot check structurally cannot see.

    DATABRICKS_LLM_MODEL is a Unity Catalog alias and IS allowlisted; the
    endpoint behind it was repointed at a different model with no deploy. Only
    the response names what actually answered.
    """
    completions = _Completions(_response("answer", model="gpt-oss-20b-080525"))

    with pytest.raises(RuntimeError, match="gpt-oss-20b-080525"):
        _provider(completions, d1_enforced=True, d1_allowed_models=_ALIAS_ONLY).complete(
            [LLMMessage("user", "question")]
        )


def test_unreported_served_model_fails_closed_when_enforced() -> None:
    """An endpoint that reports no model cannot be verified, so it is refused.

    This is the test that fails if anyone re-collapses the served name to
    self.model before the check: the alias is allowlisted by construction (the
    boot validator requires it), so the collapsed value would always pass.
    """
    completions = _Completions(_response("answer", model=None))

    with pytest.raises(RuntimeError, match="reported no served model"):
        _provider(completions, d1_enforced=True, d1_allowed_models=_ALIAS_ONLY).complete(
            [LLMMessage("user", "question")]
        )


def test_partner_served_model_is_refused_even_when_a_human_allowlisted_it() -> None:
    """Runtime mirror of the boot-time partner-family guard: an allowlist is
    typed by a human, and partner brands look native at the call site."""
    served = "databricks-claude-sonnet-5"
    completions = _Completions(_response("answer", model=served))

    with pytest.raises(RuntimeError, match="partner-hosted"):
        _provider(
            completions, d1_enforced=True, d1_allowed_models=("llm-endpoint", served)
        ).complete([LLMMessage("user", "question")])


def test_compliant_served_model_is_served_unchanged_when_enforced() -> None:
    """The guard must not break the deployment it exists to protect."""
    completions = _Completions(_response("Grounded answer.", model="open-weight-served"))

    result = _provider(
        completions,
        d1_enforced=True,
        d1_allowed_models=("llm-endpoint", "open-weight-served"),
    ).complete([LLMMessage("user", "question")])

    assert result.text == "Grounded answer."
    assert result.model == "open-weight-served"


@pytest.mark.parametrize("served", ["databricks-claude-sonnet-5", None])
def test_unarmed_provider_never_refuses_on_the_served_model(served: str | None) -> None:
    """Today's prod posture (D1_ENFORCED unset) and the OpenAI rollback path:
    the tripwire is inert, it only makes the served model visible."""
    completions = _Completions(_response("Grounded answer.", model=served))

    result = _provider(completions).complete([LLMMessage("user", "question")])

    assert result.text == "Grounded answer."


def test_streaming_violation_refuses_without_re_sending_the_question() -> None:
    """The stream path is the default UI route, so it needs the same guard --
    raised as D1ResidencyError, which stream()'s SSE fallback re-raises. A
    plain exception there reads as "endpoint has no SSE" and re-sends the
    analyst question to the very endpoint D1 fences off."""
    completions = _Completions(
        _response("answer", model="gpt-oss-20b-080525"),
        stream_events=[_stream_event("answer", finish_reason="stop", model="gpt-oss-20b-080525")],
    )
    provider = _provider(completions, d1_enforced=True, d1_allowed_models=_ALIAS_ONLY)

    chunks = provider.stream([LLMMessage("user", "question")])
    with pytest.raises(RuntimeError, match="gpt-oss-20b-080525"):
        next(chunks)

    # Exactly one upstream call (the stream), and nothing painted before it.
    assert [call.get("stream", False) for call in completions.calls] == [True]


def test_armed_truncating_off_perimeter_completion_raises_the_d1_error() -> None:
    """Residency BEFORE the truncation check, pinned.

    The exact deployment the guard targets -- an alias repointed to a reasoning
    model -- truncates on EVERY turn (thought and answer share one budget). If
    the finish_reason check ran first, that deployment would be misdiagnosed as
    a truncation bug and the llm_served_model ops line would never fire.
    """
    completions = _Completions(
        _response("partial", finish_reason="length", model="gpt-oss-20b-080525")
    )

    with pytest.raises(D1ResidencyError, match="gpt-oss-20b-080525"):
        _provider(completions, d1_enforced=True, d1_allowed_models=_ALIAS_ONLY).complete(
            [LLMMessage("user", "question")]
        )


def test_armed_streaming_violation_with_truncation_never_falls_back() -> None:
    """The 2026-07-28 shape end to end: off-perimeter AND finish_reason=length.

    The truncation raise must not reach the SSE fallback first -- the fallback
    would re-send the analyst question to the fenced-off endpoint (the exact
    double disclosure test_stream_rejects_truncated_output pins as acceptable
    only UNARMED). Residency is checked before the finish-reason raise and
    D1ResidencyError is excluded from the fallback, so: one upstream call.
    """
    completions = _Completions(
        _response("partial", finish_reason="length", model="gpt-oss-20b-080525"),
        stream_events=[
            _stream_event("partial", finish_reason="length", model="gpt-oss-20b-080525")
        ],
    )
    provider = _provider(completions, d1_enforced=True, d1_allowed_models=_ALIAS_ONLY)

    with pytest.raises(D1ResidencyError, match="gpt-oss-20b-080525"):
        list(provider.stream([LLMMessage("user", "question")]))

    assert [call.get("stream", False) for call in completions.calls] == [True]


def test_streaming_served_model_is_not_laundered_by_the_endpoint_fallback() -> None:
    """A stream whose events carry no `model` is unverifiable, not compliant.

    _complete_stream seeds its response model with the configured alias, so the
    raw reported value has to travel out separately or this fails open.
    """
    completions = _Completions(
        _response("answer", model=None),
        stream_events=[_stream_event("answer", finish_reason="stop", model=None)],
    )
    provider = _provider(completions, d1_enforced=True, d1_allowed_models=_ALIAS_ONLY)

    with pytest.raises(RuntimeError, match="reported no served model"):
        list(provider.stream([LLMMessage("user", "question")]))


def test_served_model_is_logged_once_per_distinct_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ops must be able to see what is actually answering, without one log line
    per Ask on a long-lived process -- and a mid-process repoint must still
    surface rather than be swallowed by an already-logged flag."""
    recorder = _LogRecorder()
    monkeypatch.setattr(llm_mod, "log", recorder)
    first = _Completions(_response("answer", model="gpt-oss-20b-080525"))
    provider = _provider(first)

    provider.complete([LLMMessage("user", "question")])
    provider.complete([LLMMessage("user", "question again")])

    assert recorder.events == [
        ("llm_served_model", {"endpoint": "llm-endpoint", "served": "gpt-oss-20b-080525"})
    ]

    repointed = _Completions(_response("answer", model="some-other-model"))
    _provider(repointed).complete([LLMMessage("user", "question")])

    assert [kw["served"] for _, kw in recorder.events] == [
        "gpt-oss-20b-080525",
        "some-other-model",
    ]


def test_factory_builds_role_aware_databricks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm_provider="databricks",
        databricks_llm_base_url="https://workspace.example/serving-endpoints",
        databricks_llm_token="token",
        databricks_llm_model="llm-endpoint",
        databricks_thinking_enabled=True,
        databricks_reasoning_effort="low",
        llm_timeout_s=31.0,
        llm_max_retries=1,
        d1_enforced=True,
        d1_allowed_llm_models=["llm-endpoint"],
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    synthesizer = get_llm_provider(role="synthesizer")
    extractor = get_llm_provider(role="extractor")

    assert isinstance(synthesizer, DatabricksProvider)
    assert synthesizer.thinking_enabled is True
    assert synthesizer.timeout == 31.0
    assert synthesizer.max_retries == 1
    # Dead config otherwise: the settings would exist and never reach the wire.
    assert synthesizer.reasoning_effort == "low"
    assert synthesizer.d1_enforced is True
    assert synthesizer.d1_allowed_models == ("llm-endpoint",)
    assert isinstance(extractor, DatabricksProvider)
    assert extractor.thinking_enabled is False
    assert extractor.reasoning_effort == "low"
    assert current_model_name(role="extractor") == "llm-endpoint"


def test_factory_defaults_the_new_knobs_when_settings_predate_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings object without the reasoning/D1 fields must still build an
    INERT provider, not AttributeError at provider-construction time."""
    settings = SimpleNamespace(
        llm_provider="databricks",
        databricks_llm_base_url="https://workspace.example/serving-endpoints",
        databricks_llm_token="token",
        databricks_llm_model="llm-endpoint",
        databricks_thinking_enabled=False,
        llm_timeout_s=31.0,
        llm_max_retries=1,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    provider = get_llm_provider(role="synthesizer")

    assert isinstance(provider, DatabricksProvider)
    assert provider.reasoning_effort is None
    assert provider.d1_enforced is False
    assert provider.d1_allowed_models == ()


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
        databricks_llm_model="llm-endpoint",
        databricks_thinking_enabled=False,
        llm_timeout_s=31.0,
        llm_max_retries=1,
    )
    setattr(settings, missing_attr, "")
    monkeypatch.setattr(llm_mod, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match=expected_env):
        get_llm_provider()


# --- json_object precondition (issue #162) ------------------------------------
#
# The endpoint 400s with `"messages" must contain the word "json" in some form,
# to use "response_format" of type json_object` unless a USER turn carries the
# word. Live probing established a system message does not satisfy it at any
# casing, and _request_messages folds every system message into one system turn
# -- so the schema instruction that structured callers send never counts. These
# tests hold the wire-level invariant for every structured caller at once.


def _json_call_messages(messages: list[LLMMessage]) -> list[dict[str, str]]:
    completions = _Completions(_response('{"ok": true}'))
    _provider(completions).complete(messages, response_format="json")
    return completions.calls[-1]["messages"]


def _has_user_json(messages: list[dict[str, str]]) -> bool:
    return any("json" in m["content"].lower() for m in messages if m["role"] == "user")


def test_json_mode_puts_the_word_json_in_a_user_turn() -> None:
    """The exact shape of the live 400: regwatch.query_guidance.

    System messages mention JSON (QUERY_GUIDANCE_SYSTEM, GUIDANCE_SCHEMA_MESSAGE)
    and the user turn is a serialized context blob that does not.
    """
    wire = _json_call_messages(
        [
            LLMMessage("system", "You route questions."),
            LLMMessage("user", '{"untrusted_question":"washout period?","route":"refused"}'),
            LLMMessage("system", "Return ONLY a JSON object matching this schema."),
        ]
    )
    assert _has_user_json(wire)


def test_the_directive_lands_on_the_last_user_turn_not_a_new_one() -> None:
    """Turn structure is preserved: no synthetic trailing user message."""
    before = [
        LLMMessage("system", "sys"),
        LLMMessage("user", "first"),
        LLMMessage("assistant", "reply"),
        LLMMessage("user", "second"),
    ]
    wire = _json_call_messages(before)
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert wire[1]["content"] == "first", "an earlier turn must not be rewritten"
    assert wire[-1]["content"].startswith("second")
    assert _has_user_json(wire)


def test_a_user_turn_that_already_says_json_is_left_alone() -> None:
    """grounded_qa synthesis already ends with 'Return the JSON object now'.

    That path works live and must stay byte-identical -- the fix is for the
    callers that lack it, not a rewrite of every structured prompt.
    """
    user = "Answer the question. Return the JSON object now."
    wire = _json_call_messages([LLMMessage("system", "sys"), LLMMessage("user", user)])
    assert [m["content"] for m in wire if m["role"] == "user"] == [user]


def test_prose_mode_never_injects_the_directive() -> None:
    """Only json_object mode has the precondition; prose turns are untouched."""
    completions = _Completions(_response("plain prose"))
    _provider(completions).complete([LLMMessage("system", "sys"), LLMMessage("user", "hello")])
    assert [m["content"] for m in completions.calls[-1]["messages"] if m["role"] == "user"] == [
        "hello"
    ]


def test_a_system_only_json_mention_does_not_satisfy_the_endpoint() -> None:
    """The probed asymmetry, pinned.

    This is the whole bug: the word was present, in a system message, and the
    endpoint still rejected the request. A caller whose only JSON mention is
    system-side must still come out with a user-side one.
    """
    wire = _json_call_messages(
        [
            LLMMessage("system", "Return ONLY a JSON object. No prose."),
            LLMMessage("user", "Extract the study design from the excerpt."),
        ]
    )
    assert _has_user_json(wire)


def test_a_caller_with_no_user_turn_still_gets_one() -> None:
    """Degenerate but reachable: everything system-side."""
    wire = _json_call_messages([LLMMessage("system", "Return ONLY a JSON object.")])
    assert _has_user_json(wire)
    assert wire[-1]["role"] == "user"
