"""Eval metrics for the gold set (spec §10.11).

Definitions (kept simple and auditable; no LLM-as-judge for the POC unless we
later flip a flag — for now everything is mechanical):

  - recall@k            : 1 if any retrieved chunk's (doc_id, page) is in the
                          gold expected_sources, else 0. Average over questions.
                          Scored from what RETRIEVAL returned, on every
                          answerable row -- including rows the system then
                          declined or failed to synthesize. Retrieval either
                          found the page or it did not; what happened next is
                          the business of refusal_accuracy, not of this metric.
  - mrr                 : reciprocal of the rank of the FIRST expected source in
                          the retrieved list (0 if absent). Average over
                          questions. Recall says the evidence was somewhere in
                          the top k; MRR says how far down. A chunker change that
                          pushes the right passage from rank 1 to rank 7 leaves
                          recall@8 untouched and halves MRR — and rank matters
                          here because only the top `rerank_top_k` passages reach
                          the prompt.
  - citation_precision  : fraction of an answer's citations that point at an
                          expected source. Denominated over rows that PRODUCED
                          an answer: a turn whose synthesis crashed cited
                          nothing because it never finished, and scoring that
                          as "cited the wrong things" is not a measurement.
  - faithfulness        : fraction of the gate's admitted SOURCE_FACT claims
                          that carry a citation (see faithfulness()). Falls
                          back to sentence_citation_rate -- the pre-PR8,
                          per-sentence text rule -- when the caller carries no
                          claim_tags (clarify copy, meta, refusals). Both
                          numbers are reported; only sentence_citation_rate is
                          the historical trend line.
  - fact_recall         : fraction of an item's expected_facts present in the
                          answer (tolerant substring) — scores answer CONTENT,
                          not just which pages were cited.
  - refusal_accuracy    : fraction of items whose withhold/answer decision was
                          correct. A must_refuse item is correct when the answer
                          was WITHHELD (no claims, no citations) in any shape --
                          see withheld_answer(). Turns that errored made no
                          decision and leave the denominator.
  - latency p50/p95     : wall-clock milliseconds per gold question, over the
                          turns that measured something. A retrieval change
                          that lifts recall by 0.01 and doubles p95 is not an
                          improvement, and until this existed the eval could
                          not see it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from regwatch.common.blocks import split_units
from regwatch.common.citations import has_citation, strip_sources_trailer
from regwatch.generate.rag_contract import ClaimTag

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
    # Strings that must NOT appear in the answer. Until this existed the gold
    # set could only express what an answer must CONTAIN, so an answer that
    # added six extra claims -- correct-looking, validly cited, and not asked
    # for -- still scored 1.000 on every metric. That makes any change to
    # answer DEPTH unfalsifiable, which is exactly what it is needed for.
    forbidden: list[str] = field(default_factory=list)


def contains_none(answer: str, forbidden: list[str]) -> bool:
    """True when none of ``forbidden`` appears in ``answer``.

    Case- and hyphen-insensitive, mirroring prompt_eval's check so the two
    negative-assertion surfaces cannot disagree about what "appears" means.
    """
    haystack = answer.lower().replace("-", " ")
    return not any(term.lower().replace("-", " ") in haystack for term in forbidden)


def claim_count(answer: str) -> int:
    """How many CITED sentences the answer rendered.

    One admitted claim renders as exactly one stamped sentence, so this equals
    len(admitted) without reaching into the runtime. Every existing metric is
    claim-count-invariant, so without this a depth change is invisible to the
    eval: a 4-claim answer and a 10-claim answer score identically.
    """
    return sum(1 for s in split_units(strip_sources_trailer(answer)) if has_citation(s))


def _longest_sentence_chars(answer: str) -> int:
    """Longest single sentence in the answer, in characters.

    Split with the SAME block-aware splitter the prose parser and its bounds
    use (``common.blocks.split_units``: a heading, a bullet sentence and a
    table cell are each one unit), so a number read off a scorecard is the
    number ``prose_turn.bounds_exceeded`` would actually compare against. The Sources trailer is stripped first, like ``claim_count``: it is
    appended at render time, not authored by the model, and left in place it
    would register as one enormous sentence.
    """
    body = strip_sources_trailer(answer or "")
    return max((len(s) for s in split_units(body)), default=0)


def _content_tokens(sentence: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", sentence.lower()) if len(t) > 2}


def redundancy(answer: str) -> float:
    """Max pairwise token-Jaccard over the answer's cited sentences.

    Nothing in the gate compares claims to each other, so a model told to
    produce more claims can satisfy that by restating one fact several ways and
    every existing metric still reports 1.000. This is the signal that
    distinguishes "six facts" from "three facts said six times", and it has to
    exist BEFORE any depth target is raised or the change cannot be evaluated.
    """
    sentences = [s for s in split_units(strip_sources_trailer(answer)) if has_citation(s)]
    if len(sentences) < 2:
        return 0.0
    token_sets = [_content_tokens(s) for s in sentences]
    worst = 0.0
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a, b = token_sets[i], token_sets[j]
            union = a | b
            if not union:
                continue
            worst = max(worst, len(a & b) / len(union))
    return worst


def rejection_reasons(details: list[dict[str, Any]]) -> dict[str, int]:
    """Histogram of decline reasons across a run.

    The aggregate scores say quality moved; only this says whether it moved
    because answers got worse or because more turns stopped being answered at
    all. Raising the claim count pressures AVAILABILITY first -- the whole-turn
    materiality guard trips on a single material drop -- so this is the metric
    that should be watched when depth changes.
    """
    counts: dict[str, int] = {}
    for item in details:
        reason = (item.get("trace") or {}).get("reason")
        if reason:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return dict(sorted(counts.items()))


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile over ``values``.

    THE percentile of the eval package: route_shadow_report imports this rather
    than keeping its own copy, so "p95" means one thing wherever it is printed.
    (embedding_benchmark.percentile is deliberately NOT this function -- it
    rounds an index instead of taking a rank, which disagrees here at even n,
    and its recorded diagnostic numbers were produced under that rule.)

    Nearest-rank rather than interpolated: every number returned is a value
    some sample actually took, so a reported p95 names a turn that really ran
    that slowly.

    Args:
        values: The observations. Any order -- a sorted copy is taken.
        pct: Which percentile to read, on a 0..100 scale.

    Returns:
        The percentile, or None when ``values`` is empty. Deliberately not 0.0:
        a fabricated zero reads as an instantaneous turn and would silently
        CLEAR a latency ceiling instead of reporting that nothing was measured
        (same rule as the NULL-not-zero latency column in store/models.py).
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(len(ordered) * pct / 100.0)))
    return float(ordered[rank - 1])


@dataclass
class Scorecard:
    n: int = 0
    recall_at_k: float = 0.0
    mrr: float = 0.0
    citation_precision: float = 0.0
    faithfulness: float = 0.0
    # The pre-PR8 faithfulness definition, kept alongside the redefinition so
    # the historical trend line survives it. Reported, never gated -- see
    # faithfulness()/sentence_citation_rate() below.
    sentence_citation_rate: float = 0.0
    # Admitted SOURCE_FACT claims a caller's claim_tags marked uncited, summed
    # across rows -- the blind spot faithfulness's "no facts -> 1.0" default
    # cannot see on its own. Reported, never gated.
    uncited_source_facts: int = 0
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
    # Answer-shape metrics. Reported, never thresholded yet: a gate on an
    # unmeasured metric is a coin flip. Their job right now is to establish the
    # baseline that a later depth change is judged against.
    mean_claims: float = 0.0
    max_claims: int = 0
    # Answer SIZE, alongside answer shape. The prose arms (v6/v7) admit a
    # sentence of any length -- turn_gate.admit_claims applies no length check
    # since issue #183 -- so a per-sentence cap and a total-response ceiling
    # have to be chosen against the observed distribution rather than guessed.
    # Measured on the rendered answer, which is what a reader actually receives.
    # Reported, never thresholded, same rule as the shape metrics above.
    mean_answer_chars: float = 0.0
    max_answer_chars: int = 0
    max_sentence_chars: int = 0
    redundant_claim_rate: float = 0.0
    forbidden_violations: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    # Items whose TRANSPORT failed (provider 429/5xx/timeout, catalog query), in
    # any bucket. Such a turn measured nothing, so it leaves every denominator
    # and is reported instead — see unmeasured_turn. Two distinct lies this
    # removes: an error was counted as a CORRECT refusal (`_refuse` sets
    # refused=True), which raised the score between two runs of identical code
    # (0.710 -> 0.726); and on an answerable row it scored recall 0, which made
    # a rate-limited CI run look like a retrieval regression (0.814 -> 0.721).
    # run_eval FAILS the build when this gets large: a run that could not
    # measure must never be reported as a run that measured well.
    errored: int = 0
    # Wall-clock milliseconds per gold question, end to end around the ask()
    # call. None when nothing was timed -- see percentile() for why that is not
    # 0.0. Transport-failed turns leave this distribution exactly as they leave
    # every other denominator: a 429 that comes back in 200ms would deflate p95
    # and a timeout would inflate it, and neither number describes the system.
    # Every row still carries its own "latency_ms" in `details`, errored rows
    # included, so an outage remains visible per row.
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    # How many turns the percentiles above were taken over. Printed so "p95 is
    # None" and "p95 was taken over 2 turns" can never look alike.
    latency_samples: int = 0
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


def sentence_citation_rate(answer_text: str) -> float:
    """The PRE-PR8 faithfulness definition, kept as a reported metric.

    Fraction of rendered sentences carrying a marker, renderer-authored
    disclosure lines included. Retained verbatim so the trend line that
    produced every recorded scorecard survives the redefinition and so the
    coincidence claim below is checkable per row rather than asserted.
    """
    text = (answer_text or "").strip()
    if not text:
        return 1.0
    # Strip a trailing "Sources" list so we don't penalize bullet citations
    # (shared with grounded_qa's memory-context strip via strip_sources_trailer).
    text = strip_sources_trailer(text)
    sentences = [s.strip() for s in split_units(text) if s.strip()]
    if not sentences:
        return 1.0
    cited = sum(1 for s in sentences if has_citation(s))
    return cited / len(sentences)


def faithfulness(answer_text: str, *, claim_tags: Sequence[ClaimTag] | None = None) -> float:
    """Fraction of SOURCE_FACT sentences that carry a citation.

    ``claim_tags`` is the gate's per-admitted-claim ledger. Absent (None or
    empty -> every non-gate path: clarify copy, meta, refusals, historical
    callers) the pre-PR8 text rule applies unchanged, so no path loses its
    measurement. Present, the denominator is the sentences the system
    PRESENTED AS FDA FACTS -- which is the only denominator that stays
    meaningful once REASONING/CONVERSATION sentences are uncited BY DESIGN
    (v7). No source facts at all -> 1.0: a turn that asserted nothing about
    the corpus cannot be unfaithful to it. That blind spot is why
    ``uncited_source_facts`` and ``sentence_citation_rate`` are reported.
    """
    if not claim_tags:
        return sentence_citation_rate(answer_text)
    facts = [t for t in claim_tags if t.kind == "source_fact"]
    if not facts:
        return 1.0
    return sum(1 for t in facts if t.cited) / len(facts)


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
    INV-1 failure this metric exists to catch.

    A `malformed_structure` error IS withholding for a must_refuse row: the
    model's output could not be admitted, so nothing was claimed. Only transport
    failures leave the denominator (see unmeasured_turn) -- they say nothing
    about judgment either way.
    """
    status = getattr(result, "status", None)
    if status in ("refused", "scope_warning"):
        return True
    if status == "clarify":
        return not (getattr(result, "citations", None) or [])
    if status == "error":
        # An error reply carries no claims and no citations. It is a withhold in
        # substance; the transport-failure subset never reaches here.
        return not (getattr(result, "citations", None) or [])
    return False


# Reasons that mean THE SYSTEM'S DEPENDENCIES FAILED, not that the model judged
# badly: the synthesizer transport raised (429/5xx/timeout), or the dosage-form
# catalog query did. Deliberately NOT including "malformed_structure" -- that is
# the model emitting output the claim gate could not admit, which is a real
# quality defect and must keep scoring against the run (it is a known live issue
# at ~12% of production turns).
_TRANSPORT_FAILURE_REASONS = frozenset({"provider_error", "catalog_error"})


def unmeasured_turn(result: Any) -> bool:
    """The turn never ran to completion, so it measured nothing.

    A row where the provider returned 429 tells you nothing about retrieval
    quality or about judgment -- yet it used to score recall 0, citation
    precision 0, AND count as a wrong decision, so a rate-limited CI run looked
    exactly like a quality regression. Live proof: on 2026-08-06 two eval jobs
    ran concurrently against the same Databricks workspace, five turns came back
    REQUEST_LIMIT_EXCEEDED, and recall fell 0.814 -> 0.721 with no code change
    to retrieval. Excluding those five restores 0.816.

    Excluded from every denominator and counted in `Scorecard.errored`, which
    run_eval prints and fails the build on when it gets large -- a run that
    could not measure must not be reported as a run that measured well.
    """
    return getattr(result, "status", None) == "error" and (
        getattr(result, "reason", None) in _TRANSPORT_FAILURE_REASONS
    )


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
        # Per-claim (kind, cited), so a reviewer can see why the faithfulness
        # denominator was what it was, row by row -- and so the coincidence
        # proof (A.3) is re-runnable from an artifact, not just asserted.
        "claim_tags": [[t.kind, t.cited] for t in getattr(result, "claim_tags", ())],
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
    sums = {"recall": 0.0, "rr": 0.0, "precision": 0.0, "faith": 0.0, "fact": 0.0, "scr": 0.0}
    uncited_source_facts = 0
    refusal_correct = 0
    clarify_correct = 0
    refused_incorrectly = 0
    cited_ungrounded = 0
    skipped = 0  # must_clarify items whose product is absent from the corpus
    errored = 0  # items whose transport failed: measured nothing, any bucket
    errored_answerable = 0  # the subset that were answerable, for content denominators
    answer_unmeasured = 0  # answerable rows whose synthesis crashed: no answer to judge
    fact_items = 0  # answered items that actually carry expected_facts
    details: list[dict[str, Any]] = []
    latencies_ms: list[float] = []  # one per item, in item order

    for it in items:
        started = perf_counter()
        result = ask_callable(it.question)
        # Wall clock around the WHOLE turn. The retrieval phase is not
        # separable here: ask() is one opaque call and its QAResult carries no
        # phase timings, so splitting retrieval out would mean changing that
        # return contract. Rounded once, at the source, so the p95 the
        # scorecard reports is literally one of the per-row numbers below.
        latencies_ms.append(round((perf_counter() - started) * 1000.0, 3))
        retrieved = result.retrieved
        citations = [c.__dict__ for c in result.citations]
        # Every branch below records this. A scorecard says a metric moved; only
        # the trace says which passages moved it, which is what a reviewer needs
        # to tell a real retrieval regression from a stale expected-source list.
        trace = _trace(result, retrieved, citations)

        # A turn whose transport failed measured nothing, whatever the row
        # expected. Checked BEFORE the expectation branches so the rule is the
        # same for every row: scoping it to decision rows only was incoherent,
        # and the first live run put all five failures on ANSWERABLE rows where
        # it did not apply.
        if unmeasured_turn(result):
            errored += 1
            if not (it.must_refuse or it.must_clarify):
                errored_answerable += 1
            details.append(
                {
                    "q": it.question,
                    "errored": getattr(result, "reason", None) or "error",
                    "trace": trace,
                }
            )
            continue

        # Decision accounting (refuse/clarify items don't contribute to
        # recall/precision/faithfulness — they assert WHICH decision is correct).
        if it.must_refuse:
            # Scored on whether the answer was WITHHELD, not on which status
            # string carried it -- see withheld_answer() and docs/EVAL_STATUS.md.
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
            # RETRIEVAL STILL HAPPENED, and the retrieved list is the evidence of
            # what it found. Scoring recall 0 here because the turn did not go on
            # to answer charges a synthesis or decision failure to retrieval --
            # a category error that cost main a red build on 2026-08-06: two rows
            # crashed with malformed_structure AFTER retrieving the expected page
            # at rank 1, and recall_at_k reported 0.791 instead of 0.837.
            #
            # Over-refusal still cannot hide. It is now charged where it belongs:
            # refused_incorrectly, refusal_accuracy (gated at 0.88) and, for a
            # genuine decline, citation_precision 0 below. A system that refused
            # every row would score refusal_accuracy 0.31 and fail loudly.
            r = recall_at_k(retrieved, it.expected_sources)
            rr = reciprocal_rank(retrieved, it.expected_sources)
            sums["recall"] += r
            sums["rr"] += rr
            # An answer that was never produced cannot be judged for citations or
            # faithfulness. A turn whose SYNTHESIS CRASHED leaves those two
            # denominators (there is nothing to score); a deliberate decline stays
            # in them at 0, because declining to answer an answerable question is
            # exactly the product failure they exist to measure.
            if result.status == "error":
                answer_unmeasured += 1
            # A deliberate decline adds nothing to either sum and stays in the
            # denominator, which scores it 0 -- the existing behavior, kept.
            details.append(
                {
                    "q": it.question,
                    "must_refuse": False,
                    "refused": True,
                    "recall": r,
                    "reciprocal_rank": rr,
                    "answer_unmeasured": result.status == "error",
                    "trace": trace,
                }
            )
            continue

        # Standard metrics
        r = recall_at_k(retrieved, it.expected_sources)
        rr = reciprocal_rank(retrieved, it.expected_sources)
        p = citation_precision(citations, it.expected_sources)
        tags = getattr(result, "claim_tags", None) or None
        f = faithfulness(result.answer, claim_tags=tags)
        scr = sentence_citation_rate(result.answer)
        sums["recall"] += r
        sums["rr"] += rr
        sums["precision"] += p
        sums["faith"] += f
        sums["scr"] += scr
        uncited_source_facts += sum(
            1 for t in (tags or ()) if t.kind == "source_fact" and not t.cited
        )
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
                "sentence_citation_rate": scr,
                "claim_tags": [[t.kind, t.cited] for t in (tags or ())],
                "fact_recall": fr,
                "n_citations": len(citations),
                "claim_count": claim_count(result.answer),
                "redundancy": redundancy(result.answer),
                "forbidden_ok": contains_none(result.answer, it.forbidden),
                # Per-row sizes: the summary fields pick a number, these are
                # how that number is defended against the actual rows.
                "answer_chars": len(result.answer or ""),
                "max_sentence_chars": _longest_sentence_chars(result.answer),
                "trace": trace,
            }
        )

    # Every branch above appends exactly one details entry per item, so the
    # three lists are positionally aligned. Stamping the category and the
    # latency here (rather than in five separate append sites) keeps that
    # invariant in one place.
    for it, detail, elapsed_ms in zip(items, details, latencies_ms, strict=True):
        detail["category"] = it.category
        detail["latency_ms"] = elapsed_ms

    # Errored turns keep their per-row latency above but leave the summary:
    # see Scorecard.latency_p50_ms.
    measured_ms = [ms for ms, d in zip(latencies_ms, details, strict=True) if not d.get("errored")]

    n = len(items)
    decision_expected = sum(1 for it in items if it.must_refuse or it.must_clarify)
    # Denominate content metrics over ALL answerable items (those that should be
    # answered with citations), so a wrongly-refused answerable item scores
    # recall/precision/faithfulness 0 rather than being dropped — otherwise
    # over-refusal is masked. must_clarify joins must_refuse in this exclusion.
    # (Skipped items are must_clarify, so they are already outside `answerable`.)
    # Transport-failed answerable items leave this denominator too: they have no
    # "recall" key, so counting them would score them 0 for a failure that says
    # nothing about retrieval. Over-refusal is untouched by this -- a genuine
    # over-refusal is status "refused", never a transport failure, and still
    # scores 0 inside the denominator.
    answerable = max(1, n - decision_expected - errored_answerable)
    correct_non_refusals = (n - decision_expected - errored_answerable) - refused_incorrectly
    # Citation precision and faithfulness judge an ANSWER. A row whose synthesis
    # crashed produced none, so it leaves their denominator -- scoring it 0 would
    # say "it cited the wrong things" about a turn that cited nothing because it
    # never finished. recall/mrr keep the full denominator: retrieval ran.
    answered = max(1, n - decision_expected - errored_answerable - answer_unmeasured)
    # refusal_accuracy is the decision-accuracy bucket: a must_refuse that WITHHELD
    # an answer, a must_clarify that clarified, and an answerable item that answered
    # all count. Corpus-absent must_clarify items are excluded from the denominator
    # (they are not scorable here) and so is every transport-failed turn, so
    # neither passes nor fails the gate.
    scored = max(1, n - skipped - errored)
    refusal_accuracy = (refusal_correct + clarify_correct + correct_non_refusals) / scored
    # Answer-shape aggregates, over the items that actually produced an answer.
    # A refused item has no claims, and averaging its 0 in would report "claims
    # went down" when what happened was "more turns refused" -- two different
    # regressions that must stay distinguishable.
    shaped = [d for d in details if "claim_count" in d]
    counts = [int(d["claim_count"]) for d in shaped]
    # Same denominator as the shape aggregates: a refused row has no answer to
    # size, and averaging its 0 in would report "answers got shorter" when what
    # happened was "more turns refused".
    answer_chars = [int(d["answer_chars"]) for d in shaped]
    sentence_chars = [int(d["max_sentence_chars"]) for d in shaped]
    return Scorecard(
        n=n,
        recall_at_k=sums["recall"] / answerable,
        # Same denominator as recall_at_k on purpose: a wrongly-refused
        # answerable item contributes 0 to both, so over-refusal cannot inflate
        # MRR by shrinking its own denominator.
        mrr=sums["rr"] / answerable,
        citation_precision=sums["precision"] / answered,
        faithfulness=sums["faith"] / answered,
        # Same denominator as faithfulness, so the two are directly comparable
        # row for row.
        sentence_citation_rate=sums["scr"] / answered,
        uncited_source_facts=uncited_source_facts,
        fact_recall=sums["fact"] / max(1, fact_items),
        refusal_accuracy=refusal_accuracy,
        refused_correctly=refusal_correct,
        clarified_correctly=clarify_correct,
        refused_incorrectly=refused_incorrectly,
        cited_ungrounded=cited_ungrounded,
        skipped=skipped,
        mean_claims=(sum(counts) / len(counts)) if counts else 0.0,
        max_claims=max(counts, default=0),
        mean_answer_chars=((sum(answer_chars) / len(answer_chars)) if answer_chars else 0.0),
        max_answer_chars=max(answer_chars, default=0),
        max_sentence_chars=max(sentence_chars, default=0),
        redundant_claim_rate=(
            (sum(1 for d in shaped if float(d["redundancy"]) >= 0.6) / len(shaped))
            if shaped
            else 0.0
        ),
        forbidden_violations=sum(1 for d in shaped if not d["forbidden_ok"]),
        rejection_reasons=rejection_reasons(details),
        errored=errored,
        latency_p50_ms=percentile(measured_ms, 50),
        latency_p95_ms=percentile(measured_ms, 95),
        latency_samples=len(measured_ms),
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
        answerable = [
            d
            for it, d in pairs
            if not it.must_refuse and not it.must_clarify and not d.get("errored")
        ]
        # Mirrors the aggregate: recall/mrr over every answerable row, citation
        # precision only over rows that produced an answer to judge.
        answered = [d for d in answerable if not d.get("answer_unmeasured")]
        scored = [d for _it, d in pairs if not d.get("skipped") and not d.get("errored")]
        correct = sum(
            1
            for it, d in pairs
            if not d.get("skipped")
            and not d.get("errored")
            and (
                (it.must_refuse and d.get("withheld"))
                or (it.must_clarify and d.get("status") == "clarify" and d.get("form_pinned"))
                # Answered, asked directly rather than inferred from the presence
                # of a "recall" key: refused rows carry one now (retrieval ran
                # and is scored), so that proxy would count a refusal as correct.
                or (not it.must_refuse and not it.must_clarify and not d.get("refused"))
            )
        )
        entry: dict[str, float] = {"n": float(len(pairs))}
        if answerable:
            entry["recall_at_k"] = sum(d.get("recall", 0.0) for d in answerable) / len(answerable)
            entry["mrr"] = sum(d.get("reciprocal_rank", 0.0) for d in answerable) / len(answerable)
            if answered:
                entry["citation_precision"] = sum(
                    d.get("citation_precision", 0.0) for d in answered
                ) / len(answered)
        if scored:
            entry["decision_accuracy"] = correct / len(scored)
        out[cat] = entry
    return out
