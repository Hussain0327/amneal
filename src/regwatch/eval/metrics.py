"""Eval metrics for the gold set (spec §10.11).

Definitions (kept simple and auditable; no LLM-as-judge for the POC unless we
later flip a flag — for now everything is mechanical):

  - recall@k            : 1 if any retrieved chunk's (doc_id, page) is in the
                          gold expected_sources, else 0. Average over questions.
  - citation_precision  : fraction of an answer's citations that point at an
                          expected source.
  - faithfulness        : fraction of an answer's sentences that carry at
                          least one citation (proxy for ungroundedness).
  - fact_recall         : fraction of an item's expected_facts present in the
                          answer (tolerant substring) — scores answer CONTENT,
                          not just which pages were cited.
  - refusal_accuracy    : fraction of items whose refuse/answer decision was
                          correct.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from regwatch.common.citations import has_citation, strip_sources_trailer

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class GoldItem:
    question: str
    expected_sources: list[dict[str, Any]]
    expected_facts: list[str] = field(default_factory=list)
    must_refuse: bool = False
    # A must_clarify item is correct iff the system asks (status "clarify") rather
    # than guessing — e.g. a multi-form drug that must not blend dosage forms. It
    # folds into refusal_accuracy (the decision-accuracy bucket) like must_refuse,
    # and like must_refuse it does not contribute to recall/precision/faithfulness.
    must_clarify: bool = False


@dataclass
class Scorecard:
    n: int = 0
    recall_at_k: float = 0.0
    citation_precision: float = 0.0
    faithfulness: float = 0.0
    fact_recall: float = 0.0
    refusal_accuracy: float = 0.0
    refused_correctly: int = 0
    clarified_correctly: int = 0
    refused_incorrectly: int = 0
    cited_ungrounded: int = 0
    # must_clarify items whose product is absent from the seeded corpus: not
    # scorable on this corpus (the resolver refuses), so they are excluded from
    # the denominator with a notice rather than counted as a wrong decision. Never
    # a silent pass — the offline gate still hard-gates the clarify behavior.
    skipped: int = 0
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
    # Strip a trailing "Sources" list so we don't penalize bullet citations
    # (shared with grounded_qa's memory-context strip via strip_sources_trailer).
    text = strip_sources_trailer(text)
    sentences = [s.strip() for s in _SENT_RE.split(text) if s.strip()]
    if not sentences:
        return 1.0
    cited = sum(1 for s in sentences if has_citation(s))
    return cited / len(sentences)


def _normalize_for_fact(text: str) -> str:
    """Lowercase, de-hyphenate, collapse whitespace — tolerant matching so
    'single-dose' matches 'single dose' and casing/spacing never causes a miss."""
    return re.sub(r"\s+", " ", (text or "").lower().replace("-", " ")).strip()


def fact_recall(answer_text: str, expected_facts: list[str]) -> float:
    """Fraction of expected facts present (tolerant substring) in the answer.

    Scores answer CONTENT, not just citation pages: the gate now checks that the
    answer actually states the right facts. Empty expected_facts → 1.0 (nothing
    to miss), so fact-less items never drag a score down.
    """
    if not expected_facts:
        return 1.0
    hay = _normalize_for_fact(answer_text)
    matched = sum(1 for f in expected_facts if _normalize_for_fact(f) in hay)
    return matched / len(expected_facts)


def evaluate(
    items: list[GoldItem],
    *,
    ask_callable: Callable[[str], Any],
    k: int = 8,
) -> Scorecard:
    """Run the gold set through `ask_callable` and produce a Scorecard."""
    if not items:
        return Scorecard()
    sums = {"recall": 0.0, "precision": 0.0, "faith": 0.0, "fact": 0.0}
    refusal_correct = 0
    clarify_correct = 0
    refused_incorrectly = 0
    cited_ungrounded = 0
    skipped = 0  # must_clarify items whose product is absent from the corpus
    fact_items = 0  # answered items that actually carry expected_facts
    details: list[dict[str, Any]] = []

    for it in items:
        result = ask_callable(it.question)
        retrieved = result.retrieved
        citations = [c.__dict__ for c in result.citations]

        # Decision accounting (refuse/clarify items don't contribute to
        # recall/precision/faithfulness — they assert WHICH decision is correct).
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
            continue
        if it.must_clarify:
            reason = getattr(result, "reason", None)
            # Corpus-membership gate: a must_clarify item asserts multi-form behavior
            # on a specific product. If that product is absent from the seeded corpus
            # the resolver refuses with reason "no_product" — the item is not testable
            # here, so SKIP it (excluded from the denominator) with an explicit notice
            # rather than scoring it as a wrong decision. This can ONLY fire when the
            # product is genuinely absent: a present multi-form drug clarifies before
            # retrieval, and any other refusal reason still counts against the score,
            # so a real regression is never masked. The clarify behavior itself is
            # hard-gated offline (tests/test_eval_gate.py).
            if result.status == "refused" and reason == "no_product":
                skipped += 1
                details.append(
                    {
                        "q": it.question,
                        "must_clarify": True,
                        "status": result.status,
                        "reason": reason,
                        "skipped": "product_absent_from_corpus",
                    }
                )
                continue
            # Reason-aware scoring: a clarify is correct for a must_clarify item only
            # when it is the MULTI-FORM clarify (not an unrelated did_you_mean /
            # brand_lookup / vague_input clarify) AND every option pins a concrete
            # (dosage_form, route) — i.e. the multi-form guard actually fired.
            options = getattr(result, "clarify", []) or []
            form_pinned = bool(options) and all(
                o.filters and o.filters.get("dosage_form") and o.filters.get("route")
                for o in options
            )
            if result.status == "clarify" and reason == "multi_form" and form_pinned:
                clarify_correct += 1
            details.append(
                {
                    "q": it.question,
                    "must_clarify": True,
                    "status": result.status,
                    "reason": reason,
                    "form_pinned": form_pinned,
                }
            )
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
        # Expected-fact scoring only over items that carry facts, with its own
        # denominator (fact_items) so fact-less answered items don't dilute it.
        fr: float | None = None
        if it.expected_facts:
            fr = fact_recall(result.answer, it.expected_facts)
            sums["fact"] += fr
            fact_items += 1
        details.append(
            {
                "q": it.question,
                "recall": r,
                "citation_precision": p,
                "faithfulness": f,
                "fact_recall": fr,
                "n_citations": len(citations),
            }
        )

    n = len(items)
    decision_expected = sum(1 for it in items if it.must_refuse or it.must_clarify)
    # Denominate content metrics over ALL answerable items (those that should be
    # answered with citations), so a wrongly-refused answerable item scores
    # recall/precision/faithfulness 0 rather than being dropped — otherwise
    # over-refusal is masked. must_clarify joins must_refuse in this exclusion.
    # (Skipped items are must_clarify, so they are already outside `answerable`.)
    answerable = max(1, n - decision_expected)
    correct_non_refusals = (n - decision_expected) - refused_incorrectly
    # refusal_accuracy is the decision-accuracy bucket: a must_refuse that refused,
    # a must_clarify that clarified, and an answerable item that answered all count.
    # Corpus-absent must_clarify items are excluded from the denominator (they are
    # not scorable here), so they neither pass nor fail the gate.
    scored = max(1, n - skipped)
    refusal_accuracy = (refusal_correct + clarify_correct + correct_non_refusals) / scored
    return Scorecard(
        n=n,
        recall_at_k=sums["recall"] / answerable,
        citation_precision=sums["precision"] / answerable,
        faithfulness=sums["faith"] / answerable,
        fact_recall=sums["fact"] / max(1, fact_items),
        refusal_accuracy=refusal_accuracy,
        refused_correctly=refusal_correct,
        clarified_correctly=clarify_correct,
        refused_incorrectly=refused_incorrectly,
        cited_ungrounded=cited_ungrounded,
        skipped=skipped,
        details=details,
    )
