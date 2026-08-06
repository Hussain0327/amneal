"""Eval metrics for the gold set (spec §10.11).

Definitions (kept simple and auditable; no LLM-as-judge for the POC unless we
later flip a flag — for now everything is mechanical):

  - recall@k            : 1 if any retrieved chunk's (doc_id, page) is in the
                          gold expected_sources, else 0. Average over questions.
  - mrr                 : reciprocal of the rank of the FIRST expected source in
                          the retrieved list (0 if absent). Average over
                          questions. Recall says the evidence was somewhere in
                          the top k; MRR says how far down. A chunker change that
                          pushes the right passage from rank 1 to rank 7 leaves
                          recall@8 untouched and halves MRR — and rank matters
                          here because only the top `rerank_top_k` passages reach
                          the prompt.
  - citation_precision  : fraction of an answer's citations that point at an
                          expected source.
  - faithfulness        : fraction of an answer's sentences that carry at
                          least one citation (proxy for ungroundedness).
  - fact_recall         : fraction of an item's expected_facts present in the
                          answer (tolerant substring) — scores answer CONTENT,
                          not just which pages were cited.
  - refusal_accuracy    : fraction of items whose withhold/answer decision was
                          correct. A must_refuse item is correct when the answer
                          was WITHHELD (no claims, no citations) in any shape --
                          see withheld_answer(). Turns that errored made no
                          decision and leave the denominator.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from regwatch.common.citations import has_citation, strip_sources_trailer
from regwatch.common.sentences import split_sentences

# Sentence splitting is shared with the turn gate on purpose: the gate admits a
# claim only when it is ONE sentence by this definition, and this metric then
# asserts every sentence carries a citation. Two definitions would let a claim be
# admitted as one sentence and scored as two. See common/sentences.py.

# The question kinds the gold set is stratified across. Canonical list lives here
# so the loader, the reporting breakdown and the integrity gate cannot drift.
#
# Stratified rather than sampled in proportion to query volume: the categories
# that break quietly (an exception clause dropped from a chunk, a superseded
# version bleeding into an answer) are rare in real traffic and catastrophic in
# a regulatory answer, so they are deliberately over-represented.
#
# "historical_comparison" is deliberately ABSENT. It is untestable on any corpus,
# not merely on the CI seed: ingest deletes a document's superseded chunks on every
# revision (pipeline._cleanup_stale_chunks), and production measures 0 of 5,494
# chunks sitting on a superseded version. There is no retrievable evidence of a
# prior version to compare against, so any row in that bucket would silently score
# as a refusal. The current-version invariant it would have tested is covered
# directly, as a synthetic fixture, by tests/test_current_version_retrieval.py.
GOLD_CATEGORIES = (
    "current_version",
    "exact_identifier",
    "table",
    "exception",
    "refusal",
    "clarification",
    "duplicate_boilerplate",
)


@dataclass
class GoldItem:
    question: str
    expected_sources: list[dict[str, Any]]
    expected_facts: list[str] = field(default_factory=list)
    # Which failure mode this item exists to catch. Empty is tolerated by the
    # loader (a malformed asset should fail on shape, not on policy) and
    # rejected by tests/test_gold_set_integrity.py, which owns the policy.
    category: str = ""
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
    mrr: float = 0.0
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
    # Decision-expecting items whose turn ENDED IN AN ERROR (provider transport
    # failure, malformed structure, catalog error). An error is not a judgment:
    # the system never got to choose, so counting it either way is a lie. It was
    # counted as a CORRECT refusal before this — `_refuse` sets refused=True on
    # the error paths — which is how a provider 400 raised the measured refusal
    # score between two runs of identical code (0.710 -> 0.726, eval_run
    # 2026-08-05 vs 2026-08-06). Excluded from the decision denominator and
    # printed, never silently dropped.
    errored: int = 0
    # Per-category breakdown. An aggregate says quality moved; only this says
    # WHERE, and that is the difference between "the re-chunk regressed" and
    # "the re-chunk regressed table questions specifically". Categories absent
    # from the gold set are absent here rather than reported as zero.
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
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


def reciprocal_rank(retrieved: list[dict[str, Any]], expected: list[dict[str, Any]]) -> float:
    """1/rank of the first expected source, 0.0 if none is retrieved.

    `retrieved` MUST already be in the order retrieval returned (best first) —
    it is, because grounded_qa records passages in retrieval order. Sorting or
    de-duplicating it before calling this would silently change the metric.

    Mirrors recall_at_k's empty-expected convention: nothing to find means a
    perfect score, so fact-less or decision-only items never drag the average.
    """
    if not expected:
        return 1.0
    for i, r in enumerate(retrieved, start=1):
        if _match_source(r, expected):
            return 1.0 / i
    return 0.0


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
    sentences = [s.strip() for s in split_sentences(text) if s.strip()]
    if not sentences:
        return 1.0
    cited = sum(1 for s in sentences if has_citation(s))
    return cited / len(sentences)


def normalize_for_fact(text: str) -> str:
    """Lowercase, de-hyphenate, collapse whitespace — tolerant matching so
    'single-dose' matches 'single dose' and casing/spacing never causes a miss.

    PUBLIC because eval/verify_gold.py must apply the IDENTICAL rule when it
    checks that an expected_fact is present in its cited evidence. A verifier
    stricter than the scorer rejects rows that would have scored fine (measured:
    a fact of "non smoking" against text reading "non-smoking"); a looser one
    admits rows the scorer will fail. Either drift is a silent trap, so there is
    exactly one implementation.
    """
    return re.sub(r"\s+", " ", (text or "").lower().replace("-", " ")).strip()


def fact_recall(answer_text: str, expected_facts: list[str]) -> float:
    """Fraction of expected facts present (tolerant substring) in the answer.

    Scores answer CONTENT, not just citation pages: the gate now checks that the
    answer actually states the right facts. Empty expected_facts → 1.0 (nothing
    to miss), so fact-less items never drag a score down.
    """
    if not expected_facts:
        return 1.0
    hay = normalize_for_fact(answer_text)
    matched = sum(1 for f in expected_facts if normalize_for_fact(f) in hay)
    return matched / len(expected_facts)


def withheld_answer(result: Any) -> bool:
    """Did the system decline to answer the question it was asked?

    This is the LABELLING POLICY for `must_refuse` rows, adjudicated 2026-08-06
    (issue #161) and documented in docs/EVAL_STATUS.md. The gold flag asserts
    "the system must not answer this", which is an INV-1 property of the reply --
    no claims, no citations -- not a demand for one particular status string.

    Withholding therefore covers three shapes:
      - "refused"       -- the hard refusal
      - "scope_warning" -- refuses to advise (INV-3 operational-advice rows)
      - "clarify" with ZERO citations -- the model declined and the pipeline
        offered next steps instead. Measured over all 12 seeded-product refusal
        rows: citations [] and an answer that names the product and asks what the
        user wants, containing no claim about the question. That is a withheld
        answer wearing a more useful affordance, and scoring it wrong was
        measuring the affordance rather than the invariant.

    A clarify that carries citations is NOT withholding: citations are claims
    about the corpus, and a must_refuse row that produces them is the exact
    INV-1 failure this metric exists to catch. `status == "error"` is not
    withholding either -- it is not a decision at all, and is excluded from the
    denominator by the caller rather than scored here.
    """
    status = getattr(result, "status", None)
    if status in ("refused", "scope_warning"):
        return True
    if status == "clarify":
        return not (getattr(result, "citations", None) or [])
    return False


def _errored(result: Any) -> bool:
    """The turn ended in a system error, so no decision was ever made."""
    return getattr(result, "status", None) == "error"


_TRACE_PASSAGE_KEYS = ("chunk_id", "doc_id", "version_id", "page", "short_name", "score")
_TRACE_CITATION_KEYS = ("short_name", "page", "chunk_id", "doc_id", "version_id", "score")


def _trace(
    result: Any,
    retrieved: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-question evidence: what was retrieved, what was cited, what was said.

    Snippets and passage text are deliberately excluded -- the ids, pages and
    scores are what make a finding checkable, and the full text would bloat the
    artifact past the point where anyone reads it.
    """
    return {
        "status": getattr(result, "status", None),
        "reason": getattr(result, "reason", None),
        "answer": getattr(result, "answer", "") or "",
        "retrieved": [{k: p.get(k) for k in _TRACE_PASSAGE_KEYS} for p in retrieved],
        "citations": [{k: c.get(k) for k in _TRACE_CITATION_KEYS} for c in citations],
    }


def evaluate(
    items: list[GoldItem],
    *,
    ask_callable: Callable[[str], Any],
    k: int = 8,
) -> Scorecard:
    """Run the gold set through `ask_callable` and produce a Scorecard."""
    if not items:
        return Scorecard()
    sums = {"recall": 0.0, "rr": 0.0, "precision": 0.0, "faith": 0.0, "fact": 0.0}
    refusal_correct = 0
    clarify_correct = 0
    refused_incorrectly = 0
    cited_ungrounded = 0
    skipped = 0  # must_clarify items whose product is absent from the corpus
    errored = 0  # decision-expecting items whose turn ended in a system error
    fact_items = 0  # answered items that actually carry expected_facts
    details: list[dict[str, Any]] = []

    for it in items:
        result = ask_callable(it.question)
        retrieved = result.retrieved
        citations = [c.__dict__ for c in result.citations]
        # Every branch below records this. A scorecard says a metric moved; only
        # the trace says which passages moved it, which is what a reviewer needs
        # to tell a real retrieval regression from a stale expected-source list.
        trace = _trace(result, retrieved, citations)

        # Decision accounting (refuse/clarify items don't contribute to
        # recall/precision/faithfulness — they assert WHICH decision is correct).
        if it.must_refuse:
            # Scored on whether the answer was WITHHELD, not on which status
            # string carried it -- see withheld_answer() and docs/EVAL_STATUS.md.
            if _errored(result):
                errored += 1
                details.append(
                    {
                        "q": it.question,
                        "must_refuse": True,
                        "refused": result.refused,
                        "errored": getattr(result, "reason", None) or "error",
                        "trace": trace,
                    }
                )
                continue
            held = withheld_answer(result)
            if held:
                refusal_correct += 1
            details.append(
                {
                    "q": it.question,
                    "must_refuse": True,
                    "refused": result.refused,
                    "withheld": held,
                    "trace": trace,
                }
            )
            continue
        if it.must_clarify:
            reason = getattr(result, "reason", None)
            # Same rule as must_refuse: an errored turn made no decision.
            if _errored(result):
                errored += 1
                details.append(
                    {
                        "q": it.question,
                        "must_clarify": True,
                        "status": result.status,
                        "reason": reason,
                        "errored": reason or "error",
                        "trace": trace,
                    }
                )
                continue
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
                        "trace": trace,
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
                    "trace": trace,
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
                    "trace": trace,
                }
            )
            continue

        # Standard metrics
        r = recall_at_k(retrieved, it.expected_sources)
        rr = reciprocal_rank(retrieved, it.expected_sources)
        p = citation_precision(citations, it.expected_sources)
        f = faithfulness(result.answer)
        sums["recall"] += r
        sums["rr"] += rr
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
                "reciprocal_rank": rr,
                "citation_precision": p,
                "faithfulness": f,
                "fact_recall": fr,
                "n_citations": len(citations),
                "trace": trace,
            }
        )

    # Every branch above appends exactly one details entry per item, so the two
    # lists are positionally aligned. Stamping the category here (rather than in
    # five separate append sites) keeps that invariant in one place.
    for it, detail in zip(items, details, strict=True):
        detail["category"] = it.category

    n = len(items)
    decision_expected = sum(1 for it in items if it.must_refuse or it.must_clarify)
    # Denominate content metrics over ALL answerable items (those that should be
    # answered with citations), so a wrongly-refused answerable item scores
    # recall/precision/faithfulness 0 rather than being dropped — otherwise
    # over-refusal is masked. must_clarify joins must_refuse in this exclusion.
    # (Skipped items are must_clarify, so they are already outside `answerable`.)
    answerable = max(1, n - decision_expected)
    correct_non_refusals = (n - decision_expected) - refused_incorrectly
    # refusal_accuracy is the decision-accuracy bucket: a must_refuse that WITHHELD
    # an answer, a must_clarify that clarified, and an answerable item that answered
    # all count. Corpus-absent must_clarify items are excluded from the denominator
    # (they are not scorable here) and so are decision items whose turn errored (no
    # decision was made), so neither passes nor fails the gate.
    scored = max(1, n - skipped - errored)
    refusal_accuracy = (refusal_correct + clarify_correct + correct_non_refusals) / scored
    return Scorecard(
        n=n,
        recall_at_k=sums["recall"] / answerable,
        # Same denominator as recall_at_k on purpose: a wrongly-refused
        # answerable item contributes 0 to both, so over-refusal cannot inflate
        # MRR by shrinking its own denominator.
        mrr=sums["rr"] / answerable,
        citation_precision=sums["precision"] / answerable,
        faithfulness=sums["faith"] / answerable,
        fact_recall=sums["fact"] / max(1, fact_items),
        refusal_accuracy=refusal_accuracy,
        refused_correctly=refusal_correct,
        clarified_correctly=clarify_correct,
        refused_incorrectly=refused_incorrectly,
        cited_ungrounded=cited_ungrounded,
        skipped=skipped,
        errored=errored,
        by_category=_by_category(items, details),
        details=details,
    )


def _by_category(
    items: list[GoldItem],
    details: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Break the scorecard down by question category.

    Denominators mirror the aggregate exactly, so a category's numbers can never
    tell a different story from the headline:
      - content metrics average over ANSWERABLE items, counting a wrongly-refused
        one as 0 (it has no "recall" key), so over-refusal cannot hide;
      - decision accuracy counts a correct withhold/clarify/answer alike, and
        excludes corpus-absent skipped items and errored turns from its
        denominator exactly as the aggregate does.
    """
    out: dict[str, dict[str, float]] = {}
    for cat in {it.category for it in items if it.category}:
        pairs = [(it, d) for it, d in zip(items, details, strict=True) if it.category == cat]
        answerable = [d for it, d in pairs if not it.must_refuse and not it.must_clarify]
        scored = [d for _it, d in pairs if not d.get("skipped") and not d.get("errored")]
        correct = sum(
            1
            for it, d in pairs
            if not d.get("skipped")
            and not d.get("errored")
            and (
                (it.must_refuse and d.get("withheld"))
                or (it.must_clarify and d.get("status") == "clarify" and d.get("form_pinned"))
                or (not it.must_refuse and not it.must_clarify and "recall" in d)
            )
        )
        entry: dict[str, float] = {"n": float(len(pairs))}
        if answerable:
            entry["recall_at_k"] = sum(d.get("recall", 0.0) for d in answerable) / len(answerable)
            entry["mrr"] = sum(d.get("reciprocal_rank", 0.0) for d in answerable) / len(answerable)
            entry["citation_precision"] = sum(
                d.get("citation_precision", 0.0) for d in answerable
            ) / len(answerable)
        if scored:
            entry["decision_accuracy"] = correct / len(scored)
        out[cat] = entry
    return out
