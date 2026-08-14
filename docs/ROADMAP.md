# REGWATCH roadmap: what is still open

One list of everything that is NOT done yet. If another doc disagrees with this
file or with [`PROD_READINESS.md`](PROD_READINESS.md), these two win.

Last updated: 2026-08-13 for the authoritative FDA corpus implementation. Live
app, database and Fly values were last checked 2026-08-11.

Labels: **BLOCKER** stops external exposure. **SHOULD-HAVE** before launch.
**DECISION** needs a person to choose. **LATER** is optional.

## What changed since the 2026-08-05 stamp

- **Data residency (D1) is closed.** All three legs run inside the company's own
  Databricks tenant. Generation is `gpt-oss-120b` (served id
  `gpt-oss-120b-080525`) behind `workspace.default.regwatch`. Query and corpus
  embeddings are Qwen3 behind `workspace.default.regwatch-embed`, 1024 dim,
  profile `ep_2e7368b354d911ea3a013c3125e276c2`, 5,494 of 5,494 chunks covered
  since 2026-07-30. The database is Databricks Lakebase. No normal analyst turn
  uses OpenAI; it remains the interactive LLM rollback, while scheduled Watch
  retains a scoped key for public-document change summaries/extraction only.
  The D1 blocker that used to head this file is gone. History lives in
  [`archive/DATA_RESIDENCY_D1.md`](archive/DATA_RESIDENCY_D1.md).
- **The answer rule changed.** "Cite or refuse" is dead as the headline rule. v7
  selective citation is live in prod: cite the facts, talk like a person.
  Sentences that state what FDA guidance requires still carry passage numbers,
  and an uncited one is still dropped by the gate (INV-1, enforced in code, not
  in the prompt). Our own reasoning and ordinary conversation carry no numbers.
  There is no sentinel and no code word for "not found" any more.

Already shipped, so not listed below: Router -> Handlers -> Synthesizer with
INV-1..9 as tests; cited conversational Q&A with
live token streaming; the White Paper populator and its runs automation; the
deficiency analyzer; the Next.js shell on Vercel; the Go edge holding all public
traffic on Fly (auth, sessions, feedback, settings, products, plus `POST /query`
orchestration, polyglot steps 0 to 5); Postgres plus pgvector on Lakebase as the
only datastore; continuous deployment and the daily watch cron. The detail is in
[`../README.md`](../README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md) and the done
sections of [`PROD_READINESS.md`](PROD_READINESS.md).

---

## Live operator action, complete this one first

### Build and validate the authoritative FDA corpus  (SHOULD-HAVE)

The code portion now includes the exact five-family policy, official snapshot
adapters, bounded document streaming, durable content-addressed artifacts,
sandboxed OCR, separately checkpointed chunk/embedding lifecycle, an exact
manifest, 512 deterministic Dagster shards, blocking coverage checks, and a
fail-closed reversible cutover. The full read-only discovery found **140,339
source records** on 2026-08-13.

Migration 0023 is deployed. The first production canary indexed 18 / 21 records
and 347 chunks, then stopped on three parse failures; the active profile is
therefore 5,494 / 5,841 embedded. The follow-up migration 0024 and worker image
must be released before ingestion resumes. Then rerun the OCR-enabled canary to
21 / 21, freeze the exact full manifest, run chunk and embedding partition
backfills, pass 512-shard acceptance plus retrieval/citation evaluation, verify
`activation_ready=true`, smoke the cutover, and rehearse rollback.

- Where: `src/regwatch/corpus/`, `src/regwatch/sources/policy.py`, migrations
  0023–0024, `Dockerfile.corpus-worker`, and the corpus runbook.
- Done when: the target environment processes the full current manifest with
  zero document errors, searchable document parity, zero policy violations,
  100% selected-profile chunk coverage, a passing eval, and tested cutover plus
  rollback.

### Provision and validate the Watch embedding profile  (SHOULD-HAVE)

The code portion is done: the daily workflow pins Qwen3, requires a named active
profile plus base URL, token, model, revision and dimension before checkout,
validates the registered profile before crawl, and preserves the post-ingest
100% coverage assertion. It fails closed instead of allowing an unprofiled
change-day ingest.

The owner portion is still open. These six repository secrets were all absent
when checked through the GitHub API on 2026-08-12:
`WATCH_ACTIVE_EMBEDDING_PROFILE`, `WATCH_QWEN_EMBEDDING_BASE_URL`,
`WATCH_QWEN_EMBEDDING_TOKEN`, `WATCH_QWEN_EMBEDDING_MODEL`,
`WATCH_QWEN_EMBEDDING_REVISION`, and `WATCH_QWEN_EMBEDDING_DIMENSION`.
Until they are provisioned, a configured production run stops at preflight and
does not crawl. Provision all six together, dispatch the workflow manually, and
verify its profile-validation and zero-pending coverage steps.

- Where: GitHub repository secrets and `.github/workflows/watch-daily.yml`.

---

## Blockers, before any external exposure

### 1. Gateway and SSO, plus distributed rate limiting  (PROD_READINESS #1)

App-layer auth is done: the Go edge owns cookie sessions, ownership checks and
the login brute-force cap, and Fly terminates HTTPS (`force_https = true`,
`AUTH_COOKIE_SECURE` pinned in `fly.toml`). What is missing is the corporate
front door: OIDC/SSO against the company IdP behind a TLS gateway.
[`DECISIONS.md`](DECISIONS.md) defers that to IT, with cookie sessions as the
pilot boundary. Separately, the rate limiter is in-memory per process across two
proxy machines, so the real fleet ceiling is about twice the configured rate.

- Where: `go/internal/api/`, `fly.toml`.
- Done when: enterprise auth fronts the app, or the cookie-session layer is
  formally accepted as the pilot boundary, and rate limiting is no longer
  per-process.

### 2. Restore drill and least-privilege database credentials  (PROD_READINESS #2)

The database is Databricks Lakebase; the last verified live head was
`0020_eval_run`, while repository head now includes 0021. The migration release gate is done
(`release_command = "alembic upgrade head"` in `fly.toml`). What is missing is
proof: nobody has ever rehearsed a restore, and the scripted
`scripts/restore_drill.sh` was deleted in R5. The app also still connects with a
full-privilege role. Polyglot step 7, where Python drops to a read-only role, is
the least-privilege path.

- Where: [`DEPLOY.md`](DEPLOY.md), the staging and restore drill section;
  `src/regwatch/store/db.py`.
- Done when: a restore drill against staging has passed and least-privilege
  credentials are in place, or formally waived.

### 3. UI smoke and load behind the approved gateway  (PROD_READINESS #4)

The UI is feature complete and live on Vercel. What is left is deploy-time proof
for the exposure decision, not code.

- Where: `regwatch/frontend/`, [`DEPLOY.md`](DEPLOY.md) smoke checklist.
- Done when: the smoke flows pass behind the approved auth path and a load test
  has run.

---

## Should-have before launch

### Observability  (PROD_READINESS #6)

Structured logging, audit rows, privacy-scrubbed Sentry wiring, component
`/health`, `/ready` and `/metrics` counters all exist. Per-turn latency is
captured (`query_log.latency_ms`, migration 0016, both runtimes) but is not
exported as histograms. Missing: latency and cost metric export, tracing, a
configured production Sentry DSN, and a decision on whether a paid live LLM
reachability probe is worth the noise.

- Where: `common/logging.py`, `common/observability.py`, `common/audit.py`,
  `api/main.py`, `go/internal/api/`.

### Watch operations  (PROD_READINESS #7)

The cron is live: `watch-daily.yml` at 07:17 UTC does crawl, match, ingest,
durable alerts, digest, with a Slack failure notice, a success digest,
healthcheck pings and an advisory threshold sweep. It failed every day from
2026-08-07 until the owner updated `WATCH_DATABASE_URL` on 2026-08-10 at 18:19
UTC. The manual run that evening and the scheduled run on 2026-08-11 both
passed, so this is not an ongoing outage.

What remains is operational: keep the secrets provisioned (see the hazard above
and [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md)), watch real run history, and
decide whether alerts should move beyond `/watch/latest` plus Slack into
product-facing email or digests. Deferred from the July watch wave: alert
ack-state and durable parsed text.

- Where: `watch/run.py`, `watch/alerts.py`, `.github/workflows/watch-daily.yml`.

### Finish the polyglot migration

Steps 0 to 5 of [`POLYGLOT_TARGET_2026-07-10.md`](POLYGLOT_TARGET_2026-07-10.md)
are done: the Go edge plus native `/query`, live since 2026-07-24. Remaining:

- **The step-5 deletion PR.** Python still serves its own `POST /query`
  (`src/regwatch/api/main.py`) even though the Go edge orchestrates the live
  path. Do the INV-coverage mapping first:
  [`archive/STEP5_INV_TEST_MAPPING.md`](archive/STEP5_INV_TEST_MAPPING.md).
- **R3**, the safe-prefix streaming rewrite. It has to keep the
  `D1ResidencyError`-excluded SSE fallback from #138.
- **Steps 6 to 9**: coarse write commands for ingest and Watch (6), Python down
  to a read-only DB role (7, the least-privilege item above), the Rust PDF CLI
  with shadow parity (8), `CommitWhitepaperRun` plus deleting the Python
  persistence layer (9).

### Secrets policy  (PROD_READINESS #10)

`.env` and friends are gitignored, the Actions secret surface is written up in
[`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md), and prod uses Fly and Vercel platform
secrets. Still needed: an approved secret manager or platform policy, and key
rotation that is documented and rehearsed.

### Container resource limits  (PROD_READINESS #11)

The supply-chain gates (pip-audit, npm audit, Trivy image scans) are green and
enforced. The one residual: neither `compose.yaml` nor `fly.toml` sets any CPU or
memory limit.

### Check whether the residency guard is armed

The guard ships and is tested: `D1_ENFORCED` plus `D1_ALLOWED_LLM_MODELS`, and a
runtime check in `generate/llm.py` that rejects a reply served by a model outside
the allowlist and always rejects the partner-hosted families (`databricks-gpt*`,
`databricks-claude*`, `databricks-gemini*`). That runtime check only runs when
`D1_ENFORCED` is on, and it is NOT on in prod. Verified 2026-08-11: `D1_ENFORCED`
appears in neither `fly secrets list -a amneal` nor `fly.toml`, and the setting
defaults to `false` (config/settings.py). So the runtime check is inert today.
`D1_ALLOWED_LLM_MODELS` is already set, which is the only thing arming it needs.

---

## Decisions needed

### Product shape: collapse to two surfaces  (DECISION)

Owner direction, 2026-08-05: end up with two surfaces, Ask (the conversational
one) and Studio (the document workspace), with Assemble, Watch, White Paper and
Deficiency folded into Studio. Assemble and White Paper become generators that
write documents into the tree. Watch, Deficiency and the compliance check become
checks that run against documents already in it. "Is this document out of date?"
is then Watch pointed at our own drafts.

Nothing has moved yet, and nothing should. The four surfaces being folded in all
have working backends. Studio mostly does not, so folding them in now would trade
shipped functionality for a mockup.

Prerequisites, in order:

1. **Studio needs a backend.** One seam is real: the left rail lists the FDA PSG
   corpus from the database and streams the PDFs inline. Everything else is
   fixtures. `CHECK_RESULTS` plus a 1.5s timer stands in for the compliance
   pipeline, and the document service and the assistant are equally fictional.
   The fixture shapes in `lib/studio-fixtures.ts` are the contract, see
   [`COMPLIANCE_STUDIO.md`](COMPLIANCE_STUDIO.md) section 8.
2. **Studio needs persistence.** A refresh destroys every recorded disposition.
   `localStorage` is not an acceptable answer for GMP dispositions.
3. **Studio needs the product scope.** It sits outside `app/(shell)/` and never
   sees `CurrentProductProvider`, so its tree cannot yet be "the documents for
   the product under review".
4. **Decide what a document is.** White Paper cells carry their own provenance
   model and are not spans in a block. Watch alerts are not documents at all.
   Each fold needs its data model reconciled with Studio's `(blockId, start,
   end)` anchor, or an explicit decision that it stays separate.
5. **Decide what happens to the shell.** If only Ask and Studio remain, the
   `(shell)` route group, the sidebar and the scope bar are all in question.

- Where: `regwatch/frontend/app/studio/`, `app/(shell)/`,
  [`COMPLIANCE_STUDIO.md`](COMPLIANCE_STUDIO.md),
  [`ARCHITECTURE.md`](ARCHITECTURE.md) section 3.
- Open question: whether Deficiency folds in as a check on an open document or
  stays a whole-submission upload. It takes a PDF today, not a document from a
  tree.

### Revalidate the 0.30 refusal threshold  (DECISION)

`REFUSAL_SCORE_THRESHOLD=0.30` withholds any passage scoring below it before the
synthesizer ever runs. It was validated against the old OpenAI 1536-dim space
([`archive/THRESHOLD_VALIDATION_2026-06-25.md`](archive/THRESHOLD_VALIDATION_2026-06-25.md)).
Prod now embeds with Qwen3 at 1024 dim, a different cosine distribution, so that
validation does not carry over and nobody has redone it. The advisory sweep in
the watch cron cannot settle it alone: its must-refuse cases stop before
retrieval and produce no cosine scores. Add scored hard negatives and rerun. See
[`EVAL_STATUS.md`](EVAL_STATUS.md).

---

## Product and quality

### Eval  (PROD_READINESS #8)

The gold set is now 62 Q&A rows plus 16 white-paper rows, with every quote
verified present at its pinned `(short_name, page)`, and the live Databricks eval
runs on every build (`.github/workflows/databricks-eval.yml`). What is still
open:

- **The blocking eval does not run what prod runs.** `ci.yml` calls it with
  `prose: false, selective: false`, so the merge gate scores the old v5 claims
  chain while prod serves v6 prose plus v7 selective citation. The v6 and v7
  numbers come from the hand-dispatched arm instead.
- The blocking floors are a ratchet, not a quality bar: `recall_at_k` 0.80 and
  `citation_precision` 0.74, each set just under the first real measurement. The
  0.90 / 0.95 / 0.95 figures are aspirational targets and have never been shown
  reachable on real geometry.
- `refusal_accuracy` is measured but not gated, by owner decision, because the
  product is deliberately moving away from refusing. Its 16 gold rows are slated
  for removal once that direction lands.
- Scoring is still mechanical `(short_name, page)` plus `expected_facts`
  substrings. LLM-as-judge is not wired, so a correct answer worded differently
  undercounts.
- The CI eval runs against a seed corpus (66 chunks, 8 documents), not the
  5,494-chunk production corpus.

See [`EVAL_STATUS.md`](EVAL_STATUS.md).

- Where: `src/regwatch/eval/`, `gold_set.jsonl`, `tests/test_eval_gate.py`.

### Post-deploy smoke against the live stack

On 2026-08-03 an answer-path regression reached real users, and the whole CI
suite was blind to it. `deploy.yml` still has no step that asks one real question
through the deployed stack and asserts it came back answered.

- Done when: a post-deploy job queries the live app end to end and fails the
  deploy, or alerts, on anything other than an answered, cited turn.
- Where: `.github/workflows/deploy.yml`, `src/regwatch/eval/`.

### Graph-assisted retrieval

The deterministic Tier-1 graph foundation is landed (`0018_knowledge_graph`,
`store/graph_store.py`): application, document and section nodes, typed hierarchy
and adjacency edges, and references back to the citable chunks. Only ingest and
the CLI write it. No runtime query path reads it yet.

The proposed consumer starts from scoped seed chunks, does a bounded typed
traversal, reranks the expanded chunks and tests whether the evidence is enough,
with at most one targeted second expansion. It stays default-off until the eval
set is bigger and the graph path shows fewer false refusals and ranking misses
with no citation, scope, latency or context-budget regression. Design and gates:
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md).

- Where: `src/regwatch/store/graph_store.py`, `src/regwatch/retrieve/`,
  `src/regwatch/generate/grounded_qa.py`, `src/regwatch/eval/`.

### Persist-and-cite beyond the White Paper  (PROD_READINESS #9)

The persist-and-cite plus freshness pattern (Orange Book and SPL provenance with
`last_fetched_at`, multi-source synthesis) is wired for the White Paper only. The
Ask and Assemble read paths still query live HTTP without persisting source rows
or freshness.

- Where: `src/regwatch/sources/`, the Q&A and assemble handlers.

### Deficiency analyzer: the precedent KB gap

Shipped and live behind no flag (migration 0019, `POST /deficiency/analyze`,
`GET /deficiency/runs[/{id}]`, sidebar item 05 for every authenticated user). Two
gaps, both re-verified 2026-08-11:

- **The KB can never be populated today.** `store/deficiency_kb.py` `add_entries`
  has zero callers anywhere in `src/`, `tests/` or `scripts/`. Its own docstring
  calls it the loader seam. `deficiency/precedents.py` therefore short-circuits
  at `kb_count() == 0`, so precedents are always absent.
- **The dimension gate.** `precedents.py` builds
  `get_embedding_provider("qwen3")` and raises unless the provider reports 1024
  dims, to match `deficiency_kb`'s `vector(1024)`. That provider reads
  `qwen_embedding_dimension`, which still defaults to 1536 in
  `config/settings.py`. So whatever process loads or queries the KB needs
  `QWEN_EMBEDDING_DIMENSION=1024` plus the endpoint credentials. The app already
  talks to that endpoint for retrieval; do not assume a script or a cron does.
- Decide whether in-API-process PDF parsing survives past MVP. A durable queue is
  the documented upgrade path.
- Where: `src/regwatch/deficiency/`, `src/regwatch/store/deficiency_kb.py`.

### Answer-gate follow-ups

- `MATERIALITY_WORDS` in `generate/turn_gate.py` is still a hand-written literal
  and could be narrowed from logged traffic.
- `common/citations.py::filter_citations` has no production caller left, only
  `tests/test_citations.py`. Retire the function and its tests together, or keep
  it on purpose and write down why.
- Where: `src/regwatch/generate/turn_gate.py`, `src/regwatch/common/citations.py`.

### Leftovers worth keeping

Small real items that would otherwise be lost when their source docs moved to
[`archive/`](archive/):

- **Evidence drawer accessibility.** The shell behind the drawer is not `inert`.
  The aria-modal attribute and the scrim shipped; nothing in the frontend sets
  `inert`.
- **White Paper phase 3.** Batch populate, watch-driven staleness flagging,
  run-to-run section diff and overlay edit history
  ([`archive/WHITEPAPER_RUNS_PHASE2_DESIGN.md`](archive/WHITEPAPER_RUNS_PHASE2_DESIGN.md) section 11).
- **Old Supabase project cleanup.** Two dangling `auth.users` rows and an
  `auth.sessions` row with `not_after = NULL`, and `REVOKE anon, authenticated ON
  SCHEMA public` was never applied. Prod moved to Lakebase, so this only matters
  if that Supabase project still exists. Not checkable from the repo.

### Watchlist management for non-technical users

Watchlist products and watched products are managed through the API and the CLI.
There is no in-app way for an analyst to add or manage them.

- Where: `regwatch/frontend/` (Watch surface), `watch/watchlist.py`.

---

## Later

- **Cross-encoder reranker.** Exists as a hook, off by default
  (`RERANKER_ENABLED`). Turn it on and tune `VECTOR_TOP_K` if retrieval precision
  needs it. Retrieve and rerank failures on the ask path are audited (INV-6).
- **Further corpus families.** None are approved. Any future expansion requires
  an explicit policy and product decision; arbitrary FDA/public sources must not
  be added opportunistically.
- **Kubernetes or Helm.** Only if the deploy outgrows Fly and Compose.
- **Refactor backlog.** The 120-item working list in
  [`archive/REFACTOR_BACKLOG_2026-07-09.md`](archive/REFACTOR_BACKLOG_2026-07-09.md).

---

## Suggested order

1. Deploy migration 0023 and complete the authoritative-corpus canary, full
   deferred sync, embedding backfill, eval, activation, and rollback gates.
2. Provision the six Watch profile secrets and verify one manual dispatch. The
   code now fails closed until that owner action is complete.
3. Gateway and SSO, plus distributed rate limiting. That is the exposure
   boundary.
4. Restore drill and least-privilege credentials. Fold least-privilege into
   polyglot step 7 where you can.
5. Make the blocking eval run the v7 chain, then revalidate the 0.30 threshold in
   the Qwen3 space.
5. Observability export and the post-deploy smoke job.
6. Secrets policy and container resource limits.
7. UI smoke and load, plus watchlist UX. The polyglot deletion PR, R3 and steps 6
   to 9 run in parallel as capacity allows.
