# Evaluation status

**Status:** current operational truth  
**Last verified:** 2026-08-05

This page separates three things that were previously conflated:

1. corpus size,
2. the deterministic offline regression gate, and
3. provider-backed evaluation against the live corpus.

## Current truth

- The committed Q&A gold set has **62 rows**, stratified by question category:
  refusal 16, current_version 14, exact_identifier 11, exception 7,
  duplicate_boilerplate 6, table 5, clarification 3. Every `expected_sources`
  entry carries a verbatim quote, verified present at its pinned
  `(short_name, page)` against the real corpus (`regwatch.eval.verify_gold`,
  enforced by `run_eval` before scoring).
- The committed White Paper gold set has **16 rows**.
- The workstation's ignored `data/raw/` directory contained **1,801 PDFs** when
  inspected on 2026-07-30. That is corpus material, not 1,801 labeled evaluation
  cases, and it is not a portable production-count assertion.
- **The provider-backed gate now runs.** As of 2026-08-05 the Databricks arm is
  configured in CI (Qwen3 embeddings + a Databricks-served LLM), so
  `run_eval --check-thresholds` executes on every build instead of being
  skipped. Before this it had never executed once: the step demanded
  `OPENAI_API_KEY`, which was never a repo secret on either arm.

The offline fixture (`tests/test_eval_gate.py`) remains useful and is not
replaced: it guards the cite/refuse mechanics deterministically on every run,
including when the live arm is unavailable.

## First measured baseline

`eval_run` id=1, 2026-08-05, commit `89320164`. Arm
`ep_2e7368b354d911ea3a013c3125e276c2` (the same profile id production serves),
corpus 66 chunks / 8 documents, digest `2b58b032e512`, `vector_top_k=50`,
`rerank_top_k=8`, reranker off, LLM `workspace.default.regwatch`.

| Metric | Measured | Gate (blocking) | Target (aspirational) |
|---|---:|---:|---:|
| `recall_at_k` | 0.814 | 0.80 | 0.90 |
| `citation_precision` | 0.756 | 0.74 | 0.95 |
| `refusal_accuracy` | 0.710 | *not gated* | 0.95 |
| `mrr` | 0.506 | — | — |
| `faithfulness` | 0.826 | — | — |
| `fact_recall` | 0.622 | — | — |

By category:

| Category | n | recall | mrr | cite prec | decision |
|---|---:|---:|---:|---:|---:|
| table | 5 | 1.000 | 0.580 | 1.000 | 1.000 |
| exact_identifier | 11 | 0.909 | 0.670 | 0.909 | 1.000 |
| current_version | 14 | 0.929 | 0.457 | 0.750 | 0.929 |
| exception | 7 | 0.857 | 0.583 | 0.857 | 1.000 |
| clarification | 3 | — | — | — | 1.000 |
| refusal | 16 | — | — | — | 0.250 |
| duplicate_boilerplate | 6 | 0.167 | 0.167 | 0.167 | 0.167 |

## Gates are a ratchet; targets are not validated acceptance criteria

**0.90 / 0.95 / 0.95 are TARGETS, not thresholds the system has been shown to
meet.** They were written against echo (hash-based) embeddings with
`REFUSAL_SCORE_THRESHOLD=0.0`, and nothing has ever demonstrated they are
reachable on real geometry against this corpus. They are recorded in
`run_eval.TARGETS`, reported in the scorecard's `target` column, and enforced
nowhere.

The BLOCKING floors (`run_eval.THRESHOLDS`) are ratcheted to the first real
measurement, set slightly below it so run-to-run drift from the live LLM
synthesizer does not flake the build red. The gate's current meaning is
**"no worse than the day it was first measured"** — a regression detector, not
a quality bar. Raise the floors as quality improves; never lower one without
recording why here.

### Why `refusal_accuracy` is temporarily not blocking

Its 0.710 is derived from 16 gold rows whose labels are **under dispute**.
Those rows deliberately name real seeded products so the query reaches vector
search and yields a cosine score — the property that makes threshold
calibration possible at all, and the thing the old 12-row set lacked entirely.
The consequence is that the resolver succeeds, evidence comes back weak, and
the system returns a clarification (`qa_clarify reason=model_refusal`) where the
gold set asserts a refusal.

Clarifying may be the better behavior. If it is, 0.250 on that category
measures a labelling disagreement rather than a defect. Gating on it would bake
the disagreement into CI. The metric is still computed, printed, and persisted
to `eval_run`; it simply does not fail the build until the rows are
adjudicated and a labelling policy is written down. Tracked in issue #161.

## Known quality defects (open, not closed by the ratchet)

Ratcheting the gate records reality; it does not fix these.

1. **`duplicate_boilerplate` at 0.167 recall / 0.167 citation precision.** The
   weakest category by a wide margin, and the one built specifically to make the
   deferred duplicate-passage problem measurable before anything touches it.
   215 chunks (3.9%) are exact duplicates in 90 groups, all cross-document.
   Tracked in issue #163.
2. **Router `BadRequestError` on the Databricks LLM path.** Every
   `regwatch.query_guidance` call fails with HTTP 400: the endpoint requires the
   literal word `json` in a *user* message when
   `response_format={"type":"json_object"}` is set, and the guidance prompt's
   JSON instruction lives only in system messages (`GUIDANCE_SCHEMA_MESSAGE` is
   `role="system"`). Impact is degraded next-step guidance, not a wrong
   answer — the refuse/clarify decision is made before the guidance call — but
   it is live in production on the Databricks path. Tracked in issue #162.

## What the 0.917 value actually was

The latest inspected watch run,
[30531864530](https://github.com/Hussain0327/amneal/actions/runs/30531864530),
uploaded a real `threshold-sweep` artifact from the OpenAI-1536 + pgvector path.
The old sweep reported:

- `current_decision_accuracy = 0.916666...`,
- `current_refuse_recall = 1.0`,
- `current_answer_retention = 0.857142...`.

That `0.917` was **not** `run_eval.refusal_accuracy`. The old threshold harness
incorrectly put the one `must_clarify` case in the must-answer bucket. The
system correctly returned `status=clarify`, `reason=multi_form`, with no
retrieval score; the harness then counted that correct clarification as a
threshold-induced answer failure.

The threshold evaluator now excludes `must_clarify` rows from the numeric
cosine-threshold curve.

## What the live threshold artifact does and does not prove

The artifact contained:

| Gold group | Rows | Rows with a cosine score | Observed outcome |
|---|---:|---:|---|
| Must answer | 6 | 6 | all six answered; max scores 0.812 to 0.896 |
| Must refuse | 5 | 0 | all five stopped before vector retrieval |
| Must clarify | 1 | 0 | correctly clarified; excluded from cutoff calibration |

The five refusal rows are useful safety-path evidence, but none supplies a
negative cosine-score example. They were handled by product resolution, brand
lookup, or scope checks before retrieval. Consequently:

- the artifact does **not** calibrate the `0.30` cosine cutoff;
- the old recommendation of `0.00` and "cleanly separable" rationale were
  invalid;
- `REFUSAL_SCORE_THRESHOLD=0.30` remains **provisional**;
- a future sweep must include scored hard negatives that resolve far enough to
  reach vector retrieval.

One gold row labeled `must_refuse` (`zorbifexol`) returned a brand-lookup
clarification rather than a hard refusal. The old sweep treated every no-score
row as refused; the corrected sweep preserves the observed `refused` flag.
Whether that clarification should be accepted requires an explicit gold-policy
decision. It is another reason not to infer a current `run_eval` result from the
threshold artifact.

The corrected sweep returns no recommendation when either the scored
must-answer or scored must-refuse distribution is empty. Resolver refusals do
not establish separation in embedding-score space.

## Required evidence before changing the cutoff

1. ~~Expand the Q&A gold set to at least the planned 30-50 reviewed cases.~~
   **Done 2026-08-05: 62 stratified rows.** Machine-verified (every quote is
   present at its pinned page); still awaiting domain review that the questions
   are well-posed.
2. ~~Add hard negatives that resolve a real application/form/version but still
   lack sufficient citable evidence or target the wrong section.~~
   **Done: 12+ refusal rows name a seeded product and therefore reach vector
   retrieval**, enforced by
   `tests/test_gold_set_integrity.py::test_refusals_include_scored_hard_negatives`.
   Note the open question in "Why `refusal_accuracy` is temporarily not
   blocking" above: these rows produce scored negatives as intended, but whether
   their `must_refuse` label is correct is exactly what needs adjudicating.
3. Run `run_eval --check-thresholds` and the corrected `threshold_sweep` against
   a controlled live-corpus snapshot. (`run_eval` half done 2026-08-05 — see
   "First measured baseline"; the sweep has not been re-run on this arm.)
4. Preserve the artifact with commit, corpus/version snapshot, embedding
   profile, provider/model, and configuration fingerprints.
5. Review retrieval ranks and cite/refuse decisions, not only aggregate
   metrics.
6. Change the threshold only when both positive and negative scored
   distributions support it.

The graph-assisted retrieval proposal in
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md) adds adaptive
evidence expansion. It does not remove the need for a calibrated sufficiency
decision or a representative evaluation set.
