# Evaluation status

**Status:** current operational truth  
**Last verified:** 2026-07-30

This page separates three things that were previously conflated:

1. corpus size,
2. the deterministic offline regression gate, and
3. provider-backed evaluation against the live corpus.

## Current truth

- The committed Q&A gold set has **12 rows**:
  - 6 ordinary must-answer questions,
  - 5 `must_refuse` questions,
  - 1 `must_clarify` question.
- The committed White Paper gold set has **16 rows**.
- The workstation's ignored `data/raw/` directory contained **1,801 PDFs** when
  inspected on 2026-07-30. That is corpus material, not 1,801 labeled evaluation
  cases, and it is not a portable production-count assertion.
- The latest CI run inspected, [30559506954](https://github.com/Hussain0327/amneal/actions/runs/30559506954),
  passed `pytest`, including the deterministic offline eval fixture. Its
  provider-backed seed and `run_eval --check-thresholds` steps were skipped
  because the repo-wide `OPENAI_API_KEY` secret was absent.
- Therefore the current provider-backed `run_eval` pass/fail status is
  **unverified**. The repository must not claim that it passes or that it fails.

The offline fixture is useful: it guards the cite/refuse mechanics on every CI
run. It is not evidence that the 12-item gold set passes against the current
production corpus, resolver state, embeddings, and model provider.

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

1. Expand the Q&A gold set to at least the planned 30-50 reviewed cases.
2. Add hard negatives that resolve a real application/form/version but still
   lack sufficient citable evidence or target the wrong section.
3. Run `run_eval --check-thresholds` and the corrected `threshold_sweep` against
   a controlled live-corpus snapshot.
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
