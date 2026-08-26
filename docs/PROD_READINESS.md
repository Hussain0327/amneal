# Production readiness checklist

Last updated: 2026-08-17 for the authoritative FDA corpus rollout. Unrelated
live app, database and Fly values were last checked 2026-08-11; repository
implementation status is intentionally separate from deployed state.

The system is deployed and running. The Fly app `amneal` (release v104) runs a Go
edge (`go/internal/api`: auth, sessions, rate limiting, native `POST /query`) in
front of the Python RAG core. The database is Databricks Lakebase, Postgres plus
pgvector in one place. The UI is Next.js on Vercel. Generation and embeddings
both run on Databricks Model Serving inside the company's own tenant. The shape
is Router -> Handlers -> Synthesizer, with the compliance invariants INV-1..9
enforced as tests. CI runs lint, type, test, eval, docker build, and the
cross-service contract lane (`tests_contract/`, a real compiled Go proxy plus
uvicorn plus Postgres).

This page tracks what is still missing. Open work is also listed, with more
context, in [`ROADMAP.md`](ROADMAP.md). Historical planning docs live in
[`archive/`](archive/); when they disagree with this file, this file plus
`README.md`, [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`DEPLOY.md`](DEPLOY.md)
are the current truth.

Labels: **BLOCKING** before external exposure. **SHOULD-HAVE** before launch.
**DONE**. **DECISION** needs a person to choose.

Each item says where the work lives so it is actionable cold.

---

## Gates 1 to 5: the exposure path

Two of these are now done. The numbers are kept because other docs cite them.

### 1. API authentication and authorization  SHOULD-HAVE (app layer and TLS done, SSO and distributed rate limiting remain)

- **Where:** the Go edge owns this since the polyglot step-4 cutover:
  `go/internal/api/` (`auth.go`, `sessions.go`, `ratelimit.go`). Python still
  keeps [`src/regwatch/api/main.py`](../src/regwatch/api/main.py) and
  [`src/regwatch/auth/`](../src/regwatch/auth/) for the surfaces it still serves.
- **In place:** cookie-session auth on every endpoint except the open probes
  (`GET /health`, `/ready`, `/metrics`, the last bearer-gated when
  `METRICS_TOKEN` is set). DB-backed opaque tokens (sha256 at rest), bcrypt
  passwords, CLI-provisioned users (`regwatch create-user` / `set-password` /
  `deactivate-user`). Per-user chat history with ownership checks, so a foreign
  `session_id` 404s. Audit rows carry the caller (`query_log.user_id`, INV-6).
  Per-user rate limiting on `POST /query` and `POST /assemble`
  (`RATE_LIMIT_PER_MINUTE`) plus a fixed 10 per email per minute login cap. CORS
  allowlist with credentials. TLS is done at the Fly edge: `force_https = true`
  and `AUTH_COOKIE_SECURE = "true"` are pinned in `fly.toml`.
- **Missing:** OIDC/SSO against the corporate IdP behind the IT TLS gateway, and
  distributed rate limiting. The limiter is in-memory per process across two
  proxy machines, so the real fleet ceiling is about twice the configured rate.
- **Done when:** enterprise auth fronts the app, or the cookie-session layer is
  formally accepted as the pilot boundary, and rate limiting is no longer
  per-process.

### 2. Production datastore  SHOULD-HAVE (live on Lakebase, restore drill and least-privilege creds remain)

- **Where:** Postgres-only storage in
  [`config/settings.py`](../config/settings.py), bootstrap in
  [`src/regwatch/store/db.py`](../src/regwatch/store/db.py), pgvector chunks in
  [`src/regwatch/store/pgvector_store.py`](../src/regwatch/store/pgvector_store.py),
  runbook in [`DEPLOY.md`](DEPLOY.md).
- **In place:** production Postgres is Databricks Lakebase
  (`ep-super-hat-d8wkrjd9.database.us-east-2.cloud.databricks.com`, database
  `databricks_postgres`, app role `regwatch_app`), with pgvector in the same
  database. Rows, vectors and audit all live together. Corpus migrations 0023
  and 0024 are deployed; this follow-up adds the evidence-backed terminal ledger
  in `0025_fda_terminal_resolution`. The corrected production canary passed
  21 / 21 and produced 499 chunks with complete active-profile embeddings. The
  full 140,438-record backfill is operator-owned and retrieval remains on
  `legacy`. `DATABASE_URL` is unconditionally required and the app refuses to
  boot without it (R5 deleted the SQLite and Chroma dual-mode). pgvector
  dimension checks fail fast.
  - Note on history: the earlier call
    ([`DATABRICKS_ADOPTION_2026-07-28.md`](DATABRICKS_ADOPTION_2026-07-28.md))
    was "Supabase stays, Databricks for inference only". That call was reversed
    and the database move to Lakebase already happened.
- **Missing:** proof. Nobody has ever rehearsed a restore, and the scripted
  `scripts/restore_drill.sh` was deleted in R5. The app also still connects with
  a full-privilege role; polyglot step 7 (Python drops to a read-only role) is
  the least-privilege path.
- **Done when:** a restore drill against staging has passed and least-privilege
  credentials are in place, or formally waived.

### 3. Migration discipline  DONE

- **Where:** `release_command = "regwatch release"` under `[deploy]` in
  `fly.toml`; `init_db()` in
  [`src/regwatch/store/db.py`](../src/regwatch/store/db.py); release steps in
  [`DEPLOY.md`](DEPLOY.md).
- **In place:** Fly runs `regwatch release` in a one-off machine before the
  machines roll. It advances Alembic to head and then runs the same full
  serving-readiness guard as an app cold boot, so migration failures and active
  embedding-profile drift fail before any long-lived machine is replaced. App
  boot repeats the verification to cover drift after the release preflight.
- **Residual:** rollback and roll-forward rehearsal is folded into the restore
  drill in #2. Migrations still have to be backward-compatible and reversible.

### 4. Production UI  SHOULD-HAVE (Vercel path landed, smoke and load remain)

- **Where:** Next.js App Router UI at
  [`regwatch/frontend/`](../regwatch/frontend/); Python backend at
  [`src/regwatch/`](../src/regwatch/). Streamlit is fully retired.
- **In place:** the scoped surfaces, Ask, Assemble, Watch, White Paper and
  Deficiency, render inside one App Router `(shell)` route group with one sidebar
  and one set of design tokens. A URL-scoped `CurrentProduct` (`?rp=&appl=`) is
  shareable, survives reload and is read by all of them. Ask is a cited
  conversational chat: right-aligned user bubbles, citation chips that link to
  FDA sources with full snippets behind a Sources disclosure, clarify pills, a
  bottom-pinned composer. An "Under review" product-scope bar runs across the
  scoped surfaces as the front-door setter, backed by `POST /resolve`. Plus
  login, per-user sessions, same-origin `/api` proxying, Sentry opt-in and
  frontend CI (`npm ci`, lint, build, vitest, frontend docker build). The deploy
  path is in [`DEPLOY.md`](DEPLOY.md).
- **Streaming is done.** `POST /query/stream` emits provisional `token` and
  `draft` delta frames while the model writes, then a single validated terminal
  `result` frame. INV-1 holds because only that terminal frame is authoritative;
  the provisional text is cosmetic and can be reset.
- **The Compliance Studio (`/studio`) is not production-relevant yet.** It sits
  outside the shell. One seam is real: the left rail lists the FDA PSG corpus
  from the database (`GET /psg/documents`), renders each PSG as a document
  (`/content`), generates a .docx of it on request (`/docx`) and streams the
  original PDF inline. The document
  service, the compliance pipeline and the assistant are fixtures, and nothing
  recorded there survives a refresh. See
  [`COMPLIANCE_STUDIO.md`](COMPLIANCE_STUDIO.md).
- **Missing:** production smoke, load testing, the approved gateway path, and an
  in-app way for non-technical users to manage products and the watchlist.
- **Done when:** the UI is deployed behind the approved auth path, proxy and
  origin behavior is verified there, and the analyst flows in the deploy smoke
  checklist pass.

### 5. LLM provider and data handling (D1)  REVERSED for model calls 2026-08-20

- **Where:** `llm_provider` and the embedding-profile settings in
  [`config/settings.py`](../config/settings.py);
  [`DATABRICKS_ADOPTION_2026-07-28.md`](DATABRICKS_ADOPTION_2026-07-28.md);
  history in [`archive/DATA_RESIDENCY_D1.md`](archive/DATA_RESIDENCY_D1.md).
- **Was closed 2026-07-30 through 2026-08-19.** All three legs ran inside the
  company's Databricks tenant:
  - generation: `workspace.default.regwatch`, serving `gpt-oss-120b` (served id
    `gpt-oss-120b-080525`, repointed from `gpt-oss-20b` on 2026-08-05). One model
    served every role: router, synthesizer, extractor.
  - query and corpus embeddings: `workspace.default.regwatch-embed`, Qwen3, 1024
    dim, profile `ep_2e7368b354d911ea3a013c3125e276c2`, 5,494 of 5,494 chunks
    covered since 2026-07-30.
  - the database: Lakebase, see #2.
- **Deliberately reversed for the two model-call legs, 2026-08-20, by owner
  decision.** Generation moves to OpenAI `gpt-5.6-terra` over the Responses
  API (no `temperature`); embeddings move to OpenAI `text-embedding-3-large`
  truncated to 1024 dimensions via the `dimensions` param. An analyst question
  now leaves for a third-party model API on the normal path -- that is the
  intended outcome of the decision, not a leak. The
  database leg is untouched: Lakebase still holds every chunk and vector,
  in-tenant, unchanged. Be precise about the split: only the model calls moved.
  `EMBEDDING_PROVIDER` and `LLM_PROVIDER` stay required-explicit (no default;
  unset refuses to boot).
- **No guard was bypassed.** `D1_ALLOWED_LLM_MODELS` plus a runtime
  served-model check in `generate/llm.py` reject a reply served by a model
  outside the allowlist, and always reject the partner-hosted families
  (`databricks-gpt*`, `databricks-claude*`, `databricks-gemini*`) even if
  someone allowlists them by hand. That runtime check only runs when
  `D1_ENFORCED` is on, and it was never on in prod: verified 2026-08-11 and
  unchanged through the 2026-08-20 reversal, the variable is in neither the
  Fly secrets nor `fly.toml` and defaults to `false`, so the check has been
  inert the entire time. The guard code stays in the repo for a possible
  future re-migration to Databricks.
- **Residual:** serving is on the profile. Scheduled Watch is now coded to use
  that same Qwen profile and fail closed. Its change-summary/extraction LLM
  work moved on 2026-08-17 (the OpenAI provider was removed) to the repo-wide
  `DATABRICKS_LLM_BASE_URL`/`_TOKEN` secrets and `DATABRICKS_LLM_MODEL`
  variable, which were already provisioned for the eval workflow (verified
  2026-08-17) -- no new provisioning needed. See #7 and
  [`ROADMAP.md`](ROADMAP.md).

---

## Should-have before launch

### 6. Observability

- **Have:** structured logging
  ([`common/logging.py`](../src/regwatch/common/logging.py)), audit rows
  ([`common/audit.py`](../src/regwatch/common/audit.py)), privacy-scrubbed Sentry
  wiring, `/health` component diagnostics (DB, vector store, provider names, key
  presence, corpus count, warnings), `/ready` for DB and vector-store
  reachability plus LLM client constructability, and `/metrics` Prometheus
  counters derived from `query_log` (bearer-gated when `METRICS_TOKEN` is set).
  The replacement corpus adds a durable run ledger, per-document typed outcome
  and duration logs, exact document/chunk/embedding coverage, pending counts,
  source-family breakdowns, policy-violation counts, and activation blockers.
- **Gap:** Sentry is optional and a missing production DSN only logs a warning.
  Per-turn latency is captured (`query_log.latency_ms`, migration 0016, written
  by both runtimes) but not exported as histograms. Cost gauges, tracing and a
  live paid LLM reachability probe are all still missing.
- **Done when:** latency and cost metrics and tracing are exported, the
  production load balancer or scraper uses `/ready` and `/metrics`, error
  tracking is configured in production, and the team decides whether a paid LLM
  reachability probe is worth the noise.

### 7. Automated ingest and watch scheduling  SHOULD-HAVE (cron landed)

- **Where:** [`src/regwatch/watch/run.py`](../src/regwatch/watch/run.py) and
  [`.github/workflows/watch-daily.yml`](../.github/workflows/watch-daily.yml).
  GitHub Actions cron remains the only scheduler for Watch. The new private
  Dagster control plane is isolated to the authoritative FDA replacement corpus
  and does not schedule Watch.
- **In place:** `regwatch watch` runs crawl, watchlist match, ingest of matched
  PSGs, durable alerts, digest. The workflow adds Slack failure notification, a
  success digest, healthcheck pings and an advisory threshold-sweep artifact.
  Partial-ingest recovery re-surfaces committed-but-unalerted versions from
  durable DB rows (`appl_nos_without_alert`).
- **Recent history:** the cron failed every day from 2026-08-07 until the owner
  updated `WATCH_DATABASE_URL` on 2026-08-10. The manual run that evening and the
  scheduled run on 2026-08-11 both passed under the pre-parity workflow. The
  first run of this workflow revision is expected to stop at profile preflight
  until all six secrets are provisioned.
- **Gap:** the workflow-side parity fix is complete: six required settings,
  Qwen/profile mode, a registered-profile gate before crawl, and a zero-pending
  coverage assertion after attempted ingest. All six repository secrets were
  still absent on 2026-08-12, so the job fails before crawl until the owner
  provisions them and verifies a manual dispatch. Also open: whether alert
  delivery moves beyond `/watch/latest` plus Slack into product-facing email or
  digests.
- **Done when:** the cron runs on the same embedding profile as the app, run
  history is monitored, healthcheck pings are active, analysts can see the
  durable alerts, and any outbound alert channel is configured.

### 8. Eval hardening

- **Where:** [`src/regwatch/eval/`](../src/regwatch/eval/), `gold_set.jsonl`,
  `tests/test_eval_gate.py`,
  [`.github/workflows/databricks-eval.yml`](../.github/workflows/databricks-eval.yml).
- **Have:** a deterministic offline gate (`tests/test_eval_gate.py`) on every
  `uv run pytest`, plus a live Databricks eval that runs on every build and
  blocks the merge. The Q&A gold set is 62 rows and the White Paper gold set is
  16, with every quote verified present at its pinned `(short_name, page)` before
  scoring.
- **Gap:**
  - The blocking eval does not run what prod runs. `ci.yml` calls it with
    `prose: false, selective: false`, so the gate scores the old v5 claims chain
    while prod serves v6 prose plus v7 selective citation.
  - The blocking floors are a ratchet, not a quality bar: `recall_at_k` 0.80 and
    `citation_precision` 0.74, each set just below the first real measurement.
  - `refusal_accuracy` is measured but not gated, by owner decision, because the
    product is deliberately moving away from refusing.
  - Scoring is still mechanical `(short_name, page)` plus `expected_facts`
    substrings, so a correct answer worded differently undercounts. LLM-as-judge
    is not wired.
  - The CI eval runs against a seed corpus (66 chunks, 8 documents), not the
    5,494-chunk production corpus.
  See [`EVAL_STATUS.md`](EVAL_STATUS.md).
- **Done when:** the blocking eval runs the chain prod serves, LLM-as-judge sits
  alongside the mechanical metrics, and the aspirational targets
  (`recall@k` 0.90, `citation_precision` 0.95) are met rather than aspired to.

### 9. Structured-source layer

- **Where:** [`src/regwatch/sources/`](../src/regwatch/sources/) handlers,
  [`src/regwatch/corpus/`](../src/regwatch/corpus/) pipeline, migrations 0023–0025,
  and [`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md).
- **Have:** an exact five-family source policy; official Drugs@FDA and Orange
  Book ZIP adapters; action-package, PSG, and reviewed FDA BE-guidance discovery;
  versioned documents; bounded streamed fetch; durable content-addressed raw
  artifacts and exact manifests; sandboxed OCR; atomic chunk publication;
  separately checkpointed embedding state; 512 deterministic Dagster shards;
  blocking acceptance checks; exact coverage; an evidence-backed terminal
  outcome ledger; and a fail-closed reversible activation gate. The frozen
  production manifest contains 140,438 source records.
  Approved Drugs@FDA labeling now supplies White Paper and Assemble label evidence.
- **Policy closure:** retired public API configuration/endpoints are removed.
  DailyMed, NDC, shortage, and REMS acquisition paths are not routable and fail
  closed if an old caller reaches their compatibility shims.
- **Gap:** release migration 0025 without disturbing the owned live sweep;
  complete the 512-shard backfill; resolve its retry-exhausted tail into indexed
  versions or validated terminal outcomes; then run acceptance, new-corpus
  retrieval/citation evaluation, serving smoke, and rollback rehearsal. Do not
  freeze a second manifest under the active backfill.
- **Done when:** all 140,438 frozen source records resolve as
  `indexed + evidence-backed terminal`, authoritative indexed chunks have 100%
  selected-profile coverage, status reports `activation_ready=true`, evaluation
  passes, and cutover plus rollback are rehearsed.

### 10. Secrets management

- **Where:** `.env` on disk, [`config/settings.py`](../config/settings.py),
  [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md).
- **Have:** `.env`, `.env.local`, local data, generated docs and logs are
  gitignored. Production uses Fly and Vercel platform secrets for `DATABASE_URL`,
  the Databricks generation and embedding tokens, `INTERNAL_RAG_TOKEN`,
  and `SENTRY_DSN`. The authoritative FDA source layer needs no retired public
  API credential. The Actions secret surface is documented in the runbook.
- **Gap:** production still needs an approved secret manager or platform policy,
  and key rotation that is documented and rehearsed.
- **Done when:** secrets come from approved platform or secret-manager injection
  and rotation is documented and tested.

### 11. Supply chain and security in CI  DONE, landed 2026-06-17

- **Where:** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
- **Done:** CI gates on `pip-audit` (Python, via `uv export`), `npm audit`
  (frontend production deps), and Trivy scans of the API and web images. Getting
  them green took Next.js 14.2 to 16.2.9 (React stays 18; no patched 14.x existed
  for the HIGH advisories) with the ESLint 9 flat-config migration, an
  `apt-get upgrade` plus a pinned npm in the web image, and a cross-platform
  regeneration of the frontend lockfile (it had omitted the linux/wasm `@emnapi`
  branch, which broke `npm ci` on the runners).
- **Residual:** no container resource limits in `compose.yaml` or `fly.toml`.

---

## Already in good shape

- Router -> Handlers -> Synthesizer with clear interfaces.
- Compliance invariants INV-1..9 enforced as tests. Under v7 selective citation
  the headline rule is "cite the facts, talk like a person": only sentences
  stating what FDA guidance requires need passage numbers, and INV-1 still drops
  an uncited one.
- Pluggable `EmbeddingProvider` and `LLMProvider`, so no model name is hard-coded
  in business logic.
- Alembic baseline plus incremental migrations; current `main` contains
  `0022_deficiency_run_source`, this branch adds `0023_authoritative_fda_corpus`,
  and the last independently verified live head remains 0020.
- CI: ruff, black, mypy strict, pytest, the live Databricks eval, docker build,
  the Go proxy lane, schema-drift checks and the cross-service contract lane, on
  Python 3.12 (see [`CI_CD.md`](CI_CD.md)).
- Frontend CI: `npm ci`, `npm run lint`, `npm run build`, `npm test` (vitest).
- Docker baseline (API plus ingest services) with a persistent `./data` mount;
  the Next.js UI runs as its own process.
- Read-only `/settings` endpoint that never leaks secrets.
- Entity-resolution hardening (canonical product key, comparison to clarify,
  mixed-product to clarify) and conversational sessions with conversational
  audit.
- Deterministic resolve front door: `POST /resolve` reuses the White Paper's
  `_build_context` to return the canonical spine without running an LLM turn. It
  writes no audit row either way, returns no answer text, and 422s with no scope
  on a mismatch, so it refuses rather than guesses. Scope can be set from three
  places, the scope-bar picker, a successful White Paper populate, and a Watch
  row, all writing the same canonical `{normalized_name, six-digit
  application_number}`.

---

## Suggested order

1. **#7** provision the six Watch profile secrets and verify a manual dispatch;
   the workflow now fails closed until then.
2. **#1** gateway, SSO and distributed rate limiting. That is the exposure
   boundary.
3. **#2** restore drill and least-privilege credentials.
4. **#8** make the blocking eval run the chain prod serves, then **#6**
   observability export.
5. **#9** source persistence beyond the White Paper.
6. **#10** secrets policy and **#11** container resource limits.
7. **#4** UI smoke, load, and the product and watchlist management UX.
