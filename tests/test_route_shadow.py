"""PR11b route/scope observation: measured, never executable."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import D1ResidencyError, LLMResponse, LLMUsage
from regwatch.generate.route import CorpusPolicyHint, RouteHistoryTurn
from regwatch.generate.route_shadow import finalize_route_observation, observe_route
from regwatch.retrieve.resolver import Resolution
from regwatch.retrieve.scope import CorpusDocumentRef, CorpusPolicySnapshot


def _route_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "standalone_question": "Across the inhalation guidances, how is ISM defined?",
        "mode": "lookup",
        "scope_hint": "corpus",
        "product_hint": None,
        "corpus_policy_hint": "inhalation_psg",
    }
    payload.update(overrides)
    return json.dumps(payload)


class _Provider:
    name = "route-stub"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        self.calls.append({"messages": messages, **kwargs})
        return LLMResponse(
            text=self.text,
            model="served-route-model",
            usage=LLMUsage(input_tokens=41, output_tokens=17),
        )

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError


class _BrokenProvider:
    name = "broken"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, *_args: Any, **_kwargs: Any) -> LLMResponse:
        raise self.error


class _PipelineProvider:
    """Valid route output plus invalid guidance, preserving fixed-copy behavior."""

    name = "pipeline-shadow"

    def __init__(self, route_payload: str) -> None:
        self.route_payload = route_payload
        self.call_kinds: list[str] = []

    def complete(self, messages: list[Any], **_kwargs: Any) -> LLMResponse:
        is_route = any(
            "[REGWATCH_ROUTE_V2]" in message.content or "[REGWATCH_ROUTE_V1]" in message.content
            for message in messages
        )
        self.call_kinds.append("route" if is_route else "guidance")
        return LLMResponse(
            text=self.route_payload if is_route else "{}",
            model=self.name,
            usage=LLMUsage(input_tokens=3, output_tokens=2),
        )


def _observe(provider: Any, *, configured_mode: str = "shadow") -> Any:
    return observe_route(
        provider_factory=lambda: provider,
        configured_model_name="configured-route-model",
        configured_mode=configured_mode,
        question="Across the FDA inhalation product-specific guidances, how is ISM defined?",
        recent_turns=(),
        trusted_product_context=None,
        max_tokens=1200,
    )


def _snapshot() -> CorpusPolicySnapshot:
    return CorpusPolicySnapshot(
        policy=CorpusPolicyHint.INHALATION_PSG,
        documents=(
            CorpusDocumentRef(
                doc_id=1,
                version_id=11,
                appl_no="020503",
                short_name="PSG_020503",
            ),
            CorpusDocumentRef(
                doc_id=2,
                version_id=22,
                appl_no="020911",
                short_name="PSG_020911",
            ),
        ),
    )


def test_route_shadow_makes_one_bounded_json_call_and_records_usage() -> None:
    provider = _Provider(_route_json())

    observed = _observe(provider)

    assert observed.error is None
    assert observed.decision is not None
    assert len(provider.calls) == 1
    assert provider.calls[0]["temperature"] == 0.0
    assert provider.calls[0]["max_tokens"] == 1200
    assert provider.calls[0]["response_format"] == "json"
    assert observed.audit["outcome"] == "success"
    assert observed.audit["scope_hint"] == "corpus"
    assert observed.audit["input_tokens"] == 41
    assert observed.audit["output_tokens"] == 17
    assert observed.audit["latency_ms"] >= 0


def test_reserved_live_configuration_is_still_effective_shadow() -> None:
    observed = _observe(_Provider(_route_json()), configured_mode="live")

    assert observed.audit["configured_mode"] == "live"
    assert observed.audit["effective_mode"] == "shadow"


def test_provider_failure_is_recorded_and_returned_not_raised() -> None:
    error = RuntimeError("endpoint unavailable")

    observed = _observe(_BrokenProvider(error))

    assert observed.error is error
    assert observed.decision is None
    assert observed.audit["outcome"] == "provider_error"
    assert observed.audit["failure_type"] == "RuntimeError"


def test_provider_construction_failure_is_also_fail_open() -> None:
    error = RuntimeError("router configuration unavailable")

    def _broken_factory() -> Any:
        raise error

    observed = observe_route(
        provider_factory=_broken_factory,
        configured_model_name="configured-route-model",
        configured_mode="shadow",
        question="What are the bioequivalence requirements?",
        recent_turns=(),
        trusted_product_context=None,
        max_tokens=1200,
    )

    assert observed.error is error
    assert observed.decision is None
    assert observed.audit["outcome"] == "provider_error"


def test_invalid_route_is_countable_without_exposing_raw_completion() -> None:
    observed = _observe(_Provider("{}"))

    assert isinstance(observed.error, ValueError)
    assert observed.audit["outcome"] == "invalid"
    assert observed.audit["failure_reason"] == "invalid_route_structure"
    assert "raw" not in observed.audit


def test_d1_residency_error_is_never_swallowed_by_shadow() -> None:
    with pytest.raises(D1ResidencyError):
        _observe(_BrokenProvider(D1ResidencyError("outside perimeter")))


def test_successful_hint_compiles_bounded_corpus_and_records_disagreement() -> None:
    observed = _observe(_Provider(_route_json()))

    finalized = finalize_route_observation(
        observed,
        original_question=(
            "Across the FDA inhalation product-specific guidances, how is ISM defined?"
        ),
        resolved_product_filters=None,
        session_product_filters=None,
        load_corpus_policies=lambda: {CorpusPolicyHint.INHALATION_PSG: _snapshot()},
        current_mode="lookup_clarify",
        current_scope="clarify",
        current_reason="no_product",
    )

    assert finalized.error is None
    assert finalized.audit["compile_status"] == "success"
    assert finalized.audit["compiled_scope"]["kind"] == "corpus"
    assert finalized.audit["compiled_scope"]["retrieval_mode"] == "exact_corpus"
    assert finalized.audit["compiled_scope"]["scope_version_count"] == 2
    assert finalized.audit["agrees_with_mode"] is False
    assert finalized.audit["agrees_with_scope"] is False
    assert finalized.audit["current_reason"] == "no_product"


def test_compile_loader_failure_is_audited_without_raising() -> None:
    observed = _observe(_Provider(_route_json()))
    error = RuntimeError("catalog down")

    def _broken_catalog() -> dict[CorpusPolicyHint, CorpusPolicySnapshot]:
        raise error

    finalized = finalize_route_observation(
        observed,
        original_question=(
            "Across the FDA inhalation product-specific guidances, how is ISM defined?"
        ),
        resolved_product_filters=None,
        session_product_filters=None,
        load_corpus_policies=_broken_catalog,
        current_mode="lookup_clarify",
        current_scope="clarify",
        current_reason="no_product",
    )

    assert finalized.error is error
    assert finalized.audit["outcome"] == "success"
    assert finalized.audit["compile_status"] == "error"
    assert "compiled_scope" not in finalized.audit


def test_history_is_sent_as_labelled_context_but_cannot_execute() -> None:
    provider = _Provider(_route_json(scope_hint="inherit", corpus_policy_hint=None))

    observed = observe_route(
        provider_factory=lambda: provider,
        configured_model_name="configured-route-model",
        configured_mode="shadow",
        question="What about user-interface requirements?",
        recent_turns=(
            RouteHistoryTurn(
                question="How is ISM defined?",
                answer="Prior answer",
                scope_kind="product",
                scope_audited=True,
            ),
        ),
        trusted_product_context="beclomethasone dipropionate",
        max_tokens=1200,
    )

    assert observed.decision is not None
    assert observed.decision.scope_hint.value == "inherit"


def test_converse_materiality_probe_is_logged_but_never_actionable() -> None:
    observed = observe_route(
        provider_factory=lambda: _Provider(
            _route_json(
                standalone_question="May I ask for help?",
                mode="converse",
                scope_hint="unknown",
                product_hint=None,
                corpus_policy_hint=None,
            )
        ),
        configured_model_name="configured-route-model",
        configured_mode="shadow",
        question="May I ask for help?",
        recent_turns=(),
        trusted_product_context=None,
        max_tokens=1200,
    )

    finalized = finalize_route_observation(
        observed,
        original_question="May I ask for help?",
        resolved_product_filters=None,
        session_product_filters=None,
        load_corpus_policies=lambda: {},
        current_mode="converse",
        current_scope="converse",
        current_reason="meta",
    )

    assert finalized.audit["converse_guard_probe"] == {
        "evaluated": True,
        "materiality_triggered": True,
        "trigger_token": "may",
    }
    assert finalized.audit["compiled_scope"]["kind"] == "converse"


def _set_route_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("REGWATCH_ROUTE_CALL", mode)
    import config.settings as cs

    cs.get_settings.cache_clear()


def test_explicit_corpus_shadow_logs_exact_corpus_without_changing_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key PR11b boundary: observe/compile, then take today's no_product path."""

    question = "Across the FDA inhalation product-specific guidances, how is ISM defined?"
    provider = _PipelineProvider(_route_json())
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    monkeypatch.setattr(qa_mod, "resolve_product", lambda _q: Resolution(status="none"))
    monkeypatch.setattr(qa_mod, "suggest_products", lambda _q: [])
    monkeypatch.setattr(qa_mod, "resolve_brand", lambda _q: [])
    monkeypatch.setattr(
        qa_mod,
        "load_corpus_policy_snapshots",
        lambda: {CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    def _retrieval_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("shadow corpus scope must not reach retrieval in PR11b")

    monkeypatch.setattr(qa_mod, "retrieve", _retrieval_must_not_run)

    _set_route_mode(monkeypatch, "off")
    off_outcome, off_audit, off_patch = qa_mod.ask_core(
        question,
        session_id="session-shadow-parity",
        turn_id="turn-off",
        load_session_filters=lambda: {"normalized_name": "albuterol sulfate"},
        load_recent_turns=lambda: [],
    )

    provider.call_kinds.clear()
    _set_route_mode(monkeypatch, "shadow")
    shadow_outcome, shadow_audit, shadow_patch = qa_mod.ask_core(
        question,
        session_id="session-shadow-parity",
        turn_id="turn-shadow",
        load_session_filters=lambda: {"normalized_name": "albuterol sulfate"},
        load_recent_turns=lambda: [],
    )

    # Turn ids are caller-owned; every user-visible/domain field is otherwise
    # identical. The only intended delta is nested audit metadata.
    off_values = asdict(off_outcome)
    shadow_values = asdict(shadow_outcome)
    off_values.pop("turn_id")
    shadow_values.pop("turn_id")
    assert shadow_values == off_values
    assert shadow_audit.answer_text == off_audit.answer_text
    assert shadow_audit.status == off_audit.status == "refused"
    assert shadow_audit.retrieved == off_audit.retrieved == []
    assert shadow_patch.update_filters is off_patch.update_filters is False
    assert shadow_patch.filters == off_patch.filters == {}
    assert provider.call_kinds == ["route", "guidance"]

    route_call = shadow_audit.route_json["route_call"]
    assert route_call["compiled_scope"]["kind"] == "corpus"
    assert route_call["compiled_scope"]["retrieval_mode"] == "exact_corpus"
    assert route_call["current_reason"] == "no_product"
    assert route_call["agrees_with_scope"] is False
    assert "route_call" not in off_audit.route_json


def test_shadow_standalone_question_never_rewrites_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "What study design does beclomethasone dipropionate require?"
    provider = _PipelineProvider(
        _route_json(
            standalone_question="MODEL REWRITE MUST REMAIN DARK",
            scope_hint="product",
            product_hint="beclomethasone dipropionate",
            corpus_policy_hint=None,
        )
    )
    searches: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    monkeypatch.setattr(
        qa_mod,
        "resolve_product",
        lambda _q: Resolution(
            status="resolved",
            normalized_name="beclomethasone dipropionate",
            by_name=True,
        ),
    )
    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", lambda *a, **k: [])

    def _retrieve(query: str, **kwargs: Any) -> list[Any]:
        searches.append((query, kwargs))
        return []

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve)
    _set_route_mode(monkeypatch, "shadow")

    outcome, audit, _patch = qa_mod.ask_core(
        original,
        session_id="session-product-shadow",
        turn_id="turn-product-shadow",
        load_session_filters=lambda: {},
        load_recent_turns=lambda: [],
    )

    assert outcome.reason == "low_top_score"
    assert searches[0][0] == original
    assert searches[0][1]["filters"] == {"normalized_name": "beclomethasone dipropionate"}
    route_call = audit.route_json["route_call"]
    assert route_call["standalone_question"] == "MODEL REWRITE MUST REMAIN DARK"
    assert route_call["compiled_scope"]["retrieval_mode"] == "exact_scoped"
    assert audit.route_json["retrieval"]["mode"] == "exact_scoped"


def test_shadow_context_read_cannot_preload_authoritative_session_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PipelineProvider(
        _route_json(
            standalone_question="What about dissolution?",
            scope_hint="inherit",
            product_hint=None,
            corpus_policy_hint=None,
        )
    )
    session_reads = [
        {"normalized_name": "albuterol sulfate"},  # shadow-only snapshot
        {"normalized_name": "beclomethasone dipropionate"},  # real pipeline
    ]
    searched_filters: list[dict[str, Any]] = []
    recent_reads = 0

    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    monkeypatch.setattr(qa_mod, "resolve_product", lambda _q: Resolution(status="none"))
    monkeypatch.setattr(qa_mod, "suggest_products", lambda _q: [])
    monkeypatch.setattr(qa_mod, "resolve_brand", lambda _q: [])
    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", lambda *a, **k: [])

    def _load_session_filters() -> dict[str, Any]:
        return session_reads.pop(0)

    def _retrieve(_query: str, **kwargs: Any) -> list[Any]:
        searched_filters.append(dict(kwargs["filters"]))
        return []

    def _load_recent_turns() -> list[Any]:
        nonlocal recent_reads
        recent_reads += 1
        return []

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve)
    _set_route_mode(monkeypatch, "shadow")

    outcome, audit, _patch = qa_mod.ask_core(
        "What about dissolution?",
        session_id="session-snapshot-parity",
        turn_id="turn-snapshot-parity",
        load_session_filters=_load_session_filters,
        load_recent_turns=_load_recent_turns,
    )

    assert outcome.reason == "low_top_score"
    assert session_reads == []  # one independent shadow read, one real read
    assert recent_reads == 2  # shadow context cannot populate the real memo
    assert searched_filters == [{"normalized_name": "beclomethasone dipropionate"}]
    assert audit.route_json["filters"] == {"normalized_name": "beclomethasone dipropionate"}
    assert audit.route_json["route_call"]["compiled_scope"]["product_filters"] == {
        "normalized_name": "albuterol sulfate"
    }
