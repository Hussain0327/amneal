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
  exact passage; if the corpus doesn't contain the answer, Ask refuses instead
  of guessing. Ambiguous questions ("propranolol") get clickable clarifying
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
3. **Refuse weak grounding.** If the best match scores below
   `REFUSAL_SCORE_THRESHOLD` (default 0.30), the LLM is never called — the
   request is refused. After synthesis, any claim whose citation doesn't trace
   to a retrieved passage is stripped; if nothing valid remains, the whole answer
   becomes a refusal.
4. **Audit everything.** Every query — answered, clarified, or refused — writes
   exactly one `query_log` row.

A refusal is shown as a refusal, never dressed up as an answer.

### How a question flows

The **Ask** path, from input to an audited answer or an explicit refusal.
Assemble and White Paper follow the same discipline over structured FDA sources.

```mermaid
flowchart TD
    Q["User question<br/>(+ session, filters, k)"] --> AUTH{"Authenticated?"}
    AUTH -- "no" --> E401["401"]
    AUTH -- "yes" --> AUDIT["Write query_log row<br/>INV-6 - every query audited"]
    AUDIT --> ROUTER["Router LLM<br/>classify intent and route"]

    ROUTER --> NAMED{"Product resolvable?"}
    NAMED -- "none or several" --> CLARIFY["clarify<br/>return clickable options"]
    NAMED -- "strategy ask" --> SCOPE["scope_warning<br/>report, never advise (INV-3)"]
    NAMED -- "one product" --> RESOLVE["Resolve product, deterministic<br/>canonical name + application no."]

    RESOLVE --> MATCH{"Name matches number?"}
    MATCH -- "no" --> REFUSE1["refused<br/>refuse-over-guess (422)"]
    MATCH -- "yes" --> RETRIEVE["Stage 1, vector top-k 50<br/>ingredient-filtered, current PSG version<br/>INV-9 - no cross-drug citation"]

    RETRIEVE --> RERANK["Stage 2, rerank to top-k 8<br/>reranker optional"]
    RERANK --> PRECHK{"Top score above threshold?"}
    PRECHK -- "no" --> REFUSE2["refused<br/>INV-2 - weak retrieval refuses"]
    PRECHK -- "yes" --> SYNTH["Synthesize LLM<br/>grounded answer + inline citations"]

    SYNTH --> VALID{"Every claim cited to<br/>a retrievable source?"}
    VALID -- "no" --> REFUSE3["refused<br/>INV-1 - cite or refuse"]
    VALID -- "yes" --> ANSWER["answer<br/>answer + citation chips"]

    CLARIFY --> FINAL["Finalize audit row<br/>status, route, citations"]
    SCOPE --> FINAL
    REFUSE1 --> FINAL
    REFUSE2 --> FINAL
    REFUSE3 --> FINAL
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
- It is **not yet externally exposed.** The work between here and an external
  launch — the data-handling / LLM-vendor decision (D1), an SSO + TLS gateway,
  gated deploy-step migrations, least-privilege DB credentials, and a rehearsed
  restore drill — is tracked in [`docs/PROD_READINESS.md`](docs/PROD_READINESS.md)
  and [`docs/ROADMAP.md`](docs/ROADMAP.md).

**Deliberate scope** (boundaries, not gaps): the reranker is a hook that's off by
default; the eval gold set is a curated starter set, expanded by hand from real
answer-feedback; and the persist-and-cite source-freshness path is proven on
White Paper before being generalized to Ask and Assemble.

## Quick start

```bash
# install (dev tools + LLM clients + local embeddings)
uv sync --extra dev --extra llm --extra local-embeddings

# configure — fill in OPENAI_API_KEY
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

# run the tests (smoke + invariants + offline eval gate)
uv run pytest -q

# API  ->  http://localhost:8000
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

| Layer | Choice |
|---|---|
| Backend | Python 3.11+ (managed by `uv`), FastAPI |
| Frontend | Next.js 16 (App Router, TypeScript) + React 18 in `regwatch/frontend/`. All four surfaces render in one `(shell)` route group — one sidebar, one product-scope bar. Talks to the API through a same-origin `/api` proxy |
| LLM | OpenAI via the Responses API (`OPENAI_API_MODE=responses`). Role-specific models: router `gpt-5-nano`, synthesizer + extractor `gpt-5.4-nano` (each falling back to `LLM_MODEL`). Pluggable behind `LLMProvider` — `anthropic` and a test-only `echo` are also supported |
| Embeddings | Pluggable. Prod: OpenAI `text-embedding-3-small` (1536-dim). Dev/Compose default: local `BAAI/bge-small-en-v1.5` (`--extra local-embeddings`) |
| Vector store | Prod: **pgvector** in the same Postgres. Dev/CI: ChromaDB persistent-on-disk |
| Structured store | Prod: **Postgres** (Supabase) via SQLModel; `REQUIRE_DATABASE_URL=true` refuses the SQLite fallback. Dev/CI: SQLite. Schema changes ship as Alembic migrations |
| Retrieval | Two-stage. Stage 1: vector top-k 50 (`VECTOR_TOP_K`). Stage 2: rerank to top-k 8 (`RERANK_TOP_K`); reranker off by default |
| Ingest | `httpx` + `selectolax`; `pdfplumber` (with `pypdf` fallback); heading- and page-aware chunking (~1000 tokens, ~150 overlap) |
| Deploy | API on Fly.io, DB + vectors on Supabase, frontend on Vercel; daily Watch via GitHub Actions cron |
| Tooling | ruff, black, mypy (strict on `src/`), pytest, import-linter layering contracts |

The LLM provider, model, reranker, and embedding provider all sit behind
interfaces; nothing is hard-coded in business logic.

## Compliance invariants

These are enforced in code with tests — see [`tests/test_invariants.py`](tests/test_invariants.py).

| | Invariant | Where it's enforced |
|---|---|---|
| INV-1 | Every factual claim is traceable to a source + page | `process/extractor.py` quote-verbatim check; `generate/grounded_qa.py` citation validator |
| INV-2 | If retrieval is weak, refuse — never guess | Two-layer refusal: pre-LLM (top score below `REFUSAL_SCORE_THRESHOLD`) and post-LLM (no valid citations) |
| INV-3 | Operational only — no authoring, no judgment | Prompt design + a structural check against `api/` for forbidden endpoints |
| INV-4 | Never report a run that didn't happen | `watch/alerts.py` skips any match whose `psg_version` is not in the DB |
| INV-5 | Verified provenance only | `WatchlistEntry` rejects sources outside `{drugsfda, anda_letter, manual}` |
| INV-6 | Every query is audited | `common/audit.py` writes a `query_log` row on every Q&A path |
| INV-9 | PSG answers are always product-resolved and ingredient-filtered — no cross-drug citation survives | `retrieve/resolver.py` + `generate/grounded_qa.py`; `tests/test_cross_drug_leak.py` |

> Invariant IDs are stable identifiers, not a contiguous range — there is no INV-7
> or INV-8 in force. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full
> set and [`docs/DECISIONS.md`](docs/DECISIONS.md) for the rationale.

## API

Every endpoint except `GET /health` requires a login. Full request/response
shapes are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
POST   /auth/login        {email, password} -> {user} + HttpOnly session cookie
POST   /auth/logout       revoke the session, clear the cookie (204)
GET    /auth/me           current user, or 401

POST   /query             grounded, conversational Q&A
                          status in: answer | summary | clarify | scope_warning | refused
POST   /query/stream      same, streamed over SSE: progress frames, then the
                          validated answer as one result frame (no answer text
                          before its citations are validated — INV-1)
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

GET    /health            liveness + component diagnostics; 503 when db/chroma is down
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
  gates (fail CI when below): `recall@k >= 0.90`, `citation_precision >= 0.95`,
  `refusal_accuracy >= 0.95`. It no-ops on an empty store, so a fresh checkout
  passes; the gate fires once a seed has run.
- **[`tests/test_eval_gate.py`](tests/test_eval_gate.py)** is a deterministic,
  offline gate: it seeds a fixed corpus and a faithful LLM stub, so the full
  pipeline (resolve -> filter -> retrieve -> cite -> refuse) is graded on every
  `uv run pytest`, including in CI where the live `run_eval` no-ops.

Growing the gold set is a human process: thumbs up/down from the Ask UI is the
candidate pool. Review them and promote good ones into `gold_set.jsonl` by hand —
nothing is auto-ingested.

## Project layout

```
config/settings.py        pydantic-settings: all thresholds + secrets
src/regwatch/
  ingest/                 psg_crawler, pdf_parser, pipeline
  process/                chunker, embedder, extractor, change_detector
  store/                  db, models, vector_store (Chroma), pgvector_store
  retrieve/               retriever (stage 1), reranker (stage 2)
  sources/                FDA source handlers + source router
  generate/               LLM provider interface, grounded_qa, prompts
  watch/                  watchlist, aliases, matcher, alerts
  assemble/               dossier builder
  eval/                   metrics, run_eval, gold_set.jsonl
  api/                    FastAPI surface
  auth/                   passwords (bcrypt), cookie sessions, require_user
  common/                 logging, audit, citations, text_normalize, conversation, ratelimit
regwatch/frontend/        Next.js (App Router, TS) UI — one (shell) for all four surfaces
tests/                    smoke, invariants, eval gate, per-module
```

## Docs

Start with the Map of Content, [`docs/MAP.md`](docs/MAP.md). Highest-value entry
points:

- [Architecture](docs/ARCHITECTURE.md) — canonical system design.
- [Non-technical guide](docs/NON_TECH_GUIDE.md) — plain English for business and
  regulatory readers.
- [Production readiness](docs/PROD_READINESS.md) — the POC-to-production path.
- [Decisions](docs/DECISIONS.md) — append-only log of what was chosen and why.
- [CI/CD pipeline](docs/CI_CD.md) - the five-job gate and a pre-push checklist;
  read before pushing so you do not fail CI.

## Docker

Full details in [`docs/DOCKER.md`](docs/DOCKER.md). The one image runs the API,
ingest jobs, and the Dagster code location; Compose adds the Next.js UI and a
local Dagster OSS stack for development.

```bash
docker build -t regwatch:local .          # shared image
docker compose up api                      # API on http://localhost:8000
docker compose up --build api web          # API + UI (http://localhost:3000)
```

Compose mounts `./data` so SQLite, Chroma, and PDFs survive restarts, and
defaults to local embeddings (`EMBEDDING_PROVIDER=local-bge-small`). For a slim
production build, set `INSTALL_LOCAL_EMBEDDINGS=false` with
`EMBEDDING_PROVIDER=openai` + `OPENAI_API_KEY` — see [`docs/DEPLOY.md`](docs/DEPLOY.md).

## License

MIT.
