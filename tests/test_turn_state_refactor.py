"""PR1 (SLM plan Phase 0): ask_core's mid-flow state is an explicit TurnState.

The ``_decline`` ceremony used to read loose ``ask_core`` closure variables
(active_filters/context_applied) at call time; the stage extraction moved that
state into the ``TurnState`` dataclass the stage functions mutate in place.
These tests pin the carry-through: what resolution/carry-over writes into the
dataclass must be exactly what a later terminal clarify reads -- through the
TurnState instance, not module or closure state.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa
from regwatch.retrieve.resolver import Resolution


def _no_decline(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("decline must not be called on a continue path")


class _FailingRouter:
    """Fails the bounded guidance call so the deterministic decline copy and
    options reach the assertions untouched."""

    name = "down"

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("router down")


def test_resolution_writes_into_the_turn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qa,
        "resolve_product",
        lambda q: Resolution(status="resolved", normalized_name="albuterol sulfate", by_name=True),
    )
    state = qa.TurnState(active_filters={})

    result = qa._resolve_and_carry_over(
        state,
        question="What BE study is recommended for albuterol sulfate?",
        _decline=_no_decline,
        _session_filters=lambda: {},
    )

    # The SAME instance continues down the pipeline -- later stages and
    # _decline observe these mutations through it.
    assert result is state
    assert state.active_filters == {"normalized_name": "albuterol sulfate"}
    assert state.resolved_by_name is True
    assert state.context_applied is False


def test_carry_over_flows_through_the_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "resolve_product", lambda q: Resolution(status="none"))
    monkeypatch.setattr(qa, "suggest_products", lambda q: [])
    monkeypatch.setattr(qa, "resolve_brand", lambda q: [])
    state = qa.TurnState(active_filters={})

    result = qa._resolve_and_carry_over(
        state,
        question="tell me more",
        _decline=_no_decline,
        # Canonical, as the shell stores it: the form carry-over compares the
        # session name against the canonicalized resolved name.
        _session_filters=lambda: {
            "normalized_name": "albuterol sulfate",
            "dosage_form": "Aerosol, Metered",
            "route": "Inhalation",
        },
    )

    assert result is state
    # Product AND the previously chosen form ride the dataclass, with the
    # carry-over flags a later decline's route_json must report.
    assert state.active_filters == {
        "normalized_name": "albuterol sulfate",
        "dosage_form": "Aerosol, Metered",
        "route": "Inhalation",
    }
    assert state.context_applied is True
    assert state.resolved_by_name is False


def test_clarify_decline_reads_the_mutated_turn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vague follow-up that inherits the session product must audit the
    carried filters and context_applied=True. ``_decline`` builds its
    route_json at CALL time, after the resolution stage ran, so the only way
    it can know either value is by reading the TurnState that stage mutated.
    """
    monkeypatch.setattr(qa, "resolve_product", lambda q: Resolution(status="none"))
    monkeypatch.setattr(qa, "suggest_products", lambda q: [])
    monkeypatch.setattr(qa, "resolve_brand", lambda q: [])
    monkeypatch.setattr(qa, "_doc_count", lambda name: 2)
    monkeypatch.setattr(qa, "get_llm_provider", lambda *a, **k: _FailingRouter())

    outcome, audit, patch = qa.ask_core(
        "tell me more",
        session_id="sess-turnstate",
        turn_id="turn-turnstate",
        load_session_filters=lambda: {"normalized_name": "Albuterol Sulfate"},
        load_recent_turns=lambda: [],
    )

    assert outcome.status == "clarify"
    assert outcome.reason == "vague_input"
    # The audit route carries the POST-carry-over state, read at decline time.
    assert audit.route_json["filters"] == {"normalized_name": "albuterol sulfate"}
    assert audit.route_json["context_applied"] is True
    assert patch.filters == {"normalized_name": "albuterol sulfate"}
    assert patch.update_filters is True
