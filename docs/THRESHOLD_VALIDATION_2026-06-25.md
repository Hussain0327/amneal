# Refusal-threshold validation - 0.30 in the OpenAI-1536 space

**Original decision packet:** 2026-06-25

**Current verification:** 2026-07-30

**Runtime change:** none; `REFUSAL_SCORE_THRESHOLD` remains `0.30`

The July verification supersedes the June claim that no production-space
artifact existed. A real artifact now exists, but it still does not provide the
data needed to calibrate a numeric cosine cutoff.

## Current conclusion

**Keep `0.30` provisional. Do not call it calibrated, and do not retune it from
the current artifact.**

The latest inspected watch run,
[30531864530](https://github.com/Hussain0327/amneal/actions/runs/30531864530),
produced a `threshold-sweep` artifact against OpenAI-1536 + pgvector:

| Gold group | Rows | Scored rows | Max-score distribution |
|---|---:|---:|---|
| Must answer | 6 | 6 | min 0.812, median 0.838, max 0.896 |
| Must refuse | 5 | 0 | unavailable |
| Must clarify | 1 | 0 | excluded from cutoff calibration |

All five must-refuse cases stopped in product resolution, brand lookup, or scope
checks before vector retrieval. Those are useful safety outcomes, but they
provide no negative cosine-score distribution. A threshold cannot be calibrated
without scored examples on both sides.

The one must-clarify case correctly returned `status=clarify`,
`reason=multi_form`. The old harness placed it in the must-answer bucket and
reported:

- `current_decision_accuracy = 0.916666...`,
- `current_refuse_recall = 1.0`,
- `current_answer_retention = 0.857142...`,
- `recommended = 0.00`,
- rationale: distributions were "cleanly separable."

That interpretation was wrong:

- `0.917` was not `run_eval.refusal_accuracy`;
- the correct clarification was not an answer failure;
- zero scored must-refuse rows means there was no separability evidence;
- `0.00` was not a valid cutoff recommendation.

The corrected harness excludes `must_clarify` from the threshold curve and
returns `recommended=null`, `provisional=true` when either scored distribution
is empty. It also preserves the observed pre-retrieval `refused` flag instead
of relabeling every no-score clarification as a refusal. See
[`EVAL_STATUS.md`](EVAL_STATUS.md) for the broader eval status.

## What `0.30` gates

`grounded_qa.ask` refuses before synthesis when retrieval is empty or weak:

```python
if not passages or max(p.score for p in passages) < s.refusal_score_threshold:
    # reason="low_top_score"
```

`Hit.score` is the normalized cosine convention used by the pgvector store:

```text
score = 1 - cosine_distance / 2
```

The value `0.30` originated in the earlier BGE-384 embedding space. Production's
OpenAI `text-embedding-3-small` vectors are 1536-dimensional and can have a
different score distribution. The current value is therefore an operating
default, not a validated OpenAI-1536 boundary.

The deterministic CI retrieval fixture uses Postgres + pgvector with a
1536-dimensional echo embedding provider. It validates mechanics but cannot
reproduce live OpenAI score distributions.

## Gold-set composition

`src/regwatch/eval/gold_set.jsonl` contains 12 real items after comments and
blank lines are excluded:

- 6 ordinary must-answer,
- 5 `must_refuse`,
- 1 `must_clarify`.

The corrected threshold curve includes only the 6 must-answer and 5 must-refuse
rows. It retains the clarification row in the artifact for auditability but
does not use resolver clarification behavior as cosine-threshold evidence.

## Current artifact schema

```json
{
  "rows": [{
    "question": "string",
    "must_refuse": false,
    "must_clarify": false,
    "max_score": 0.0,
    "recall_hit": true,
    "refused": false,
    "status": "answer",
    "reason": null,
    "n_retrieved": 8
  }],
  "curve": [{
    "threshold": 0.3,
    "refuse_recall": 1.0,
    "answer_retention": 1.0,
    "decision_accuracy": 1.0
  }],
  "distributions": {
    "must_answer": {"n": 6, "n_scored": 6, "min": 0.0, "median": 0.0, "max": 0.0},
    "must_refuse": {"n": 5, "n_scored": 0, "min": null, "median": null, "max": null}
  },
  "counts": {
    "must_answer": 6,
    "must_refuse": 5,
    "must_clarify_excluded": 1
  },
  "recommendation": {
    "recommended": null,
    "current": 0.3,
    "rationale": "string",
    "provisional": true,
    "overlap": false
  }
}
```

The artifact contains additional recommendation metrics and pathology lists;
the abbreviated schema above highlights the calibration fields.

## Decision procedure

1. Require at least one scored row in both the must-answer and must-refuse
   distributions. In practice, use several reviewed hard negatives, not one.
2. If a scored must-refuse row answers at `0.30`, investigate retrieval and
   evidence sufficiency first. A wrong cited answer is worse than a refusal.
3. If scored distributions overlap, no single threshold solves the problem.
   Improve retrieval, filtering, or sufficiency logic rather than hiding the
   overlap with a tuned scalar.
4. If the distributions separate, compare candidate cutoffs while preserving
   answer retention and validate individual cite/refuse outcomes.
5. Record the corpus snapshot, embedding profile, provider/model, settings, and
   commit with the artifact before changing production configuration.

## How to regenerate

The scheduled path is the `watch-daily` workflow. Download its advisory artifact
after a credentialed run:

```bash
gh workflow run watch-daily.yml
gh run download <run-id> -n threshold-sweep -D ./tsweep
```

Or run directly from a controlled environment:

```bash
EMBEDDING_PROVIDER=openai \
DATABASE_URL='postgresql://.../postgres?sslmode=require' \
OPENAI_API_KEY=... \
uv run python -m regwatch.eval.threshold_sweep --out threshold_sweep.json
```

The harness reads live retrieval and synthesis results, refuses to run on an
empty store, does not mutate the corpus, and never changes the runtime
threshold. Its output is advisory.

## Evidence still required

- Expand the Q&A gold set from 12 to the planned 30-50 reviewed cases.
- Include hard negatives that resolve product, form, and version and reach
  vector retrieval.
- Run the corrected sweep and provider-backed `run_eval` on a controlled
  snapshot.
- Review rank, citation, clarification, and refusal details alongside aggregate
  metrics.

Until that evidence exists, the truthful decision is: **no threshold change;
`0.30` remains provisional.**
