"""Observe conversational routing without allowing it to steer a turn.

The route model is deliberately advisory.  This module owns the bounded model
call, strict parsing, and deterministic scope-compilation audit record used by
``REGWATCH_ROUTE_CALL=shadow``.  It never chooses a retrieval query, mutates a
session, or renders user-visible text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from regwatch.generate.llm import (
    D1ResidencyError,
    LLMProvider,
    estimate_cost_usd,
)
from regwatch.generate.route import (
    ROUTE_PROMPT,
    CorpusPolicyHint,
    RouteDecision,
    RouteHistoryTurn,
    ScopeHint,
    TurnMode,
    build_route_request,
    parse_route_decision,
)
from regwatch.generate.turn_gate import materiality_trigger
from regwatch.retrieve.scope import (
    CompiledScope,
    CorpusPolicySnapshot,
    compile_scope,
    probe_corpus_intent,
)


@dataclass(frozen=True)
class RouteShadowObservation:
    """One completed route-call attempt, before scope compilation."""

    audit: dict[str, Any]
    decision: RouteDecision | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class FinalizedRouteShadow:
    """The JSON-safe observation after application-owned compilation."""

    audit: dict[str, Any]
    error: Exception | None = None


def _latency_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def observe_route(
    *,
    provider_factory: Callable[[], LLMProvider],
    configured_model_name: str,
    configured_mode: str,
    question: str,
    recent_turns: tuple[RouteHistoryTurn, ...],
    trusted_product_context: str | None,
    max_tokens: int,
) -> RouteShadowObservation:
    """Make one bounded route call and record it without executing the result.

    ``live`` remains shadow-equivalent in this PR.  Recording both the configured
    and effective modes prevents an accidentally early ``live`` secret from
    changing behavior while making the configuration error visible in audit.
    D1 residency failures are the sole exception to fail-open shadowing and are
    re-raised before any generic error handling.
    """

    started = perf_counter()
    audit: dict[str, Any] = {
        "attempted": True,
        "configured_mode": configured_mode,
        "effective_mode": "shadow",
        "prompt": ROUTE_PROMPT.as_dict(),
        "configured_model": configured_model_name,
        "outcome": "request_error",
    }
    try:
        request = build_route_request(
            question=question,
            recent_turns=recent_turns,
            trusted_product_context=trusted_product_context,
        )
    except Exception as exc:
        audit.update(
            {
                "latency_ms": _latency_ms(started),
                "failure_reason": "request_error",
                "failure_type": type(exc).__name__,
            }
        )
        return RouteShadowObservation(audit=audit, error=exc)

    try:
        # Construct the client inside the fail-open boundary too. A missing or
        # temporarily invalid router configuration is still a shadow failure;
        # it must not turn an otherwise healthy deterministic Ask turn into an
        # error response. D1 remains the deliberate fail-closed exception.
        provider = provider_factory()
        response = provider.complete(
            request.messages,
            temperature=0.0,
            max_tokens=max_tokens,
            response_format="json",
        )
    except D1ResidencyError:
        raise
    except Exception as exc:
        audit.update(
            {
                "outcome": "provider_error",
                "latency_ms": _latency_ms(started),
                "failure_reason": "provider_error",
                "failure_type": type(exc).__name__,
            }
        )
        return RouteShadowObservation(audit=audit, error=exc)

    audit.update(
        {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd": estimate_cost_usd(response.model, response.usage),
        }
    )
    try:
        decision = parse_route_decision(response.text, request)
    except ValueError as exc:
        audit.update(
            {
                "outcome": "invalid",
                "latency_ms": _latency_ms(started),
                "failure_reason": str(exc),
                "failure_type": type(exc).__name__,
            }
        )
        return RouteShadowObservation(audit=audit, error=exc)

    audit.update(
        {
            "outcome": "success",
            "latency_ms": _latency_ms(started),
            "mode": decision.mode.value,
            "scope_hint": decision.scope_hint.value,
            "standalone_question": decision.standalone_question,
            "product_hint": decision.product_hint,
            "corpus_policy_hint": (
                decision.corpus_policy_hint.value if decision.corpus_policy_hint else None
            ),
        }
    )
    return RouteShadowObservation(audit=audit, decision=decision)


def finalize_route_observation(
    observation: RouteShadowObservation,
    *,
    original_question: str,
    resolved_product_filters: Mapping[str, object] | None,
    session_product_filters: Mapping[str, object] | None,
    load_corpus_policies: Callable[[], Mapping[CorpusPolicyHint, CorpusPolicySnapshot]],
    current_mode: str,
    current_scope: str,
    current_reason: str,
    prior_audited_scope: CompiledScope | None = None,
    prior_audit_id: int | None = None,
) -> FinalizedRouteShadow:
    """Compile a successful hint and compare it with today's authoritative path.

    The corpus catalog is loaded only when compilation could need it.  Any
    loader/compiler fault is recorded and returned to the caller for logging,
    but cannot alter the current resolver/retrieval/response path.
    """

    audit = deepcopy(observation.audit)
    audit.update(
        {
            "current_mode": current_mode,
            "current_scope": current_scope,
            "current_reason": current_reason,
            "agrees_with_mode": None,
            "agrees_with_scope": None,
        }
    )
    decision = observation.decision
    if decision is None:
        audit["compile_status"] = "not_attempted"
        return FinalizedRouteShadow(audit=audit)

    # PR15's converse guard is not built here, but the broad materiality
    # predicate's economics must be measured before that design is finalized.
    # Probe only turns the route model classified as converse and record the
    # trigger token, never a copy of the question. This remains observation;
    # it cannot reroute or suppress the current response.
    converse_trigger = (
        materiality_trigger(original_question) if decision.mode is TurnMode.CONVERSE else None
    )
    audit["converse_guard_probe"] = {
        "evaluated": decision.mode is TurnMode.CONVERSE,
        "materiality_triggered": converse_trigger is not None,
        "trigger_token": converse_trigger,
    }

    # Observation only, and recorded BEFORE compilation so it survives a
    # compiler fault. Without this, a corpus-phrased question that also resolves
    # a drug is authorized as a product scope (correctly) and leaves no trace
    # that corpus intent was ever proposed, which would let the shadow window
    # under-report exactly the false negatives Checkpoint 3 asks about.
    probe = probe_corpus_intent(
        decision,
        original_question=original_question,
        resolved_product_filters=resolved_product_filters,
    )
    if probe is not None:
        audit["corpus_intent_probe"] = probe.as_audit_json()

    try:
        needs_corpus_catalog = decision.scope_hint is ScopeHint.CORPUS or (
            decision.scope_hint is ScopeHint.INHERIT
            and prior_audited_scope is not None
            and prior_audited_scope.corpus_policy is not None
        )
        corpus_policies = load_corpus_policies() if needs_corpus_catalog else {}
        compiled = compile_scope(
            decision,
            original_question=original_question,
            resolved_product_filters=resolved_product_filters,
            session_product_filters=session_product_filters,
            corpus_policies=corpus_policies,
            prior_audited_scope=prior_audited_scope,
            prior_audit_id=prior_audit_id,
        )
    except Exception as exc:
        audit.update(
            {
                "compile_status": "error",
                "compile_failure_type": type(exc).__name__,
            }
        )
        return FinalizedRouteShadow(audit=audit, error=exc)

    audit.update(
        {
            "compile_status": "success",
            "compiled_scope": compiled.as_audit_json(),
            "agrees_with_mode": decision.mode.value == current_mode,
            "agrees_with_scope": compiled.kind.value == current_scope,
        }
    )
    return FinalizedRouteShadow(audit=audit)
