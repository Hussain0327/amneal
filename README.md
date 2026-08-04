# regwatch

regwatch helps a generic-drug regulatory team answer FDA research questions in
minutes instead of days — and every answer comes back with its source and page,
or an honest "not found."

A regulatory analyst preparing a generic-drug application spends hours reading
FDA **Product-Specific Guidances** (PSGs — the agency's per-drug instructions for
proving a generic is equivalent to the brand) and cross-checking the Orange Book,
Drugs@FDA, DailyMed, drug-shortage, and REMS databases. regwatch does that
reading for them: it watches those sources, scopes every question to one product
at a time so citations can't cross drugs, and answers in plain language with a
citation on every claim.

The product **surfaces, organizes, compares, and cites public FDA information.**
It never drafts submission content, never makes a regulatory recommendation, and
never acts on its own — a person makes every regulatory decision.

## What it does

Four surfaces, one shared "product under review":

- **Ask** — Plain-language Q&A over the guidance corpus, as a cited chat. Every
  claim carries an inline `[source, p.N]` citation you can click to read the
  exact passage. If the corpus cannot support an answer, Ask keeps unverified
  text out of the answer channel and guides the analyst toward a safer next
  question. Ambiguous questions ("propranolol") get clickable clarifying
  options rather than a wrong answer.
- **Assemble** — For a target product, build a fully cited dossier: the relevant
  PSG(s), extracted bioequivalence requirements, the brand-drug label from
  openFDA, applicable guidance, and a requirements checklist.
- **Watch** — Crawl the FDA PSG database daily, detect new and revised
  guidances, match them against the company's product pipeline, and surface a
  cited summary of what changed.
- **White Paper** — Given a brand-drug name and application number, fill the CRA
  White Paper template cell by cell. Each cell carries its provenance (source +
  locator + when it was fetched). Cells the system can't determine
  deterministically — patents, BE strategy, every required-studies judgment —
  are left for an analyst with the evidence attached, never auto-answered.

All four share one **current product**, set from the scope-bar picker, a Watch
row, or a White Paper run. The product is held in the URL (`?rp=&appl=`) so a
view is shareable and survives reload.

## The core rule: cite or refuse

This is the whole point, and it's enforced in code with tests — not a guideline.

1. **Resolve the product first.** A question is pinned to one drug (canonical
   name + 6-digit application number) *before* anything is retrieved. Shared FDA
   boilerplate can't leak a wrong-drug citation.
2. **Retrieve, scoped and current.** Retrieval is filtered to that product's
   ingredient and to the *current* PSG version, so a superseded passage can't be
   cited as if it were live.
3. **Keep weak evidence out of answers.** If the best match scores below
   `REFUSAL_SCORE_THRESHOLD` (default 0.30), those passages are sent to neither
   the answer synthesizer nor the guidance planner. The planner may select a
   safe, allowlisted next step, but it cannot author factual answer text.
   After synthesis, any claim whose citation doesn't trace to a retrieved
   passage is stripped; if nothing valid remains, the whole answer becomes a
   refusal.
4. **Give each healthy Ask turn one bounded AI role.** A grounded question uses
   the synthesizer. A pre-synthesis product, form, scope, capability, or
   evidence-gap outcome uses the router-role guidance planner, which can only
   select an application-approved next step and existing option IDs. Product,
   form, scope, status, citation validation, and display copy remain enforced
   by application code. Operational errors do not enter the guidance path.
5. **Audit everything.** Every query — answered, clarified, or refused — writes
   exactly one `query_log` row.

A non-answer is shown as an evidence gap, clarification, or scope boundary —
never dressed up as a grounded answer.

### How a question flows

The **Ask** path, from input to an audited answer or an explicit refusal.
Assemble and White Paper follow the same discipline over structured FDA sources.

```mermaid
flowchart TD
    Q["User question<br/>(+ session, filters, k)"] --> AUTH{"Authenticated?"}
    AUTH -- "no" --> E401["401"]
    AUTH -- "yes" --> ROUTE["Deterministic route + enforcement<br/>scope, capability, product, form, status"]
    ROUTE -- "operational error" --> ERROR["error response<br/>no AI reroute"]
    ROUTE -- "needs clarification or guidance" --> GUIDE["Router-role guidance planner<br/>select allowlisted next step + option IDs<br/>no display prose"]
    ROUTE -- "resolved answer path" --> RETRIEVE["Stage 1, vector top-k 50<br/>product/form/current-version scoped<br/>INV-9 - no cross-drug citation"]

    RETRIEVE --> RERANK["Stage 2, rerank to top-k 8<br/>reranker optional"]
    RERANK --> PRECHK{"Top score above threshold?"}
    PRECHK -- "no - withhold weak passages" --> GUIDE
    PRECHK -- "yes" --> SYNTH["Synthesizer LLM<br/>grounded structured claims"]

    SYNTH --> VALID{"Every claim cited to<br/>a retrievable source?"}
    VALID -- "no" --> REFUSE["evidence gap / refused<br/>no second AI call<br/>INV-1 - cite or refuse"]
    VALID -- "yes" --> ANSWER["answer<br/>answer + citation chips"]

    GUIDE --> COPY["Application renders trusted copy<br/>and existing options"]
    COPY --> FINAL["Finalize one audit row<br/>status, route, citations"]
    ERROR --> FINAL
    REFUSE --> FINAL
    ANSWER --> FINAL
```

The canonical system design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Status

regwatch runs as a **limited internal pilot**, not a generally available product.

- The **daily Watch pipeline runs in production** — a GitHub Actions cron
  ([`.github/workflows/watch-daily.yml`](.github/workflows/watch-daily.yml))
  runs `regwatch watch` against the live Supabase Postgres each day, and is the
  sole driver of the daily pipeline in prod.
- The **API (Fly.io)**, **Postgres + pgvector (Supabase)**, and **frontend
  (Vercel)** are deployed. The structured store and the vectors live in one
  managed Postgres.
- **LLM inference runs inside the company's Databricks tenant** (since
  2026-07-28): one small open-weight model, `gpt-oss-20b`, served from the
  Model Serving endpoint `workspace.default.regwatch`, handles every LLM role
  (`LLM_PROVIDER=databricks`). OpenAI remains the tested rollback path and
  still serves embeddings while the Qwen3 embedding migration is staged - the
  last piece of the data-residency (D1) move. See
  [`docs/DATABRICKS_ADOPTION_2026-07-28.md`](docs/DATABRICKS_ADOPTION_2026-07-28.md)
  and [`docs/DATA_RESIDENCY_D1.md`](docs/DATA_RESIDENCY_D1.md).
- The **polyglot migration**
  ([`docs/POLYGLOT_TARGET_2026-07-10.md`](docs/POLYGLOT_TARGET_2026-07-10.md))
  is through step 5: the Go proxy owns the public port, serves auth, sessions,
  feedback, settings, and products natively, and since 2026-07-24 also
  orchestrates `POST /query` end-to-end (it persists the audit row and calls
  Python's internal, token-gated RAG compute endpoint). Python keeps the
  stateless RAG core. The SQLite/Chroma dual-mode was deleted (R5) - Postgres +
  pgvector is the only datastore. Remaining: legacy-path deletion, hardening
  step R3, steps 6-9.
- It is **not yet externally exposed.** The work between here and an external
  launch - an SSO + TLS gateway, least-privilege DB credentials, a rehearsed
  restore drill, and finishing the D1 embedding move - is tracked in
  [`docs/PROD_READINESS.md`](docs/PROD_READINESS.md) and
  [`docs/ROADMAP.md`](docs/ROADMAP.md).

**Deliberate scope** (boundaries, not gaps): the reranker is a hook that's off by
default; the eval gold set is a curated starter set, expanded by hand from real
answer-feedback; and the persist-and-cite source-freshness path is proven on
White Paper before being generalized to Ask and Assemble.

## Quick start

```bash
# install (dev tools + LLM clients + local embeddings)
uv sync --extra dev --extra llm --extra local-embeddings

# configure - fill in OPENAI_API_KEY (default local providers; prod-parity
# Databricks LLM vars are documented in .env.example under LLM_PROVIDER)
cp .env.example .env && $EDITOR .env

# initialize the DB and data directories
uv run regwatch init-db

# discover Amneal sponsor-name aliases from Drugs@FDA (no hand-coding)
uv run regwatch aliases --refresh

# seed the verified starter PSGs by application number
# (`regwatch ingest-all` loads the full catalog)
uv run regwatch seed

# create a login (password is prompted, never passed as an argument)
uv run regwatch create-user analyst@example.com --name "Analyst"

# run the tests (smoke + invariants + offline eval gate). Postgres-only since
# R5: point TEST_DATABASE_URL at a DISPOSABLE local Postgres with pgvector
# (e.g. `docker compose up -d db`, then the URL below; its contents are wiped)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q

# API (RAG core)  ->  http://localhost:8000
# In production the Go proxy (go/) fronts this and serves auth/sessions/feedback/
# settings/products; run it in front for the full surface (see docs/DEPLOY.md).
uv run uvicorn regwatch.api.main:app --reload

# UI (separate terminal)  ->  http://localhost:3000
cd regwatch/frontend && npm install && npm run dev

# eval scorecard against the seeded corpus
uv run python -m regwatch.eval.run_eval
```

To share the whole app (API + UI) over one public link, run
`./scripts/share-demo.sh` — it builds both and opens a cloudflared tunnel. The UI
proxies `/api/*` to the backend, so only one origin is exposed.

## How it's built

One browser-visible origin, two runtimes on Fly, one datastore, and a
Databricks model plane inside the company tenant:

```mermaid
flowchart LR
    B["Browser<br/>(analyst)"] -->|"HTTPS + HttpOnly cookie"| V["Vercel<br/>Next.js 16 shell"]
    V -->|"/api/* rewrite"| GO["Fly.io - Go proxy (public port)<br/>auth + sessions + rate limits<br/>native /query orchestration + audit"]
    GO -->|"6PN private network"| PY["Fly.io - Python FastAPI<br/>stateless RAG core:<br/>resolve, retrieve, cite or refuse"]
    GO --> PG[("Supabase Postgres + pgvector<br/>one DB: rows, vectors, audit")]
    PY --> PG
    PY -->|"OpenAI-compatible API"| DBX["Databricks Model Serving<br/>gpt-oss-20b - all LLM roles<br/>qwen3-embedding-0.6b - staged"]
    CRON["GitHub Actions cron<br/>daily watch + ingest"] --> PG
```

| Layer | Choice |
|---|---|
| Edge / control plane | **Go** proxy (`go/`, module `github.com/Hussain0327/amneal/go`) holds the public port. Since the step-4 polyglot cutover it serves auth, sessions, feedback, settings, and product CRUD natively (sqlc over the same Postgres) and applies rate limiting + `Fly-Client-IP` handling; since the step-5 cutover it also orchestrates `POST /query` natively (persists the audit row, calls Python's internal RAG compute endpoint), and relays the remaining endpoints to Python. Migration plan: [`docs/POLYGLOT_TARGET_2026-07-10.md`](docs/POLYGLOT_TARGET_2026-07-10.md) |
| Backend (RAG core) | Python 3.11+ (managed by `uv`), FastAPI — the stateless retrieval / synthesis / refusal core behind the proxy: Ask, Assemble, White Paper, Watch, and query orchestration |
| Frontend | Next.js 16 (App Router, TypeScript) + React 18 in `regwatch/frontend/`. All four surfaces render in one `(shell)` route group — one sidebar, one product-scope bar. Talks to the API through a same-origin `/api` proxy |
| LLM | **Databricks-hosted `gpt-oss-20b`** in prod (`LLM_PROVIDER=databricks`): one small open-weight model on the Model Serving endpoint `workspace.default.regwatch` serves ALL roles (router, synthesizer, extractor), keeping analyst questions inside the company tenant (D1). A runtime served-model guard (`D1_ENFORCED` + `D1_ALLOWED_LLM_MODELS`) refuses any response served by an off-perimeter model once armed. Pluggable behind `LLMProvider`: `openai` (Responses API; router `gpt-5-nano`, synthesizer + extractor `gpt-5.4-nano`) is the tested rollback path; `anthropic` and a test-only `echo` also ship |
| Embeddings | Pluggable AND profile-versioned. Prod today: OpenAI `text-embedding-3-small` (1536-dim, matching the `vector(1536)` chunk column) - the last D1 gap, since every analyst question is embedded before retrieval. Staged replacement: Databricks-hosted `Qwen3-Embedding-0.6B` (1024-dim, endpoint `workspace.default.regwatch-embed`) via the embedding-profiles machinery (`ACTIVE_EMBEDDING_PROFILE`; blue/green re-embed into a named profile, never in-place). Local `BAAI/bge-small-en-v1.5` (384-dim) remains for offline tooling only -- the K6 dim assert refuses it against the app datastore |
| Vector store | **pgvector** in the same Postgres, everywhere (Supabase in prod, a disposable local/CI Postgres otherwise). No other vector backend since R5 |
| Structured store | **Postgres** via SQLModel (Supabase in prod); `DATABASE_URL` is mandatory and the app refuses to boot without it. Schema changes ship as Alembic migrations |
| Retrieval | Two-stage. Stage 1: vector top-k 50 (`VECTOR_TOP_K`). Stage 2: rerank to top-k 8 (`RERANK_TOP_K`); reranker off by default |
| Ingest | `httpx` + `selectolax`; `pdfplumber` (with `pypdf` fallback); heading- and page-aware chunking (~1000 tokens, ~150 overlap) |
| Deploy | One Fly.io app, **two process groups**: the Go proxy on the public port, the Python app (dual-stack `regwatch serve`) on an internal port behind it. DB + vectors on Supabase, frontend on Vercel, LLM (and staged embeddings) on Databricks Model Serving, daily Watch via GitHub Actions cron. Alembic (run by the Fly release command) stays the single schema authority |
| Tooling | Python: ruff, black, mypy (strict on `src/`), pytest, import-linter layering contracts. Go: gofmt, go vet, golangci-lint, sqlc (generated store + `sqlc vet` against a real schema). A cross-service contract suite (`tests_contract/`) boots the real Go proxy + uvicorn + Postgres to prove the wire contract across the boundary |

The LLM provider, model, reranker, and embedding provider all sit behind
interfaces; nothing is hard-coded in business logic.

## Compliance invariants

These are enforced in code with tests — see [`tests/test_invariants.py`](tests/test_invariants.py).

| | Invariant | Where it's enforced |
|---|---|---|
| INV-1 | Every factual claim is traceable to a source + page | `process/extractor.py` quote-verbatim check; `generate/grounded_qa.py` citation validator |
| INV-2 | If retrieval is weak, refuse to answer — never guess | The score threshold blocks weak passages from the synthesizer and guidance planner; post-synthesis citation validation blocks unsupported claims |
| INV-3 | Operational only — no authoring, no judgment | Prompt design + a structural check against `api/` for forbidden endpoints |
| INV-4 | Never report a run that didn't happen | `watch/alerts.py` skips any match whose `psg_version` is not in the DB |
| INV-5 | Verified provenance only | `WatchlistEntry` rejects sources outside `{drugsfda, anda_letter, manual}` |
| INV-6 | Every query is audited | `common/audit.py` writes a `query_log` row on every Q&A path |
| INV-7 | Cross-product integrity: never blend two applications' data | `whitepaper/populator.py` matches PSGs on exact application-number tokens (with `sources/psg.py`); `tests/test_whitepaper_populator.py` |
| INV-8 | Structured citations obey a strict token grammar and are honored only when backed | `common/citations.py` `validate_structured_citations`; `whitepaper/populator.py` collapses any unbacked cell to analyst input; `tests/test_citations.py` |
| INV-9 | PSG answers are always product-resolved and ingredient-filtered — no cross-drug citation survives | `retrieve/resolver.py` + `generate/grounded_qa.py`; `tests/test_cross_drug_leak.py` |

## API

Every product/data endpoint requires a login. `GET /health`, `GET /ready`, and
`GET /metrics` are operational endpoints outside the auth router; `/metrics` is
open by default and bearer-gated when `METRICS_TOKEN` is set. Full
request/response shapes are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Since the step-4 cutover the **Go proxy serves `/auth/*`, `/sessions`,
`/feedback`, `/settings`, and `/products` natively** at the public edge, and
since step 5 it also **orchestrates `POST /query` natively** (it persists the
audit row and calls Python's internal, token-gated
`POST /internal/query/compute` for the RAG work; the `/internal/` subtree is
never exposed publicly). The remaining structured-source endpoints relay to
Python. The public wire contract is identical either way - the
[`tests_contract/`](tests_contract/) suite proves it across the boundary.

```
POST   /auth/login        {email, password} -> {user} + HttpOnly session cookie
POST   /auth/logout       revoke the session, clear the cookie (204)
GET    /auth/me           current user, or 401

POST   /query             grounded, conversational Q&A
                          status in: answer | summary | clarify | scope_warning | refused
POST   /query/stream      same over SSE: progress frames, provisional token
                          deltas (a cosmetic live draft), then the validated
                          answer as the one authoritative result frame (INV-1)
POST   /resolve           deterministic product resolution (not an LLM turn);
                          422 on a name != number mismatch. Backs the scope picker
POST   /feedback          rate a Q&A answer {audit_id, rating: -1|1, comment?}

POST   /sources/search    structured FDA source lookup
POST   /assemble          cited dossier for {active_ingredient, dosage_form?, rld?}
POST   /whitepaper        populate the CRA White Paper for {rld_name, application_number}
POST   /whitepaper/docx   render a reviewed White Paper result to a .docx

GET    /watch/latest      matched changes since a cursor
GET    /products          the watchlist
POST   /products          add a manual product (INV-5 enforced)

GET    /sessions          the caller's chat sessions (updated_at desc)
GET    /sessions/{id}     one session + its messages
DELETE /sessions/{id}     delete a session and its messages

GET    /health            liveness + component diagnostics; 503 when db/vector_store is down
GET    /ready             readiness: db + vector store + LLM constructability
GET    /metrics           Prometheus counters; bearer-gated when METRICS_TOKEN is set
GET    /settings          non-secret config
```

The auto-docs routes (`/docs`, `/redoc`, `/openapi.json`) are disabled — they
register outside the auth wall and would disclose the API surface anonymously.

### Auth

Sessions are DB-backed opaque tokens in an HttpOnly `regwatch_session` cookie
(SameSite=Lax; the DB stores only the token's sha256; passwords are bcrypt-hashed).
There is no self-signup — users are provisioned from the CLI:

```bash
uv run regwatch create-user analyst@example.com --name "Analyst"  # password prompted
uv run regwatch list-users
uv run regwatch set-password analyst@example.com
uv run regwatch deactivate-user analyst@example.com
```

Chat history is per-user: `GET /sessions` lists only the caller's sessions, and a
session owned by someone else 404s. `POST /query` and `POST /assemble` share a
per-user rate limit (`RATE_LIMIT_PER_MINUTE`, default 30). Set
`AUTH_COOKIE_SECURE=true` behind TLS. CORS is allow-listed via
`CORS_ALLOW_ORIGINS_CSV`. TLS termination and OIDC/SSO are environment work — see
[`docs/PROD_READINESS.md`](docs/PROD_READINESS.md).

## Watchlist sources

The watchlist is built from three allowed sources, in order; INV-5 rejects
anything else, including model memory:

1. `drugsfda` — `api.fda.gov/drug/drugsfda.json`, filtered to applications whose
   sponsor matches a discovered Amneal variant. Aliases come from
   `uv run regwatch aliases`, not hand-coding.
2. `anda_letter` — user-uploaded approval letters.
3. `manual` — explicit overrides.

## Eval

Two layers grade the cite-or-refuse pipeline:

- **`uv run python -m regwatch.eval.run_eval`** scores a curated gold set
  ([`src/regwatch/eval/gold_set.jsonl`](src/regwatch/eval/gold_set.jsonl)) of
  real, must-refuse, and must-clarify questions against the live corpus. Hard
  gates, when the provider-backed job is enabled, are `recall@k >= 0.90`,
  `citation_precision >= 0.95`, and `refusal_accuracy >= 0.95`. It exits
  nonzero on an empty store. The latest inspected CI run skipped its live seed
  and eval steps because the repo-wide `OPENAI_API_KEY` was absent, so current
  live-corpus pass/fail is unverified.
- **[`tests/test_eval_gate.py`](tests/test_eval_gate.py)** is a deterministic,
  offline gate: it seeds a fixed corpus and a faithful LLM stub, so the full
  pipeline (resolve -> filter -> retrieve -> cite -> refuse) is graded on every
  `uv run pytest`, including CI. Passing this fixture is not a live-corpus
  quality result.

Growing the gold set is a human process: thumbs up/down from the Ask UI is the
candidate pool. Review them and promote good ones into `gold_set.jsonl` by hand —
nothing is auto-ingested.

See [`docs/EVAL_STATUS.md`](docs/EVAL_STATUS.md) for the verified gold-set
counts, latest live-artifact interpretation, and the still-provisional `0.30`
cutoff.

## Project layout

```
config/settings.py        pydantic-settings: all thresholds + secrets
src/regwatch/
  ingest/                 psg_crawler, pdf_parser, pipeline
  process/                chunker, embedder, extractor, change_detector
  store/                  db, models, vector_store, pgvector_store
  retrieve/               retriever (stage 1), reranker (stage 2)
  sources/                FDA source handlers + source router
  generate/               LLM provider interface, grounded_qa, prompts
  watch/                  watchlist, aliases, matcher, alerts
  assemble/               dossier builder
  whitepaper/             CRA White Paper populator + .docx writer
  eval/                   metrics, run_eval, gold_set.jsonl
  api/                    FastAPI surface
  auth/                   passwords (bcrypt), cookie sessions, require_user
  common/                 logging, audit, citations, text_normalize, conversation, ratelimit
migrations/               Alembic migration history (the single schema authority)
go/                       Go proxy: public edge + native auth/sessions/feedback/settings/products + native /query orchestration (sqlc store)
regwatch/frontend/        Next.js (App Router, TS) UI — one (shell) for all four surfaces
tests/                    smoke, invariants, eval gate, per-module
tests_contract/           cross-service contract suite: real Go proxy + uvicorn + Postgres
```

## Docs

Start with the Map of Content, [`docs/MAP.md`](docs/MAP.md). Highest-value entry
points:

- [Architecture](docs/ARCHITECTURE.md) — canonical system design.
- [Non-technical guide](docs/NON_TECH_GUIDE.md) — plain English for business and
  regulatory readers.
- [Production readiness](docs/PROD_READINESS.md) — the POC-to-production path.
- [Data residency (D1)](docs/DATA_RESIDENCY_D1.md) - why analyst queries must
  stay in-tenant, and the migration that gets them there.
- [Databricks adoption](docs/DATABRICKS_ADOPTION_2026-07-28.md) - the inference-
  plane decision, cost model, and rollout state.
- [Decisions](docs/DECISIONS.md) — append-only log of what was chosen and why.
- [CI/CD pipeline](docs/CI_CD.md) - the CI gate (Python lint/type/test, audit,
  docker build, frontend, Go proxy, schema-drift, and the cross-service contract
  lane) and a pre-push checklist; read before pushing so you do not fail CI.

## Docker

Full details in [`docs/DOCKER.md`](docs/DOCKER.md). The one image runs the API
and ingest jobs; Compose adds the Next.js UI and a local pgvector Postgres
service for development.

```bash
docker build -t regwatch:local .          # shared image
docker compose up api                      # API on http://localhost:8000
docker compose up --build api web          # API + UI (http://localhost:3000)
```

Compose mounts `./data` so raw/processed PDFs survive restarts, runs Postgres
via its `db` service, and defaults to `EMBEDDING_PROVIDER=openai` (set
`OPENAI_API_KEY` in `.env`; the pgvector chunk table is 1536-dim) — see
[`docs/DEPLOY.md`](docs/DEPLOY.md).

## License

MIT.
