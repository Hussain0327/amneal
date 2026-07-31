"""Unit tests for the embedding diagnostic's pure scoring helpers.

These pin the math that turns hits into the reported numbers, and -- just as
importantly -- the cases where those numbers mean nothing and must say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from regwatch.eval.embedding_benchmark import (
    ArmResult,
    compare_arms,
    doc_level_rank,
    load_gold_items,
    percentile,
    rank_of_first_match,
    run_arm,
)


def test_rank_of_first_match_page_level() -> None:
    expected = [{"short_name": "PSG_1", "page": 4}]
    metas = [
        {"short_name": "PSG_2", "page": 4},  # wrong doc
        {"short_name": "PSG_1", "page": 5},  # wrong page
        {"short_name": "PSG_1", "page": 4},  # match at rank 3
    ]
    assert rank_of_first_match(metas, expected) == 3
    assert rank_of_first_match(metas[:2], expected) is None
    assert rank_of_first_match([], expected) is None


def test_doc_level_rank() -> None:
    metas = [{"short_name": "PSG_9"}, {"short_name": "PSG_1"}]
    assert doc_level_rank(metas, {"PSG_1", "PSG_3"}) == 2
    assert doc_level_rank(metas, {"PSG_3"}) is None
    assert doc_level_rank([], {"PSG_1"}) is None


def test_percentile_nearest_rank_and_empty() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 50) == 30.0  # round(0.5*3)=2 -> index 2
    assert percentile(values, 95) == 40.0
    assert percentile(values, 0) == 10.0
    assert percentile([], 50) == 0.0


def test_arm_summary_hit_rate_and_mrr() -> None:
    arm = ArmResult(name="x", ranks=[1, None, 2], doc_ranks=[None, 1])
    s = arm.summary()
    assert s["gold_n"] == 3
    assert abs(s["gold_hit_rate"] - 2 / 3) < 1e-9
    assert abs(s["gold_mrr"] - (1.0 + 0.5) / 3) < 1e-9
    assert s["doc_n"] == 2
    assert s["doc_hit_rate"] == 0.5
    assert s["doc_mrr"] == 0.5
    # Empty latency lists report 0.0 rather than crashing.
    assert s["search_p95_ms"] == 0.0


def _summary(**overrides: float) -> dict[str, float | int]:
    base: dict[str, float | int] = {
        "gold_n": 6,
        "gold_hit_rate": 1.0,
        "gold_mrr": 0.9,
        "doc_n": 100,
        "doc_hit_rate": 0.95,
        "doc_mrr": 0.85,
    }
    base.update(overrides)
    return base


def test_comparison_stays_quiet_within_one_item_of_noise() -> None:
    legacy = _summary()
    # Down 1/6 on gold (exactly one item) and 1/100 on doc: inside the noise band.
    profile = _summary(gold_hit_rate=1.0 - 1 / 6, doc_hit_rate=0.94)
    result = compare_arms(legacy, profile)
    assert result.observations == []
    assert not result.degenerate


def test_comparison_reports_moves_beyond_noise() -> None:
    legacy = _summary()
    profile = _summary(doc_hit_rate=0.95 - 2 / 100)  # two items lower
    result = compare_arms(legacy, profile)
    assert any("doc_hit_rate" in note and "lower" in note for note in result.observations)
    assert not result.degenerate


def test_comparison_reports_improvements_too() -> None:
    """A diagnostic that only reported regressions would quietly hide the case
    where the profile arm is better, which is just as much a finding."""
    legacy = _summary()
    profile = _summary(doc_hit_rate=0.95 + 5 / 100)
    result = compare_arms(legacy, profile)
    assert any("doc_hit_rate" in note and "higher" in note for note in result.observations)


def test_comparison_skips_metrics_with_no_data() -> None:
    """A metric with no questions is skipped -- but only while another set still
    carries evidence (here the gold set does)."""
    legacy = _summary(doc_n=0, doc_hit_rate=0.0, doc_mrr=0.0)
    profile = _summary(doc_n=0, doc_hit_rate=0.0, doc_mrr=0.0)
    result = compare_arms(legacy, profile)
    assert result.observations == []
    assert not result.degenerate


@pytest.mark.parametrize(
    "metric",
    ["gold_hit_rate", "gold_mrr", "doc_hit_rate", "doc_mrr"],
)
def test_comparison_reports_a_move_in_every_metric(metric: str) -> None:
    """Each metric must be able to surface on its own; without this, dropping one
    from the checks tuple would silently blind the diagnostic."""
    legacy = _summary()
    n = 6 if metric.startswith("gold") else 100
    profile = _summary(**{metric: float(legacy[metric]) - 5.0 / n})
    result = compare_arms(legacy, profile)
    assert any(metric in note for note in result.observations)


def test_comparison_flags_a_run_that_evaluated_no_questions() -> None:
    """Zero questions drives every delta to 0, which reads exactly like
    'unchanged'. It has to be called out, not averaged into silence."""
    result = compare_arms(ArmResult(name="x").summary(), ArmResult(name="y").summary())
    assert result.degenerate
    assert "measured nothing" in result.degenerate_reason
    assert result.observations == []


def test_comparison_flags_a_baseline_that_retrieved_nothing() -> None:
    """All-miss in both arms (stale gold pages, wiped corpus) is also delta 0."""
    legacy = ArmResult(name="legacy", ranks=[None] * 6, doc_ranks=[None] * 100).summary()
    profile = ArmResult(name="prof", ranks=[None] * 6, doc_ranks=[None] * 100).summary()
    result = compare_arms(legacy, profile)
    assert result.degenerate
    assert "retrieved nothing" in result.degenerate_reason


class _InstructedProvider:
    """Stands in for Qwen3: records which embedding path each call took."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_query(self, query: str) -> list[float]:
        self.calls.append("query")
        return [1.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append("documents")
        return [[0.0, 1.0] for _ in texts]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append("documents")
        return [[0.0, 1.0] for _ in texts]


@dataclass
class _FakeHit:
    metadata: dict[str, Any]
    score: float


def test_run_arm_embeds_questions_through_the_query_path() -> None:
    """Instruction-tuned providers prefix queries; embedding a benchmark question
    through the document path would penalize that arm and hide a real difference."""
    provider = _InstructedProvider()
    hits = [_FakeHit({"short_name": "P", "page": 1}, 0.9)]
    arm = run_arm(
        "fake",
        provider,
        lambda vec, k: hits,
        [{"question": "q?", "expected_sources": [{"short_name": "P", "page": 1}]}],
        ["refuse?"],
        [{"question": "d?", "short_names": {"P"}}],
        8,
    )
    assert provider.calls == ["query", "query", "query"]
    assert arm.ranks == [1]
    assert arm.doc_ranks == [1]


def test_load_gold_items_partitions_by_kind(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        "\n".join(
            [
                '{"question": "a?", "expected_sources": [{"short_name": "P", "page": 1}]}',
                '{"question": "r?", "expected_sources": [], "must_refuse": true}',
                '{"question": "c?", "expected_sources": [], "must_clarify": true}',
                "# comment",
                '{"question": "no-sources?", "expected_sources": []}',
            ]
        )
    )
    answerable, refuse = load_gold_items(gold)
    assert [a["question"] for a in answerable] == ["a?"]
    assert refuse == ["r?"]
