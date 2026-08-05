"""Eval-harness metrics: deterministic tests against synthetic results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from regwatch.eval.metrics import (
    GoldItem,
    citation_precision,
    evaluate,
    fact_recall,
    faithfulness,
    recall_at_k,
    reciprocal_rank,
)


@dataclass
class _FakeCit:
    short_name: str
    page: int
    chunk_id: str = "x"
    doc_id: int = 1
    version_id: int = 1
    source_url: str = "u"
    snippet: str = "s"


@dataclass
class _FakeOpt:
    filters: dict[str, Any]


@dataclass
class _FakeResult:
    answer: str
    citations: list[_FakeCit]
    refused: bool
    model_name: str = "stub"
    audit_id: int = 0
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    status: str = "answer"
    reason: str | None = None
    clarify: list[_FakeOpt] = field(default_factory=list)


def test_recall_at_k_match() -> None:
    retrieved = [{"short_name": "PSG_001", "page": 3, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert recall_at_k(retrieved, expected) == 1


def test_recall_at_k_miss() -> None:
    retrieved = [{"short_name": "PSG_001", "page": 7, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert recall_at_k(retrieved, expected) == 0


def test_reciprocal_rank_first_position() -> None:
    retrieved = [{"short_name": "PSG_001", "page": 3, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert reciprocal_rank(retrieved, expected) == 1.0


def test_reciprocal_rank_third_position() -> None:
    retrieved = [
        {"short_name": "PSG_999", "page": 1, "doc_id": 9},
        {"short_name": "PSG_999", "page": 2, "doc_id": 9},
        {"short_name": "PSG_001", "page": 3, "doc_id": 1},
    ]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert reciprocal_rank(retrieved, expected) == 1 / 3


def test_reciprocal_rank_miss_is_zero() -> None:
    retrieved = [{"short_name": "PSG_001", "page": 7, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert reciprocal_rank(retrieved, expected) == 0.0


def test_reciprocal_rank_no_expected_is_one() -> None:
    """Mirrors recall_at_k: nothing to find cannot be a miss."""
    assert reciprocal_rank([{"short_name": "PSG_001", "page": 3}], []) == 1.0


def test_reciprocal_rank_separates_rank_where_recall_cannot() -> None:
    """The regression MRR exists to catch.

    Both orderings put the expected passage inside the top k, so recall@k is
    blind to the difference. Only rank 1 survives a top-3 prompt cut.
    """
    expected = [{"short_name": "PSG_001", "page": 3}]
    hit = {"short_name": "PSG_001", "page": 3, "doc_id": 1}
    noise = [{"short_name": "PSG_999", "page": i, "doc_id": 9} for i in range(1, 8)]
    top = [hit, *noise]
    bottom = [*noise, hit]
    assert recall_at_k(top, expected) == recall_at_k(bottom, expected) == 1
    assert reciprocal_rank(top, expected) == 1.0
    assert reciprocal_rank(bottom, expected) == 1 / 8


def test_citation_precision_partial() -> None:
    citations = [
        {"short_name": "PSG_001", "page": 3, "doc_id": 1},
        {"short_name": "PSG_999", "page": 9, "doc_id": 9},
    ]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert citation_precision(citations, expected) == 0.5


def test_faithfulness_full() -> None:
    text = "Claim A [PSG_001, p.3]. Claim B [PSG_001, p.4]."
    assert faithfulness(text) == 1.0


def test_faithfulness_partial() -> None:
    text = "Claim A [PSG_001, p.3]. Uncited claim with no source."
    assert faithfulness(text) == 0.5


def test_fact_recall_all_present() -> None:
    text = "Fasting single-dose two-way crossover in vivo study [PSG_001, p.4]."
    assert fact_recall(text, ["fasting", "single-dose", "two-way crossover", "in vivo"]) == 1.0


def test_fact_recall_tolerant_to_hyphen_and_case() -> None:
    # "single-dose" expected; answer says "SINGLE DOSE" (no hyphen, different case).
    assert fact_recall("A SINGLE DOSE crossover study.", ["single-dose"]) == 1.0


def test_fact_recall_partial() -> None:
    assert fact_recall("Fasting study only.", ["fasting", "dissolution"]) == 0.5


def test_fact_recall_empty_is_one() -> None:
    # Items with no expected_facts never drag the score down.
    assert fact_recall("anything at all", []) == 1.0


def test_evaluate_runs_through() -> None:
    gold = [
        GoldItem(
            question="q1",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            must_refuse=False,
        ),
        GoldItem(question="q2 oos", expected_sources=[], must_refuse=True),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "q1":
            return _FakeResult(
                answer="Foo [PSG_001, p.3].",
                citations=[_FakeCit("PSG_001", 3)],
                refused=False,
                retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
            )
        return _FakeResult(answer="refused", citations=[], refused=True)

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.n == 2
    assert sc.recall_at_k == 1.0
    assert sc.citation_precision == 1.0
    assert sc.refusal_accuracy == 1.0
    assert sc.refused_correctly == 1


def test_refusal_accuracy_penalizes_wrong_refusals() -> None:
    gold = [
        GoldItem(
            question="q1",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            must_refuse=False,
        ),
        GoldItem(
            question="q2",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            must_refuse=False,
        ),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "q1":
            # Wrongly refuses a real (non-refusal) question.
            return _FakeResult(answer="refused", citations=[], refused=True)
        return _FakeResult(
            answer="Foo [PSG_001, p.3].",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.n == 2
    assert sc.refused_incorrectly == 1
    # (refusal_correct=0 + (n=2 - refusal_expected=0 - refused_incorrectly=1)) / n=2
    assert sc.refusal_accuracy == 0.5


def test_must_clarify_scored_only_for_multiform_clarify() -> None:
    # A multi-form clarify (reason "multi_form", options pin form+route) is the only
    # clarify that satisfies a must_clarify expectation.
    gold = [GoldItem(question="estradiol", expected_sources=[], must_clarify=True)]

    def _ask(_q: str) -> _FakeResult:
        return _FakeResult(
            answer="which form?",
            citations=[],
            refused=False,
            status="clarify",
            reason="multi_form",
            clarify=[
                _FakeOpt(
                    {"normalized_name": "estradiol", "dosage_form": "Gel", "route": "Transdermal"}
                ),
                _FakeOpt(
                    {"normalized_name": "estradiol", "dosage_form": "Tablet", "route": "Vaginal"}
                ),
            ],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.clarified_correctly == 1
    assert sc.refusal_accuracy == 1.0
    assert sc.skipped == 0


def test_mrr_averages_over_answerable_including_wrong_refusals() -> None:
    """A wrongly-refused answerable item scores 0 and stays in the denominator.

    Otherwise over-refusal would raise MRR: refuse every question you would have
    ranked badly and the average of what remains looks better.
    """
    gold = [
        GoldItem(
            question="q1",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
        ),
        GoldItem(
            question="q2",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
        ),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "q1":
            # Expected passage sits at rank 2 -> rr 0.5.
            return _FakeResult(
                answer="Foo [PSG_001, p.3].",
                citations=[_FakeCit("PSG_001", 3)],
                refused=False,
                retrieved=[
                    {"short_name": "PSG_999", "page": 1, "doc_id": 9},
                    {"short_name": "PSG_001", "page": 3, "doc_id": 1},
                ],
            )
        return _FakeResult(answer="refused", citations=[], refused=True)

    sc = evaluate(gold, ask_callable=_ask)
    # (0.5 + 0.0) / 2 answerable items.
    assert sc.mrr == 0.25
    assert sc.recall_at_k == 0.5


def test_must_clarify_wrong_clarify_reason_not_counted() -> None:
    # A did_you_mean clarify (typo suggestion) must NOT satisfy a multi-form
    # expectation, even though status == "clarify".
    gold = [GoldItem(question="albuteral", expected_sources=[], must_clarify=True)]

    def _ask(_q: str) -> _FakeResult:
        return _FakeResult(
            answer="did you mean albuterol?",
            citations=[],
            refused=False,
            status="clarify",
            reason="did_you_mean",
            clarify=[_FakeOpt({"normalized_name": "albuterol sulfate"})],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.clarified_correctly == 0
    assert sc.refusal_accuracy == 0.0  # n=1, decision_expected=1, skipped=0
    assert sc.skipped == 0


def test_must_clarify_absent_product_is_skipped() -> None:
    # A must_clarify item whose product is absent from the corpus refuses with reason
    # "no_product" → it is SKIPPED (excluded from the denominator), not scored as a
    # wrong decision. The one answerable item still scores normally.
    gold = [
        GoldItem(question="q1", expected_sources=[{"short_name": "PSG_001", "page": 3}]),
        GoldItem(question="estradiol", expected_sources=[], must_clarify=True),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "q1":
            return _FakeResult(
                answer="Foo [PSG_001, p.3].",
                citations=[_FakeCit("PSG_001", 3)],
                refused=False,
                retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
            )
        return _FakeResult(
            answer="refused",
            citations=[],
            refused=True,
            status="refused",
            reason="no_product",
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.skipped == 1
    assert sc.clarified_correctly == 0
    # Skipped item is out of the denominator: only the answered item scores.
    assert sc.refusal_accuracy == 1.0
