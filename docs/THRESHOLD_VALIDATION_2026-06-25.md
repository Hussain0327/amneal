# Refusal-threshold validation -- 0.30 in the prod OpenAI-1536 space (2026-06-25)

DECISION PACKET. This is an analysis + recommendation for a human to sign off.
It does NOT change any runtime value. `refusal_score_threshold` stays `0.30`
(`config/settings.py:142`) until a human acts on the PROPOSED entry appended to
`docs/DECISIONS.md`.

## TL;DR

- The advisory threshold sweep has **never run against the production
  OpenAI-1536 + pgvector space**, so there is **no real distribution data** to
  retune against today.
- Cause: the sweep only runs inside the `watch-daily` GitHub Actions job, which
  is **skipped** because the repository secret `WATCH_DATABASE_URL` is unset.
- Therefore the recommendation is **KEEP 0.30** (status quo, provisional) and
  **generate the real sweep before any retune**. Retuning blind would be
  guessing in a vector space whose cosine distribution we have not measured.
- The exact one-command reproduction (run it in an env that has the prod
  `DATABASE_URL` + `EMBEDDING_PROVIDER=openai` + `OPENAI_API_KEY`) is in
  [section 5](#5-how-to-regenerate-the-real-sweep).

## 1. What `0.30` gates, and why it is unvalidated

`grounded_qa.ask` refuses BEFORE calling the LLM when retrieval is weak
(`src/regwatch/generate/grounded_qa.py:1235`):

```python
if not passages or max(p.score for p in passages) < s.refusal_score_threshold:
    # -> reason="low_top_score" refusal (INV-2 / INV-1: refuse over guess)
```

`0.30` was calibrated in the **bge-small-384** cosine era (`docs/DECISIONS.md`
Phase 2: "Tune on the gold set"). Production now embeds with **OpenAI
text-embedding-3-small (1536 dims)** -- a *different* vector space with a
*different* cosine-similarity distribution. CI cannot revalidate it: CI runs
bge-384 + Chroma; only the prod path (the `watch-daily` job, which exports
`EMBEDDING_PROVIDER=openai` + a real `DATABASE_URL`) exercises OpenAI-1536 +
pgvector. The harness (`src/regwatch/eval/threshold_sweep.py`) exists precisely
to read the score distribution off that real prod path. It is **advisory and
read-only**: it never mutates `0.30` and exits 0 even when its recommendation
differs.

INV-1 framing (governs the tradeoff direction): **a wrong cited answer is worse
than a wrong refusal.** When in doubt the gate should err toward refusing. A
higher threshold refuses more (safer, fewer cross-drug / absent-product leaks,
but more over-refusal of answerable questions); a lower threshold answers more
(but risks leaking a low-confidence / wrong-drug passage past the gate). 0.30 is
the current operating point on that curve.

## 2. Data source and provenance

| Field | Value |
|-------|-------|
| Intended data source | `watch-daily` job artifact `threshold-sweep` -> `threshold_sweep.json`, produced in the prod OpenAI-1536 + pgvector space |
| Artifact retrieved? | **NO -- none exists.** See provenance below. |
| Date checked | 2026-06-25 |
| Repo | `Hussain0327/amneal` |
| Live threshold at time of writing | `refusal_score_threshold = 0.30` (`config/settings.py:142`), unchanged |
| Gold set | `src/regwatch/eval/gold_set.jsonl` -- 12 gold items as `_load_gold` actually parses them (it SKIPS `#` comment + blank lines): 5 `must_refuse` + 1 `must_clarify` + 6 plain must-answer. See the count note below. |

Gold-set count note (re-derived from the real loader, not `wc -l`):
`run_eval.py::_load_gold` (lines 41-58) drops every `#`-comment and blank line,
so the file's `wc -l` of 26 is NOT the item count -- 14 of those lines are
comments/blanks that never become gold items. The 12 real items are
`grep -vcE '^\s*(#|$)' src/regwatch/eval/gold_set.jsonl` = 12, split 5
`must_refuse` / 1 `must_clarify` / 6 plain must-answer. The sweep harness buckets
items by `not must_refuse` (`threshold_sweep.py:180,279`
`must_answer = [r for r in rows if not r.must_refuse]`), so the lone
`must_clarify` item (it has `must_refuse=False`) lands in the **must-answer**
bucket. As the harness counts them: **7 must-answer rows** (6 plain + 1
must_clarify) and **5 must-refuse rows**. Per-run cost is therefore ~12 real
retrievals + ~12 LLM syntheses, not ~26.

Provenance of the "no artifact" finding (all verified 2026-06-25):

- `gh run list --workflow watch-daily.yml`: the 7 most recent runs
  (2026-06-19 .. 2026-06-25) are **all 8-11 seconds** and all `success`. A real
  run (crawl + ingest + ~12 LLM `ask` calls for the sweep) would take minutes,
  not seconds -- every run took the fast skip path.
- Step inspection of the latest run (`28157160622`): the step
  `skipped (secret not configured)` ran, and every real step -- including
  `threshold sweep (advisory)` and `upload threshold sweep artifact` -- has
  conclusion `skipped`. The workflow gates every real step on
  `if: env.DATABASE_URL != ''`, and `DATABASE_URL` comes from
  `secrets.WATCH_DATABASE_URL`, which is **not configured** in this repo.
- `gh api .../actions/runs/28157160622/artifacts` -> `total_count = 0`.
- `gh run download <id> -n threshold-sweep` -> "no valid artifacts found to
  download" for every recent run.

These three observations (the run id `28157160622`, the 7 run durations, and
`artifacts total_count = 0`) are GitHub Actions observations made at the repo on
2026-06-25; they are not re-derivable from this offline tree. The MECHANISM they
rest on -- every real step gated on `if: env.DATABASE_URL != ''`, fed from
`secrets.WATCH_DATABASE_URL` -- IS verifiable in `.github/workflows/watch-daily.yml`
(the `skipped (secret not configured)` step plus the `DATABASE_URL` guards). A
reviewer with prod GitHub access should spot-check the run id before flipping the
`docs/DECISIONS.md` entry to ACCEPTED.

Conclusion: **the sweep has never executed in the OpenAI-1536 space.** No real
numbers are available; this packet does not fabricate any.

## 3. The sweep's output schema (what a real run WOULD emit)

So a reader of the future real artifact can build the comparison table, here is
the exact JSON schema emitted by `threshold_sweep.py::_serialize` (verified from
source -- these are the REAL field names; the lane's shorthand "precision" /
"cross-drug-leak-rate" are NOT literal fields, see the mapping note below):

```
{
  "rows": [ {                  # one per gold item, threshold-INDEPENDENT
      "question": str,
      "must_refuse": bool,
      "max_score": float|null, # pre-gate best cosine; null = retrieval never ran
      "recall_hit": bool,      # recall@k == 1 against expected_sources
      "status": str|null, "reason": str|null, "n_retrieved": int
  } ],
  "curve": [ {                 # one per candidate threshold (grid [0,0.60] step 0.01)
      "threshold": float,
      "refuse_recall": float,     # frac of must_refuse rows that WOULD refuse at t
      "answer_retention": float,  # frac of must-answer rows that WOULD answer at t
      "decision_accuracy": float  # (correct refuse + correct answer) / n
  } ],
  "distributions": {
      "must_answer": {"n","n_scored","min","median","max"},
      "must_refuse": {"n","n_scored","min","median","max"}
  },
  "recommendation": {
      "recommended": float|null, "current": 0.30,
      "rationale": str, "provisional": bool, "overlap": bool,
      "current_refuse_recall", "current_answer_retention", "current_decision_accuracy",
      "recommended_refuse_recall", "recommended_answer_retention", "recommended_decision_accuracy",
      "wrongly_refused_at_current": [str],  # PATHOLOGY (a): must-answer over-refused at 0.30
      "leaking_at_current":        [str]    # PATHOLOGY (b): must-refuse LEAKING past 0.30
  }
}
```

Note on bucketing: the harness has NO separate must_clarify bucket. It splits the
12 items into `must_refuse` (5) and "must-answer" (everything with
`must_refuse=False`, which is 7: the 6 plain answerable items plus the 1
`must_clarify` item). So `distributions.must_answer.n` and `n_must_answer` will
read 7, and `distributions.must_refuse.n` will read 5.

Mapping the lane's requested columns onto the real schema (no field invented):

| Lane column | Real field | Meaning |
|-------------|-----------|---------|
| refusal_accuracy | `curve[t].refuse_recall` | fraction of must-refuse items correctly refused at `t` |
| cross-drug-leak-rate | `1 - curve[t].refuse_recall`, itemized by `recommendation.leaking_at_current` | must-refuse (wrong-drug / absent-product) items that ANSWER at `t` |
| answer-rate / recall | `curve[t].answer_retention` (item-level recall is `rows[].recall_hit`) | fraction of must-answer items still answered at `t` |
| precision | (no literal field) | the harness does not emit per-threshold precision; INV-1 leakage is captured by `leaking_at_current` + `refuse_recall` |
| (combined) | `curve[t].decision_accuracy` | overall correct-decision rate at `t` |

## 4. Candidate-threshold table

**No real artifact exists (see section 2), so the cells below are intentionally
UNPOPULATED.** Fabricating cosine numbers in a vector space we have not measured
would be exactly the "fill data gaps from memory" failure the project forbids.
Run the section 5 command in a prod-credentialed env, then fill this table from
`threshold_sweep.json`.

| candidate `t` | refuse_recall (refusal acc.) | leak rate `=1-refuse_recall` (INV-1) | answer_retention (answer rate) | decision_accuracy |
|---------------|------------------------------|--------------------------------------|--------------------------------|-------------------|
| 0.20          | _pending real sweep_ | _pending_ | _pending_ | _pending_ |
| 0.25          | _pending real sweep_ | _pending_ | _pending_ | _pending_ |
| **0.30 (current)** | _pending real sweep_ | _pending_ | _pending_ | _pending_ |
| 0.35          | _pending real sweep_ | _pending_ | _pending_ | _pending_ |
| 0.40          | _pending real sweep_ | _pending_ | _pending_ | _pending_ |

(The real grid is finer -- `[0, 0.60]` at 0.01; these rows are the human-readable
decision band around 0.30. The harness emits the full grid.)

### Decision FRAMEWORK to apply once the table is populated

Read it INV-1-first (refuse over guess). The harness already encodes this
selection rule (`threshold_sweep.py::recommend`); a human applies judgment on top:

1. **If `leaking_at_current` is non-empty** (a must-refuse / wrong-drug / absent
   item ANSWERS at 0.30): this is an active INV-1 cross-drug-leak. **RAISE** the
   threshold to just above the highest leaking must-refuse `max_score`. This is
   the strongest signal and overrides the retention-preserving default.
2. **Else if `wrongly_refused_at_current` is non-empty** (answerable items
   over-refused at 0.30) AND no leakage: 0.30 is too high. **LOWER** only as far
   as recovers those answers WITHOUT admitting any must-refuse item -- i.e. to a
   value still above every must-refuse `max_score`. Never trade a leak for a
   recovered answer.
3. **Else if distributions are cleanly separable** (lowest must-answer
   `max_score` > highest must-refuse `max_score`) and 0.30 sits inside the gap:
   **KEEP 0.30** -- it is correct and the slack is healthy.
4. **If distributions OVERLAP** (`recommendation.overlap == true`): no perfect
   cutoff exists; the harness flags the recommendation `provisional`. Per INV-1,
   pick toward the **refuse** side -- the cutoff that drives `refuse_recall` to
   1.0 (zero leakage) even at some `answer_retention` cost -- and open a retrieval
   ticket, because the real fix is better retrieval, not a threshold.

Tie-break across all branches: **err toward refuse.** A recovered answer is
never worth a single cross-drug leak (INV-1).

## 5. How to regenerate the real sweep

The sweep needs the PROD embedding space (OpenAI-1536 + pgvector), not CI's
bge-384 + Chroma. Two ways:

**A. Trigger the wired CI job (preferred -- runs in the real prod DB).**
Requires the repo secret `WATCH_DATABASE_URL` (the prod Supabase pooler URL with
`?sslmode=require`) plus `OPENAI_API_KEY` to be set in
`Settings -> Secrets and variables -> Actions`. Then dispatch `watch-daily`
(it has `workflow_dispatch:`) and download the `threshold-sweep` artifact:

```
gh workflow run watch-daily.yml
# ...wait for the run to finish (minutes, not seconds)...
gh run download <run-id> -n threshold-sweep -D ./tsweep
cat ./tsweep/threshold_sweep.json
```

**B. Run the harness directly against the prod DB** (any host with the prod
creds; read-only retrieval + ~12 cheap `gpt-5.4-nano` syntheses):

```
EMBEDDING_PROVIDER=openai \
DATABASE_URL='postgresql://...pooler.supabase.com:5432/postgres?sslmode=require' \
OPENAI_API_KEY=... \
REQUIRE_DATABASE_URL=true \
uv run python -m regwatch.eval.threshold_sweep --out threshold_sweep.json
```

Safety properties of the harness (so running it cannot regress prod):

- **Read-only w.r.t. the safety path.** It reads `result.retrieved[*].score`
  (populated even on the `low_top_score` refusal path); it never imports-to-
  mutate, never changes `0.30`, and exits 0 even when its recommendation differs.
- **Refuses to run on an empty store** (`collection_size() == 0` -> exit 2): a
  sweep over zero data would "recommend" off nothing.
- **Cost / external calls.** ~12 real retrievals + ~12 cheap LLM syntheses
  against the live OpenAI + Supabase endpoints (one per gold item). Each `ask`
  carries the same timeouts as prod (it IS the prod path). Acceptable for an
  advisory sweep.

After running: populate the section 4 table from `threshold_sweep.json`, apply
the section 4 framework, and update the PROPOSED entry in `docs/DECISIONS.md` (or
open the sign-off to flip it to ACCEPTED).

## 6. Recommendation

**KEEP 0.30 (provisional), and generate the real sweep before any retune.**

Rationale:

- There is **no measured distribution** in the OpenAI-1536 prod space
  (section 2). Retuning blind would violate the project's "no filling gaps from
  memory" rule and could move the gate the wrong way.
- KEEP 0.30 is the **lowest-regret** action: it is the long-standing operating
  point, the eval gate (`refusal_accuracy >= 0.95` in `tests/test_eval_gate.py`)
  is green against it in the CI bge space, and there is no evidence of a prod
  regression. The harness's own default selection rule never refuses an item the
  live 0.30 currently answers -- it only ever recommends a safer-or-equal value
  -- so "no data" defaults to "no change."
- The moment a real artifact lands: if it shows ANY `leaking_at_current`
  (cross-drug leak), **RAISE** per section 4.1 (INV-1 dominates). 0.30 is
  explicitly **provisional**, not validated, until that artifact exists.

This recommendation is recorded as **PROPOSED -- pending human sign-off** in
`docs/DECISIONS.md`. It does not change `0.30`.
