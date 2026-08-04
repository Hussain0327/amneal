# REGWATCH Roadmap - Open / Not-Yet-Done Work

The single consolidated list of everything **not yet done**, aggregated from
every doc in the repo. If a doc and this file disagree on what remains, this
file plus [`PROD_READINESS.md`](PROD_READINESS.md) win.

- **Already shipped** (and therefore *not* listed here) lives in
  [`../README.md`](../README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), and the
  🟢 sections of [`PROD_READINESS.md`](PROD_READINESS.md). In short, live today:
  Router -> Handlers -> Synthesizer with INV-1..9 as tests; the seven FDA source
  handlers; cited conversational Q&A with token-delta SSE streaming; the White
  Paper populator plus its runs automation; the deficiency analyzer; the unified
  Next.js shell on Vercel; the Go edge holding all public traffic on Fly (native auth /
  sessions / feedback / settings / products plus `POST /query` orchestration -
  polyglot steps 0-5); Supabase Postgres+pgvector as the only datastore (R5);
  prod LLM inference on Databricks-hosted `gpt-oss-20b` inside the company
  tenant (all roles, OpenAI as rollback); continuous deployment
  (`deploy.yml` ships every green `main` push) and the daily watch cron.
- `PROD_READINESS.md` holds the prod-launch detail; items below map to its gates
  (#1-#11) where applicable. This file additionally captures product/quality and
  future items that aren't strictly launch gates.

_Status: 2026-08-04, against `feat/deficiency-mvp`. Since the 2026-07-29 stamp:
the DefPredict deficiency analyzer shipped (migration 0019) and the structured
turn contract replaced the per-sentence citation gate (`0a96f7e`). Prod query
embeddings are still OpenAI — no `ACTIVE_EMBEDDING_PROFILE` secret is set on the
Fly app, so the D1 blocker below remains fully open._

Legend: 🔴 blocks external exposure · 🟡 should-have before launch · ⚪ decision needed · 🔵 future / optional

---

## 🔴 Launch blockers - must land before any external exposure

### D1 - close the embedding leak, then arm the guard  (PROD_READINESS #5)
The data-handling decision is MADE (2026-07-28: Databricks inference plane,
in-tenant open weights - see
[`DATABRICKS_ADOPTION_2026-07-28.md`](DATABRICKS_ADOPTION_2026-07-28.md) and
[`DATA_RESIDENCY_D1.md`](DATA_RESIDENCY_D1.md)) and generation already runs
in-tenant. What still leaves the perimeter: query embeddings
(OpenAI `text-embedding-3-small`) and the watch cron's ingest embeddings
(`WATCH_OPENAI_API_KEY`). In order: wire the Qwen3 1024-dim embedding profile
(the `workspace.default.regwatch-embed` Model Service exists, 2026-07-29) ->
re-embed the corpus -> retrieval benchmark old-vs-new -> flip
`ACTIVE_EMBEDDING_PROFILE` (app and watch cron in the same change,
`SECRETS_RUNBOOK.md` §3.4) -> arm `D1_ENFORCED` with BOTH the endpoint alias
and the served model id allowlisted.
- Where: `src/regwatch/process/embedder.py`, `config/settings.py`,
  [`OPEN_MODEL_ROLLOUT.md`](OPEN_MODEL_ROLLOUT.md).
- Done when: no analyst-derived text leaves the tenant on any path and the
  boot + runtime residency guard is armed.
- **Blocking sub-item — the dimension parity gap.** `.github/workflows/
  watch-daily.yml` maps four `QWEN_*` secrets plus `ACTIVE_EMBEDDING_PROFILE`,
  but **not** `QWEN_EMBEDDING_DIMENSION`, and its preflight does not require it.
  `config/settings.py` defaults `qwen_embedding_dimension` to **1536** while the
  deployed endpoint is **1024**-dim, and `process/embedder.py` raises on a
  provider/profile mismatch. So the first FDA revision after a profile promotion
  fails *after* the preflight has passed. Add the var to the workflow env block
  **and** the preflight required-set, and add `WATCH_QWEN_EMBEDDING_DIMENSION`
  to [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md) §3.4, before flipping.

### Gateway / SSO + distributed rate limiting  (PROD_READINESS #1)
App-layer auth is done (the Go edge owns cookie sessions, ownership checks,
and the login brute-force cap) and TLS is done (Fly `force_https`,
`AUTH_COOKIE_SECURE` pinned). Remaining: OIDC/SSO against the corporate IdP
(per [`DECISIONS.md`](DECISIONS.md), deferred to the IT gateway - app-layer
cookie sessions are the pilot boundary), and the rate limiter is
in-memory/per-process across two proxy machines, so the effective fleet
ceiling is ~2x the configured rate until limiting is distributed or the
gateway owns it.
- Where: `go/internal/api/`, `fly.toml`.
- Done when: enterprise auth fronts the app (or the cookie-session layer is
  formally accepted as the pilot boundary) and rate limiting is not
  per-process.

### Datastore proof work - restore drill + least-privilege creds  (PROD_READINESS #2)
Supabase Postgres+pgvector is provisioned, migrated, and serving prod; the
migration release gate is done (`release_command = "alembic upgrade head"`).
What remains is proof: a rehearsed restore drill (`scripts/restore_drill.sh`
was deleted in R5 and a drill has never been run - see `DEPLOY.md` §6.4) and
least-privilege app DB credentials. Note polyglot step 7 (Python drops to a
read-only DB role) IS the least-privilege implementation path.
- Where: [`DEPLOY.md`](DEPLOY.md), `src/regwatch/store/db.py`.
- Done when: a restore drill against staging has passed and least-priv
  credentials are in place (or formally waived).

### UI production smoke + load behind the approved gateway  (PROD_READINESS #4)
The UI is feature-complete and live on Vercel. Remaining is deploy-time proof
for the exposure decision, not code.
- Where: `regwatch/frontend/`, [`DEPLOY.md`](DEPLOY.md) smoke checklist.
- Done when: smoke flows pass behind the approved auth/gateway path and a load
  test has run.

---

## 🟡 Operability hardening - should-have before launch

### Observability  (PROD_READINESS #6)
Structured logging, audit rows, privacy-scrubbed Sentry wiring, component
`/health`, `/ready`, and `/metrics` counters exist. Per-turn latency is now
captured (`query_log.latency_ms`, migration 0016, both runtimes) but not
exported as histograms. Missing: latency/**cost** metrics export, tracing, a
configured production Sentry DSN, and a decision on a paid live LLM
reachability probe.
- Where: `common/logging.py`, `common/observability.py`, `common/audit.py`,
  `api/main.py`, `go/internal/api/`.

### Watch operations  (PROD_READINESS #7)
The production cron is LIVE (`watch-daily.yml`, 07:17 UTC daily: crawl ->
match -> ingest -> durable alerts -> digest, with Slack failure notification,
a success-digest post, healthcheck pings, and an advisory threshold sweep).
Remaining is operational: keep the secrets provisioned
([`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md)), monitor real scheduled run
history, and decide whether alert delivery moves beyond `/watch/latest` +
Slack into product-facing email/digests. Deferred from the July watch wave:
alert ack-state and durable parsed text.
- Where: `watch/run.py`, `watch/alerts.py`, `.github/workflows/watch-daily.yml`.

### Polyglot strangler - finish the migration
Steps 0-5 of [`POLYGLOT_TARGET_2026-07-10.md`](POLYGLOT_TARGET_2026-07-10.md)
are done (Go edge + native `/query`, flipped live 2026-07-24). Remaining:
- The **step-5 deletion PR** - remove the now-dead Python `/query`
  orchestration path.
  [`STEP5_INV_TEST_MAPPING.md`](STEP5_INV_TEST_MAPPING.md) is the prerequisite
  INV-coverage mapping.
- **R3** - the safe-prefix streaming rewrite (must preserve the
  `D1ResidencyError`-excluded SSE fallback from #138).
- **Steps 6-9**: coarse write commands for ingest/Watch (6), Python to a
  read-only DB role (7 - the least-priv item above), the Rust PDF CLI with
  shadow parity (8), `CommitWhitepaperRun` + delete the Python persistence
  layer (9).

### ⚪ Refusal-threshold validation
`REFUSAL_SCORE_THRESHOLD=0.30` remains provisional
([`THRESHOLD_VALIDATION_2026-06-25.md`](THRESHOLD_VALIDATION_2026-06-25.md)).
The watch cron now produces a real OpenAI-1536 + pgvector artifact, but its five
must-refuse cases all stopped before retrieval and supplied no negative cosine
scores. It therefore cannot justify keeping or retuning `0.30`. Add scored hard
negatives and rerun the corrected harness; see
[`EVAL_STATUS.md`](EVAL_STATUS.md).

### Secrets management  (PROD_READINESS #10)
`.env` and friends are gitignored, the Actions secret surface is documented in
[`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md), and prod uses Fly/Vercel platform
secrets. Needs an approved secret manager/platform policy and rotation that is
documented AND rehearsed.

### CI security residual  (PROD_READINESS #11)
The supply-chain gates (pip-audit, npm audit, Trivy image scans) are green and
enforced. Residual: container resource limits (none in `compose.yaml` /
`fly.toml`).

---

## 🟡 Product & quality

### Eval expansion  (PROD_READINESS #8)
Gold sets are small (12 Q&A + 16 white-paper rows; spec wants 30-50) and
scoring is mechanical `(short_name, page)` + `expected_facts`. The latest CI
run skipped provider-backed evaluation, so current live-corpus pass/fail is
unverified. The previously reported `0.917` was the advisory threshold sweep's
`current_decision_accuracy`, not `run_eval.refusal_accuracy`; it came from
misclassifying the valid must-clarify case. Expand and re-pair the gold set to
what the seed actually ingests, add scored hard negatives, turn the live gate on
for an explicit baseline run, and add LLM-as-judge alongside the mechanical
metrics. See [`EVAL_STATUS.md`](EVAL_STATUS.md).
- Where: `src/regwatch/eval/`, `gold_set.jsonl`, `tests/test_eval_gate.py`.

### Graph-assisted adaptive retrieval
The deterministic Tier-1 graph foundation is landed (`0018_knowledge_graph`,
`store/graph_store.py`): application/document/section nodes, typed hierarchy
and adjacency edges, and references back to the citable chunks. No runtime
query path reads it yet.

The proposed consumer starts from scoped seed chunks, performs bounded typed
traversal, reranks the expanded source chunks, and tests evidence sufficiency.
It may make one targeted second expansion before refusing. Runtime traversal
must stay default-off until the eval set is expanded and the graph-assisted path
shows lower false-refusal/ranking-miss rates without citation, scope, latency,
or context-budget regressions. Full design and staged gates:
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md).
- Where: `src/regwatch/store/graph_store.py`, `src/regwatch/retrieve/`,
  `src/regwatch/generate/grounded_qa.py`, `src/regwatch/eval/`.

### Persist-and-cite beyond the White Paper  (PROD_READINESS #9)
The persist-and-cite + freshness pattern (OB/SPL provenance with
`last_fetched_at`, multi-source synthesis) is wired for the White Paper but
**not** the Ask/Assemble read paths, which still query live HTTP without
persisting source rows/freshness.
- Where: `src/regwatch/sources/`, the Q&A/assemble handlers.

### Deficiency analyzer (DefPredict) - close the precedent-KB gap
Shipped and live behind no flag (migration 0019, `POST /deficiency/analyze`,
`GET /deficiency/runs[/{id}]`, sidebar item 05 for every authenticated user).
Two verified gaps:
- **The precedent KB can never be populated today.** `store/deficiency_kb.py`
  `add_entries` has **zero callers** in `src/`, `tests/`, or `scripts/` — its own
  docstring calls it "the loader seam". `deficiency/precedents.py` therefore
  short-circuits at `kb_count() == 0` and precedents are always absent.
- **The moment it *is* loaded, every run hard-raises.** `precedents.py` builds
  `get_embedding_provider("qwen3")` and raises unless `provider.dim == 1024`,
  while `settings.py` defaults `qwen_embedding_dimension` to 1536 and no
  `QWEN_EMBEDDING_BASE_URL` / `_TOKEN` Fly secret exists. Set the credentials and
  `QWEN_EMBEDDING_DIMENSION=1024` **before** loading the KB.
- Note for the D1 section above: the qwen3 provider is already reachable in
  production through this path, which contradicts framing it as fully unwired.
- Decide whether in-API-process PDF parsing survives past MVP (a durable queue is
  the documented upgrade path).
- Where: `src/regwatch/deficiency/`, `src/regwatch/store/deficiency_kb.py`.

### Structured turn contract - the follow-ups `0a96f7e` deferred
The commit that replaced the per-sentence citation gate with the `claims[]`
envelope names these as explicitly not-in-scope:
- Instrument `output_tokens` and re-size `SYNTHESIZER_MAX_TOKENS` from data. The
  900 -> 1600 raise was a sized guess: a truncated JSON envelope is
  *unparseable*, where truncated prose merely lost a sentence.
- Narrow `MATERIALITY_WORDS` (a hand-written literal in `generate/turn_gate.py`)
  using logged traffic.
- `common/citations.py::filter_citations` is dead in production code — the prose
  gate was its only caller — but `tests/test_citations.py` still imports and
  exercises it. Retire the function and its tests together, or keep it
  deliberately and say why.
- Where: `src/regwatch/generate/turn_gate.py`, `turn_schema.py`,
  `common/citations.py`.

### Post-deploy smoke against the live stack
On 2026-08-03 an answer-path regression reached production users — real queries
refused with "not in corpus", copy describing a retrieval failure that never
happened — and **the entire CI suite was blind to it**. `deploy.yml` has no step
that asks one real question through the deployed stack and asserts
`status == answer`. Every gate is offline or fixture-backed.
- Done when: a post-deploy job queries the live app end-to-end and fails the
  deploy (or alerts) on anything other than an answered, cited turn.
- Where: `.github/workflows/deploy.yml`, `src/regwatch/eval/`.

### Rescued from archived docs
Small, real items that would otherwise have been lost when their source docs
moved to [`archive/`](archive/):
- **Supabase P1.1** — two dangling `auth.users` rows and an `auth.sessions` row
  with `not_after = NULL` in the Supabase project. Live-DB state, not verifiable
  from the repo; the original audit called it "a trap for the future".
- **Supabase P2.4** — `REVOKE anon/authenticated ON SCHEMA public`, as
  defense-in-depth beyond the RLS deny-all.
- **Evidence drawer a11y** — the shell behind the drawer is not `inert`
  (aria-modal and the scrim shipped). Appears in no other doc.
- **White Paper phase 3** — batch populate, watch-driven staleness flagging,
  run-to-run section diff, and overlay edit history
  ([`WHITEPAPER_RUNS_PHASE2_DESIGN.md`](WHITEPAPER_RUNS_PHASE2_DESIGN.md) §11).

### Non-technical product / watchlist management UX
Watchlist products and watched products are managed via API/CLI; there is no
in-app non-technical UX to add/manage them.
- Where: `regwatch/frontend/` (Watch surface), `watch/watchlist.py`.

---

## 🔵 Future / optional

- **Cross-encoder reranker** - exists as a hook, off by default
  (`RERANKER_ENABLED`); enable + tune `VECTOR_TOP_K` if retrieval precision
  needs it. Retrieve/rerank failures on the ask path are audited (INV-6).
- **Corpus expansion beyond PSGs** - broaden the retrievable corpus past
  product-specific guidances. (`ingest/`, `sources/`)
- **Ingest hardening at scale** - multi-worker Alembic init race + large-ingest
  resilience for `regwatch ingest-all`. (`store/db.py`, `ingest/`)
- **Kubernetes / Helm** - manifests or a chart if the deploy outgrows
  Fly/Compose.
- **Refactor backlog** - the 120-item working list in
  [`REFACTOR_BACKLOG_2026-07-09.md`](REFACTOR_BACKLOG_2026-07-09.md).

---

## Suggested order
1. **D1 embedding flip + arm the guard** (in flight - the endpoint exists;
   wire, re-embed, benchmark, flip, arm).
2. **Gateway/SSO + distributed rate limiting** (the exposure boundary).
3. **Restore drill + least-privilege creds** (fold least-priv into polyglot
   step 7 where possible).
4. **Observability export** + **watch ops proof** (operability).
5. **Eval expansion** (also unblocks the live CI gate) + **persist-and-cite
   beyond the White Paper**.
6. **Secrets policy** + **container resource limits**.
7. **UI production smoke/load** + watchlist management UX. The polyglot
   deletion PR, R3, and steps 6-9 proceed in parallel as capacity allows.
