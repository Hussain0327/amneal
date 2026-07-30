# REGWATCH Roadmap - Open / Not-Yet-Done Work

The single consolidated list of everything **not yet done**, aggregated from
every doc in the repo. If a doc and this file disagree on what remains, this
file plus [`PROD_READINESS.md`](PROD_READINESS.md) win.

- **Already shipped** (and therefore *not* listed here) lives in
  [`../README.md`](../README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), and the
  🟢 sections of [`PROD_READINESS.md`](PROD_READINESS.md). In short, live today:
  Router -> Handlers -> Synthesizer with INV-1..9 as tests; the seven FDA source
  handlers; cited conversational Q&A with token-delta SSE streaming; the White
  Paper populator plus its runs automation; the unified Next.js four-surface
  shell on Vercel; the Go edge holding all public traffic on Fly (native auth /
  sessions / feedback / settings / products plus `POST /query` orchestration -
  polyglot steps 0-5); Supabase Postgres+pgvector as the only datastore (R5);
  prod LLM inference on Databricks-hosted `gpt-oss-20b` inside the company
  tenant (all roles, OpenAI as rollback); continuous deployment
  (`deploy.yml` ships every green `main` push) and the daily watch cron.
- `PROD_READINESS.md` holds the prod-launch detail; items below map to its gates
  (#1-#11) where applicable. This file additionally captures product/quality and
  future items that aren't strictly launch gates.

_Status: 2026-07-29, against `main` after PR #138 (the D1 runtime served-model
guard + migration 0016 `query_log.latency_ms`) auto-deployed - `main` == prod._

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
`REFUSAL_SCORE_THRESHOLD=0.30` remains provisional - tuned on the gold set,
never validated in prod score space
([`THRESHOLD_VALIDATION_2026-06-25.md`](THRESHOLD_VALIDATION_2026-06-25.md)).
The sweep harness runs advisory in the watch cron and uploads artifacts; what
is missing is the decision: keep or retune, off real sweep data.

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
scoring is mechanical `(short_name, page)` + `expected_facts`. The live-corpus
eval currently FAILS `refusal_accuracy` (0.917 < 0.95) - this is why the
repo-wide `OPENAI_API_KEY` CI secret is deliberately withheld (see
[`CI_CD.md`](CI_CD.md)); expanding and re-pairing the gold set to what the
seed actually ingests is the path to turning the live gate back on. Add
LLM-as-judge alongside the mechanical metrics.
- Where: `src/regwatch/eval/`, `gold_set.jsonl`, `tests/test_eval_gate.py`.

### Persist-and-cite beyond the White Paper  (PROD_READINESS #9)
The persist-and-cite + freshness pattern (OB/SPL provenance with
`last_fetched_at`, multi-source synthesis) is wired for the White Paper but
**not** the Ask/Assemble read paths, which still query live HTTP without
persisting source rows/freshness.
- Where: `src/regwatch/sources/`, the Q&A/assemble handlers.

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
