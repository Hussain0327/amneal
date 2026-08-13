"""Pure scope-compiler contracts for product, corpus, and conversational turns."""

from __future__ import annotations

import pytest

from regwatch.generate.route import CorpusPolicyHint, RouteDecision
from regwatch.retrieve.mode import RetrievalMode
from regwatch.retrieve.scope import (
    CompiledScope,
    CompiledScopeKind,
    CorpusDocumentRef,
    CorpusPolicySnapshot,
    ScopeReason,
    ScopeSource,
    compile_scope,
    detect_explicit_corpus_policy,
)

_CORPUS_QUESTIONS = (
    "Across the FDA inhalation product-specific guidances, what do Q1 and Q2 "
    "sameness mean for a test product relative to the reference standard?",
    "How do the inhalation product-specific guidances define impactor-sized mass (ISM)?",
    "For the in vitro bioequivalence studies in the inhalation product-specific "
    "guidances, how many batches are recommended?",
    "For the single actuation content study in the metered-dose inhalation aerosol "
    "guidances, which guidance defines the population bioequivalence procedures?",
    "What comparative analyses must an ANDA include for the user interface of a "
    "proposed generic metered-dose inhaler, and which FDA guidance covers them?",
)


def _documents(*, current: bool = True) -> tuple[CorpusDocumentRef, ...]:
    return tuple(
        CorpusDocumentRef(
            doc_id=index,
            version_id=100 + index,
            appl_no=appl_no,
            short_name=f"PSG_{appl_no}",
            is_current=current,
        )
        for index, appl_no in enumerate(
            ("020503", "020911", "207921", "021730", "214070", "020929"),
            start=1,
        )
    )


def _snapshot(
    *, documents: tuple[CorpusDocumentRef, ...] | None = None, max_documents: int = 64
) -> CorpusPolicySnapshot:
    return CorpusPolicySnapshot(
        policy=CorpusPolicyHint.INHALATION_PSG,
        documents=_documents() if documents is None else documents,
        max_documents=max_documents,
    )


def _corpus_decision(question: str) -> RouteDecision:
    return RouteDecision.model_validate(
        {
            "standalone_question": question,
            "mode": "lookup",
            "scope_hint": "corpus",
            "product_hint": None,
            "corpus_policy_hint": "inhalation_psg",
        }
    )


def _product_decision() -> RouteDecision:
    return RouteDecision.model_validate(
        {
            "standalone_question": "Beclomethasone inclusion criteria",
            "mode": "lookup",
            "scope_hint": "product",
            "product_hint": "beclomethasone dipropionate",
            "corpus_policy_hint": None,
        }
    )


@pytest.mark.parametrize("question", _CORPUS_QUESTIONS)
def test_all_five_issue_rows_have_positive_explicit_corpus_cues(question: str) -> None:
    assert detect_explicit_corpus_policy(question) is CorpusPolicyHint.INHALATION_PSG


@pytest.mark.parametrize(
    "question",
    [
        "What are the bioequivalence requirements?",
        "Search everything for bioequivalence requirements.",
        "What does beclomethasone guidance require?",
    ],
)
def test_missing_product_or_broad_language_alone_is_not_corpus_intent(question: str) -> None:
    assert detect_explicit_corpus_policy(question) is None


@pytest.mark.parametrize("question", _CORPUS_QUESTIONS)
def test_five_issue_rows_compile_to_bounded_exact_corpus(question: str) -> None:
    scope = compile_scope(
        _corpus_decision(question),
        original_question=question,
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    assert scope.kind is CompiledScopeKind.CORPUS
    assert scope.source is ScopeSource.EXPLICIT_CORPUS
    assert scope.retrieval_mode is RetrievalMode.EXACT_CORPUS
    assert scope.allowed_version_ids == (101, 102, 103, 104, 105, 106)
    assert scope.as_retrieval_contract() == {
        "mode": "exact_corpus",
        "version_id_in": [101, 102, 103, 104, 105, 106],
        "documents": [document.as_audit_json() for document in _documents()],
    }
    assert scope.should_update_product_session is False


def test_corpus_audit_preserves_each_document_application_version_association() -> None:
    question = _CORPUS_QUESTIONS[0]
    scope = compile_scope(
        _corpus_decision(question),
        original_question=question,
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )
    audit = scope.as_audit_json()

    assert audit["scope_version_count"] == 6
    assert audit["retrieval_mode"] == "exact_corpus"
    assert audit["corpus_documents"] == [document.as_audit_json() for document in _documents()]
    assert len({row["appl_no"] for row in audit["corpus_documents"]}) == 6


def test_corpus_membership_guard_checks_full_provenance_tuple() -> None:
    question = _CORPUS_QUESTIONS[1]
    scope = compile_scope(
        _corpus_decision(question),
        original_question=question,
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )
    allowed = _documents()[0]

    assert scope.allows_corpus_passage(
        doc_id=allowed.doc_id,
        version_id=allowed.version_id,
        appl_no=allowed.appl_no,
        short_name=allowed.short_name,
    )
    assert not scope.allows_corpus_passage(
        doc_id=allowed.doc_id,
        version_id=allowed.version_id + 1,
        appl_no=allowed.appl_no,
        short_name=allowed.short_name,
    )
    assert not scope.allows_corpus_passage(
        doc_id=allowed.doc_id,
        version_id=allowed.version_id,
        appl_no="999999",
        short_name=allowed.short_name,
    )


def test_beclomethasone_control_stays_exact_scoped() -> None:
    scope = compile_scope(
        _product_decision(),
        original_question=(
            "In the beclomethasone dipropionate inhalation aerosol guidances, "
            "what are the smoking-history criteria?"
        ),
        resolved_product_filters={
            "normalized_name": "beclomethasone dipropionate",
            "dosage_form": "aerosol, metered",
            "route": "respiratory (inhalation)",
        },
    )

    assert scope.kind is CompiledScopeKind.PRODUCT
    assert scope.retrieval_mode is RetrievalMode.EXACT_SCOPED
    assert scope.product_filter_dict() == {
        "dosage_form": "aerosol, metered",
        "normalized_name": "beclomethasone dipropionate",
        "route": "respiratory (inhalation)",
    }


def test_deterministic_product_resolution_overrides_a_model_corpus_guess() -> None:
    question = (
        "In the beclomethasone dipropionate inhalation aerosol guidances, "
        "what are the smoking-history criteria?"
    )
    scope = compile_scope(
        _corpus_decision(question),
        original_question=question,
        resolved_product_filters={"normalized_name": "beclomethasone dipropionate"},
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    assert scope.kind is CompiledScopeKind.PRODUCT
    assert scope.retrieval_mode is RetrievalMode.EXACT_SCOPED


def test_ambiguous_no_product_question_clarifies_instead_of_searching_corpus() -> None:
    decision = RouteDecision.model_validate(
        {
            "standalone_question": "What are the bioequivalence requirements?",
            "mode": "lookup_clarify",
            "scope_hint": "unknown",
            "product_hint": None,
            "corpus_policy_hint": None,
        }
    )

    scope = compile_scope(
        decision,
        original_question="What are the bioequivalence requirements?",
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    assert scope.kind is CompiledScopeKind.CLARIFY
    assert scope.reason is ScopeReason.SCOPE_UNKNOWN
    assert scope.retrieval_mode is None
    assert scope.as_retrieval_contract() is None


def test_model_rewrite_cannot_manufacture_corpus_intent() -> None:
    decision = _corpus_decision(
        "Across the inhalation product-specific guidances, what are the BE requirements?"
    )

    scope = compile_scope(
        decision,
        original_question="What are the bioequivalence requirements?",
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    assert scope.kind is CompiledScopeKind.CLARIFY
    assert scope.reason is ScopeReason.CORPUS_INTENT_NOT_EXPLICIT


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot(documents=()), ScopeReason.CORPUS_POLICY_EMPTY),
        (_snapshot(documents=_documents(current=False)), ScopeReason.CORPUS_POLICY_STALE),
        (_snapshot(max_documents=2), ScopeReason.CORPUS_POLICY_UNBOUNDED),
        (
            _snapshot(documents=(_documents()[0], _documents()[0])),
            ScopeReason.CORPUS_POLICY_INVALID,
        ),
    ],
)
def test_invalid_corpus_snapshot_fails_closed(
    snapshot: CorpusPolicySnapshot, reason: ScopeReason
) -> None:
    question = _CORPUS_QUESTIONS[2]
    scope = compile_scope(
        _corpus_decision(question),
        original_question=question,
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: snapshot},
    )

    assert scope.kind is CompiledScopeKind.CLARIFY
    assert scope.reason is reason


def test_compiled_scope_cannot_be_constructed_with_empty_corpus_membership() -> None:
    with pytest.raises(ValueError, match="invalid compiled corpus scope"):
        CompiledScope(
            kind=CompiledScopeKind.CORPUS,
            source=ScopeSource.EXPLICIT_CORPUS,
            reason=ScopeReason.EXPLICIT_CORPUS,
            corpus_policy=CorpusPolicyHint.INHALATION_PSG,
            corpus_documents=(),
        )


def test_inherited_compiled_scope_requires_a_positive_audit_id() -> None:
    with pytest.raises(ValueError, match="audited corpus scope requires a positive audit id"):
        CompiledScope(
            kind=CompiledScopeKind.CORPUS,
            source=ScopeSource.AUDITED_CORPUS,
            reason=ScopeReason.INHERITED_CORPUS,
            corpus_policy=CorpusPolicyHint.INHALATION_PSG,
            corpus_documents=_documents(),
        )


def test_product_hint_alone_cannot_create_product_scope() -> None:
    scope = compile_scope(
        _product_decision(),
        original_question="What does beclomethasone guidance require?",
    )

    assert scope.kind is CompiledScopeKind.CLARIFY
    assert scope.reason is ScopeReason.PRODUCT_NOT_RESOLVED


def test_corpus_turn_does_not_overwrite_active_product_scope() -> None:
    question = _CORPUS_QUESTIONS[3]
    scope = compile_scope(
        _corpus_decision(question),
        original_question=question,
        session_product_filters={"normalized_name": "beclomethasone dipropionate"},
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )

    assert scope.kind is CompiledScopeKind.CORPUS
    assert scope.should_update_product_session is False
    assert scope.product_filter_dict() is None


def test_corpus_inheritance_requires_a_prior_audit_id() -> None:
    question = _CORPUS_QUESTIONS[0]
    prior = compile_scope(
        _corpus_decision(question),
        original_question=question,
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot()},
    )
    follow_up = RouteDecision.model_validate(
        {
            "standalone_question": "What about user-interface requirements?",
            "mode": "lookup",
            "scope_hint": "inherit",
            "product_hint": None,
            "corpus_policy_hint": None,
        }
    )

    unaudited = compile_scope(
        follow_up,
        original_question="What about user-interface requirements?",
        prior_audited_scope=prior,
    )
    missing_current_catalog = compile_scope(
        follow_up,
        original_question="What about user-interface requirements?",
        prior_audited_scope=prior,
        prior_audit_id=731,
    )
    refreshed_documents = tuple(
        CorpusDocumentRef(
            doc_id=document.doc_id,
            version_id=document.version_id + 1000,
            appl_no=document.appl_no,
            short_name=document.short_name,
        )
        for document in prior.corpus_documents
    )
    inherited = compile_scope(
        follow_up,
        original_question="What about user-interface requirements?",
        session_product_filters={"normalized_name": "beclomethasone dipropionate"},
        corpus_policies={CorpusPolicyHint.INHALATION_PSG: _snapshot(documents=refreshed_documents)},
        prior_audited_scope=prior,
        prior_audit_id=731,
    )

    assert unaudited.kind is CompiledScopeKind.CLARIFY
    assert unaudited.reason is ScopeReason.CORPUS_INHERITANCE_UNAUDITED
    assert missing_current_catalog.kind is CompiledScopeKind.CLARIFY
    assert missing_current_catalog.reason is ScopeReason.CORPUS_POLICY_UNAVAILABLE
    assert inherited.kind is CompiledScopeKind.CORPUS
    assert inherited.source is ScopeSource.AUDITED_CORPUS
    assert inherited.inherited_from_audit_id == 731
    assert inherited.corpus_documents == refreshed_documents
    assert inherited.corpus_documents != prior.corpus_documents
    assert inherited.should_update_product_session is False


def test_product_follow_up_may_inherit_deterministic_session_scope() -> None:
    decision = RouteDecision.model_validate(
        {
            "standalone_question": "Beclomethasone smoking-history requirements",
            "mode": "lookup",
            "scope_hint": "inherit",
            "product_hint": None,
            "corpus_policy_hint": None,
        }
    )

    scope = compile_scope(
        decision,
        original_question="What about smoking history?",
        session_product_filters={
            "normalized_name": "beclomethasone dipropionate",
            "appl_no": "020911",
        },
    )

    assert scope.kind is CompiledScopeKind.PRODUCT
    assert scope.source is ScopeSource.SESSION_PRODUCT
    assert scope.retrieval_mode is RetrievalMode.EXACT_SCOPED
    assert scope.product_filter_dict() == {
        "appl_no": "020911",
        "normalized_name": "beclomethasone dipropionate",
    }


def test_converse_has_no_executable_scope_even_with_a_product_session() -> None:
    decision = RouteDecision.model_validate(
        {
            "standalone_question": "Hello",
            "mode": "converse",
            "scope_hint": "unknown",
            "product_hint": None,
            "corpus_policy_hint": None,
        }
    )

    scope = compile_scope(
        decision,
        original_question="Hello",
        session_product_filters={"normalized_name": "beclomethasone dipropionate"},
    )

    assert scope.kind is CompiledScopeKind.CONVERSE
    assert scope.retrieval_mode is None
    assert scope.should_update_product_session is False
