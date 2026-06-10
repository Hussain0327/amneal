# Production Readiness Checklist

Status of `regwatch` against a real production deployment. The codebase today
is a strong POC: clean Router → Handlers → Synthesizer architecture, compliance
invariants enforced as tests (INV-1..9), and CI running lint / type / test /
eval / docker build. This document tracks what stands between that and prod.

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

### 2. Production-grade datastores
- **Where:** SQLite at [`config/settings.py:81`](../config/settings.py),
  on-disk Chroma at `chroma_dir`.
- **Gap:** Single-node, no concurrency/HA, no connection pooling, no
  backup/restore. Fine for one container, not for prod load.
- **Done when:** Postgres (pgvector or a served vector store) provisioned;
  backup + point-in-time restore tested; connection pooling configured.

### 3. Migrations run as a deploy step, not on app boot
- **Where:** `init_db()` in the FastAPI lifespan,
  [`src/regwatch/api/main.py:42`](../src/regwatch/api/main.py).
- **Gap:** Schema changes apply during app startup. Risky under multiple
  replicas (race) and hides failures in boot logs.
- **Done when:** Alembic `upgrade head` is an explicit, gated deploy/CI step;
  app boot only verifies schema version and refuses to start on mismatch.

### 4. Production UI deployment + hardening
- **Where:** Next.js UI at [`regwatch/frontend/`](../regwatch/frontend/);
  Python backend source remains [`src/regwatch/`](../src/regwatch/).
- **Gap:** The TypeScript UI exists for Ask / Assemble / Watch, but it is not
  containerized, authenticated, load-tested, or deployed behind a production
  gateway.
- **Done when:** the UI is deployed behind the approved auth/gateway path; API
  origin/proxy behavior is documented for that environment; and Streamlit is
  explicitly retired or kept internal-only.

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
  audit rows ([`common/audit.py`](../src/regwatch/common/audit.py)).
- **Gap:** no metrics, no tracing, no error tracking (Sentry-equivalent).
  `/health` is liveness-only.
- **Done when:** request/latency/cost metrics exported; readiness probe that
  checks DB + vector store + LLM reachability; error tracking wired.

### 7. Automated ingest / watch scheduling
- **Where:** APScheduler noted as a stub (README "What's not done").
- **Gap:** The "watch" value prop — PSG crawl → change detect → digest — runs
  only on demand. No cadence.
- **Done when:** scheduled worker/cron runs the crawl + change detection +
  digest reliably, with run logging that respects INV-4 (never report a run
  that didn't happen).

### 8. Eval hardening
- **Where:** [`src/regwatch/eval/`](../src/regwatch/eval/), `gold_set.jsonl`,
  `tests/test_eval_gate.py`.
- **Have:** a deterministic, offline eval gate (`tests/test_eval_gate.py`) that
  fires in CI on every `uv run pytest`; `fact_recall` scores answer content
  (`expected_facts`), and `faithfulness`/`fact_recall` print on `run_eval`.
- **Gap:** gold set is 11 items (spec §10.11 wants 30–50); live-corpus scoring
  is still mechanical `(short_name, page)` + `expected_facts` substrings;
  LLM-as-judge not wired, so semantically-equivalent answers undercount.
- **Done when:** gold set expanded to 30–50 paired to what the seed actually
  ingests; LLM-as-judge added alongside mechanical metrics; thresholds
  (`recall@k≥0.90`, `citation_precision≥0.95`, `refusal_accuracy≥0.95`) hold.

### 9. Structured-source layer completion
- **Where:** [`src/regwatch/sources/`](../src/regwatch/sources/) handlers.
- **Gap:** Drugs@FDA, Orange Book, NDC, Shortages, REMS are handler-level
  evidence today — no persisted source tables, freshness metadata, caching,
  or cross-source answer synthesis.
- **Done when:** sources persisted with freshness/last-fetched metadata,
  cached, and synthesized into a multi-source answer graph.

### 10. Secrets management
- **Where:** `.env` on disk, [`config/settings.py`](../config/settings.py).
- **Gap:** demo-grade secret handling.
- **Done when:** secrets sourced from a secret manager (Vault / cloud KMS /
  platform env), not a checked-out file; key rotation documented.

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

---

## Suggested order
1. **#1 API auth + rate limit** — cheapest big win, unblocks any exposure.
2. **#5 LLM/data-handling decision** — needs you; gates compliance sign-off.
3. **#2 + #3 datastore + migration discipline** — infra foundation.
4. **#6 observability** + **#7 scheduling** — operability.
5. **#8 eval** + **#9 sources** — answer quality at scale.
6. **#10 secrets** + **#11 CI security** — harden.
7. **#4 production UI** — parallelizable once the API contract is auth-gated.
