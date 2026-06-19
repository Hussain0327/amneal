# RegWatch Engineering Audit — 2026-06-19

**System:** FDA regulatory-intelligence RAG (Python/FastAPI backend, Next.js 16 frontend, Postgres+pgvector via Supabase, Fly.io + Vercel, GitHub Actions CI/cron).
**Method:** 16-agent workflow — 9 dimension experts (xhigh + ultrathink) + adversarial verification (2 skeptics per critical/high gap: one reproduces the evidence, one challenges severity) + synthesis.
**Branch:** `main` · **Scope:** 9-dimension audit, verifier-adjudicated.

> This document is the canonical record of the audit so remediation does not drift. The **Remediation Log** at the bottom is updated as items land.

---

## Verdict

**Production-ready for a controlled pilot. Grade B+. No confirmed P0.**

The fundamentals are strong: clean layered architecture with real port/adapter seams for the vector store and LLM providers, adversarially-tested refuse-or-cite safety invariants (INV-1..6), a deterministic offline eval gate wired into CI, a tested DR restore drill, and — verified — **every fix from the Jun-18 production lock-pileup incident is present on `main`** (per-connection timeouts, lock-safe RLS/DDL, self-migrating Fly `release_command`). All configured quality gates (ruff, black, mypy strict, tsc strict, eslint 9) pass clean.

The remaining work is **release-engineering and operational maturity, not defects**: manual deploys, single-machine prod, a silent ingest cron, a root container, and a completely untested frontend. None cause an outage, breach, data loss, or wrong answer **today**.

### Correction on the headline performance scare (PERF-1)
The claim "every query brute-force scans all ~10.7k chunks because HNSW is disabled" is **REFUTED for the real production path** (re-verified directly):
- `pgvector_store.py:226-228` builds **btree indexes** on `chunk(normalized_name, doc_id, version_id, appl_no)`.
- `grounded_qa.ask` resolves a `normalized_name` before reaching `retrieve()` (`grounded_qa.py:910`); unresolved paths return early via `_clarify`/`_refuse` (`grounded_qa.py:682-801`).
- `SET LOCAL enable_indexscan = off` (`pgvector_store.py:499`) disables *plain* index scans, but **bitmap index scans stay on** — Postgres narrows to one drug's ~6 chunks via `ix_chunk_normalized_name` first, then exact-orders.

So it is a bitmap-narrowed exact scan over a handful of rows, **not** a full-corpus scan. It is ms-cheap. Adopt `hnsw.iterative_scan` only when filtered-retrieval p95 > ~50ms **or** the chunk table exceeds ~100k rows. Tracked, non-urgent.

---

## Scorecard

| Dimension | Grade | One-liner |
|---|:--:|---|
| Architecture & System Design | B | Clean layered RAG, excellent vector/LLM seams; no repository seam for Postgres; `ARCHITECTURE.md` drifted. |
| Modularity, SOLID & Patterns | A | Swappable strategies proven by Echo tests, zero import cycles, contract-first FE/BE types. |
| Implementation Quality & Standards | A | All gates clean, near-total typing, exemplary backend timeouts; one 633-line `ask()`. |
| Version Control & Config Mgmt | A | No tracked secrets/artifacts, typed pydantic Settings, lockfiles committed; `.env.example` drift, no secret-scan. |
| Deployment, CI/CD, IaC & Containers | B- | Great CI + verified incident fixes; **no deploy automation, root container, single-machine downtime, silent cron**. |
| Testing Strategy & Coverage | B | 574 backend tests + adversarial invariants + offline eval gate; **zero frontend tests**, no pytest-timeout. |
| Maintenance & Tech Debt | B | No TODO/dead-code, real Sentry+health, append-only `DECISIONS.md`; silent cron, stale README, no Dependabot. |
| Security Engineering | B | Auth chokepoint, parameterized SQL, tested RLS/leak controls; weak password/lockout, Report-Only CSP. |
| Performance Engineering | B- | Strong DB/embedding I/O hygiene; 10.7k-scan claim refuted; **no load testing**, pool/threadpool size mismatch. |

---

## Strengths (verified — credit where due)

- **Refuse-or-cite is tested adversarially, not theater.** `test_invariants.py` / `test_cross_drug_leak.py` / `test_grounded_qa_citations.py` drive the real `ask()` pipeline and prove fabricated `[PSG_999999, p.7]` markers are stripped, a beclomethasone question cannot cite albuterol's identical boilerplate, and an uncited confident answer collapses to refusal — with an audit row on **every** path including failure (INV-6).
- **Every Jun-18 incident fix is verified on `main`:** per-conn timeouts (`db.py:207`), lock-safe RLS skipping contended tables (`db.py:374`), lock-safe index DDL (`db.py:331`), migration-only `lock_timeout` (`db.py:231`), self-migrating `release_command` (`fly.toml:18`).
- **Real hexagonal seams:** `store/vector_store.py` is a true Chroma↔pgvector facade; `LLMProvider`/`EmbeddingProvider` Protocols keep model names out of business logic, proven swappable by Echo impls (LSP/DIP).
- **Deterministic offline eval gate** (`test_eval_gate.py`) acts as CI acceptance testing (recall ≥0.90, citation_precision ≥0.95, refusal_accuracy ≥0.95, faithfulness == 1.0); CI exercises the **real pgvector datastore** via a service container.
- **Disciplined implementation:** ruff+black+mypy(strict)+tsc(strict)+eslint9 all clean; near-total typing; zero TODO/FIXME/bare-except; exemplary external-call timeout discipline.
- **Operational maturity for a POC:** Sentry with PII/SQL scrubbing + prod-fails-loud-on-missing-DSN, component-level `/health`, append-only 24-entry `DECISIONS.md`, a tested DR restore drill with a production-ref destructive-op guard.
- **Config/VCS hygiene is genuinely clean:** no secrets in code or full git history, no tracked `.env`/`.coverage`/artifacts, centralized typed Settings, both lockfiles committed, CI CVE gating (pip-audit + npm audit + Trivy).

---

## Per-dimension: Have vs. Gaps

### Architecture & System Design — B
- **Have:** layered `sources→ingest→process→store→retrieve→generate` pipeline; clean uvicorn-only API / Vercel proxy / separate-cron topology; clean vector-store + LLM ports; Strategy-pattern source handlers with per-source fault isolation.
- **Gaps:** `ARCH-1`/`MOD-2` (M→tracked) no repository/port seam for Postgres — 18 domain/composer modules import `session_scope` and write raw SQLAlchemy inline; `ARCH-2` (L) `populator.py` 2,255-line god-module mutating a 30-field `_Ctx`; `ARCH-3` (S) `ARCHITECTURE.md` drift; `ARCH-4` (S) no import-linter fitness function; `ARCH-5` (M) scope-filtered Ask path disables ANN by design — see PERF-1 correction.

### Modularity, SOLID & Patterns — A
- **Have:** swappable LLM/embedding/source strategies via Protocol+factory, proven by Echo tests; zero circular imports; contract-first FE/BE types with a CI drift gate; shared I/O helpers with `owned_client` resource cleanup; thin HTTP layer.
- **Gaps (all low):** `MOD-1` vector facade leaks Chroma `$eq/$and` dialect; `MOD-3` duplicated app-number normalization in `psg.py`/`orange_book.py`; `MOD-4` `SourceHandler.client` meaningless for the local PSG handler.

### Implementation Quality & Standards — A
- **Have:** all gates clean; near-total return-type coverage; exemplary timeout discipline; post-incident DB lock hardening; scoped/code-tagged suppressions; 100% module docstrings.
- **Gaps:** `IMPL-1` (M) 633-line, 25-branch `ask()` on the safety path; `IMPL-2` (S) frontend `fetch` wrappers lack default AbortSignal/timeout; `IMPL-3` (M) ~59% public-fn docstring coverage.

### Version Control & Config Mgmt — A
- **Have:** no tracked secrets/artifacts; centralized typed Settings; lockfiles committed; CI CVE gating; 12-factor env separation; conventional commits + clean branch hygiene.
- **Gaps:** `CFG-1` (S) `.env.example` drift incl. the Jun-18 `DB_*_TIMEOUT` knobs; `CFG-2` (S) `RERANKER_ENABLED` bypasses Settings; `CFG-3` (S) no secret-scanning gate.

### Deployment, CI/CD, IaC & Containers — B-
- **Have:** CI runs lint+typecheck+test+eval+supply-chain+contract-drift on every PR against real pgvector; all incident fixes present; reversible/backward-compatible migrations; documented layered rollback + tested restore drill; boot guard prevents version-skew crash loops; `/health` + uptime probe + fail-loud env.
- **Gaps:** `CD-1` (M, **P1**) no deploy automation — manual `fly deploy` ships a non-CI image; `CD-2` (S, **P1**) single-machine prod = downtime per deploy, no canary; `CONT-1` (M, **P1**) root container, no HEALTHCHECK, unpinned base; `CD-3`/`OBS-1` (S, **P1**) watch-daily cron has no failure alert; `IAC-1` (M) IaC/secrets partly out-of-band + DEPLOY.md migration drift.

### Testing Strategy & Coverage — B
- **Have:** 574 tests, ~82% line coverage, adversarial INV-1..6, failure-mode coverage (provider RuntimeError→audited refusal, httpx timeout resilience, restore-drill prod-ref guard), offline eval gate, pgvector exercised in CI.
- **Gaps:** `TEST-1` (L, **P1**) zero frontend tests; `TEST-2` (S, **P1**) no pytest-timeout; `TEST-3` (M) `watch/aliases.py` 0%; `TEST-4`/`PERF-2` (M) no perf/load test; `TEST-5` (S) resolver degradation path untested; `TEST-6` (S) stale `.coverage` + no coverage gate.

### Maintenance & Tech Debt — B
- **Have:** no TODO/dead-code; real backend Sentry+`/health`; tested DR drill; append-only `DECISIONS.md`; audience-tagged onboarding docs.
- **Gaps:** `OBS-1` = `CD-3` (S, **P1**) silent watch-daily cron; `DOC-1` (S) stale README stack table; `MAINT-1` (S) no Dependabot/Renovate; `MAINT-2` (M) Supabase Auth DB leftovers — decision debt; `MAINT-3` (S) no documented VACUUM/REINDEX cadence.

### Security Engineering — B
- **Have:** single auth chokepoint, parameterized SQL throughout, **tested** RLS deny-all + cross-drug-leak controls.
- **Gaps:** `SEC-2` (M, **P1**) no password-strength policy; `SEC-3` (M, **P1**) login limiter email-only, no per-IP/lockout; `SEC-1` (S) `SECURITY.md` falsely says CI has no scanning; `SEC-4` (M) CSP Report-Only w/ unsafe-inline; `SEC-5` (M) CSRF on SameSite+CORS, no token; `SEC-6` (S) PDF fetch follows cross-host redirects (cron-only, not API-reachable).

### Performance Engineering — B-
- **Have:** pooling w/ pre-ping/recycle + per-conn timeouts; batched/retried/timed OpenAI embeddings; shared SDK client; sync endpoints offloaded; no N+1 in list endpoints; lock-safe HNSW build; bulk-insert batching.
- **Gaps:** `PERF-1` **refuted** as an outage — tracked perfective only; `PERF-2` (M) no load/stress test or SLOs; `PERF-3` (S) DB pool (10) < threadpool (40) starvation risk; `PERF-4` (M) retrieve() metadata fan-out; `PERF-5` (M) no OpenAI-embedding cache; `PERF-6` (M) HNSW params unvalidated vs gold set.

---

## Prioritized Backlog

### P0 — none
Every confirmed finding is a missing safeguard or maturity gap, not an active outage/breach/data-loss/wrong-answer defect. The Jun-18 incident class is fixed and verified.

### P1 — fix before broader rollout
| # | ID | Item | Area | Effort |
|:-:|---|---|---|:--:|
| 1 | `CD-3`/`OBS-1` | watch-daily cron has no failure alerting → stale-answers-without-warning | Maint/Deploy | **S** |
| 2 | `CD-1` | No deploy automation — prod ships a non-CI image via manual `fly deploy` | Deploy | M |
| 3 | `CD-2` | Single-machine prod (`min_machines_running=1`) = downtime per deploy, no canary | Deploy | **S** |
| 4 | `CONT-1` | API container runs as root, unpinned base, single-stage, no HEALTHCHECK | Deploy/Sec | M |
| 5 | `TEST-1` | Entire Next.js frontend has zero automated tests | Testing | L |
| 6 | `SEC-2`/`SEC-3` | No password-strength policy + login limiter keyed only on email | Security | M |
| 7 | `TEST-2` | No pytest-timeout — a hung test can stall CI to the 6h ceiling | Testing | **S** |

### P2 — hygiene / tracked
`ARCH-3` ARCHITECTURE.md drift · `DOC-1` README stack table · `CFG-1` `.env.example` drift + drift test · `CFG-3` CI secret-scan (`--scanners ...,secret` or gitleaks) · `MAINT-1` Dependabot/Renovate · `IMPL-1` decompose `ask()` · `ARCH-1`/`MOD-2` incremental repository seam into `store/queries.py` · `ARCH-2` split `populator.py` · `TEST-6` coverage gate (`--cov-fail-under=80`) · `TEST-3`/`TEST-5` untested degradation paths · `ARCH-4` import-linter · `PERF-2`/`PERF-3` load test + pool sizing · `MOD-1`/`MOD-3`/`CFG-2` minor SOLID/DRY · `SEC-1`/`SEC-4`/`SEC-5`/`SEC-6` defense-in-depth · `PERF-1` `hnsw.iterative_scan` (recall-gated, non-urgent).

---

## Remediation Log

| Date | Items | Status | Commit/Branch | Notes |
|---|---|---|---|---|
| 2026-06-19 | Audit | Complete | — | 16-agent 9-dim audit, B+, no P0 |
| 2026-06-19 | **P1-S batch:** `CD-3`/`OBS-1`, `CD-2`, `TEST-2` | **Committed + pushed to `main`** | `2382271` | cron failure-alert + dead-man's-switch; `min_machines_running` 1→2; pytest-timeout 60s. |
| 2026-06-19 | **P2-quick batch:** `ARCH-3`, `DOC-1`, `CFG-1`, `CFG-2`, `CFG-3`, `MAINT-1`, `TEST-6`, `ARCH-4`, `MOD-3` | **Committed + pushed to `main`** | `e3a2595` + `5a6df34` | docs drift fixed; .env.example + drift test; reranker→Settings; Trivy `vuln,secret`; Dependabot (`uv` ecosystem); coverage floor 80 (measured 82%); import-linter (2 contracts); sources DRY. Gate green: 557 pass/25 skip, ruff/black/mypy/lint-imports clean. |
| 2026-06-19 | `IMPL-2` (frontend fetch timeout) | **HELD** | — | Spec ready; `regwatch/frontend/lib/api.ts` is the concurrent agent's territory — apply after that work settles. |
| 2026-06-19 | `.env` reconciled to `.env.example` | Done | local file | Appended 11 optional default knobs; 49/49 keys match. |

> Pushed to `origin/main` on 2026-06-19: `e3a2595` (CI/deps), `2382271` (ops), `5a6df34` (config/docs), on top of `ff3f6a2` (merged PR #16, UX S1). Remaining: P1-Medium (`CD-1`, `CONT-1`, `SEC-2`/`SEC-3`) + `IMPL-2`.
