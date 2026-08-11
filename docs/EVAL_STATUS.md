# Evaluation status

**Status:** current operational truth. **Last updated:** 2026-08-11

Three different things get called "the eval". They are not the same and this
page keeps them apart:

1. the deterministic offline gate that runs in every pytest run,
2. the live provider-backed gate that runs in CI against a seeded corpus,
3. the by-hand dark runs used to check a new prompt policy before it ships.

## What is measured, and against what

- The Q&A gold set has **62 rows**: refusal 16, current_version 14,
  exact_identifier 11, exception 7, duplicate_boilerplate 6, table 5,
  clarification 3. Every `expected_sources` entry carries a verbatim quote that
  `regwatch.eval.verify_gold` checks against the real corpus before `run_eval`
  scores anything. A gold set that disagrees with the corpus is noise wearing a
  number, so the run stops instead.
- The White Paper gold set has **16 rows**.
- The live eval runs against the **CI seed corpus**, not production. The
  2026-08-05 baseline run measured 66 chunks over 8 documents. Production held
  5,494 chunks when it was checked on 2026-08-11. Do not read a CI eval number
  as a statement about the production corpus.
- The offline fixture gate (`tests/test_eval_gate.py`) drives the real
  `ask()` pipeline over a tiny seeded corpus with a hash-based embedder and a
  faithful LLM stub. It is fast, deterministic, and still the only gate that
  runs when the live arm is unavailable.

### Which arm CI actually runs

`.github/workflows/databricks-eval.yml` is the single live eval lane, and every
live run serializes through one non-canceling concurrency group.

| Entry point | Flags | Blocking? |
|---|---|---|
| called from `ci.yml` on every PR and main | `prose: false`, `selective: false` | yes |
| `workflow_dispatch` | `prose: true` by default, `selective` optional | no, uploads an artifact |

**So the blocking gate measures the v5 claims-JSON arm, while production serves
v7.** The v6 and v7 arms only run when someone dispatches them by hand; their
artifacts upload as `dark-eval-scorecard` and `dark-eval-scorecard-v7`. That gap
is real and worth closing.

The arm is picked at runtime: Databricks when the Qwen3 embedding secrets and
`DATABRICKS_SERVING_RUNTIME_VERSION` are present, the legacy OpenAI arm when
only `OPENAI_API_KEY` is, and skipped with a warning when neither is. The
Databricks arm is the one that matters, because production embeds queries with
Databricks-hosted Qwen3 and a gate running on OpenAI's 1536-dim space never
measured the geometry prod serves.

## First measured baseline

`eval_run` id=1, 2026-08-05, commit `89320164`. Arm
`ep_2e7368b354d911ea3a013c3125e276c2` (the profile id production also serves),
corpus 66 chunks / 8 documents, digest `2b58b032e512`, `vector_top_k=50`,
`rerank_top_k=8`, reranker off, LLM `workspace.default.regwatch`.

| Metric | Measured | Gate (blocking) | Target (aspirational) |
|---|---:|---:|---:|
| `recall_at_k` | 0.814 | 0.80 | 0.90 |
| `citation_precision` | 0.756 | 0.74 | 0.95 |
| `refusal_accuracy` | 0.710 as recorded / **0.903 re-scored** | not gated | 0.95 |
| `mrr` | 0.506 | - | - |
| `faithfulness` | 0.826 | - | - |
| `fact_recall` | 0.622 | - | - |

`refusal_accuracy` carries two numbers because the run was re-scored, not
re-run. 0.710 is what the artifact recorded under the old status-string
predicate. 0.903 is the same 62 replies scored under the withhold policy
adjudicated on 2026-08-06. The 2026-08-06 run on `f63ddfd` gives 0.726 / 0.902
the same way.

By category:

| Category | n | recall | mrr | cite prec | decision |
|---|---:|---:|---:|---:|---:|
| table | 5 | 1.000 | 0.580 | 1.000 | 1.000 |
| exact_identifier | 11 | 0.909 | 0.670 | 0.909 | 1.000 |
| current_version | 14 | 0.929 | 0.457 | 0.750 | 0.929 |
| exception | 7 | 0.857 | 0.583 | 0.857 | 1.000 |
| clarification | 3 | - | - | - | 1.000 |
| refusal | 16 | - | - | - | 0.250 as recorded / **1.000 re-scored** |
| duplicate_boilerplate | 6 | 0.167 | 0.167 | 0.167 | 0.167 |

## v7 selective citation, reported with PR #182

v7 is live in production. The headline rule is no longer "cite or refuse", it is
**cite the facts, talk like a person**. Every sentence is a source fact (must
carry its passage numbers), our own reasoning (must open with one of four pinned
phrases, carries no numbers), or plain conversation. INV-1 is unchanged and
still enforced in code: an uncited source fact is still dropped by the gate.

The numbers reported with #182:

| Metric | v7 |
|---|---:|
| malformed structure | 0 |
| citation precision | 0.779 |
| refusal accuracy | 0.903 |
| uncited source facts | 0 |

Two things to keep straight when reading any metric with "refusal" in its name:

- **v7 has no sentinel and no code word for "not found".** When the passages do
  not answer the question the model says so in ordinary words, names what it
  does have nearby, and offers a next step. The whole reply is plain prose with
  zero passage numbers.
- **The wire shape did not change.** That turn still leaves as
  `status="refused"`, `reason="model_refusal"`, `citations=[]`. So
  `metrics.withheld_answer` scores it correctly without any change: no claims,
  no citations, answer withheld. Only the text is now the model's own.

The `malformed_structure` rate is the other number to watch. It sat at roughly
12% of production turns before v7 (measured 2026-08-04); the #182 run reports 0.
That is one run, not a trend.

## Gates are a ratchet, targets are not acceptance criteria

Blocking floors, in `run_eval.THRESHOLDS`: `recall_at_k` 0.80,
`citation_precision` 0.74. Nothing else blocks.

The 0.90 / 0.95 / 0.95 numbers in `run_eval.TARGETS` are **targets**. They were
written against hash-based echo embeddings with `REFUSAL_SCORE_THRESHOLD=0.0`
and nothing has shown they are reachable on real geometry against this corpus.
They are printed in the scorecard's `target` column and enforced nowhere.

The floors are ratcheted to the first real measurement and sit slightly below
it, because the eval drives a live LLM and a floor set exactly at the
measurement would flake red on noise. The gate means "no worse than the day it
was first measured". Raise the floors as quality improves; never lower one
without recording why here.

Where each number lives: the scorecard table prints `recall_at_k`, `mrr`,
`citation_precision`, `faithfulness`, `sentence_citation_rate`, `fact_recall`
and `refusal_accuracy`. The `eval_run` ledger stores six of those as columns
(everything except `sentence_citation_rate`). The full scorecard, including
`uncited_source_facts`, `rejection_reasons` and the per-row traces, is in the
artifact JSON.

### The refusal labelling policy (adjudicated 2026-08-06, issue #161)

**A `must_refuse` row asserts the system must not ANSWER the question. It does
not assert which status string the reply wears.** A row is correct when the
answer was withheld: no claim about the question, zero citations.

| Outcome | Withheld? | Why |
|---|---|---|
| `status="refused"` (any reason) | yes | the hard refusal |
| `status="scope_warning"` | yes | refuses to advise (INV-3 rows) |
| `status="clarify"` with **no** citations | yes | declined, then offered next steps |
| `status="clarify"` **with** citations | **no** | citations are claims, and this is the INV-1 failure the metric exists to catch |
| `status="answer"` / `"summary"` | **no** | it answered |
| `status="error"` from a transport failure | **not measured** | see below |

The rule is implemented once, in `metrics.withheld_answer`.

It was adjudicated by re-scoring the two recorded scorecard artifacts row by
row, not by argument. The 12 seeded-product refusal rows come back as `clarify`
with `citations: []` and copy like *"You're asking about Budesonide. FDA has 1
product-specific guidance document for it, what would you like to know?"*. No
claim about extractables, washout periods or confidence intervals. That is a
withheld answer; scoring it wrong was measuring the affordance instead of the
invariant. **No gold row was relabelled.** Under the policy both runs score the
refusal category 16/16 and 15/15, and `refusal_accuracy` 0.903 / 0.902.

### `refusal_accuracy` is measured but not gated (owner decision, 2026-08-06)

It blocked for part of one day at 0.88 and was un-gated the same day. Not
because the labels are disputed, they are settled. The reason is that the
product deliberately declines less now, and gating on how often it declines
would fail the build for doing the new thing correctly. v7 is that direction
landing.

Read the name carefully. `refusal_accuracy` is decision accuracy over the whole
gold set, not a refusal rate: 16 `must_refuse` rows, 3 `must_clarify` rows, and
43 answerable rows that are correct only when they actually answer. Two thirds
of its weight is answerable rows. On the 2026-08-06 main run it read 0.887 with
all 16 refusal rows correct; the entire shortfall was 7 of 43 answerable rows
producing no answer.

## Two scoring rules that were wrong once

### A synthesis crash is not a retrieval failure

`recall_at_k` and `mrr` are scored from what retrieval returned, on every
answerable row, including rows the system then declined or failed to
synthesize. Retrieval either found the expected page or it did not.

This was a red build on main, not a hypothetical. On 2026-08-06 two rows came
back `malformed_structure` after retrieving the expected page at rank 1. The
scorer had no `recall` key for those rows, counted them 0, and reported
`recall_at_k` 0.791 against its 0.80 floor. Scored from the retrieved lists
already sitting in the trace, the same run reads 0.837. `citation_precision`
moves 0.744 to 0.780 for the matching reason: a turn that crashed cited nothing
because it never finished, so it leaves that denominator instead of being scored
as having cited wrongly. The floor was not lowered, the attribution was fixed.

Over-refusal still cannot hide. A genuine decline stays in the
`citation_precision` denominator at 0 and lands in `refused_incorrectly`. A
system that refused every row would score `citation_precision` 0 and fail.

### A turn that failed in transport measured nothing

A turn whose transport failed, meaning `reason` is `provider_error` or
`catalog_error`, leaves every denominator and is counted as `errored`.
`malformed_structure` is deliberately not in that set: that is the model
emitting output the gate could not admit, which is a real quality defect and
must keep scoring against the run.

This closed two observed lies:

1. **An error counted as a correct refusal.** The error paths build their reply
   with `_refuse`, so `refused` was `True`. That is why identical code measured
   0.710 on 2026-08-05 and 0.726 on 2026-08-06: one 400 landed on a refusal row.
2. **An error counted as a retrieval miss.** On 2026-08-06 two eval jobs ran
   concurrently against the same Databricks workspace, five turns came back
   `REQUEST_LIMIT_EXCEEDED`, and `recall_at_k` fell 0.814 to 0.721 with no
   change to retrieval anywhere in the diff. Both open PRs went red.

Replaying that run's recorded per-row outcomes through the corrected scorer
returns every metric to baseline:

| Metric | As measured (5 x 429) | Replayed, transport failures excluded | Baseline |
|---|---:|---:|---:|
| `recall_at_k` | 0.721 | **0.816** | 0.814 |
| `citation_precision` | 0.674 | **0.763** | 0.756 |
| `refusal_accuracy` | 0.823 | **0.895** | 0.903 |

Because transport failures leave the denominators, a bad enough outage could
otherwise shrink the gate to a few lucky rows and report green. When more than
`MAX_UNMEASURED_FRACTION` (10%) of turns fail in transport,
`--check-thresholds` exits **3** with a message naming the provider, before it
scores anything, rather than exiting 2 and sending someone hunting a retrieval
bug that is not there. The 2026-08-06 run lost 5 of 62 (8%), under the cap, so
its metrics stand.

The underlying problem is not fixed here. The shared Databricks endpoint is
pay-per-token with a workspace QPS limit, and two live evals at once exceed it.
The durable fixes are a provisioned-throughput endpoint (a cost decision) or
serializing the live eval across PRs (a CI latency decision). The workflow now
serializes every live eval through one concurrency group, which removes the
self-collision; the gate reports the condition honestly either way.

## `faithfulness` and `sentence_citation_rate`

Changed by PR #178, 2026-08-10.

- `faithfulness` is the fraction of the gate's admitted **source fact** claims
  that carry a citation, read from `turn_gate.claim_tags`. Python-internal, no
  DB column, no wire field.
- `sentence_citation_rate` is the old per-sentence text rule, kept verbatim so
  the historical trend line survives.
- `uncited_source_facts` counts admitted source-fact claims the tags marked
  uncited. It exists because `faithfulness` returns 1.0 when a turn asserted no
  source facts at all, which is correct but blind on its own.
- Every caller with no claim tags (clarify copy, meta, refusals, and any older
  `faithfulness(text)` call site) falls back to the text rule unchanged.
- Neither new field is in `THRESHOLDS`.

The two definitions used to coincide almost everywhere, because the v5/v6 gate
never admitted an uncited claim. **Under v7 they diverge by design**: reasoning
and conversation sentences are admitted uncited on purpose, so
`sentence_citation_rate` drops on a perfectly good answer while `faithfulness`
holds. `faithfulness` is the number to read for v7 turns;
`sentence_citation_rate` is a trend line for the old shape, not a quality bar.

The one pre-v7 divergence, still true: a turn carrying `PARTIAL_DROP_DISCLOSURE`
gets a renderer-authored sentence that is not a claim, so the text rule counts
it uncited while `faithfulness` does not. A local 62-row v6 run measured this:
of 33 answered rows, 6 carry the disclosure line and score
`sentence_citation_rate < 1.0` with `faithfulness` 1.0.

## Known quality defects

**`duplicate_boilerplate` at 0.167 recall and 0.167 citation precision.** The
weakest category by a wide margin. Issue #163.

The traces do not support the duplicate-competition explanation. Five of the six
rows returned `status=refused reason=no_product` with `retrieved: []`: the
queries never reached vector search. They are phrased corpus-wide ("Across the
FDA inhalation product-specific guidances...") and name no drug, so the resolver
returns `none` and `ask_core` refuses before retrieval. The one row that names a
product scored recall 1.0 and citation precision 1.0 with the expected pages at
ranks 1 and 3.

So 0.167 is 1 of 6 on rows that never got to compete. The real question it
surfaced is a product one: **should a corpus-wide question about shared
boilerplate be answerable without naming a product?** Today it is not. The
duplicate-group cap remains deferred and unstarted, and whether it is even the
right fix is untested.

The router `BadRequestError` on the Databricks path was fixed 2026-08-06 (issue
#162): the endpoint requires the literal word `json` in a user message when
`response_format={"type":"json_object"}` is set, and the guidance prompt's JSON
instruction lived only in system messages. Fixed at the provider seam
(`_ensure_user_json_token`) so every structured caller is covered, with the
prompt texts and their audited hashes left byte-identical.

## The 0.30 refusal threshold is still unvalidated

`REFUSAL_SCORE_THRESHOLD` defaults to 0.30. Passages scoring below it are
withheld from the synthesizer before it runs. Nothing has calibrated that
number.

The one live sweep artifact came from the old OpenAI 1536-dim path:

| Gold group | Rows | Rows with a cosine score | Outcome |
|---|---:|---:|---|
| Must answer | 6 | 6 | all six answered, max scores 0.812 to 0.896 |
| Must refuse | 5 | 0 | all five stopped before vector retrieval |
| Must clarify | 1 | 0 | correctly clarified, excluded from calibration |

Every refusal row was handled by product resolution, brand lookup or a scope
check before retrieval, so not one of them produced a negative cosine score.
With no scored negatives there is no separation to calibrate against, and the
corrected sweep now returns no recommendation when either scored distribution is
empty. The old `0.917` figure from that artifact was `current_decision_accuracy`
from the threshold harness, not `run_eval.refusal_accuracy`, and it was inflated
by counting a correct `multi_form` clarification as an answer failure. The
evaluator now excludes `must_clarify` rows from the cosine curve.

On top of all that, **the vector space itself changed**. Production has embedded
on Databricks Qwen3 at 1024 dimensions since 2026-07-30, so nothing measured in
the OpenAI 1536-dim space transfers.

### Before the cutoff moves

1. ~~Expand the Q&A gold set to 30-50 reviewed cases.~~ **Done 2026-08-05: 62
   stratified rows**, machine-verified. Domain review that the questions are
   well posed is still outstanding.
2. ~~Add hard negatives that resolve a real product but still lack citable
   evidence.~~ **Done: 12 or more refusal rows name a seeded product and reach
   vector retrieval**, enforced by
   `tests/test_gold_set_integrity.py::test_refusals_include_scored_hard_negatives`.
3. Re-run the corrected `threshold_sweep` on the Qwen3 1024-dim arm against a
   controlled corpus snapshot. Not yet done on this arm.
4. Keep the artifact with commit, corpus snapshot, embedding profile, model and
   configuration fingerprints.
5. Review retrieval ranks and the cite/withhold decisions row by row, not only
   the aggregates.
6. Move the threshold only when both the positive and the negative scored
   distributions support it.

The graph-assisted retrieval proposal in
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md) adds adaptive
evidence expansion. It does not remove the need for a calibrated sufficiency
decision or a representative evaluation set.
