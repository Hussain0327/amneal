"""Eval metrics for the gold set (spec §10.11).

Definitions (kept simple and auditable; no LLM-as-judge for the POC unless we
later flip a flag — for now everything is mechanical):

  - recall@k            : 1 if any retrieved chunk's (doc_id, page) is in the
                          gold expected_sources, else 0. Average over questions.
  - citation_precision  : fraction of an answer's citations that point at an
                          expected source.
  - faithfulness        : fraction of an answer's sentences that carry at
                          least one citation (proxy for ungroundedness).
  - refusal_accuracy    : fraction of items whose refuse/answer decision was
                          correct.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_CITE_RE = re.compile(r"\[([A-Za-z0-9_./-]+),\s*p\.(\d+)\]")


@dataclass
class GoldItem:
    question: str
    expected_sources: list[dict[str, Any]]
    expected_facts: list[str] = field(default_factory=list)
    must_refuse: bool = False


@dataclass
class Scorecard:
    n: int = 0
    recall_at_k: float = 0.0
    citation_precision: float = 0.0
    faithfulness: float = 0.0
    refusal_accuracy: float = 0.0
    refused_correctly: int = 0
    refused_incorrectly: int = 0
    cited_ungrounded: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _match_source(candidate: dict[str, Any], expected: list[dict[str, Any]]) -> bool:
    """A retrieval hit matches if its (short_name OR doc_id) AND page match an expected source."""
    cand_short = candidate.get("short_name")
    cand_doc = candidate.get("doc_id")
    cand_page = candidate.get("page")
    for e in expected:
        if cand_page != e.get("page"):
            continue
        if e.get("short_name") and e["short_name"] == cand_short:
            return True
        if e.get("doc_id") is not None and e["doc_id"] == cand_doc:
            return True
    return False


def recall_at_k(retrieved: list[dict[str, Any]], expected: list[dict[str, Any]]) -> int:
    if not expected:
        return 1
    return 1 if any(_match_source(r, expected) for r in retrieved) else 0


def citation_precision(
    answer_citations: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> float:
    if not answer_citations:
        return 0.0 if expected else 1.0
    matched = sum(1 for c in answer_citations if _match_source(c, expected))
    return matched / len(answer_citations)


def faithfulness(answer_text: str) -> float:
    """Fraction of declarative sentences that carry at least one citation."""
    text = (answer_text or "").strip()
    if not text:
        return 1.0
    # Strip a trailing "Sources" list so we don't penalize bullet citations.
    text = re.split(r"\n\s*Sources:\s*\n", text, maxsplit=1)[0]
    sentences = [s.strip() for s in _SENT_RE.split(text) if s.strip()]
    if not sentences:
        return 1.0
    cited = sum(1 for s in sentences if _CITE_RE.search(s))
    return cited / len(sentences)


def evaluate(
    items: list[GoldItem],
    *,
    ask_callable: Callable[[str], Any],
    k: int = 8,
) -> Scorecard:
    """Run the gold set through `ask_callable` and produce a Scorecard."""
    if not items:
        return Scorecard()
    sums = {"recall": 0.0, "precision": 0.0, "faith": 0.0}
    refusal_correct = 0
    refused_incorrectly = 0
    cited_ungrounded = 0
    details: list[dict[str, Any]] = []

    for it in items:
        result = ask_callable(it.question)
        retrieved = result.retrieved
        citations = [c.__dict__ for c in result.citations]

        # Refusal accounting
        if it.must_refuse:
            if result.refused:
                refusal_correct += 1
            details.append(
                {
                    "q": it.question,
                    "must_refuse": True,
                    "refused": result.refused,
                }
            )
            # Refusal-expected items don't contribute to recall/precision/faithfulness.
            continue
        if result.refused:
            refused_incorrectly += 1
            details.append(
                {
                    "q": it.question,
                    "must_refuse": False,
                    "refused": True,
                }
            )
            continue

        # Standard metrics
        r = recall_at_k(retrieved, it.expected_sources)
        p = citation_precision(citations, it.expected_sources)
        f = faithfulness(result.answer)
        sums["recall"] += r
        sums["precision"] += p
        sums["faith"] += f
        if p < 1.0:
            cited_ungrounded += 1
        details.append(
            {
                "q": it.question,
                "recall": r,
                "citation_precision": p,
                "faithfulness": f,
                "n_citations": len(citations),
            }
        )

    n = len(items)
    non_refusal = max(1, n - sum(1 for it in items if it.must_refuse) - refused_incorrectly)
    refusal_expected = sum(1 for it in items if it.must_refuse)
    correct_non_refusals = (n - refusal_expected) - refused_incorrectly
    refusal_accuracy = (refusal_correct + correct_non_refusals) / n
    return Scorecard(
        n=n,
        recall_at_k=sums["recall"] / non_refusal,
        citation_precision=sums["precision"] / non_refusal,
        faithfulness=sums["faith"] / non_refusal,
        refusal_accuracy=refusal_accuracy,
        refused_correctly=refusal_correct,
        refused_incorrectly=refused_incorrectly,
        cited_ungrounded=cited_ungrounded,
        details=details,
    )
