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
        return _FakeResult(answer="refused", citations=[], refused=True, status="refused")

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


def test_by_category_localizes_a_regression() -> None:
    """The point of stratifying: say WHICH kind of question broke.

    Both categories have one answerable item; only the table one misses. The
    aggregate reports 0.5 and names nothing.
    """
    gold = [
        GoldItem(
            question="table q",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="table",
        ),
        GoldItem(
            question="exception q",
            expected_sources=[{"short_name": "PSG_001", "page": 9}],
            category="exception",
        ),
    ]

    def _ask(q: str) -> _FakeResult:
        page = 7 if q == "table q" else 9  # the table question retrieves the wrong page
        return _FakeResult(
            answer=f"Foo [PSG_001, p.{page}].",
            citations=[_FakeCit("PSG_001", page)],
            refused=False,
            retrieved=[{"short_name": "PSG_001", "page": page, "doc_id": 1}],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.recall_at_k == 0.5
    assert sc.by_category["table"]["recall_at_k"] == 0.0
    assert sc.by_category["exception"]["recall_at_k"] == 1.0
    assert sc.by_category["table"]["n"] == 1


def test_by_category_counts_a_wrong_refusal_as_zero_not_absent() -> None:
    """Denominators must mirror the aggregate, or over-refusal hides per category."""
    gold = [
        GoldItem(
            question="q1",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="table",
        ),
        GoldItem(
            question="q2",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="table",
        ),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "q1":
            return _FakeResult(answer="refused", citations=[], refused=True)
        return _FakeResult(
            answer="Foo [PSG_001, p.3].",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.by_category["table"]["recall_at_k"] == 0.5
    assert sc.by_category["table"]["decision_accuracy"] == 0.5


def test_by_category_reports_decision_accuracy_for_refusals() -> None:
    """A refusal category has no content metrics, only a decision."""
    gold = [GoldItem(question="oos", expected_sources=[], category="refusal", must_refuse=True)]
    sc = evaluate(gold, ask_callable=lambda _q: _FakeResult("", [], refused=True, status="refused"))
    entry = sc.by_category["refusal"]
    assert entry["decision_accuracy"] == 1.0
    assert "recall_at_k" not in entry


def test_by_category_omits_uncategorized_items() -> None:
    """An uncategorized row must not invent a category bucket."""
    gold = [GoldItem(question="q", expected_sources=[{"short_name": "PSG_001", "page": 3}])]
    sc = evaluate(
        gold,
        ask_callable=lambda _q: _FakeResult(
            answer="Foo [PSG_001, p.3].",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
        ),
    )
    assert sc.by_category == {}


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


# --- The withhold policy for must_refuse rows (issue #161, 2026-08-06) --------
#
# A must_refuse row asserts "the system must not answer this". These tests pin
# what does and does not satisfy that, because the whole adjudication turns on
# it: the metric must measure the INV-1 property of the reply, not which status
# string carried it. See metrics.withheld_answer and docs/EVAL_STATUS.md.


def _refusal_gold() -> list[GoldItem]:
    return [GoldItem(question="oos", expected_sources=[], category="refusal", must_refuse=True)]


def test_clarify_without_citations_is_a_withheld_answer() -> None:
    """The shape all 12 seeded-product refusal rows actually return.

    The model declined, the pipeline offered next steps, and the reply carries
    zero citations and no claim about the question. Scoring this as a failed
    refusal was measuring the affordance instead of the invariant.
    """
    sc = evaluate(
        _refusal_gold(),
        ask_callable=lambda _q: _FakeResult(
            answer="You're asking about Budesonide. What would you like to know?",
            citations=[],
            refused=False,
            status="clarify",
            reason="model_refusal",
        ),
    )
    assert sc.refused_correctly == 1
    assert sc.refusal_accuracy == 1.0
    assert sc.by_category["refusal"]["decision_accuracy"] == 1.0


def test_a_clarify_that_cites_is_not_withholding() -> None:
    """The INV-1 failure the metric exists to catch, and the policy's boundary.

    Citations are claims about the corpus. A must_refuse row that produces them
    has answered, whatever its status says, and must score zero.
    """
    sc = evaluate(
        _refusal_gold(),
        ask_callable=lambda _q: _FakeResult(
            answer="The limit is 80-125% [PSG_001, p.3].",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            status="clarify",
            reason="model_refusal",
        ),
    )
    assert sc.refused_correctly == 0
    assert sc.refusal_accuracy == 0.0
    assert sc.by_category["refusal"]["decision_accuracy"] == 0.0


def test_an_answer_to_a_must_refuse_row_scores_zero() -> None:
    sc = evaluate(
        _refusal_gold(),
        ask_callable=lambda _q: _FakeResult(
            answer="Yes, 80.00-125.00%.",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            status="answer",
        ),
    )
    assert sc.refused_correctly == 0
    assert sc.refusal_accuracy == 0.0


def test_scope_warning_is_a_withheld_answer() -> None:
    """The two INV-3 operational-advice rows refuse to advise, not to retrieve."""
    sc = evaluate(
        _refusal_gold(),
        ask_callable=lambda _q: _FakeResult(
            answer="I can't advise on submission strategy.",
            citations=[],
            refused=True,
            status="scope_warning",
            reason="scope_warning",
        ),
    )
    assert sc.refused_correctly == 1
    assert sc.refusal_accuracy == 1.0


def test_a_transport_failure_is_not_scored_as_a_correct_refusal() -> None:
    """The regression that made the metric non-deterministic.

    The error paths build their reply with `_refuse`, so `refused` is True and a
    provider 429 used to count as correct judgment -- which is why the same code
    measured 0.710 on 2026-08-05 and 0.726 on 2026-08-06. A transport failure is
    not a decision: it leaves the denominator and is reported instead.
    """
    gold = [
        *_refusal_gold(),
        GoldItem(
            question="answerable",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="exception",
        ),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "oos":
            return _FakeResult(
                answer="",
                citations=[],
                refused=True,
                status="error",
                reason="provider_error",
            )
        return _FakeResult(
            answer="Foo [PSG_001, p.3].",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.errored == 1
    assert sc.refused_correctly == 0, "a transport failure must not be banked as a refusal"
    # Denominator is 2 - 1 errored = 1: only the answered item scores.
    assert sc.refusal_accuracy == 1.0
    dropped = [d for d in sc.details if d.get("errored")]
    assert len(dropped) == 1 and dropped[0]["errored"] == "provider_error"
    assert "refusal" not in sc.by_category or "decision_accuracy" not in sc.by_category["refusal"]


def test_a_transport_failure_on_an_answerable_row_leaves_the_content_denominators() -> None:
    """The 2026-08-06 rate-limit run, in miniature.

    Five turns came back 429 on ANSWERABLE rows. Each scored recall 0 inside a
    denominator of 43, dragging recall 0.814 -> 0.721 and turning a provider
    outage into what looked like a retrieval regression. Excluding them restores
    the real number -- here, a clean 1.0 over the one row that actually ran.
    """
    gold = [
        GoldItem(
            question="ran",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="exception",
        ),
        GoldItem(
            question="rate_limited",
            expected_sources=[{"short_name": "PSG_002", "page": 1}],
            category="exception",
        ),
    ]

    def _ask(q: str) -> _FakeResult:
        if q == "rate_limited":
            return _FakeResult(
                answer="", citations=[], refused=True, status="error", reason="provider_error"
            )
        return _FakeResult(
            answer="Foo [PSG_001, p.3].",
            citations=[_FakeCit("PSG_001", 3)],
            refused=False,
            retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
        )

    sc = evaluate(gold, ask_callable=_ask)
    assert sc.errored == 1
    assert sc.recall_at_k == 1.0, "the 429 row must not score recall 0"
    assert sc.citation_precision == 1.0
    assert sc.refused_incorrectly == 0, "a transport failure is not an over-refusal"
    assert sc.by_category["exception"]["recall_at_k"] == 1.0


def test_malformed_structure_still_counts_against_the_run() -> None:
    """The boundary: only TRANSPORT failures leave the denominator.

    malformed_structure is the model emitting output the claim gate could not
    admit -- a real quality defect, live at ~12% of production turns. Excluding
    it would hide exactly the thing worth catching, so an answerable row that
    ends this way still scores 0 inside the denominator.
    """
    gold = [
        GoldItem(
            question="q",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="exception",
        )
    ]
    sc = evaluate(
        gold,
        ask_callable=lambda _q: _FakeResult(
            answer="", citations=[], refused=True, status="error", reason="malformed_structure"
        ),
    )
    assert sc.errored == 0, "malformed_structure is a measurement, not a lost turn"
    assert sc.recall_at_k == 0.0
    assert sc.refused_incorrectly == 1


def test_malformed_structure_on_a_must_refuse_row_is_a_withhold() -> None:
    """It made no claim and cited nothing, which is what the row asserts."""
    sc = evaluate(
        _refusal_gold(),
        ask_callable=lambda _q: _FakeResult(
            answer="", citations=[], refused=True, status="error", reason="malformed_structure"
        ),
    )
    assert sc.errored == 0
    assert sc.refused_correctly == 1


def test_a_declined_answerable_row_scores_the_retrieval_that_actually_ran() -> None:
    """A synthesis crash is not a retrieval failure (main went red on this).

    Two rows crashed with malformed_structure AFTER retrieving the expected page
    at rank 1, and recall_at_k reported 0.791 against its 0.80 floor instead of
    0.837. Retrieval either found the page or it did not; what synthesis did
    next belongs to a different metric.
    """
    gold = [
        GoldItem(
            question="crashed",
            expected_sources=[{"short_name": "PSG_001", "page": 3}],
            category="current_version",
        )
    ]
    sc = evaluate(
        gold,
        ask_callable=lambda _q: _FakeResult(
            answer="",
            citations=[],
            refused=True,
            status="error",
            reason="malformed_structure",
            retrieved=[{"short_name": "PSG_001", "page": 3, "doc_id": 1}],
        ),
    )
    assert sc.recall_at_k == 1.0, "retrieval found the expected page; say so"
    assert sc.mrr == 1.0
    # It still failed to deliver: that is charged to the decision bucket.
    assert sc.refused_incorrectly == 1
    assert sc.refusal_accuracy == 0.0
    assert sc.by_category["current_version"]["decision_accuracy"] == 0.0


def test_a_crashed_turn_leaves_the_citation_denominator_but_a_decline_does_not() -> None:
    """The boundary between "produced nothing to judge" and "declined to answer".

    A crashed turn cited nothing because it never finished -- scoring that as
    "cited the wrong things" is not a measurement. A deliberate decline stays in
    the denominator at 0, which is how over-refusal keeps failing the build.
    """
    retrieved = [{"short_name": "PSG_001", "page": 3, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]

    def _gold(q: str) -> list[GoldItem]:
        return [
            GoldItem(question=q, expected_sources=expected, category="table"),
            GoldItem(question="answered", expected_sources=expected, category="table"),
        ]

    def _make(status: str, reason: str) -> Any:
        def _ask(q: str) -> _FakeResult:
            if q == "answered":
                return _FakeResult(
                    answer="Foo [PSG_001, p.3].",
                    citations=[_FakeCit("PSG_001", 3)],
                    refused=False,
                    retrieved=retrieved,
                )
            return _FakeResult(
                answer="",
                citations=[],
                refused=True,
                status=status,
                reason=reason,
                retrieved=retrieved,
            )

        return _ask

    crashed = evaluate(_gold("crashed"), ask_callable=_make("error", "malformed_structure"))
    declined = evaluate(_gold("declined"), ask_callable=_make("refused", "low_top_score"))

    assert crashed.citation_precision == 1.0, "the crashed row has no answer to judge"
    assert declined.citation_precision == 0.5, "a decline cited nothing and is scored for it"
    # Retrieval is measured identically in both: it ran and it worked.
    assert crashed.recall_at_k == declined.recall_at_k == 1.0
