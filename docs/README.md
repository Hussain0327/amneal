# REGWATCH Documentation Index

Use this file as the map for the docs folder.

## Start Here

Read these in order if you are new to the project:

1. `../README.md` - project overview, current stack, quick start, API surface.
2. `NON_TECH_GUIDE.md` - plain-English explanation for regulatory and business readers.
3. `TECH_GUIDE_SIMPLE.md` - simplified technical walkthrough of how the code connects.
4. `DOCKER.md` - container setup, services, data mounts, and ingest notes.
5. `PROJECT_SPEC.md` - original proof-of-concept specification and compliance rules.
6. `DECISIONS.md` - append-only history of important engineering decisions.

## Current Reference Docs

| File | Audience | Purpose |
|---|---|---|
| `NON_TECH_GUIDE.md` | Non-technical stakeholders | Explains what REGWATCH does, what it must not do, and why citations/refusals matter. |
| `TECH_GUIDE_SIMPLE.md` | Technical readers | Explains folder structure, core flows, and the files to read first. |
| `DOCKER.md` | Engineers / deployment owners | Documents the Dockerfile, Compose services, embedding modes, and ingest implications. |
| `PROJECT_SPEC.md` | Engineers / reviewers | Original POC spec, phase plan, and compliance invariants. |
| `DECISIONS.md` | Engineers / reviewers | Append-only decision log. Read this before changing architecture. |
| `PROD_READINESS.md` | Engineers / deployment owners | Tracks what stands between the POC and a real production deployment, prioritized and mapped to the tree. |
| `CLAUDE.md` | Agent operators | Working instructions for Claude Code or similar coding agents. |
| `typescript-ui-replaces-streamlit-golden-pudding.md` | Product / engineering planning | Future architecture plan for router, handlers, synthesizer, and TypeScript UI. |

## Historical Notes

These `.txt` files are retained as planning notes and conversation artifacts.
They are not the canonical source of truth when they conflict with current code
or current Markdown docs.

| File | What It Contains |
|---|---|
| `understand.txt` | Beginner reading order for the whole codebase. |
| `router.txt` | Early notes on FDA source routing and source-specific handlers. |
| `plan.txt` | Longer planning draft for multi-source architecture and future UI. |
| `bugFixes.txt` | Stage A production-hardening notes and debt-paydown sequence. |

## Documentation Rules

- Keep `README.md` concise. Put detailed operational docs in `docs/`.
- Add new architecture decisions to `DECISIONS.md`, not only to chat.
- If a doc says something is production-ready, include the verification that
  proves it.
- If implementation changes Docker, deployment, ingest, or source-handler
  behavior, update `DOCKER.md` or `TECH_GUIDE_SIMPLE.md` in the same change.
- Keep historical `.txt` notes unless they become actively misleading; prefer
  adding a current `.md` source of truth over rewriting old notes.
