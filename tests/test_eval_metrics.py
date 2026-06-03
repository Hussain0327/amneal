"""Eval-harness metrics: deterministic tests against synthetic results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from regwatch.eval.metrics import (
    GoldItem,
    citation_precision,
    evaluate,
    faithfulness,
    recall_at_k,
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
class _FakeResult:
    answer: str
    citations: list[_FakeCit]
    refused: bool
    model_name: str = "stub"
    audit_id: int = 0
    retrieved: list[dict[str, Any]] = field(default_factory=list)


def test_recall_at_k_match() -> None:
    retrieved = [{"short_name": "PSG_001", "page": 3, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert recall_at_k(retrieved, expected) == 1


def test_recall_at_k_miss() -> None:
    retrieved = [{"short_name": "PSG_001", "page": 7, "doc_id": 1}]
    expected = [{"short_name": "PSG_001", "page": 3}]
    assert recall_at_k(retrieved, expected) == 0


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
