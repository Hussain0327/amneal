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
| `refusal_accuracy` | 0.710 as recorded / **0.903 re-scored** | *not gated* | 0.95 |
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
the 0.710 / 0.726 the artifacts recorded). It blocked at a floor of 0.88 for
part of that day; see the next section for why it no longer does.

### `refusal_accuracy` is measured but NOT gated (owner decision, 2026-08-06)

It blocked briefly at 0.88 and was un-gated the same day — **not** for the
earlier "the labels are disputed" reason, which is settled and still enforced by
`metrics.withheld_answer`. The product is moving to a conversational Ask layer
that is not meant to refuse. Gating a system on how often it declines while
deliberately teaching it to decline less would fail the build for doing the new
thing correctly.

The metric and its 16 refusal gold rows are slated for removal from the codebase
once that direction lands. Until then it stays measured, printed and persisted,
so the transition shows up in the record instead of vanishing.

**Read the name carefully before drawing conclusions from it.** `refusal_accuracy`
is decision accuracy over the *whole* gold set, not a refusal rate: 16
`must_refuse` rows + 3 `must_clarify` rows + **43 answerable rows that are
correct only when they actually answer**. Roughly two-thirds of its weight is
answerable rows. On the 2026-08-06 main run it read 0.887 with all 16 refusal
rows correct — the entire shortfall was 7 of 43 answerable rows producing no
answer.

### A synthesis crash is not a retrieval failure

`recall_at_k` and `mrr` are scored from **what retrieval returned**, on every
answerable row — including rows the system then declined or failed to
synthesize. Retrieval either found the expected page or it did not; what
happened afterwards belongs to a different metric.

This was a live red build on main, not a hypothetical. On 2026-08-06 two rows
came back `malformed_structure` **after** retrieving the expected page at rank
1 (`PSG_021730 p.1` at 0.848, `PSG_214070 p.1` at 0.849, 8 and 7 passages each).
The scorer had no `recall` key for those rows, so they counted 0, and
`recall_at_k` reported **0.791** against its 0.80 floor. Scored from the
retrieved lists that were sitting in the trace the whole time, the same run
reads **0.837**. `citation_precision` moves 0.744 → 0.780 for the matching
reason: a turn that crashed cited nothing because it never finished, so it
leaves that denominator rather than being scored as having cited wrongly.

The floor was not lowered to fix this — the attribution was. The same run that
failed at 0.791 passes at 0.837 against the unchanged 0.80.

**Over-refusal still cannot hide.** It is charged where it belongs: a genuine
decline stays in the `citation_precision` denominator at 0, and lands in
`refused_incorrectly`. A system that refused every row would score
`citation_precision` 0 and fail.

### A turn that failed in transport measured nothing

A turn whose **transport** failed — `reason` in `{provider_error,
catalog_error}`: the synthesizer raised 429/5xx/timeout, or the dosage-form
catalog query did — is excluded from **every** denominator and counted as
`errored` in the scorecard and the `eval_run` artifact.

`malformed_structure` is deliberately **not** in that set. That is the model
emitting output the claim gate could not admit — a real quality defect, live at
~12% of production turns — so it keeps scoring against the run.

This closed two distinct lies, both observed:

1. **An error counted as a correct refusal.** The error paths build their reply
   with `_refuse`, so `refused` is `True`. That is why identical code measured
   0.710 on 2026-08-05 and 0.726 on 2026-08-06 — one 400 landed on a refusal row.
2. **An error counted as a retrieval miss.** On an *answerable* row the same
   turn scored recall 0 inside the full denominator. On 2026-08-06 two eval jobs
   ran concurrently against the same Databricks workspace, five turns came back
   `REQUEST_LIMIT_EXCEEDED`, and `recall_at_k` fell 0.814 → 0.721 with no change
   to retrieval anywhere in the diff. Both open PRs went red.

Scoping the rule to decision rows only was incoherent, and the first live run
proved it: all five failures landed on answerable rows, where it did not apply.
It now applies to every row. Replaying that run's recorded per-row outcomes
through the corrected scorer returns every metric to its baseline:

| Metric | As measured (5 × 429) | Replayed, transport failures excluded | Baseline |
|---|---:|---:|---:|
| `recall_at_k` | 0.721 | **0.816** | 0.814 |
| `citation_precision` | 0.674 | **0.763** | 0.756 |
| `refusal_accuracy` | 0.823 | **0.895** | 0.903 |

### A run that could not measure fails differently from one that measured badly

Because transport failures leave the denominators, a bad enough outage could
otherwise shrink the gate to a handful of lucky rows and report green. When more
than `MAX_UNMEASURED_FRACTION` (10%) of turns fail in transport,
`--check-thresholds` exits **3** with a message naming the provider — before it
scores anything — rather than exiting 2 and sending someone hunting a retrieval
bug that is not there. The 2026-08-06 run lost 5 of 62 (8%), under the cap, so
its metrics stand.

**The underlying infrastructure problem is not fixed here.** `databricks-gpt-oss-120b`
is a pay-per-token endpoint with a workspace QPS limit, and two CI eval jobs
running at once exceed it. The durable fixes are a provisioned-throughput
endpoint (a cost decision) or serialising the live-eval job across PRs (a CI
latency decision). Both are owner calls; the gate now merely reports the
condition honestly instead of misattributing it.

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
