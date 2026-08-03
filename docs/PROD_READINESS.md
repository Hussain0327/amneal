# Production Readiness Checklist

Status of `regwatch` against production. The system IS deployed: the Fly app
`amneal` runs a Go edge (`go/internal/api` -- auth, sessions, rate limiting,
and native `POST /query`) in front of the Python RAG core, with Supabase
Postgres+pgvector as the only datastore and the Next.js UI on Vercel. The
architecture is Router -> Handlers -> Synthesizer with compliance invariants
enforced as tests (INV-1..9); CI runs lint / type / test / eval / docker
build plus the cross-service contract lane (`tests_contract/`, real compiled
Go proxy + uvicorn + Postgres). This document tracks the remaining gaps.

This is an active readiness checklist. Historical notes and original planning
docs are archived in `docs/README.md`; when they conflict with this file, use
this file plus `README.md`, `docs/ARCHITECTURE.md`, and `docs/DEPLOY.md` as the
current source of truth.

Legend: 🔴 blocking · 🟡 should-have before launch · 🟢 done · ⚪ decision needed

Each item notes where it lives in the tree so the work is actionable cold.

---

## 🔴 Blocking — must land before any external exposure

### 1. API authentication & authorization 🟡 (app layer + TLS done - OIDC/SSO and distributed rate limiting remain)
- **Where:** the Go edge now owns these natively since the polyglot step-4
  cutover -- `go/internal/api/` (`auth.go`, `sessions.go`, `ratelimit.go`);
  Python retains [`src/regwatch/api/main.py`](../src/regwatch/api/main.py)
  and [`src/regwatch/auth/`](../src/regwatch/auth/) for the surfaces it still
  serves.
- **Now in place:** cookie-session auth on every endpoint except the open
  probes (`GET /health`, `/ready`, `/metrics` -- the last bearer-gated when
  `METRICS_TOKEN` is set)
  — DB-backed opaque tokens (sha256 at rest), bcrypt passwords, CLI-provisioned
  users (`regwatch create-user` / `set-password` / `deactivate-user`); per-user
  chat history with ownership enforcement (a foreign `session_id` 404s); audit
  rows carry the caller identity (`query_log.user_id`, INV-6); per-user rate
  limiting on `POST /query` / `POST /assemble` (`RATE_LIMIT_PER_MINUTE`) plus a
  fixed 10/email/minute login brute-force cap; CORS allowlist with credentials.
  TLS is done: Fly terminates HTTPS (`force_https = true`) and
  `AUTH_COOKIE_SECURE = "true"` is pinned in `fly.toml`.
- **Remaining gap:** OIDC/SSO against the corporate IdP, and distributed rate
  limiting -- the limiter is in-memory/per-process across 2 proxy machines, so
  the effective fleet ceiling is roughly 2x the configured rate.
- **Done when:** enterprise auth (OIDC/SSO) fronts the app (or the
  cookie-session layer is formally accepted as the pilot boundary), and rate
  limiting is distributed rather than per-process.

### 2. Production-grade datastores 🟡 (live on Supabase - restore drill + least-priv creds remain)
- **Where:** Postgres-only storage in [`config/settings.py`](../config/settings.py),
  Postgres bootstrap in [`src/regwatch/store/db.py`](../src/regwatch/store/db.py),
  pgvector chunks in
  [`src/regwatch/store/pgvector_store.py`](../src/regwatch/store/pgvector_store.py),
  and the cutover runbook in [`DEPLOY.md`](DEPLOY.md).
- **Now in place:** the structured store is Postgres and vectors are pgvector
  in the same database, everywhere — R5 deleted the SQLite/Chroma dual-mode,
  so `DATABASE_URL` is unconditionally mandatory (the app refuses to boot
  without it, no flag needed); pgvector dimension checks fail fast; the
  deploy runbook covers Supabase, migration, smoke checks, rollback, uptime,
  and a staging restore (currently a manual Supabase-backup restore; a
  scripted PG-native `pg_dump` drill is open work, see #7-adjacent notes in
  `DEPLOY.md` §6.4).
- **Remaining gap:** the managed database (Supabase Postgres+pgvector) is
  provisioned, migrated, smoke-tested, and serving prod. What remains is
  proof work: a rehearsed restore drill (the scripted
  `scripts/restore_drill.sh` was deleted in R5 and the drill has never been
  run) and least-privilege app database credentials accepted or implemented.
- **Done when:** a restore drill against staging has passed and least-priv
  credentials are in place (or formally waived).

### 3. Migration discipline 🟢 (release gate landed: fly.toml release_command)
- **Where:** `release_command = "alembic upgrade head"` in `fly.toml`
  `[deploy]`; `init_db()` in
  [`src/regwatch/store/db.py`](../src/regwatch/store/db.py); schema-release
  instructions in [`DEPLOY.md`](DEPLOY.md).
- **Now in place:** schema-advancing releases run Alembic as a gated deploy
  step -- Fly's `[deploy]` `release_command` runs `alembic upgrade head`
  before the machines roll, so the release fails before any new code serves
  if the migration fails. Postgres startup still verifies the Alembic stamp
  matches head and refuses to start on mismatch (boot = verification only).
- **Residual:** rollback/roll-forward rehearsal is folded into the restore
  drill in #2; migrations remain required to be backward-compatible and
  reversible.

### 4. Production UI deployment + hardening 🟡 (Vercel path landed)
- **Where:** Next.js App Router UI at [`regwatch/frontend/`](../regwatch/frontend/)
  (Streamlit fully retired); Python backend source remains
  [`src/regwatch/`](../src/regwatch/).
- **Now in place:** all four surfaces — Ask, Assemble, Watch, White Paper —
  render inside one App Router `(shell)` route group with one sidebar and one
  set of design tokens (commit 2720f1b). A URL-scoped `CurrentProduct`
  (`?rp=&appl=`) is shareable, survives reload, and is read by all four
  surfaces. Ask is rebuilt as a cited conversational chat (right-aligned user
  bubbles, gold RW avatar, citation chips that link to FDA sources with full
  snippets behind a Sources disclosure, clarify option pills, a bottom-pinned
  composer, Enter-to-send — commit f30eaef). An "Under review" product-scope
  bar runs across all four surfaces as the front-door setter via a resolve-
  backed picker (commits f30eaef, c5e7f93). Plus: login, per-user sessions,
  same-origin `/api` proxying, Sentry opt-in, and frontend CI (`npm ci`, lint,
  build, plus a frontend docker build). The Vercel + Fly/Railway deploy path is
  documented in [`DEPLOY.md`](DEPLOY.md).
- **Remaining gap:** production smoke, load testing, approved gateway/SSO path,
  and non-technical product/watchlist management UX are still launch work.
  Token-delta streaming is DONE: `/query/stream` emits provisional cosmetic
  `token` delta frames plus the single validated terminal `result` frame
  (`_sse_event("token", ...)` in `src/regwatch/api/main.py`); INV-1 holds
  because the authoritative answer text ships only in the validated terminal
  frame.
- **Done when:** the UI is deployed behind the approved auth/gateway path; API
  origin/proxy behavior is verified for that environment; and the analyst flows
  in the deploy smoke checklist pass.

### 5. 🟡 LLM provider + data-handling decision (D1) - decision MADE 2026-07-28
- **Where:** `llm_provider` in [`config/settings.py`](../config/settings.py);
  [`DATA_RESIDENCY_D1.md`](DATA_RESIDENCY_D1.md),
  [`DATABRICKS_ADOPTION_2026-07-28.md`](DATABRICKS_ADOPTION_2026-07-28.md).
- **Now in place:** the decision is taken -- Databricks-hosted open-weights
  inference inside Amneal's tenant, inference plane only. Prod generation
  runs gpt-oss-20b behind `workspace.default.regwatch` since 2026-07-28
  (OpenAI is rollback only), and the runtime residency guard (`D1_ENFORCED` /
  `D1_ALLOWED_LLM_MODELS` / `D1ResidencyError`) is deployed but unarmed.
- **Remaining gap:** query embeddings still go to OpenAI
  (`text-embedding-3-small`) -- the last real leak. In order: wire the
  Qwen3 1024-dim embedding profile, re-embed the corpus, benchmark
  retrieval, flip `ACTIVE_EMBEDDING_PROFILE`, then arm `D1_ENFORCED` with
  BOTH the endpoint alias and the served model id in the allowlist. The
  decision itself is logged in [`DECISIONS.md`](DECISIONS.md).
- **Done when:** the embedding flip lands and the guard is armed.

---

## 🟡 Should-have before launch

### 6. Observability
- **Have:** structured logging ([`common/logging.py`](../src/regwatch/common/logging.py)),
  audit rows ([`common/audit.py`](../src/regwatch/common/audit.py)), privacy-
  scrubbed Sentry wiring, `/health` component diagnostics for DB, vector store,
  provider names, key presence, corpus count, and warnings; `/ready` for DB +
  vector-store reachability plus LLM client constructability; and `/metrics`
  Prometheus counters derived from `query_log` (bearer-gated when
  `METRICS_TOKEN` is set).
- **Gap:** Sentry is optional and a missing production DSN only logs a warning.
  Per-turn latency is now captured (`query_log.latency_ms`, migration 0016,
  written by both runtimes), but it is not yet exported as histograms; cost
  gauges, tracing, and a live paid LLM reachability probe are also still
  missing.
- **Done when:** latency/cost metrics and tracing are exported; the production
  load balancer/scraper uses `/ready` and `/metrics`; error tracking is
  configured in the production environment; and the team decides whether a paid
  LLM reachability probe is worth its cost/noise.

### 7. Automated ingest / watch scheduling 🟡 (GitHub cron path landed)
- **Where:** [`src/regwatch/watch/run.py`](../src/regwatch/watch/run.py) and
  [`.github/workflows/watch-daily.yml`](../.github/workflows/watch-daily.yml).
  (The Dagster orchestration package, `src/regwatch/orchestration/`, was
  deleted in R5 — GitHub Actions cron is the sole scheduler.)
- **Now in place:** `regwatch watch` runs crawl → match → ingest matched PSGs
  → build durable alerts → write digest; GitHub Actions defines the
  production `watch-daily` cron with failure Slack notification, healthcheck
  pings, and advisory threshold-sweep artifact upload. Partial-ingest recovery
  is covered by re-surfacing committed-but-unalerted versions from durable DB
  rows (`appl_nos_without_alert`).
- **Remaining gap:** production ops must keep `WATCH_DATABASE_URL` and
  `WATCH_OPENAI_API_KEY` configured, verify real scheduled runs complete, and
  decide whether alert delivery should move beyond `/watch/latest` + optional
  Slack failure notifications into product-facing email/Slack digests.
- **Done when:** a recent scheduled `watch-daily` run has completed against prod
  with monitored run history, healthcheck pings are active, analysts can see the
  resulting durable alerts, and any desired outbound alert channel is configured.

### 8. Eval hardening
- **Where:** [`src/regwatch/eval/`](../src/regwatch/eval/), `gold_set.jsonl`,
  `tests/test_eval_gate.py`.
- **Have:** a deterministic, offline eval gate (`tests/test_eval_gate.py`) that
  fires in CI on every `uv run pytest`; `fact_recall` scores answer content
  (`expected_facts`), and `faithfulness`/`fact_recall` print on `run_eval`.
- **Gap:** gold sets are still small (12 Q&A JSON rows, 16 white-paper cell
  rows; spec wants 30–50 for Q&A); live-corpus scoring is still mechanical
  `(short_name, page)` + `expected_facts` substrings; LLM-as-judge is not wired,
  so semantically-equivalent answers undercount. The latest inspected CI run
  skipped provider-backed `run_eval`, so current live pass/fail is unverified.
  The latest advisory threshold artifact had six scored answer rows but zero
  scored refusal rows; it cannot validate the `0.30` cosine cutoff. See
  [`EVAL_STATUS.md`](EVAL_STATUS.md).
- **Done when:** gold set expanded to 30–50 paired to what the seed actually
  ingests; LLM-as-judge added alongside mechanical metrics; thresholds
  (`recall@k≥0.90`, `citation_precision≥0.95`, `refusal_accuracy≥0.95`) hold.

### 9. Structured-source layer completion
- **Where:** [`src/regwatch/sources/`](../src/regwatch/sources/) handlers,
  [`src/regwatch/whitepaper/`](../src/regwatch/whitepaper/) populator.
- **Gap (largely closed by Gate 2):** Orange Book now parses
  patent/exclusivity from the same cached ZIP; DailyMed (SPL) is a new handler;
  the White-Paper populator persists Orange Book product/patent/exclusivity and
  the DailyMed SPL resolution with `last_fetched_at` freshness
  (`ob_product`/`ob_patent`/`ob_exclusivity`/`spl_document`, migration 0005) and
  synthesizes a multi-source, cited cell graph (Orange Book + Drugs@FDA + NDC +
  DailyMed + Shortages + REMS + PSG). **Remaining:** apply the same
  persist-and-cite pattern to the conversational/assemble paths, and a broader
  cross-source answer graph beyond the white paper.
- **Done when:** the persisted-source + freshness pattern is the default across
  every read path, not only the white paper.

### 10. Secrets management
- **Where:** `.env` on disk, [`config/settings.py`](../config/settings.py).
- **Have:** `.env`, `.env.local`, local data, generated docs, and
  logs are gitignored; the production runbook uses platform secrets for
  `DATABASE_URL`, `OPENAI_API_KEY`, `SENTRY_DSN`, and optional `OPENFDA_API_KEY`.
- **Gap:** production still needs an approved secret manager/platform policy and
  documented key rotation.
- **Done when:** secrets are sourced from approved platform/secret-manager
  injection and rotation is documented/tested.

### 11. Supply-chain & security in CI  ✅ landed 2026-06-17
- **Where:** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
- **Done:** CI gates on `pip-audit` (Python, via `uv export`), `npm audit`
  (frontend prod deps), and Trivy scans of the API + web images. The scans were
  added in #10 and turned green in #11: Next.js 14.2 → 16.2.9 (React stays 18;
  no patched 14.x existed for the HIGH advisories) with the ESLint-9 flat-config
  migration (`next lint` removed in 16); the web image runs `apt-get upgrade` +
  pins npm 11.17.0 (clearing the base image's Debian + npm-bundled-dep CVEs); and
  the frontend lockfile was regenerated cross-platform (it had omitted the
  linux/wasm `@emnapi` branch, which broke `npm ci` on the runners).
- **Residual:** container resource limits (none in `compose.yaml`/`fly.toml`).
  (The chromadb `pip-audit` ignore this line used to reference no longer
  applies — chromadb was removed from dependencies along with the
  SQLite/Chroma dual-mode in R5.)

---

## 🟢 Already in good shape
- Router → Handlers → Synthesizer architecture with clear interfaces.
- Compliance invariants INV-1..9 enforced as tests.
- Pluggable `EmbeddingProvider` / `LLMProvider` (no hard-coded models in
  business logic).
- Alembic migration baseline + incremental migrations.
- CI: ruff, black, mypy strict, pytest, eval thresholds, docker build, the Go
  proxy lane, schema-drift checks, and the cross-service contract lane, on
  Python 3.12 (see [`CI_CD.md`](CI_CD.md)).
- Frontend CI: `npm ci`, `npm run lint`, `npm run build`, and `npm test`
  (vitest) for `regwatch/frontend`.
- Docker baseline (API + ingest services) with persistent `./data` mount; the
  Next.js UI (`regwatch/frontend/`) runs as its own process.
- Read-only `/settings` endpoint that never leaks secrets.
- Entity-resolution hardening (canonicalized product key, comparison→clarify,
  mixed-product→clarify) and conversational sessions with conversational audit.
- Deterministic resolve front-door: `POST /resolve` reuses the White Paper's
  `_build_context` to return the canonical spine without running an LLM turn —
  it writes NO audit row (success or failure) and returns no answer text, and
  422s with no scope on a mismatch (refuse over guess). Product scope is
  settable from three surfaces — the scope-bar picker, a successful White Paper
  populate, and a Watch row — all writing the canonical `{normalized_name,
  six-digit application_number}`.

---

## Suggested order
1. **#5 LLM/data-handling decision** — needs business/compliance sign-off.
2. **#1 gateway/SSO/distributed rate limiting** — final exposure boundary.
3. **#2 + #3 production datastore proof + migration release gate**.
4. **#6 observability** + **#7 production Watch worker** — operability.
5. **#8 eval expansion** + **#9 source persistence beyond White Paper**.
6. **#10 secrets policy** + **#11 CI security scans**.
7. **#4 UI production smoke/load/product-management polish**.
