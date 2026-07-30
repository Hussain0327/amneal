# REGWATCH Documentation Index

Use this file as the map for the docs folder.

## Start Here

Open **`MAP.md`** first — the visual hub: a diagram of how the surfaces, API,
sources, and stores connect, plus links to every doc grouped by purpose. It is
also the center of the Obsidian graph view.

Then read these in order if you are new to the project:

1. `../README.md` - project overview, current stack, quick start, API surface.
2. `NON_TECH_GUIDE.md` - plain-English explanation for regulatory and business readers.
3. `TECH_GUIDE_SIMPLE.md` - simplified technical walkthrough of how the code connects.
4. `ARCHITECTURE.md` - canonical system design: Router -> Handlers -> Synthesizer, the four UI surfaces, and the compliance invariants.
5. `CONVERSATIONAL_SESSIONS.md` - chat sessions, follow-up context, and audit rules.
6. `DOCKER.md` - container setup, services, data mounts, and ingest notes.
7. `PROD_READINESS.md` - active production-readiness checklist.
8. `DEPLOY.md` - active production cutover and operations runbook.
9. `ROADMAP.md` - consolidated list of open / not-yet-done work (launch blockers and future workstreams).
10. `DECISIONS.md` - append-only history of important engineering decisions.

## Current Reference Docs

| File | Audience | Purpose |
|---|---|---|
| `MAP.md` | Everyone | Visual hub: a system diagram of how the four surfaces, API, FDA sources, and stores connect, plus links to every doc grouped by purpose. The center of the Obsidian graph. |
| `NON_TECH_GUIDE.md` | Non-technical stakeholders | Explains what REGWATCH does, what it must not do, and why citations/refusals matter. |
| `TECH_GUIDE_SIMPLE.md` | Technical readers | Explains folder structure, core flows, and the files to read first. |
| `ARCHITECTURE.md` | Engineers / reviewers | Canonical system design: Router -> Handlers -> Synthesizer, pluggable Embedding/LLM providers, the unified four-surface Next.js shell, and INV-1..9. |
| `CONVERSATIONAL_SESSIONS.md` | Product / engineering / compliance reviewers | Explains session IDs, follow-up context, response statuses, and auditability. |
| `DOCKER.md` | Engineers / deployment owners | Documents the Dockerfile, Compose services, embedding modes, and ingest implications. |
| `DECISIONS.md` | Engineers / reviewers | Append-only decision log. Read this before changing architecture. |
| `PROD_READINESS.md` | Engineers / deployment owners | Tracks what stands between the POC and a real production deployment, prioritized and mapped to the tree. |
| `ROADMAP.md` | Engineers / product / reviewers | Consolidated open / not-done work: launch blockers (LLM data-handling decision, provisioned Postgres/pgvector, gateway/TLS/SSO) and future workstreams (token-delta Ask streaming, eval expansion, observability, Watch cron proof). |
| `whitepaper_schema.md` | Engineers / regulatory reviewers | DRAFT one-row-per-cell field-extraction map for the White Paper populator: {source, endpoint/query or SPL section, lookup key, mode} per template cell. |
| `DEPLOY.md` | Engineers / deployment owners | Production cutover runbook (Supabase + Fly.io/Railway + Vercel) plus the Operations section: rollback levers, uptime monitoring, and the monthly staging restore drill (`scripts/restore_drill.sh`). |
| `CI_CD.md` | Engineers / contributors | The CI gate explained: every job mapped to the exact local command that satisfies each, a copy-paste pre-push checklist, and the recurring gotchas (black-not-ruff, stale `api-types.ts`, `uv.lock` drift, Trivy web-image esbuild + GHSA/CVE alias flips). Read before pushing. |
| `SECRETS_RUNBOOK.md` | Engineers / deployment owners | Every secret across the GitHub Actions workflows and Fly, what consumes it, and how to rotate it. |
| `DATA_RESIDENCY_D1.md` | Engineers / compliance | The D1 boundary: which paths send analyst queries off-perimeter, what is closed (generation, 2026-07-28), what remains (query embeddings + watch cron), and the runtime served-model guard. |
| `DATABRICKS_ADOPTION_2026-07-28.md` | Engineers / decision record | The Databricks verdict: inference plane only, Supabase stays; cost model; the gpt-oss-20b flip and the truncation-incident knobs. |
| `POLYGLOT_TARGET_2026-07-10.md` | Engineers | The approved 4-runtime strangler plan. Steps 0-5 done (Go edge + native /query); 6-9 open. |
| `GO_PROXY_ROLLOUT.md` | Engineers | How the Go proxy took the public edge, including the deploy-incident history (complete). |
| `GO_NATIVE_QUERY_ROLLOUT.md` | Engineers | The step-5 `/query` cutover runbook; flip live 2026-07-24; the rollback lever. |
| `OPEN_MODEL_ROLLOUT.md` | Engineers | Embedding profiles + open-weight providers: shipped dormant 2026-07-23; the embedding half is now in flight. |
| `STEP5_INV_TEST_MAPPING.md` | Engineers / reviewers | INV-by-INV test-coverage mapping across the Go/Python boundary, prerequisite for the step-5 deletion PR. |
| `THRESHOLD_VALIDATION_2026-06-25.md` | Engineers / reviewers | Why the 0.30 refusal threshold stays (provisional) and the sweep required before any retune. |
| `WHITEPAPER_RUNS_PHASE2_DESIGN.md` | Engineers | Design doc for the White Paper runs automation (P1+P2 shipped). |
| `REFACTOR_BACKLOG_2026-07-09.md` | Engineers | The 120-item refactor backlog (working list). |
| `../regwatch/backend/README.md` | Backend engineers | Explains why backend source remains in `src/regwatch` and how to run the FastAPI API. |
| `../regwatch/frontend/README.md` | Frontend engineers | Explains how to run the Next.js UI and how it proxies to the API. |
| `CLAUDE.md` | Agent operators | Working instructions for Claude Code or similar coding agents. |

## Archived / Historical Docs

These files are retained for context only. They are not the canonical source of
truth when they conflict with current code or the current reference docs above.
Do not treat an archived blocker as open if `README.md`, `ARCHITECTURE.md`,
`PROD_READINESS.md`, or `DEPLOY.md` says the work has landed.

| File | What It Contains |
|---|---|
| `PROJECT_SPEC.md` | Original POC spec and phase plan. Its status banner says current-state docs win when implementation has evolved. |
| `typescript-ui-replaces-streamlit-golden-pudding.md` | Original Next.js-replaces-Streamlit plan; now realized and archived. |
| `Jun8th.md` | Point-in-time work log from June 8, 2026. |
| `audit_findings.md` | Point-in-time audit notes; use only as historical context unless revalidated. |
| `ENGINEERING_AUDIT_2026-06-19.md` | Point-in-time SWE audit (verdict B+, zero P0); open items since folded into ROADMAP. |
| `POLYGLOT_ARCHITECTURE_REVIEW_2026-06-17.md` | June "no new language" review - OVERRULED by the July 10 polyglot assessment and target. |
| `POLYGLOT_ASSESSMENT_2026-07-10.md` | The assessment that approved the 4-runtime target; context for `POLYGLOT_TARGET_2026-07-10.md`. |
| `SUPABASE_AUTH_AUDIT_2026-06-17.md` | Point-in-time Supabase/auth audit; the auth split-brain it flagged was resolved by the Go auth cutover. |
| `PROD_GAPS_AND_ORCHESTRATION_COST_2026-06-17.md` | Point-in-time production-gap and cost analysis. |
| `CONVERSATIONAL_REFUSAL_PLAN.md` | Point-in-time plan for the follow-up-cliff refusal feature. |
| `UX_S1_EVIDENCE_DRAWER.md` | Build notes for the S1 evidence drawer. |
| `BACKEND_REFACTOR_BACKLOG_2026-07-07.md` | Superseded by `REFACTOR_BACKLOG_2026-07-09.md`. |

These `.txt` files are retained as planning notes and conversation artifacts.

| File | What It Contains |
|---|---|
| `understand.txt` | Beginner reading order for the whole codebase. |
| `router.txt` | Early notes on FDA source routing and source-specific handlers. |
| `plan.txt` | Longer planning draft for multi-source architecture and future UI. |
| `bugFixes.txt` | Stage A production-hardening notes and debt-paydown sequence. |
| `supabaseMigrationJun12.txt` | Point-in-time Supabase migration notes. |
| `mythos3.txt` | Local planning / recap notes. |

## Documentation Rules

- Keep `README.md` concise. Put detailed operational docs in `docs/`.
- Add new architecture decisions to `DECISIONS.md`, not only to chat.
- If a doc says something is production-ready, include the verification that
  proves it.
- If implementation changes Docker, deployment, ingest, or source-handler
  behavior, update `DOCKER.md` or `TECH_GUIDE_SIMPLE.md` in the same change.
- If you change `.github/workflows/ci.yml` (add/remove a job or step, change a
  gating command, or add a vuln suppression), update `CI_CD.md` in the same change
  so the pre-push checklist never drifts from the actual gate.
- Keep archive notes unless they become actively misleading; prefer adding a
  current `.md` source of truth over rewriting old notes.
