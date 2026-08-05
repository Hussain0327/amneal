# REGWATCH Simple Technical Guide

This guide explains the REGWATCH codebase for technical readers who want the
big picture before reading implementation details.

It is intentionally simple. It focuses on how the project connects end to end.

## One-Line System Summary

REGWATCH ingests public FDA data, stores searchable/citable evidence, answers
questions only from that evidence, and refuses when it cannot support an answer.

## Current Shape

REGWATCH is a deployed production service: the backend runs on Fly.io (app
"amneal", TLS via `force_https`), the Next.js UI is on Vercel
(amneal.vercel.app), and Supabase hosts the Postgres + pgvector datastore.
Remaining pre-external-exposure work (enterprise SSO/OIDC, least-privilege DB
creds, a rehearsed restore drill, distributed rate limiting) is tracked in
`docs/ROADMAP.md`. The code paths described below are all shipped on `main`.

Main stack:

- Python 3.11+ / 3.12
- Go edge service (`go/`) on the public port: auth, sessions, feedback,
  settings, products served natively, rate limiting, and since 2026-07-24 the
  end-to-end orchestration of `POST /query`
- FastAPI RAG core behind the Go edge (conversational query pipeline with
  session/turn IDs)
- Next.js (App Router, TypeScript) UI in `regwatch/frontend/`
- Authenticated: cookie-session auth on every endpoint except `GET /health`
  (DB-backed opaque tokens, bcrypt passwords, per-user chat history + rate
  limiting)
- Postgres + pgvector as the only datastore (structured store and vectors in
  the same DB); `DATABASE_URL` is mandatory - the app refuses to boot without
  it
- SQLModel / SQLAlchemy over the structured store
- `httpx` and `selectolax` for FDA crawling
- `pdfplumber` and `pypdf` for PDF parsing
- pluggable LLM providers; prod runs the `databricks` provider (open-weight
  gpt-oss-20b for all roles, hosted in the company Databricks tenant), with
  the `openai` role-model provider kept as the rollback path
- pluggable embedding providers
- Alembic migrations (baseline + incremental, currently through `0016`)
- Docker / Compose for the local API + ingest baseline
- pytest, ruff, black, mypy

## High-Level Flow

The system has two main flows:

1. Ingest FDA source material.
2. Answer user questions from stored evidence.

Ingest flow:

```text
FDA PSG index
  -> scrape PSG rows
  -> download PDF
  -> parse PDF page text
  -> chunk text with page metadata
  -> embed chunks
  -> store chunks in pgvector
  -> store document/version/BE fields in the structured store (Postgres)
```

Q&A flow:

```text
user question
  -> create or load chat session
  -> resolve follow-up context when safe
  -> resolve product/entity
  -> retrieve scoped chunks
  -> refuse if retrieval is weak
  -> call LLM with only retrieved evidence
  -> validate citations
  -> audit the query with session_id and turn_id
  -> return answer, summary, clarification, scope warning, or refusal
```

## Folder Map

```text
config/
  settings.py              runtime config from env

scripts/
  export_openapi.py        dump the OpenAPI schema
  fly-deploy.sh            Fly.io deploy helper
  gen-store-schema.sh      regenerate the Go sqlc store schema
  share-demo.sh            start API + UI behind one public link

docker/
  entrypoint.sh            container startup: create data dirs and init DB

go/
  (Go edge service)        public port: auth, sessions, feedback, settings,
                           products, rate limits, and POST /query orchestration

migrations/
  versions/                Alembic migrations (through 0016)

src/regwatch/
  ingest/                  crawl FDA PSGs and parse PDFs
  process/                 chunk, embed, extract BE fields, detect changes
  store/                   Postgres models/session + pgvector vector store
  common/conversation.py    chat session/message persistence and safe context
  retrieve/                product resolution and vector retrieval
  generate/                prompts, LLM providers, grounded Q&A
  watch/                   watchlist, alias discovery, matching, alerts
  assemble/                product dossier builder
  whitepaper/              cited White Paper populator + .docx export + entity-resolution spine
  sources/                 rules-first source router + FDA source handlers (OB, Drugs@FDA, NDC, DailyMed, Shortages, REMS, PSG)
  auth/                    cookie-session auth: opaque tokens, request deps
  api/                     FastAPI endpoints
  eval/                    gold set, metrics, deterministic eval gate
  common/                  citations, audit, logging, text normalization, conversation, rate limiting, observability

regwatch/
  backend/                 backend workspace docs; source stays in src/regwatch
  frontend/                Next.js (App Router, TypeScript) UI — five scoped surfaces in one (shell) route group, plus /studio outside it
tests/                     unit, integration, invariant, eval-gate tests
tests_contract/            cross-service contract suite over real Go + uvicorn + Postgres
docs/                      specs, decisions, plans, onboarding docs
```

Top-level Docker files:

- `Dockerfile`: builds the shared Python app image.
- `compose.yaml`: runs the API and ingest services (the UI runs separately).
- `.dockerignore`: keeps local data, secrets, docs, and caches out of the image.

## The Core Data Model

Read `src/regwatch/store/models.py` early. It explains the system.

Important tables:

- `Product`: verified watchlist product.
- `PsgDocument`: current FDA PSG document record.
- `PsgVersion`: captured PSG version.
- `BeRequirement`: extracted BE fields with citations.
- `QueryLog`: durable audit log for Q&A and related paths.
- `User` / session tables: auth identities, opaque session tokens, and
  per-user chat ownership (see migration `0004`).

The vector store (pgvector, in the same Postgres database as the structured
store) holds PSG text chunks and metadata.

Each vector chunk carries metadata like:

- `doc_id`
- `version_id`
- `normalized_name`
- `dosage_form`
- `route`
- `page`
- `source_url`
- `appl_no`

That metadata is what makes citations and scoped retrieval possible.

## Important Files To Read First

Best first pass:

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `docs/PROJECT_SPEC.md`
4. `docs/DECISIONS.md`
5. `docs/DOCKER.md`
6. `config/settings.py`
7. `src/regwatch/store/models.py`
8. `src/regwatch/ingest/pipeline.py`
9. `src/regwatch/generate/grounded_qa.py`
10. `src/regwatch/retrieve/resolver.py`
11. `src/regwatch/common/citations.py`
12. `tests/test_invariants.py`

If those make sense, the rest of the repo will be much easier.

## Ingest Components

### `ingest/psg_crawler.py`

Scrapes the FDA PSG index page.

It creates `PsgListing` objects that include:

- application number
- active ingredient
- normalized name
- PSG type
- route
- dosage form
- recommended date
- PDF URL

It also filters listings for seed products.

### `ingest/pdf_parser.py`

Parses PDF bytes into:

- full text
- per-page text list
- parser engine name

Page boundaries are important because every answer citation needs a page.

### `process/chunker.py`

Splits page text into chunks while keeping:

- page number
- section path
- document metadata

This is the text that goes into pgvector.

### `process/embedder.py`

Defines the embedding interface.

Providers:

- `openai`: `text-embedding-3-small`, 1536-dim - the production provider
- `qwen3`: Qwen3 embeddings over an OpenAI-compatible private endpoint
  (Databricks-served target)
- `local-bge-small`: local sentence-transformers model (384-dim; offline
  tooling only - the dimension assert rejects it against the app datastore)
- `echo`: deterministic test provider (1536-dim, matching the pgvector column)

Business logic calls the provider interface, not a hard-coded model.

### `process/extractor.py`

Uses an LLM to extract structured BE requirement fields from a PSG.

Important behavior:

- every populated field must have a citation
- the citation must include page and verbatim quote
- the quote must actually appear in the source page
- invalid fields are dropped

### `ingest/pipeline.py`

This is the ingest orchestrator.

It does:

1. download PDF
2. upsert `PsgDocument`
3. create `PsgVersion` if content changed
4. parse PDF
5. chunk text
6. embed chunks
7. store chunks in pgvector
8. run BE extraction
9. save `BeRequirement`

## Retrieval Components

### `retrieve/resolver.py`

This is a key correctness file.

Problem it solves:

FDA PSGs share boilerplate language across products. If retrieval is unfiltered,
a beclomethasone question can retrieve albuterol chunks because both documents
contain similar phrases.

Solution:

Resolve the product first, then retrieve only within that product's chunks.

Resolver outcomes:

- `resolved`: one product matched
- `ambiguous`: multiple products matched
- `none`: no product matched

### `retrieve/retriever.py`

Embeds the query and searches pgvector.

It supports metadata filters like:

- `normalized_name`
- `dosage_form`
- `route`
- `psg_type`

For product-specific PSG questions, `normalized_name` is the important filter.

### `retrieve/reranker.py`

Optional cross-encoder reranking hook.

It is off by default.

## Generation Components

### `generate/prompts.py`

Stores prompts in one place.

Main prompt types:

- grounded Q&A
- BE extraction
- change summary

The prompts tell the model to answer only from provided evidence and to refuse
when the answer is not in the source context.

### `generate/llm.py`

Defines the LLM provider interface.

Current providers:

- Databricks (`DatabricksProvider`) - the production provider since
  2026-07-28: open-weight gpt-oss-20b served from the company Databricks
  tenant (endpoint `workspace.default.regwatch`), one model for all roles.
- OpenAI - the rollback path; uses the **Responses API** by default
  (`OPENAI_API_MODE=responses`; `chat` falls back to Chat Completions), with
  role-specific models: router `gpt-5-nano`, synthesizer + extractor
  `gpt-5.4-nano`, each falling back to `LLM_MODEL`.
- Anthropic
- echo for tests

Business logic always calls `get_llm_provider(role=...)`; no model name is
hard-coded.

### `generate/grounded_qa.py`

This is the main Q&A path.

It does:

1. resolve product scope when needed
2. retrieve passages
3. rerank and trim passages
4. refuse before LLM if retrieval is weak
5. call LLM with source passages
6. validate citations
7. strip fake citations
8. refuse if answer has no valid citations
9. write audit log
10. return `QAResult`

## Citation System

`common/citations.py` is the centralized citation grammar.

PSG citation format:

```text
[PSG_020503, p.3]
```

It also supports compound citations:

```text
[PSG_020503, p.4; PSG_021730, p.4]
```

Why this matters:

- generator validation and eval scoring use the same parser
- fake citations can be removed before returning an answer
- citation behavior does not drift across the codebase

## Watch System

The watch system tracks products and alerts on relevant PSG changes.

Files:

- `watch/watchlist.py`
- `watch/aliases.py`
- `watch/matcher.py`
- `watch/alerts.py`

Concepts:

- Products must come from verified sources.
- Applicant aliases are discovered from Drugs@FDA instead of guessed.
- PSG listings are matched to watchlist products.
- Alerts are emitted only when the PSG version exists in the structured store.

## Dossier System

`assemble/dossier.py` builds a Markdown research brief for a product.

It can include:

- matched PSGs
- BE extraction fields
- citations and source links
- RLD label info from openFDA when available
- a PSG Q&A summary
- a checklist scaffold

It is a research scaffold, not submission content.

## API And UI

### `api/main.py` and the Go edge

The Go edge service (`go/`) owns the public port. It serves `/auth/*`,
`/sessions*`, `/feedback`, `/settings`, and `/products` natively, orchestrates
`POST /query` end-to-end (it persists the `query_log` audit row and calls the
Python RAG core only through the token-gated internal
`POST /internal/query/compute`), and relays everything else to FastAPI.

Public endpoints:

- `GET /health` — open liveness + component diagnostics
- `GET /ready` — open readiness probe for DB/vector/LLM constructability
- `GET /metrics` — Prometheus counters; open by default, bearer-gated when
  `METRICS_TOKEN` is set
- `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` - cookie-session
  auth (Go-served)
- `POST /query` - conversational; Go-orchestrated at the edge; accepts
  `session_id`/`user_id`, returns `session_id`/`turn_id`/`status`
  (`answer`/`summary`/`clarify`/`scope_warning`/`refused`)
- `POST /query/stream` - SSE `status` progress frames, provisional `token`
  delta frames (a cosmetic live draft), then one validated terminal
  `QueryResponse` `result` frame; falls back client-side to `POST /query` if
  the stream fails before the result
- `POST /feedback` - per-turn answer feedback against an audit row (Go-served)
- `POST /resolve` — deterministic entity resolution to a canonical spine
  (`{normalized_name, six-digit application number}`). It is NOT an LLM turn:
  it writes NO audit row and returns no answer text; a mismatch 422s with no
  scope set (refuse over guess).
- `POST /sources/search`
- `POST /assemble`
- `POST /whitepaper` (populate), `POST /whitepaper/runs/{id}/docx` (export a saved run)
- `GET /watch/latest`
- `GET /products`, `POST /products` (Go-served)
- `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` — per-user
  chat history (foreign `session_id` 404s) (Go-served)
- `GET /settings` (Go-served)

All other endpoints are behind a `require_user` dependency.
Auth is a DB-backed cookie session (opaque token hashed at rest, bcrypt
passwords, CLI-provisioned users) with per-user rate limiting
(`RATE_LIMIT_PER_MINUTE`, default 30), plus login brute-force caps per email and
per source IP. CORS is allow-listed with credentials via
`CORS_ALLOW_ORIGINS_CSV` (defaults to the Next.js dev origins).

### `regwatch/frontend/` (Next.js, TypeScript)

The production UI is the Next.js App Router app in `regwatch/frontend/`. All
**five scoped surfaces** (Ask / Assemble / Watch / White Paper / Deficiency)
render inside one `(shell)` route-group layout — one sidebar, one set of design
tokens, and a shared **"Under review" product-scope bar** across all of them.
The **Compliance Studio** (`/studio`) sits outside that shell: it reads our own
CMC drafts rather than public FDA material, and is fixture-backed.

- The current product is **URL-scoped** (`?rp=&appl=`) so it is shareable and
  survives reload; all five shell surfaces read it.
- Product scope is settable from three places — the bar's resolve-backed
  picker, a successful White Paper populate, and a Watch row — each writing the
  canonical `{normalized_name, six-digit application number}`.
- **Ask** is a cited conversational chat (right-aligned user bubbles, gold RW
  avatar, citation chips that link to FDA sources with full snippets in a
  Sources disclosure, clarify-option pills, bottom-pinned composer,
  Enter-to-send), not an editorial document/ledger view.

It uses a typed client in `regwatch/frontend/lib/api.ts` mirroring the Pydantic
models, and a same-origin `/api` proxy (`regwatch/frontend/next.config.mjs`) so
one origin exposes the whole app. Run it with
`cd regwatch/frontend && npm run dev`, or use `scripts/share-demo.sh` to start
API + UI behind one public link. (The earlier Streamlit POC has been fully
retired.)

## Eval

Files:

- `eval/gold_set.jsonl`
- `eval/metrics.py`
- `eval/run_eval.py`

Metrics:

- retrieval recall
- citation precision
- faithfulness proxy
- `fact_recall` — fraction of a gold item's `expected_facts` present in the
  answer (scores answer content, not just which pages were cited)
- refusal accuracy

`run_eval.py` scores the gold set against the live corpus and exits nonzero on
an empty store. Its provider-backed CI lane is key-gated; the latest inspected
run skipped it, so current live pass/fail is unverified.
`tests/test_eval_gate.py` is a deterministic, offline gate: it seeds a fixed
corpus and a faithful LLM stub and hard-gates every metric, so the fixture fires
inside `uv run pytest` (and therefore in CI). The eval is intentionally
mechanical and auditable; it does not yet use an LLM-as-judge. See
[`EVAL_STATUS.md`](EVAL_STATUS.md) for the current evidence.

Open eval work (see `docs/ROADMAP.md`): expand the gold set (12 Q&A + 16
white-paper rows -> 30-50), add an LLM-as-judge alongside the mechanical
`(short_name, page)` + `expected_facts` scoring, and hold thresholds
recall@k >= 0.90, citation_precision >= 0.95, refusal_accuracy >= 0.95.

## Tests

The tests explain expected behavior better than comments.

Key tests:

- `test_invariants.py`: compliance invariants
- `test_cross_drug_leak.py`: product scoping before retrieval
- `test_citations.py`: citation grammar
- `test_resolver.py`: entity/product resolution
- `test_pipeline_idempotent.py`: ingest idempotency
- `test_grounded_qa_citations.py`: fake citation stripping
- `test_api.py`: endpoint behavior

## Safety Model

The safety model is simple:

1. Use only trusted FDA/public source evidence.
2. Resolve product scope before searching PSG chunks.
3. Retrieve evidence with metadata filters.
4. Give the LLM only retrieved evidence.
5. Validate citations after the LLM responds.
6. Refuse if evidence or citations are weak.
7. Audit every query.

The LLM is not trusted by itself. The code checks its output.

## Current Model Provider State

Current state (this is implemented and deployed, not planned):

- Production runs the Databricks provider since 2026-07-28
  (`LLM_PROVIDER=databricks`): open-weight gpt-oss-20b served inside the
  company Databricks tenant (endpoint `workspace.default.regwatch`), one
  model for all roles. This is the data-residency decision recorded in
  `docs/DATABRICKS_ADOPTION_2026-07-28.md`.
- Two tuning knobs shipped 2026-07-29: `DATABRICKS_REASONING_EFFORT`
  (default `low`) and `SYNTHESIZER_MAX_TOKENS` (default 900).
- Embeddings still use OpenAI `text-embedding-3-small` (1536-dim) - the
  remaining data-residency gap. A Databricks Qwen3-Embedding-0.6B endpoint
  (1024-dim, `workspace.default.regwatch-embed`) exists; app wiring is
  pending.
- OpenAI is the LLM rollback path. Its provider uses the **Responses API**
  by default (`OPENAI_API_MODE=responses`; `chat` falls back to Chat
  Completions) with role-specific models, each falling back to `LLM_MODEL`:
  - `gpt-5-nano` (reasoning) for the router/classification role
  - `gpt-5.4-nano` for answer synthesis and BE extraction
- Reasoning models that reject `temperature` are retried without it.

## Multi-Source Architecture

Full Router -> Handlers -> Synthesizer flow:

```text
Question
  -> classify intent
  -> resolve entity/product
  -> route to FDA source handlers
  -> run source handlers
  -> validate evidence
  -> synthesize cited answer
  -> validate output
  -> audit
```

Source handlers exist today in `src/regwatch/sources/` behind a rules-first
router (`sources/router.py`) and are reachable via `POST /sources/search`:

- PSG: scoped RAG over PDF chunks
- Orange Book: structured lookup
- Drugs@FDA: structured lookup
- Drug Shortages: structured lookup
- NDC: structured lookup
- DailyMed (SPL): structured lookup
- REMS: structured lookup

The core idea is that only PSG needs RAG. Most other FDA sources are queried as
structured records.

The **White Paper populator** (`src/regwatch/whitepaper/`) is the shipped
instance of multi-source synthesis: it fuses Orange Book + Drugs@FDA + NDC +
DailyMed + Shortages + REMS + PSG into a cited cell graph with tri-state cells
(`populated` / `verified_absent -> 'No'` / `analyst_input_required`), persists
OB/SPL provenance with `last_fetched_at` freshness (migration `0005`), and
exports a `.docx` rendered from the exact reviewed result.

Still open (see `docs/ROADMAP.md`): the main `POST /query` answer path still
runs PSG-scoped RAG only (it does not yet synthesize the structured source
handlers), and the persist-and-cite + freshness pattern proven in the White
Paper has not yet been applied to the Ask/Assemble read paths.

## Local Commands

Common commands:

```bash
uv sync --extra dev --extra llm
uv run pytest -q
uv run regwatch init-db
uv run regwatch create-user                              # provision a login (auth gates every endpoint but /health)
uv run regwatch aliases --refresh
uv run regwatch seed
uv run regwatch ingest-all                               # full ~1,795-PSG A-Z catalog crawl
uv run python -m regwatch.eval.run_eval
uv run python -m regwatch.eval.run_eval --check-thresholds   # CI eval gate
uv run uvicorn regwatch.api.main:app --reload
cd regwatch/frontend && npm install && npm run dev      # Next.js UI on http://localhost:3000
```

## What To Ignore At First

Do not start with:

- `.venv/`
- `.git/`
- `.pytest_cache/`
- `.mypy_cache/`
- `.ruff_cache/`
- `__pycache__/`
- raw PDFs unless you want source examples

Start with the code under `src/regwatch/` and the tests.

## Mental Model

Think of REGWATCH as three systems connected together:

1. Evidence builder
   - fetches and stores FDA evidence

2. Evidence answerer
   - retrieves the right evidence and answers with citations

3. Evidence guardrail
   - validates citations, refuses weak answers, and audits everything

If you keep that model in mind, the codebase is much easier to read.
