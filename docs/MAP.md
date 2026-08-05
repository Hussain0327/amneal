# REGWATCH — Map of Content

**Start here.** This is the hub: how the system fits together, and a link to every
living doc grouped by purpose. (Open the graph view — this note is the center.)

REGWATCH watches FDA Product-Specific Guidances and other public FDA sources, and
lets a CRA analyst **ask cited questions, assemble dossiers, watch for changes,
populate the CRA White Paper, and predict submission deficiencies** — all scoped
to **one product under review**. It **surfaces, organizes, compares, and cites;
it never authors, decides, or files** (the INV-1..9 invariants, enforced as
tests).

A sixth surface, the **Compliance Studio**, inverts the input: it reads our own
CMC drafts rather than public FDA material. It is UI and domain model only.

## How it connects

```mermaid
flowchart TB
  subgraph UI["Next.js shell · regwatch/frontend (one (shell) layout)"]
    direction LR
    Ask["Ask<br/>cited chat"]
    Assemble["Assemble<br/>dossier"]
    Watch["Watch<br/>change feed"]
    WP["White Paper<br/>populate + cite"]
    Def["Deficiency<br/>predicted findings"]
  end
  Studio["Compliance Studio · /studio<br/>OUR CMC drafts, not FDA material<br/>fixtures only, no backend yet"]
  Scope{{"Under review: ONE product<br/>scoped in the URL, shareable"}}
  Scope -.scopes.-> UI
  Studio -.->|"not scoped, outside the shell"| Scope

  UI -->|"same-origin /api proxy"| EDGE["Go proxy · go/ (public port)<br/>auth · sessions · rate limits<br/>native /query orchestration + audit"]
  EDGE -->|"6PN relay"| API["FastAPI · src/regwatch/api<br/>stateless RAG core · INV-1..9"]
  API --> Router["Router → Handlers → Synthesizer"]
  API --> Resolve["POST /resolve<br/>deterministic, no audit row"]
  Router --> Sources["FDA sources<br/>PSG · Orange Book · Drugs@FDA<br/>NDC · DailyMed · Shortages · REMS"]
  Router --> Stores[("Postgres + pgvector (Supabase)<br/>the ONLY datastore since R5:<br/>rows + vectors + audit log")]
  EDGE --> Stores
  Sources --> Stores
  Router --> DBX["Databricks Model Serving<br/>gpt-oss-20b (all LLM roles)<br/>qwen3-embedding-0.6b (staged)"]
```

Every UI surface talks only to the **Go edge** through the same-origin `/api`
proxy: the edge serves auth, sessions, feedback, settings, and products natively,
orchestrates `POST /query` (persisting the audit row), and relays the rest to the
FastAPI RAG core over Fly's private network. The backend resolves the product
**before** retrieval and constrains retrieval to the current PSG version, so
shared FDA boilerplate can't leak a wrong-drug or a superseded citation. The
scope bar's picker validates a product through `POST /resolve` (entity resolution
only - not an LLM turn, writes no audit row). LLM synthesis runs on a
Databricks-hosted open-weight model inside the company tenant (the D1
data-residency boundary); embeddings are mid-migration to the same plane.

## The docs, by purpose

This is the only index of `docs/`. Every living doc appears exactly once below.

**Start here**
- [Project overview & quick start](../README.md)
- [Non-technical guide](NON_TECH_GUIDE.md) — plain English for regulatory/business readers
- [Simple technical guide](TECH_GUIDE_SIMPLE.md) — folder map and core flows

**How it's built**
- [Architecture](ARCHITECTURE.md) — canonical system design (Router → Handlers → Synthesizer, the surfaces, INV-1..9)
- [Compliance Studio](COMPLIANCE_STUDIO.md) — `/studio`: the span-anchored finding model, the disposition record, the evidence gate behind "Fixed", and what is deliberately not built
- [Graph-assisted adaptive retrieval](GRAPH_ASSISTED_RETRIEVAL.md) — proposed bounded graph traversal from citable chunks; Tier-1 graph storage is landed, runtime traversal is not
- [Polyglot target](POLYGLOT_TARGET_2026-07-10.md) - the TS/Go/Python/Rust strangler plan (steps 0-5 done)
- [Go proxy rollout](GO_PROXY_ROLLOUT.md) - how Go took the public edge (complete)
- [Go native query rollout](GO_NATIVE_QUERY_ROLLOUT.md) - the step-5 `/query` cutover runbook (flip live 2026-07-24). `tests/test_go_native_query_pin.py` reads this file by path and pins its status line to `fly.toml` — do not move it or add a second status block
- [Step-5 INV test mapping](STEP5_INV_TEST_MAPPING.md) - INV-by-INV test coverage across the Go/Python boundary
- [Project spec](PROJECT_SPEC.md) — the original build-ready spec (current-state docs win where they conflict)
- [Operating rules for Claude Code](CLAUDE.md) — hard rules + defaults

**Models & data residency**
- [Data residency D1](DATA_RESIDENCY_D1.md) - analyst queries must stay in-tenant; what leaks and the fix
- [Databricks adoption](DATABRICKS_ADOPTION_2026-07-28.md) - inference-plane decision, cost model, incident log, rollout state
- [Open-model rollout](OPEN_MODEL_ROLLOUT.md) - embedding profiles + open-weight providers (shipped dormant; the embedding half is not yet flipped in prod)
- [Evaluation status](EVAL_STATUS.md) - current gold-set counts, CI/live evidence, and the 0.917 correction
- [Threshold validation](THRESHOLD_VALIDATION_2026-06-25.md) - the 0.30 refusal threshold's provisional status + sweep harness

**The web app**
- [Frontend README](../regwatch/frontend/README.md) — the Next.js shell, the surfaces, the scope picker
- [Conversational sessions](CONVERSATIONAL_SESSIONS.md) — chat sessions, follow-up context, audit rules

**The White Paper**
- [White-paper field-extraction schema](whitepaper_schema.md) — one row per template cell: {source, key, mode}
- [White-paper runs, phase 2](WHITEPAPER_RUNS_PHASE2_DESIGN.md) — the saved-run + analyst-overlay compliance model (shipped)

**Run & ship**
- [Docker guide](DOCKER.md) — containers, services, data mounts
- [Deploy runbook](DEPLOY.md) - Supabase + Fly + Vercel operations, rollback levers, restore procedure
- [CI/CD pipeline](CI_CD.md) - every CI job mapped to its local command; read before pushing
- [Secrets runbook](SECRETS_RUNBOOK.md) - where every secret lives and how to rotate it

**Status & what's left**
- [Production readiness](PROD_READINESS.md) — the prod gates, what's done vs remaining
- [Roadmap](ROADMAP.md) — the consolidated list of open / not-yet-done work
- [Refactor backlog](REFACTOR_BACKLOG_2026-07-09.md) — the 120-item working list
- [Decisions](DECISIONS.md) — append-only log of what was picked and why
- [Security policy](../SECURITY.md)

**History** — [`archive/`](archive/): point-in-time audits, completed rollout plans, and
superseded reviews. Each carries an `ARCHIVED` banner saying what replaced it. Never
treat an archived blocker as open; `ROADMAP.md` and `PROD_READINESS.md` own open work.

## Documentation rules

- Keep `../README.md` concise. Detailed operational docs live here in `docs/`.
- New architecture decisions go in `DECISIONS.md`, not only in chat.
- If a doc says something is production-ready, include the verification that proves it.
- If a change touches Docker, deploy, ingest, or source-handler behavior, update
  `DOCKER.md` / `DEPLOY.md` / `TECH_GUIDE_SIMPLE.md` in the same change.
- If you change `.github/workflows/ci.yml` (add/remove a job or step, change a gating
  command, or add a vuln suppression), update `CI_CD.md` in the same change so the
  pre-push checklist never drifts from the actual gate.
- When a doc stops being true, move it to `archive/` with a banner. Do not leave a
  stale plan sitting in `docs/` where it reads as current.
