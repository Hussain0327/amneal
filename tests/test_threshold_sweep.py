"""Unit tests for the refusal-threshold revalidation harness.

Pure-Python: a STUB ask_callable returns canned QAResult-like objects carrying
``.retrieved`` (each row a dict with a numeric ``"score"``), ``.refused``,
``.status``, ``.reason``. No LLM, no DB, no network. The stub mirrors the prod
contract: ``retrieved`` is populated (with scores) even on a refusal, and is
empty (``[]``) for a pre-retrieval refusal (None max_score).

These tests must FAIL if the sweep math breaks — they assert exact refuse_recall,
answer_retention, the recommended cutoff lands between the clusters, the
no-retention-loss invariant under overlap, preservation of pre-retrieval
decisions, and that the 0.30 pathology flags fire on the right items.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from regwatch.eval.metrics import GoldItem
from regwatch.eval.threshold_sweep import (
    collect_scores,
    default_candidates,
    recommend,
    sweep,
)


@dataclass
class FakeResult:
    """A QAResult stand-in with only the fields the sweep reads."""

    retrieved: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    status: str = "answer"
    reason: str | None = None


def _retrieved(*scores: float) -> list[dict[str, Any]]:
    """A retrieved list whose rows carry the given cosine scores."""
    return [{"score": s, "short_name": "PSG_X", "page": 1, "doc_id": 1} for s in scores]


def _stub(mapping: dict[str, FakeResult]) -> Callable[[str], FakeResult]:
    """Build an ask_callable from question -> FakeResult."""

    def ask(question: str) -> FakeResult:
        return mapping[question]

    return ask


def _item(
    question: str,
    *,
    must_refuse: bool = False,
    must_clarify: bool = False,
) -> GoldItem:
    # expected_sources empty -> recall_at_k returns 1 (recall_hit True); irrelevant
    # to threshold math, which keys off max_score only.
    return GoldItem(
        question=question,
        expected_sources=[],
        must_refuse=must_refuse,
        must_clarify=must_clarify,
    )


def test_separable_distributions_recommend_cutoff_between_clusters() -> None:
    # must-answer high (0.50-0.70), must-refuse low (0.05-0.20), no overlap.
    mapping = {
        "a1": FakeResult(retrieved=_retrieved(0.70, 0.4)),
        "a2": FakeResult(retrieved=_retrieved(0.55, 0.3)),
        "a3": FakeResult(retrieved=_retrieved(0.50)),
        "r1": FakeResult(retrieved=_retrieved(0.20, 0.1), refused=True, reason="low_top_score"),
        "r2": FakeResult(retrieved=_retrieved(0.05), refused=True, reason="low_top_score"),
    }
    items = [
        _item("a1", must_refuse=False),
        _item("a2", must_refuse=False),
        _item("a3", must_refuse=False),
        _item("r1", must_refuse=True),
        _item("r2", must_refuse=True),
    ]
    rows = collect_scores(items, ask_callable=_stub(mapping))
    # max_score is the per-row best cosine.
    by_q = {r.question: r for r in rows}
    assert by_q["a1"].max_score == 0.70
    assert by_q["r1"].max_score == 0.20

    sw = sweep(rows)
    rec = recommend(rows, sw, current=0.30)

    assert rec.overlap is False
    assert rec.provisional is False
    assert rec.recommended is not None
    # A clean separator must sit strictly between the clusters: above the highest
    # must-refuse (0.20) and at or below the lowest must-answer (0.50).
    assert 0.20 < rec.recommended <= 0.50
    # At the recommendation, perfect separation: refuse all must-refuse, keep all
    # must-answer.
    assert rec.recommended_refuse_recall == 1.0
    assert rec.recommended_answer_retention == 1.0
    # No standing pathologies at 0.30 here (all must-answer >= 0.30, all
    # must-refuse < 0.30).
    assert rec.wrongly_refused_at_current == []
    assert rec.leaking_at_current == []


def test_overlap_keeps_all_must_answer_answered_and_flags_leak() -> None:
    # A must-refuse row (0.45) scores ABOVE a must-answer row (0.35): overlap.
    # 0.35 is the lowest must-answer; at current 0.30 it is answered, and 0.45 is
    # a must-refuse leaking through.
    mapping = {
        "a1": FakeResult(retrieved=_retrieved(0.60)),
        "a2": FakeResult(retrieved=_retrieved(0.35)),
        "r1": FakeResult(retrieved=_retrieved(0.45), refused=False),  # leaks at 0.30
        "r2": FakeResult(retrieved=_retrieved(0.10), refused=True, reason="low_top_score"),
    }
    items = [
        _item("a1", must_refuse=False),
        _item("a2", must_refuse=False),
        _item("r1", must_refuse=True),
        _item("r2", must_refuse=True),
    ]
    rows = collect_scores(items, ask_callable=_stub(mapping))
    sw = sweep(rows)
    rec = recommend(rows, sw, current=0.30)

    assert rec.overlap is True
    assert rec.provisional is True
    assert rec.recommended is not None
    # The invariant under test: the recommendation must NOT lose answer retention
    # vs current — every currently-answered must-answer item stays answered.
    assert rec.recommended_answer_retention is not None
    assert rec.recommended_answer_retention >= rec.current_answer_retention
    # Both must-answer rows are >= 0.30, so current retention is 1.0 and the
    # recommendation must keep it 1.0 -> the cutoff cannot exceed 0.35.
    assert rec.current_answer_retention == 1.0
    assert rec.recommended_answer_retention == 1.0
    assert rec.recommended <= 0.35
    # The leak is flagged: r1 (0.45 >= 0.30) is a must-refuse answered at current.
    assert "r1" in rec.leaking_at_current
    assert "r2" not in rec.leaking_at_current


def test_none_score_must_refuse_counts_as_refused_at_every_threshold() -> None:
    # Pre-retrieval refusal: retrieved == [] -> max_score None -> refuses at all t.
    mapping = {
        "a1": FakeResult(retrieved=_retrieved(0.55)),
        "rn": FakeResult(retrieved=[], refused=True, status="refused", reason="no_product"),
    }
    items = [
        _item("a1", must_refuse=False),
        _item("rn", must_refuse=True),
    ]
    rows = collect_scores(items, ask_callable=_stub(mapping))
    rn = next(r for r in rows if r.question == "rn")
    assert rn.max_score is None
    assert rn.n_retrieved == 0

    sw = sweep(rows)
    # The None-score must_refuse row contributes a refusal at EVERY candidate, so
    # refuse_recall is 1.0 across the whole grid (it is the only must_refuse row).
    for point in sw.curve:
        assert point.refuse_recall == 1.0
    # It never appears as a leak at 0.30 (None is not >= 0.30).
    rec = recommend(rows, sw, current=0.30)
    assert rec.leaking_at_current == []
    assert rec.recommended is None
    assert rec.provisional is True
    assert "no scored must-refuse rows" in rec.rationale


def test_none_score_clarification_is_not_relabelled_as_refusal() -> None:
    mapping = {
        "answer": FakeResult(retrieved=_retrieved(0.55)),
        "clarified_negative": FakeResult(
            retrieved=[],
            refused=False,
            status="clarify",
            reason="brand_lookup",
        ),
    }
    items = [
        _item("answer"),
        _item("clarified_negative", must_refuse=True),
    ]

    rows = collect_scores(items, ask_callable=_stub(mapping))
    negative = next(r for r in rows if r.question == "clarified_negative")
    assert negative.max_score is None
    assert negative.refused is False

    sw = sweep(rows, candidates=[0.30])
    assert sw.curve[0].refuse_recall == 0.0
    assert sw.curve[0].decision_accuracy == 0.5


def test_must_clarify_is_excluded_from_threshold_curve() -> None:
    mapping = {
        "answer": FakeResult(retrieved=_retrieved(0.55)),
        "refuse": FakeResult(retrieved=_retrieved(0.10), refused=True),
        "clarify": FakeResult(
            retrieved=[],
            status="clarify",
            reason="multi_form",
        ),
    }
    items = [
        _item("answer"),
        _item("refuse", must_refuse=True),
        _item("clarify", must_clarify=True),
    ]

    rows = collect_scores(items, ask_callable=_stub(mapping))
    clarify = next(r for r in rows if r.question == "clarify")
    assert clarify.must_clarify is True

    sw = sweep(rows, candidates=[0.30])
    assert sw.n_must_answer == 1
    assert sw.n_must_refuse == 1
    assert sw.n_must_clarify == 1
    assert sw.curve[0].answer_retention == 1.0
    assert sw.curve[0].refuse_recall == 1.0
    assert sw.curve[0].decision_accuracy == 1.0


def test_current_030_pathology_flags_wrongly_refused() -> None:
    # A must-answer item at 0.25 is below 0.30 -> already wrongly refused today.
    mapping = {
        "a_low": FakeResult(retrieved=_retrieved(0.25), refused=True, reason="low_top_score"),
        "a_ok": FakeResult(retrieved=_retrieved(0.55)),
        "r1": FakeResult(retrieved=_retrieved(0.10), refused=True, reason="low_top_score"),
    }
    items = [
        _item("a_low", must_refuse=False),
        _item("a_ok", must_refuse=False),
        _item("r1", must_refuse=True),
    ]
    rows = collect_scores(items, ask_callable=_stub(mapping))
    sw = sweep(rows)
    rec = recommend(rows, sw, current=0.30)

    # Pathology (a): a_low (0.25 < 0.30) is flagged wrongly-refused; a_ok is not.
    assert rec.wrongly_refused_at_current == ["a_low"]
    # current_answer_retention reflects the wrongly-refused item: 1 of 2 answered.
    assert rec.current_answer_retention == 0.5
    # No must-refuse leaks here.
    assert rec.leaking_at_current == []
    # The retention floor is the (degraded) current 0.5, so the recommendation may
    # legitimately raise the cutoff, but must never drop retention below 0.5.
    assert rec.recommended_answer_retention is not None
    assert rec.recommended_answer_retention >= 0.5


def test_empty_gold_is_graceful() -> None:
    rows = collect_scores([], ask_callable=_stub({}))
    assert rows == []
    sw = sweep(rows)
    # No rows -> vacuously perfect curve, no distribution stats.
    assert sw.n_must_answer == 0
    assert sw.n_must_refuse == 0
    assert sw.must_answer_stats is not None and sw.must_answer_stats.n_scored == 0
    rec = recommend(rows, sw, current=0.30)
    # Nothing to refuse, nothing to retain, no pathologies.
    assert rec.wrongly_refused_at_current == []
    assert rec.leaking_at_current == []
    assert rec.current_answer_retention == 1.0


def test_curve_metrics_are_exact_on_grid() -> None:
    # Two must-answer (0.40, 0.50), two must-refuse (0.10, 0.35). Hand-check t=0.30.
    mapping = {
        "a1": FakeResult(retrieved=_retrieved(0.40)),
        "a2": FakeResult(retrieved=_retrieved(0.50)),
        "r1": FakeResult(retrieved=_retrieved(0.10), refused=True),
        "r2": FakeResult(retrieved=_retrieved(0.35), refused=False),  # leaks at 0.30
    }
    items = [
        _item("a1", must_refuse=False),
        _item("a2", must_refuse=False),
        _item("r1", must_refuse=True),
        _item("r2", must_refuse=True),
    ]
    rows = collect_scores(items, ask_callable=_stub(mapping))
    sw = sweep(rows, candidates=default_candidates())
    point = next(p for p in sw.curve if abs(p.threshold - 0.30) < 1e-9)
    # At 0.30: must-answer both >= 0.30 -> retention 1.0; must-refuse r1 (0.10)
    # refuses, r2 (0.35) does NOT -> refuse_recall 0.5.
    assert point.answer_retention == 1.0
    assert point.refuse_recall == 0.5
    # decision_accuracy: a1,a2 correct (answer), r1 correct (refuse), r2 wrong
    # (answers) -> 3/4 = 0.75.
    assert point.decision_accuracy == 0.75
