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
| `CI_CD.md` | Engineers / contributors | The CI gate explained: all five jobs mapped to the exact local command that satisfies each, a copy-paste pre-push checklist, and the recurring gotchas (black-not-ruff, stale `api-types.ts`, `uv.lock` drift, Trivy web-image esbuild + GHSA/CVE alias flips). Read before pushing. |
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
