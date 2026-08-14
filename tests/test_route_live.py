"""PR12: `REGWATCH_ROUTE_CALL=live` gives the route call executable meaning.

Reuses the shadow test harness (``_PipelineProvider``, ``_route_json``,
``_snapshot``, ``_set_route_mode``) from ``test_route_shadow`` so the live
tests exercise the exact same route-request/response shapes the observation
suite already pins.

Every test here would fail if the live-steering wiring in
``grounded_qa._resolve_and_carry_over`` / ``_compile_route_live_scope`` were
reverted: off/shadow prove the OLD (pre-PR12) failure mode still fires
(clarify, not retrieval), and live proves it no longer does.
"""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate import grounded_qa as qa_mod
from regwatch.generate.llm import LLMResponse
from regwatch.generate.route import CorpusPolicyHint
from regwatch.retrieve.resolver import ExternalDrugMatch, Resolution
from tests.test_route_shadow import _PipelineProvider, _route_json, _set_route_mode, _snapshot

_SESSION_PRODUCT = {"normalized_name": "albuterol sulfate"}
# Issue #219 verbatim: no listed _FOLLOW_UP_PREFIXES entry, and none of its
# tokens are in _FOLLOW_UP_PRONOUNS, so _looks_like_follow_up(question) is
# False -- the exact heuristic gap the issue forbids closing by widening those
# lists.
_FOLLOWUP_QUESTION = "what kind of fasting? you mentioned 'similar fasting'"
_STANDALONE_REWRITE = (
    "What kind of fasting is required for the recommended bioequivalence study "
    "for albuterol sulfate, given the prior answer mentioned "
    "similar fasting conditions?"
)


def _apply_common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the resolver/candidate lookups this branch always consults.

    ``candidates=["some other product"]`` keeps the turn OUT of the
    empty_corpus branch (issue #219's real corpus is not empty) so every
    scenario below lands on the same need_product/low_top_score fork the
    heuristic reaches today.
    """
    monkeypatch.setattr(
        qa_mod,
        "resolve_product",
        lambda _q: Resolution(status="none", candidates=["some other product"]),
    )
    monkeypatch.setattr(qa_mod, "suggest_products", lambda _q: [])
    monkeypatch.setattr(
        qa_mod,
        "lookup_external_drug",
        lambda _q: ExternalDrugMatch(corpus_products=[], known_absent=False),
    )
    monkeypatch.setattr(qa_mod, "current_dosage_form_routes", lambda *a, **k: [])


# ---------- (a) live carries the session product through the #219 follow-up ----------


def test_live_carries_session_product_through_a_content_word_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before PR12 this always hit need_product; live must reach retrieval."""
    provider = _PipelineProvider(
        _route_json(
            standalone_question=_STANDALONE_REWRITE,
            mode="lookup",
            scope_hint="inherit",
            product_hint=None,
            corpus_policy_hint=None,
        )
    )
    searches: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    _apply_common_stubs(monkeypatch)

    def _retrieve(query: str, **kwargs: Any) -> list[Any]:
        searches.append((query, dict(kwargs["filters"])))
        return []

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve)
    _set_route_mode(monkeypatch, "live")

    outcome, audit, _patch = qa_mod.ask_core(
        _FOLLOWUP_QUESTION,
        session_id="session-219-live",
        turn_id="turn-219-live",
        load_session_filters=lambda: dict(_SESSION_PRODUCT),
        load_recent_turns=lambda: [],
    )

    # low_top_score (not need_product) proves retrieval actually ran, and the
    # exact standalone rewrite -- not the raw follow-up -- is what was
    # searched. (A low_top_score decline rebuilds route_json from scratch and
    # does not carry the retrieval stage's top-level keys forward, same as
    # the pre-existing retrieval_query_rewritten flag, so retrieval_query_source
    # is asserted from the search call, not from this refusal's audit row.)
    assert outcome.status == "refused"
    assert outcome.reason == "low_top_score"
    assert len(searches) == 1
    assert searches[0][0] == _STANDALONE_REWRITE
    assert searches[0][1] == _SESSION_PRODUCT
    assert audit.route_json["filters"] == _SESSION_PRODUCT
    route_call = audit.route_json["route_call"]
    assert route_call["live_steering"] == {
        "applied": True,
        "outcome": "applied",
        "compiled_kind": "product",
    }


# ---------- (b) route call failure falls back to the heuristic, never fails the turn ----------


def test_live_route_failure_falls_back_to_the_heuristic_and_never_fails_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingProvider:
        name = "raising-router"

        def complete(self, *_a: Any, **_k: Any) -> LLMResponse:
            raise TimeoutError("router endpoint unavailable")

        def stream(self, *_a: Any, **_k: Any) -> Any:
            raise NotImplementedError

    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: _RaisingProvider())
    _apply_common_stubs(monkeypatch)

    def _retrieve_must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a failed route call must never itself reach retrieval")

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve_must_not_run)
    _set_route_mode(monkeypatch, "live")

    outcome, audit, _patch = qa_mod.ask_core(
        _FOLLOWUP_QUESTION,
        session_id="session-219-live-fallback",
        turn_id="turn-219-live-fallback",
        load_session_filters=lambda: dict(_SESSION_PRODUCT),
        load_recent_turns=lambda: [],
    )

    # The turn completed -- no exception escaped ask_core -- and reached
    # exactly the outcome the unmodified heuristic reaches on this phrasing.
    assert outcome.status == "clarify"
    assert outcome.reason == "need_product"
    route_call = audit.route_json["route_call"]
    assert route_call["outcome"] == "provider_error"
    assert route_call["live_steering"] == {
        "applied": False,
        "outcome": "route_call_failed",
        "compiled_kind": None,
    }


def test_live_compile_failure_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route call that SUCCEEDS but a catalog read that blows up must also
    degrade to the heuristic, not crash the turn.
    """
    provider = _PipelineProvider(
        _route_json(
            standalone_question="Across the inhalation guidances, how is ISM defined?",
            mode="lookup",
            scope_hint="corpus",
            product_hint=None,
            corpus_policy_hint="inhalation_psg",
        )
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    _apply_common_stubs(monkeypatch)

    def _broken_catalog() -> Any:
        raise RuntimeError("catalog down")

    monkeypatch.setattr(qa_mod, "load_corpus_policy_snapshots", _broken_catalog)

    def _retrieve_must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a compile failure must never itself reach retrieval")

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve_must_not_run)
    _set_route_mode(monkeypatch, "live")

    outcome, audit, _patch = qa_mod.ask_core(
        "Across the FDA inhalation product specific guidances, how is ISM defined?",
        session_id="session-compile-fail",
        turn_id="turn-compile-fail",
        load_session_filters=lambda: {},
        load_recent_turns=lambda: [],
    )

    assert outcome.status == "clarify"
    assert outcome.reason == "need_product"
    route_call = audit.route_json["route_call"]
    assert route_call["live_steering"] == {
        "applied": False,
        "outcome": "compile_error",
        "compiled_kind": None,
    }


# ---------- (c) shadow: the heuristic still decides, byte-identical to before PR12 ----------


def test_shadow_mode_still_hard_clarifies_the_219_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR12 must not leak into mode=shadow: same route decision as (a), but
    configured_mode=shadow reaches the OLD failure mode, proving the
    heuristic -- not the route call -- is still the decider.
    """
    provider = _PipelineProvider(
        _route_json(
            standalone_question=_STANDALONE_REWRITE,
            mode="lookup",
            scope_hint="inherit",
            product_hint=None,
            corpus_policy_hint=None,
        )
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    _apply_common_stubs(monkeypatch)

    def _retrieve_must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("shadow must not reach retrieval on the #219 phrasing")

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve_must_not_run)
    _set_route_mode(monkeypatch, "shadow")

    outcome, audit, _patch = qa_mod.ask_core(
        _FOLLOWUP_QUESTION,
        session_id="session-219-shadow",
        turn_id="turn-219-shadow",
        load_session_filters=lambda: dict(_SESSION_PRODUCT),
        load_recent_turns=lambda: [],
    )

    assert outcome.status == "clarify"
    assert outcome.reason == "need_product"
    route_call = audit.route_json["route_call"]
    assert "live_steering" not in route_call
    # The compiler still observed a valid inherited product scope -- PR11b's
    # existing behavior -- it is simply never allowed to act on it.
    assert route_call["compiled_scope"]["kind"] == "product"
    assert route_call["agrees_with_scope"] is False


# ---------- (d) off: no route call happens at all ----------


def test_off_mode_never_constructs_a_route_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def _must_not_be_called(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("route_call_mode=off must never construct a router provider")

    monkeypatch.setattr(qa_mod, "get_llm_provider", _must_not_be_called)
    _apply_common_stubs(monkeypatch)
    _set_route_mode(monkeypatch, "off")

    outcome, audit, _patch = qa_mod.ask_core(
        _FOLLOWUP_QUESTION,
        session_id="session-219-off",
        turn_id="turn-219-off",
        load_session_filters=lambda: dict(_SESSION_PRODUCT),
        load_recent_turns=lambda: [],
    )

    assert outcome.status == "clarify"
    assert outcome.reason == "need_product"
    assert "route_call" not in audit.route_json


# ---------- (e) EXACT_CORPUS: compiled and audited, never executed ----------


def test_live_corpus_scope_is_compiled_but_never_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR12's EXACT_CORPUS slice is compile + audit, not execution.

    Real corpus-wide retrieval needs a $in-membership translation the
    retriever does not have yet, and the post-retrieval mixed-products guard
    is not scope-aware yet (that is PR13's job). Until both land, a live turn
    that resolves a valid, catalog-backed corpus scope must fall through to
    today's deterministic path exactly like shadow does -- proving an unsafe
    (unbounded or ungated) corpus authorization cannot execute.
    """
    question = (
        "Across the FDA inhalation product specific guidances, what do Q1 and " "Q2 sameness mean?"
    )
    provider = _PipelineProvider(_route_json())  # module default: scope_hint=corpus
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    _apply_common_stubs(monkeypatch)
    monkeypatch.setattr(
        qa_mod,
        "load_corpus_policy_snapshots",
        lambda: {CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    def _retrieve_must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a compiled corpus scope must never reach retrieval in PR12")

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve_must_not_run)
    _set_route_mode(monkeypatch, "live")

    outcome, audit, _patch = qa_mod.ask_core(
        question,
        session_id="session-corpus-live",
        turn_id="turn-corpus-live",
        load_session_filters=lambda: {},
        load_recent_turns=lambda: [],
    )

    assert outcome.status == "clarify"
    assert outcome.reason == "need_product"
    assert audit.route_json["filters"] == {}
    route_call = audit.route_json["route_call"]
    assert route_call["compiled_scope"]["kind"] == "corpus"  # observed, catalog-backed
    assert route_call["compiled_scope"]["scope_version_count"] == 2
    assert route_call["live_steering"] == {
        "applied": False,
        "outcome": "compiled_corpus",
        "compiled_kind": "corpus",
    }


def test_live_never_carries_a_product_when_a_brand_or_fuzzy_candidate_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anti-leak guard still wins over an inherit hint in live mode.

    A route decision proposing inherit must not override the did-you-mean /
    brand-lookup guard that exists specifically to catch a question naming a
    DIFFERENT product than the session's.
    """
    provider = _PipelineProvider(
        _route_json(
            standalone_question="What about propranolol dosing?",
            mode="lookup",
            scope_hint="inherit",
            product_hint=None,
            corpus_policy_hint=None,
        )
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: provider)
    monkeypatch.setattr(qa_mod, "resolve_product", lambda _q: Resolution(status="none"))
    monkeypatch.setattr(qa_mod, "suggest_products", lambda _q: ["propranolol hydrochloride"])
    monkeypatch.setattr(
        qa_mod,
        "lookup_external_drug",
        lambda _q: ExternalDrugMatch(corpus_products=[], known_absent=False),
    )

    def _retrieve_must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a leak-guard candidate must block live carry-over")

    monkeypatch.setattr(qa_mod, "retrieve", _retrieve_must_not_run)
    _set_route_mode(monkeypatch, "live")

    outcome, audit, _patch = qa_mod.ask_core(
        "what about propranolol?",
        session_id="session-leak-guard",
        turn_id="turn-leak-guard",
        load_session_filters=lambda: dict(_SESSION_PRODUCT),
        load_recent_turns=lambda: [],
    )

    assert outcome.status == "clarify"
    assert outcome.reason == "did_you_mean"
    route_call = audit.route_json["route_call"]
    assert route_call["live_steering"] == {
        "applied": False,
        "outcome": "leak_guard",
        "compiled_kind": "product",
    }
