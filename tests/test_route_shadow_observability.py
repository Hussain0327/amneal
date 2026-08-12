"""The two blind spots that would have made the Checkpoint 3 window under-report.

Both are observation-only. The point of these tests is as much what does NOT
change as what does: the shadow may learn more, but it may never authorize
differently, because ``compile_scope``'s precedence is the live contract PR12
will inherit.
"""

from __future__ import annotations

from typing import Any

from regwatch.generate.route import (
    CorpusPolicyHint,
    RouteDecision,
    ScopeHint,
    TurnMode,
)
from regwatch.retrieve.scope import (
    CompiledScope,
    CompiledScopeKind,
    CorpusDocumentRef,
    ScopeReason,
    ScopeSource,
    compile_scope,
    compiled_scope_from_audit,
    probe_corpus_intent,
)
from regwatch.store.db import session_scope
from regwatch.store.models import QueryLog
from regwatch.store.queries import load_prior_corpus_scope

_CORPUS_QUESTION = (
    "Across the FDA inhalation product specific guidances, what do Q1 and Q2 sameness mean?"
)
_PRODUCT_FILTERS = {"normalized_name": "albuterol sulfate"}


def _corpus_decision(**overrides: Any) -> RouteDecision:
    fields: dict[str, Any] = {
        "mode": TurnMode.LOOKUP,
        "scope_hint": ScopeHint.CORPUS,
        "standalone_question": _CORPUS_QUESTION,
        "product_hint": None,
        "corpus_policy_hint": CorpusPolicyHint.INHALATION_PSG,
    }
    fields.update(overrides)
    return RouteDecision(**fields)


# ---------- blind spot 1: product precedence masking a corpus proposal ----------


def test_product_precedence_still_wins_the_authorization() -> None:
    """The probe must not change what gets authorized. This is the guard rail."""
    compiled = compile_scope(
        _corpus_decision(),
        original_question=_CORPUS_QUESTION,
        resolved_product_filters=_PRODUCT_FILTERS,
    )

    assert compiled.kind is CompiledScopeKind.PRODUCT
    assert compiled.reason is ScopeReason.RESOLVED_PRODUCT


def test_a_masked_corpus_proposal_is_now_visible() -> None:
    """The false negative Checkpoint 3 would otherwise never see.

    A corpus-phrased question that also resolves a drug is authorized as a
    product scope, and the audit row used to say only reason=resolved_product.
    The probe records that corpus intent WAS explicit and would have compiled.
    """
    probe = probe_corpus_intent(
        _corpus_decision(),
        original_question=_CORPUS_QUESTION,
        resolved_product_filters=_PRODUCT_FILTERS,
    )

    assert probe is not None
    assert probe.reached_corpus_branch is False, "product precedence skipped the corpus branch"
    assert probe.detected_policy == CorpusPolicyHint.INHALATION_PSG.value
    assert probe.would_be_reason == ScopeReason.EXPLICIT_CORPUS.value


def test_the_probe_agrees_with_the_compiler_when_the_branch_is_reached() -> None:
    """With no product resolved, the probe must restate the real decision.

    If these ever disagree the probe has become its own policy, which would make
    the window's false-positive count fiction.
    """
    probe = probe_corpus_intent(
        _corpus_decision(), original_question=_CORPUS_QUESTION, resolved_product_filters=None
    )
    compiled = compile_scope(
        _corpus_decision(),
        original_question=_CORPUS_QUESTION,
        resolved_product_filters=None,
        corpus_policies={},
    )

    assert probe is not None
    assert probe.reached_corpus_branch is True
    # The compiler goes on to expand the catalog (empty here, so it clarifies),
    # but the INTENT verdict the probe reports is the one the compiler reached.
    assert probe.would_be_reason == ScopeReason.EXPLICIT_CORPUS.value
    assert compiled.reason is not ScopeReason.CORPUS_INTENT_NOT_EXPLICIT


def test_a_bare_corpus_hint_without_explicit_intent_is_recorded_as_such() -> None:
    """The false POSITIVE side: the model guessed corpus, the text does not say so."""
    probe = probe_corpus_intent(
        _corpus_decision(), original_question="What are the bioequivalence requirements?"
    )

    assert probe is not None
    assert probe.detected_policy is None
    assert probe.would_be_reason == ScopeReason.CORPUS_INTENT_NOT_EXPLICIT.value


def test_the_policy_mismatch_branch_is_unreachable_while_one_policy_exists() -> None:
    """Documents why there is no mismatch test, so nobody writes an impossible one.

    RouteDecision forbids a CORPUS hint with a null corpus_policy_hint
    (route.py:93-95), and CorpusPolicyHint has exactly one member. So a detected
    policy can only ever equal the proposed one, and CORPUS_POLICY_MISMATCH
    cannot be produced through a valid decision. The branch exists for the
    second policy; it mirrors compile_scope, which carries the same dead branch
    for the same reason.
    """
    assert [hint.value for hint in CorpusPolicyHint] == ["inhalation_psg"]


def test_the_probe_stays_quiet_on_turns_it_does_not_apply_to() -> None:
    """No corpus proposal means no probe field, so the audit does not grow noise."""
    product_turn = _corpus_decision(
        scope_hint=ScopeHint.PRODUCT, product_hint="albuterol sulfate", corpus_policy_hint=None
    )
    converse_turn = _corpus_decision(
        mode=TurnMode.CONVERSE, scope_hint=ScopeHint.UNKNOWN, corpus_policy_hint=None
    )

    assert probe_corpus_intent(product_turn, original_question=_CORPUS_QUESTION) is None
    assert probe_corpus_intent(converse_turn, original_question=_CORPUS_QUESTION) is None


# ---------- blind spot 2: the unobservable inheritance leg ----------


def _audited_corpus_scope() -> CompiledScope:
    return CompiledScope(
        kind=CompiledScopeKind.CORPUS,
        source=ScopeSource.EXPLICIT_CORPUS,
        reason=ScopeReason.EXPLICIT_CORPUS,
        corpus_policy=CorpusPolicyHint.INHALATION_PSG,
        corpus_documents=(
            CorpusDocumentRef(doc_id=1, version_id=11, appl_no="020503", short_name="PSG_A"),
            CorpusDocumentRef(doc_id=2, version_id=12, appl_no="020911", short_name="PSG_B"),
        ),
    )


def test_an_audited_corpus_scope_round_trips_through_its_audit_json() -> None:
    """Reconstruction has to be exact or inheritance inherits the wrong set."""
    original = _audited_corpus_scope()

    rebuilt = compiled_scope_from_audit(original.as_audit_json())

    assert rebuilt == original


def test_reconstruction_refuses_anything_that_is_not_a_sound_corpus_scope() -> None:
    """A corrupt audit row degrades to "no prior scope", never to an authorization."""
    sound = _audited_corpus_scope().as_audit_json()

    assert compiled_scope_from_audit(None) is None
    assert compiled_scope_from_audit({}) is None
    assert compiled_scope_from_audit({**sound, "kind": "product"}) is None
    assert compiled_scope_from_audit({**sound, "corpus_documents": []}) is None
    assert compiled_scope_from_audit({**sound, "corpus_policy": "not_a_policy"}) is None
    assert compiled_scope_from_audit({**sound, "source": "nonsense"}) is None
    assert compiled_scope_from_audit({**sound, "corpus_documents": [{"doc_id": 1}]}) is None


def test_inheritance_compiles_once_the_prior_scope_is_supplied() -> None:
    """The leg that was invisible: with an audited prior, INHERIT now works."""
    decision = _corpus_decision(scope_hint=ScopeHint.INHERIT, corpus_policy_hint=None)
    prior = _audited_corpus_scope()

    unaudited = compile_scope(
        decision, original_question="And what about Q3?", prior_audited_scope=None
    )
    assert unaudited.reason is ScopeReason.CORPUS_INHERITANCE_UNAUDITED

    inherited = compile_scope(
        decision,
        original_question="And what about Q3?",
        prior_audited_scope=prior,
        prior_audit_id=1604,
        corpus_policies={
            CorpusPolicyHint.INHALATION_PSG: __import__(
                "regwatch.retrieve.scope", fromlist=["CorpusPolicySnapshot"]
            ).CorpusPolicySnapshot(
                policy=CorpusPolicyHint.INHALATION_PSG, documents=prior.corpus_documents
            )
        },
    )
    assert inherited.kind is CompiledScopeKind.CORPUS
    assert inherited.reason is ScopeReason.INHERITED_CORPUS
    assert inherited.inherited_from_audit_id == 1604


def test_the_loader_finds_the_newest_corpus_turn_in_this_session_only() -> None:
    """Scoped to the session, newest first, and blind to other conversations."""
    scope_json = _audited_corpus_scope().as_audit_json()
    with session_scope() as session:
        session.add_all(
            [
                QueryLog(
                    mode="qa",
                    session_id="s-1",
                    query_text="older corpus turn",
                    answer_text="...",
                    model_name="m",
                    route_json={"route_call": {"compiled_scope": scope_json}},
                ),
                QueryLog(
                    mode="qa",
                    session_id="s-1",
                    query_text="a later product turn",
                    answer_text="...",
                    model_name="m",
                    route_json={
                        "route_call": {"compiled_scope": {"kind": "product", "reason": "resolved"}}
                    },
                ),
                QueryLog(
                    mode="qa",
                    session_id="s-2",
                    query_text="another conversation entirely",
                    answer_text="...",
                    model_name="m",
                    route_json={"route_call": {"compiled_scope": scope_json}},
                ),
            ]
        )

    found = load_prior_corpus_scope("s-1")

    assert found is not None
    assert compiled_scope_from_audit(found.compiled_scope) == _audited_corpus_scope()
    assert load_prior_corpus_scope("s-never-had-one") is None
    assert load_prior_corpus_scope(None) is None
