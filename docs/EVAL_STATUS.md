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
| `refusal_accuracy` | 0.710 as recorded / **0.903 re-scored** | 0.88 | 0.95 |
| `mrr` | 0.506 | — | — |
| `faithfulness` | 0.826 | — | — |
| `fact_recall` | 0.622 | — | — |

`refusal_accuracy` carries two numbers because the run was re-scored, not
re-run: 0.710 is what the artifact recorded under the old status-string
predicate, 0.903 is the same 62 replies scored under the withhold policy
adjudicated on 2026-08-06 (below). The 2026-08-06 run on `f63ddfd` gives
0.726 / 0.902 the same way.

By category:

| Category | n | recall | mrr | cite prec | decision |
|---|---:|---:|---:|---:|---:|
| table | 5 | 1.000 | 0.580 | 1.000 | 1.000 |
| exact_identifier | 11 | 0.909 | 0.670 | 0.909 | 1.000 |
| current_version | 14 | 0.929 | 0.457 | 0.750 | 0.929 |
| exception | 7 | 0.857 | 0.583 | 0.857 | 1.000 |
| clarification | 3 | — | — | — | 1.000 |
| refusal | 16 | — | — | — | 0.250 as recorded / **1.000 re-scored** |
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

### The refusal labelling policy (adjudicated 2026-08-06, issue #161)

**A `must_refuse` row asserts that the system must not ANSWER the question. It
does not assert which status string the reply wears.** A row is scored correct
when the answer was *withheld*: the reply makes no claim about the question and
carries **zero citations**. Three shapes satisfy that, and they are the three
the pipeline actually produces:

| Outcome | Withheld? | Why |
|---|---|---|
| `status="refused"` (any reason) | yes | the hard refusal |
| `status="scope_warning"` | yes | refuses to advise (INV-3 rows) |
| `status="clarify"` with **no** citations | yes | declined, then offered next steps |
| `status="clarify"` **with** citations | **no** | citations are claims; this is the INV-1 failure the metric exists to catch |
| `status="answer"` / `"summary"` | **no** | it answered |
| `status="error"` | **not measured** | see below |

The rule is implemented once, in `metrics.withheld_answer`, and is the boundary
the issue asked to be written down: *the reply's INV-1 property, not its
affordance*.

**How it was adjudicated.** Not by argument — by reading the two recorded
scorecard artifacts row by row (the 2026-08-05 CI run and the 2026-08-06 run on
`f63ddfd`) and re-scoring all 62 rows under the policy. The 12 seeded-product
refusal rows come back as `clarify` / `reason=model_refusal` with `citations:
[]` and an answer of the form *"You're asking about Budesonide. FDA has 1
product-specific guidance document for it — what would you like to know?"* —
no claim about extractables, washout periods, or 90% confidence intervals. That
is a withheld answer. Scoring it wrong was measuring the affordance.

**Outcome: no gold row was relabelled.** The labels were right; the scorer was
measuring the wrong property. Under the policy both runs score the refusal
category **16/16 and 15/15** and `refusal_accuracy` **0.903 / 0.902** (versus
the 0.710 / 0.726 the artifacts recorded), and `refusal_accuracy` is blocking
again at a floor of **0.88**.

### Why the floor is 0.88

62 rows, so one row is worth ~1.6 points. Against a measurement of 0.902:

- one refusal row flipping to a real answer → 0.885–0.887, still passes (this is
  the live-LLM drift the ratchet is meant to absorb);
- two → 0.869–0.871, fails.

That is the intended sensitivity: the gate catches the system starting to
**answer** what it must not, and does not flake on a single drifting turn.

### Errored turns are not decisions

A turn that ends in `status="error"` (provider transport failure, malformed
structure, catalog error) is excluded from the `refusal_accuracy` denominator
and reported as `errored` in the scorecard and `eval_run` artifact.

This was a real defect, not housekeeping. The error paths build their reply with
`_refuse`, so `refused` is `True`, and the old scorer banked a provider failure
as a **correct refusal** — which is exactly why identical code measured 0.710 on
2026-08-05 and 0.726 on 2026-08-06 (one `regwatch.query_guidance` 400 landed on
a refusal row). A metric that rises when the provider breaks cannot support a
floor. The count is printed loudly on every run; it is never a silent drop.

## Known quality defects (open, not closed by the ratchet)

Ratcheting the gate records reality; it does not fix these.

1. **`duplicate_boilerplate` at 0.167 recall / 0.167 citation precision.** The
   weakest category by a wide margin. Tracked in issue #163.

   **The recorded traces do not support the duplicate-competition explanation.**
   Reading the per-row traces from both scorecard artifacts: five of the six rows
   returned `status=refused reason=no_product` with **`retrieved: []`** — the
   queries never reached vector search at all. They are phrased corpus-wide
   ("Across the FDA inhalation product-specific guidances…", "How do the
   inhalation product-specific guidances define ISM?") and name no drug, so the
   resolver returns `none` and `ask_core` hard-refuses before retrieval. The one
   row that names a product (beclomethasone) scored **recall 1.0, citation
   precision 1.0** with the expected pages ranked 1 and 3.

   So 0.167 is `1/6` on rows that never got to compete, not evidence that
   duplicate passages crowd out the expected document. The real question this
   category surfaced is a product one: **should a corpus-wide question about
   shared boilerplate be answerable without naming a product?** Today it is not.
   The duplicate-group cap remains deferred Phase 3 work and unstarted; whether
   it is the right fix is now untested, because the measurement that motivated it
   was measuring the resolver gate. Evidence posted to issue #163.

2. ~~**Router `BadRequestError` on the Databricks LLM path.**~~ **Fixed**
   2026-08-06 (issue #162). Every `regwatch.query_guidance` call was failing with
   HTTP 400: the endpoint requires the literal word `json` in a *user* message
   when `response_format={"type":"json_object"}` is set, and the guidance
   prompt's JSON instruction lived only in system messages
   (`GUIDANCE_SCHEMA_MESSAGE` is `role="system"`), which
   `DatabricksProvider._request_messages` folds into a single system turn.
   Fixed at the provider seam (`_ensure_user_json_token`) so every structured
   caller is covered — router guidance, synthesis, BE extraction, change
   summary and all deficiency structured calls — with the prompt texts, and
   therefore the audited prompt-identity hashes, left byte-identical. The
   deficiency structured callers had the same latent defect and are covered by
   the same fix.

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
