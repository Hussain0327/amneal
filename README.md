# regwatch

A regulatory prep accelerator for a generic-drug Clinical Regulatory Affairs team.

It watches FDA Product-Specific Guidances (PSGs), matches changes against the
company's product pipeline, extracts cited bioequivalence requirements, and
answers plain-language questions over the FDA guidance corpus. Every answer
carries a source and a page, or an explicit "not found."

**This is a POC, not a production deployment.** It surfaces, organizes,
compares, and cites public FDA information. It does not author submission
content, render regulatory judgment, or take autonomous action. See
[Section 4 of the spec](#compliance-invariants) for the invariants that
encode this.

## What it does, exactly

- **Watch.** Crawl the PSG database, detect new and revised guidances, match
  them against a verified watchlist built from Drugs@FDA, surface a cited
  summary of what changed.
- **Assemble.** For a target product (active ingredient + dosage form +
  RLD), build a fully cited dossier: PSG(s), extracted BE requirements,
  RLD label from openFDA, applicable guidance via retrieval, dissolution
  method link, and a requirements checklist scaffold.
- **Ask.** Plain-language Q&A over the corpus. Inline `[short_name, p.N]`
  citations on every claim, exact-string refusal when the corpus does not
  contain the answer. **Conversational**: `/query` carries a chat session so
  follow-ups like "What about dissolution?" reuse the prior product — but only
  the product *filter* carries over, never prior chat text as evidence; every
  answer re-retrieves and re-validates citations. When a question names no
  product (or names several), it **clarifies** with clickable options instead
  of guessing; regulatory-strategy asks get a `scope_warning`, not a guess.

The product is **resolved before retrieval** and retrieval is **constrained to
the current PSG version**, so shared FDA boilerplate can't leak a wrong-drug or
a superseded citation. See [`docs/CONVERSATIONAL_SESSIONS.md`](docs/CONVERSATIONAL_SESSIONS.md).

## Non-goals

- No drafting, suggestion, or generation of FDA submission content.
- No regulatory recommendations ("you should run study X"). The system
  reports what the guidance says; the human decides.
- No internal or proprietary data. Public FDA sources only.
- No autonomous action — no filing, no submitting, no email to FDA.
- Not production-deployable. Pluggable interfaces (`EmbeddingProvider`,
  `LLMProvider`) so the IT/AI team can swap models without touching
  business logic.

## Compliance invariants

These are code with tests, not guidelines. See `tests/test_invariants.py`.

| | Invariant | Where it's enforced |
|---|---|---|
| INV-1 | Every factual claim is traceable to a source + page | `process/extractor.py` quote-verbatim check; `generate/grounded_qa.py` citation validator |
| INV-2 | If retrieval is weak, refuse — never guess | Two-layer refusal: pre-LLM (top score below `REFUSAL_SCORE_THRESHOLD`) and post-LLM (refusal string or no valid citations) |
| INV-3 | Operational only — no authoring, no judgment | Prompt design + structural grep against `api/` for forbidden endpoint names |
| INV-4 | Never report a run that didn't happen | `watch/alerts.py` skips any match whose `psg_version` is not in the DB |
| INV-5 | Verified provenance only | `WatchlistEntry.__post_init__` rejects sources outside `{drugsfda, anda_letter, manual}` |
| INV-6 | Every query is audited | `common/audit.py` writes a `query_log` row on every Q&A path (now with `session_id`/`turn_id`/`status`/`route_json`) |
| INV-9 | PSG answers are always product-resolved and ingredient-filtered — no cross-drug citation can survive | `retrieve/resolver.py` resolves the product before retrieval; `generate/grounded_qa.py` forces a `normalized_name` filter; `tests/test_cross_drug_leak.py` |

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ via `uv` |
| Scrape | `httpx` + `selectolax` (`pdfplumber` primary, `pypdf` fallback) |
| Chunking | Heading + page-aware recursive splitter, ~1000 tokens, ~150 overlap |
| Embeddings | Pluggable. Local `BAAI/bge-small-en-v1.5` via sentence-transformers (`--extra local-embeddings`); Docker defaults to `echo` unless opted into the heavier local model image |
| Vector store | ChromaDB, persistent on disk |
| Structured store | SQLite via SQLModel |
| DB migrations | Alembic baseline + incremental migrations |
| Retrieval | Two-stage. Stage 1: `VECTOR_TOP_K=50` (wide). Stage 2: rerank → `RERANK_TOP_K=8`. Reranker off by default; when off, stage 2 is `passages[:rerank_top_k]` |
| LLM | Pluggable behind `LLMProvider`. OpenAI via the **Responses API** (`OPENAI_API_MODE=responses`, default; `chat` falls back to Chat Completions). Role-specific models: router `gpt-5-nano` (reasoning), synthesizer + extractor `gpt-5.4-nano`, each falling back to `LLM_MODEL`. `anthropic` and `echo` (test-only) also supported |
| API | FastAPI. `POST /query` is conversational — accepts/returns `session_id`+`turn_id`, with response `status` ∈ `answer`/`summary`/`clarify`/`scope_warning`/`refused` |
| UI | **Next.js 14 (App Router, TypeScript) in `regwatch/frontend/`** — Ask / Assemble / Watch. Talks to the API through a same-origin `/api` proxy. (The earlier Streamlit POC was retired.) |
| Tooling | ruff, black, mypy strict on `src/`, pytest |

The LLM provider, model, and reranker are all behind interfaces. Nothing is
hard-coded in business logic.

## Quick start

```bash
# install
uv sync --extra dev --extra llm --extra local-embeddings

# copy env, fill in OPENAI_API_KEY
cp .env.example .env
$EDITOR .env

# init DB + dirs
uv run regwatch init-db

# discover sponsor-name aliases from Drugs@FDA (no guessing)
uv run regwatch aliases --refresh

# seed the verified seed PSGs (by application number — albuterol, levalbuterol,
# beclomethasone, albuterol+budesonide); `regwatch ingest-all` loads the full catalog
uv run regwatch seed

# tests (smoke + invariants + eval metrics)
uv run pytest -q

# API
uv run uvicorn regwatch.api.main:app --reload

# UI — the Next.js app in regwatch/frontend/ (separate terminal)
cd regwatch/frontend && npm install && npm run dev      # http://localhost:3000

# eval scorecard (deterministic gate also runs inside `uv run pytest`)
uv run python -m regwatch.eval.run_eval
```

To share the whole app (API + UI) over one public link, run
`./scripts/share-demo.sh` — it builds and starts both and opens a cloudflared
tunnel. The UI proxies `/api/*` to the backend, so only one origin is exposed.

`regwatch init-db` applies Alembic migrations for the active `SQLITE_PATH`.
When adding or changing tables, create a new migration under `migrations/versions/`
instead of relying on `SQLModel.metadata.create_all`.

## Docs

Start with [`docs/README.md`](docs/README.md) for the documentation map.
Key guides:

- [`docs/NON_TECH_GUIDE.md`](docs/NON_TECH_GUIDE.md) for business and
  regulatory stakeholders.
- [`docs/TECH_GUIDE_SIMPLE.md`](docs/TECH_GUIDE_SIMPLE.md) for technical
  onboarding.
- [`docs/CONVERSATIONAL_SESSIONS.md`](docs/CONVERSATIONAL_SESSIONS.md) for the
  chat-session / follow-up model.
- [`docs/DOCKER.md`](docs/DOCKER.md) for container setup and ingest notes.
- [`docs/PROD_READINESS.md`](docs/PROD_READINESS.md) for the POC→production path.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) for the append-only decision log.

## Docker

Full details are in [`docs/DOCKER.md`](docs/DOCKER.md).

The container image runs the same Python app for the API and ingest jobs.
Startup runs `regwatch init-db`; large ingest runs are separate commands so API
boot stays fast. The `regwatch/frontend/` Next.js UI is run separately
(`npm run dev`, or `./scripts/share-demo.sh`); it is not part of the compose
stack today.

```bash
# build the shared image
docker build -t regwatch:local .

# optional heavier image with local sentence-transformer embeddings
docker build --build-arg INSTALL_LOCAL_EMBEDDINGS=true -t regwatch:local-embeddings .

# API on http://localhost:8000
docker compose up api

# one-shot seed ingest; later this becomes the broad PSG/source sync job
docker compose --profile ingest run --rm ingest
```

Compose mounts `./data` into `/app/data` so SQLite, Chroma, raw PDFs, and
processed files survive restarts. The default Compose image uses
`EMBEDDING_PROVIDER=echo` to avoid shipping PyTorch/CUDA in the baseline image;
set `INSTALL_LOCAL_EMBEDDINGS=true` and `EMBEDDING_PROVIDER=local-bge-small`
in `.env`, then run `docker compose build`, if you want local BGE embeddings
inside Docker. Do not run broad production ingest with `echo` embeddings.
Container defaults are:

```
DATA_DIR=/app/data
CHROMA_DIR=/app/data/chroma
SQLITE_PATH=/app/data/regwatch.db
RAW_PDF_DIR=/app/data/raw
PROCESSED_DIR=/app/data/processed
EMBEDDING_PROVIDER=echo
API_HOST=0.0.0.0
API_PORT=8000
```

## API

```
POST  /query        grounded, conversational Q&A
                    in:  {question, filters?, k?, session_id?, user_id?}
                    out: {answer, citations[], refused, status, interpretation,
                          clarify[], model_name, audit_id, session_id, turn_id}
                    status ∈ answer | summary | clarify | scope_warning | refused
POST  /sources/search structured FDA source lookup — {routed_sources[], records[]}
POST  /assemble     cited dossier for {active_ingredient, dosage_form?, rld?}
GET   /watch/latest matched changes since cursor
GET   /products     watchlist
POST  /products     add a manual product (INV-5 enforced)
GET   /health       liveness
GET   /settings     non-secret config
```

CORS is allow-listed via `CORS_ALLOW_ORIGINS_CSV` (defaults to the Next.js dev
origins). There is no auth layer yet — see [`docs/PROD_READINESS.md`](docs/PROD_READINESS.md).

OpenAPI docs at `/docs`. Every response is reproducible in Postman without
internal access.

## Watchlist sources

The watchlist is built from three allowed sources, in this order:

1. `drugsfda` — `api.fda.gov/drug/drugsfda.json`, filtered to applications
   whose `sponsor_name` matches any discovered Amneal variant. Aliases are
   discovered with `uv run regwatch aliases`, not hand-coded. On a recent
   run this returned 8 distinct variants including `AMNEAL EU LTD`,
   `AMNEAL IRELAND LTD`, `AMNEAL PHARMS NY`.
2. `anda_letter` — user-uploaded approval letters, asserted by the user.
3. `manual` — explicit overrides.

INV-5 rejects anything else, including model memory.

## Eval

Two layers:

- **`uv run python -m regwatch.eval.run_eval`** scores the gold set
  (`src/regwatch/eval/gold_set.jsonl`, 11 items: 6 real + 5 must-refuse) against
  the live corpus. Hard gates (fail CI when below): `recall@k ≥ 0.90`,
  `citation_precision ≥ 0.95`, `refusal_accuracy ≥ 0.95`. `faithfulness` and
  `fact_recall` (fraction of an item's `expected_facts` present in the answer)
  are printed for observability. It exits clean on an empty store so a fresh
  checkout passes; the real gate only fires once a seed has run.
- **`tests/test_eval_gate.py`** is a deterministic, offline gate that seeds a
  fixed corpus and a faithful LLM stub, so the full pipeline (resolve → filter →
  retrieve → cite → refuse) is graded on every `uv run pytest` — including
  `faithfulness` and `fact_recall` hard-gated at 1.0 — and fires in CI where the
  live `run_eval` no-ops on an empty corpus.

## Layout

```
config/settings.py        pydantic-settings, all thresholds + secrets here
src/regwatch/
  ingest/                 psg_crawler, pdf_parser, pipeline
  process/                chunker, embedder, extractor, change_detector
  store/                  db, models, vector_store (Chroma wrapper)
  retrieve/               retriever (stage 1), reranker (stage 2, off by default)
  sources/                FDA source handlers + rules-first source router
  generate/               llm provider interface, grounded_qa, prompts
  watch/                  watchlist, aliases (Drugs@FDA discovery), matcher, alerts
  assemble/               dossier
  eval/                   metrics, run_eval, gold_set.jsonl
  api/                    FastAPI surface
  common/                 logging, audit, citations, text_normalize, conversation (chat sessions)
regwatch/
  backend/                backend workspace docs; source stays in src/regwatch
  frontend/               Next.js (App Router, TS) UI — Ask / Assemble / Watch
tests/                    smoke, invariants (INV-1..6, INV-9), eval gate, per-module
```

## Build phases

Built phase by phase. After each phase, the full test suite and the phase's
Definition of Done passed before moving on.

| Phase | Outcome |
|---|---|
| 0 | Scaffold, providers, CI, smoke tests |
| 1 | PSG crawler + PDF parser + chunker + embedder + cited BE extraction, idempotent |
| 2 | Two-stage retrieval + grounded Q&A with citations + refusal + audit |
| 3 | Drugs@FDA watchlist, alias discovery, fuzzy matcher, version diff, JSONL digest |
| 4 | Dossier builder + FastAPI |
| 5 | Eval harness + gold set + CI thresholds |
| 6+ | Next.js UI (`regwatch/frontend/`), OpenAI Responses API + role-specific models, conversational sessions, current-version retrieval, entity-resolution hardening, deterministic eval gate |

## What's not done

- **No auth.** Every endpoint is open and CORS is the only boundary. Auth +
  rate limiting are the top blocker before any external exposure.
- **Datastores are single-node** (SQLite + on-disk Chroma); no HA, pooling, or
  backup/restore. Migrations still run on app boot rather than as a deploy step.
- The gold set is 11 items, not the spec's 30–50, and scoring is mechanical
  (`(short_name, page)` + `expected_facts` substrings). LLM-as-judge is not wired.
- The cross-encoder reranker exists as a hook but is off by default. Turn
  on with `RERANKER_ENABLED=true` and tune `VECTOR_TOP_K` upward.
- Auto scheduling via APScheduler is a stub. POC runs ingest on demand.
- FDA source handlers exist (`sources/`: PSG, Orange Book, Drugs@FDA, Shortages,
  NDC, REMS) but are live-HTTP only — no persisted source tables, freshness
  metadata, caching, or cross-source answer synthesis yet.

See [`docs/PROD_READINESS.md`](docs/PROD_READINESS.md) for the full,
prioritized path from POC to production.

## License

MIT.
