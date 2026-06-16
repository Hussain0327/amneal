# Production Readiness Checklist

Status of `regwatch` against a real production deployment. The codebase today
is a strong POC: clean Router → Handlers → Synthesizer architecture, compliance
invariants enforced as tests (INV-1..9), and CI running lint / type / test /
eval / docker build. This document tracks what stands between that and prod.

This is an active readiness checklist. Historical notes and original planning
docs are archived in `docs/README.md`; when they conflict with this file, use
this file plus `README.md`, `docs/ARCHITECTURE.md`, and `docs/DEPLOY.md` as the
current source of truth.

Legend: 🔴 blocking · 🟡 should-have before launch · 🟢 done · ⚪ decision needed

Each item notes where it lives in the tree so the work is actionable cold.

---

## 🔴 Blocking — must land before any external exposure

### 1. API authentication & authorization 🟡 (app layer landed Jun 10 2026 — gateway/TLS remain)
- **Where:** [`src/regwatch/api/main.py`](../src/regwatch/api/main.py),
  [`src/regwatch/auth/`](../src/regwatch/auth/),
  [`src/regwatch/common/ratelimit.py`](../src/regwatch/common/ratelimit.py)
- **Now in place:** cookie-session auth on every endpoint except `GET /health`
  — DB-backed opaque tokens (sha256 at rest), bcrypt passwords, CLI-provisioned
  users (`regwatch create-user` / `set-password` / `deactivate-user`); per-user
  chat history with ownership enforcement (a foreign `session_id` 404s); audit
  rows carry the caller identity (`query_log.user_id`, INV-6); per-user rate
  limiting on `POST /query` / `POST /assemble` (`RATE_LIMIT_PER_MINUTE`) plus a
  fixed 10/email/minute login brute-force cap; CORS allowlist with credentials.
- **Remaining gap:** everything below the app layer is environment work — TLS
  termination (then `AUTH_COOKIE_SECURE=true`), OIDC/SSO against the corporate
  IdP, and a gateway so the app is never directly reachable. The rate limiter
  is in-memory/per-process; multi-replica deployments need gateway limiting.
- **Done when:** the IT gateway terminates TLS + enterprise auth in front of
  the app (or the cookie-session layer is formally accepted as the pilot
  boundary), and distributed rate limiting is owned by that gateway.

### 2. Production-grade datastores 🟡 (Postgres/pgvector path landed — proof remains)
- **Where:** dual-mode storage in [`config/settings.py`](../config/settings.py),
  Postgres bootstrap in [`src/regwatch/store/db.py`](../src/regwatch/store/db.py),
  pgvector chunks in
  [`src/regwatch/store/pgvector_store.py`](../src/regwatch/store/pgvector_store.py),
  and the cutover runbook in [`DEPLOY.md`](DEPLOY.md).
- **Now in place:** `DATABASE_URL` switches the structured store to Postgres
  and vectors to pgvector in the same database; `REQUIRE_DATABASE_URL=true`
  refuses SQLite fallback in production; pgvector dimension checks fail fast;
  the deploy runbook covers Supabase, migration, smoke checks, rollback, uptime,
  and staging restore drills.
- **Remaining gap:** the code/runbook are ready, but a real production launch
  still needs the managed database provisioned, the migration completed from a
  clean snapshot, backup/restore actually exercised, and least-privilege app
  database credentials accepted or implemented.
- **Done when:** production Postgres/pgvector is live, smoke-tested, monitored,
  and a restore drill against staging has passed.

### 3. Migration discipline 🟡 (verification landed — release gate remains)
- **Where:** `init_db()` in [`src/regwatch/store/db.py`](../src/regwatch/store/db.py),
  Docker entrypoint [`docker/entrypoint.sh`](../docker/entrypoint.sh), and
  schema-release instructions in [`DEPLOY.md`](DEPLOY.md).
- **Now in place:** Postgres startup verifies the Alembic stamp matches head and
  refuses to start on mismatch. `DEPLOY.md` requires `alembic upgrade head`
  before schema-advancing releases.
- **Remaining gap:** the container entrypoint still runs `regwatch init-db` by
  default, which is acceptable as a bootstrap/verify guard but not a substitute
  for a controlled release gate. Multi-replica production needs an explicit
  deploy step and operator runbook ownership for schema changes.
- **Done when:** schema-advancing releases run Alembic as a gated deploy step,
  app boot is treated as verification only, and rollback/roll-forward behavior
  is rehearsed.

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
  and non-technical product/watchlist management UX are still launch work. Ask
  has no real token-by-token streaming yet — the streaming-capable client
  targets `/query/stream`, which the backend does not implement, so it falls
  back to a blocking `POST /query` (the thinking ticker is honest, not faked);
  see ROADMAP.
- **Done when:** the UI is deployed behind the approved auth/gateway path; API
  origin/proxy behavior is verified for that environment; and the analyst flows
  in the deploy smoke checklist pass.

### 5. ⚪ LLM provider + data-handling decision (D1)
- **Where:** `llm_provider="openai"` default,
  [`config/settings.py:25`](../config/settings.py); Responses API.
- **Gap:** Every Q&A sends FDA-related queries to OpenAI. For a regulatory
  team this needs a deliberate data-handling review (BAA / zero-retention /
  in-house OpenAI-compatible model) before launch. **This is your call, not
  a code change.**
- **Done when:** provider chosen, data-processing terms reviewed, decision
  logged in [`DECISIONS.md`](DECISIONS.md).

---

## 🟡 Should-have before launch

### 6. Observability
- **Have:** structured logging ([`common/logging.py`](../src/regwatch/common/logging.py)),
  audit rows ([`common/audit.py`](../src/regwatch/common/audit.py)), privacy-
  scrubbed Sentry wiring, and `/health` component diagnostics for DB, vector
  store, provider names, key presence, corpus count, and warnings.
- **Gap:** Sentry is optional and a missing production DSN only logs a warning;
  there are still no exported request/latency/cost metrics, tracing, or LLM
  reachability readiness check.
- **Done when:** request/latency/cost metrics exported; readiness probe that
  checks DB + vector store + LLM reachability; error tracking configured in the
  production environment.

### 7. Automated ingest / watch scheduling 🟡 (local Dagster path landed)
- **Where:** [`src/regwatch/watch/run.py`](../src/regwatch/watch/run.py),
  [`src/regwatch/orchestration/definitions.py`](../src/regwatch/orchestration/definitions.py),
  and Compose Dagster services in [`compose.yaml`](../compose.yaml).
- **Now in place:** `regwatch watch` runs crawl → match → ingest matched PSGs
  → build alerts → write digest; Dagster defines `watch_digest_job` and a daily
  06:00 UTC schedule for the local/Compose orchestration path.
- **Remaining gap:** the production Fly/Vercel deploy keeps Watch/Dagster out
  of scope and suggests ad hoc runs. A known residual remains: if a version row
  commits before chunks embed and the run errors, a later run may treat it as
  unchanged and never alert without an `alerted_at` marker or durable-diff
  derivation.
- **Done when:** production has a supported scheduled worker/cron/Dagster
  deployment, monitored run history, failure recovery for partial ingest, and
  alerting that still respects INV-4.

### 8. Eval hardening
- **Where:** [`src/regwatch/eval/`](../src/regwatch/eval/), `gold_set.jsonl`,
  `tests/test_eval_gate.py`.
- **Have:** a deterministic, offline eval gate (`tests/test_eval_gate.py`) that
  fires in CI on every `uv run pytest`; `fact_recall` scores answer content
  (`expected_facts`), and `faithfulness`/`fact_recall` print on `run_eval`.
- **Gap:** gold sets are still small (12 Q&A JSON rows, 16 white-paper cell
  rows; spec wants 30–50 for Q&A); live-corpus scoring is still mechanical
  `(short_name, page)` + `expected_facts` substrings; LLM-as-judge is not wired,
  so semantically-equivalent answers undercount.
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
- **Have:** `.env`, `.env.local`, local data, Chroma stores, generated docs, and
  logs are gitignored; the production runbook uses platform secrets for
  `DATABASE_URL`, `OPENAI_API_KEY`, `SENTRY_DSN`, and optional `OPENFDA_API_KEY`.
- **Gap:** production still needs an approved secret manager/platform policy and
  documented key rotation.
- **Done when:** secrets are sourced from approved platform/secret-manager
  injection and rotation is documented/tested.

### 11. Supply-chain & security in CI
- **Where:** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
- **Gap:** runs ruff/black/mypy/pytest/eval/docker-build, but no dependency
  audit or container image scan.
- **Done when:** `pip-audit`/`uv` audit + container scan (e.g. Trivy) gate CI.

---

## 🟢 Already in good shape
- Router → Handlers → Synthesizer architecture with clear interfaces.
- Compliance invariants INV-1..9 enforced as tests.
- Pluggable `EmbeddingProvider` / `LLMProvider` (no hard-coded models in
  business logic).
- Alembic migration baseline + incremental migrations.
- CI: ruff, black, mypy strict, pytest, eval thresholds, docker build, on a
  3.11/3.12 matrix.
- Frontend CI: `npm ci`, `npm run lint`, and `npm run build` for
  `regwatch/frontend`.
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
