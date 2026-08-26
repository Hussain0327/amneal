# regwatch

Last updated: 2026-08-13

regwatch helps a generic-drug regulatory team answer FDA research questions in
minutes instead of days. Every factual sentence comes back with the source and
page it came from, and when the corpus does not have the answer, the reply says
so in plain words.

An analyst preparing a generic-drug application spends hours reading FDA
**Product-Specific Guidances** (PSGs, the agency's per-drug instructions for
proving a generic is equivalent to the brand) and cross-checking Drugs@FDA,
approval/action packages, FDA bioequivalence guidance, and the Orange Book.
regwatch does that reading for them. It pins every question to one product so
citations cannot cross drugs and answers in plain language with a citation on
every FDA fact.

The product **surfaces, organizes, compares, and cites public FDA information.**
It never drafts submission content, never makes a regulatory recommendation, and
never acts on its own. A person makes every regulatory decision.

## What it does

Five surfaces share one "product under review". A sixth, the Compliance Studio,
sits apart from them and is described below.

- **Ask**: plain-language Q&A over the guidance corpus, as a cited chat. Every
  FDA fact carries an inline `[source, p.N]` citation you can click to read the
  exact passage. When the corpus cannot support an answer, Ask says that in
  ordinary words and points at a better next question. Ambiguous questions
  ("propranolol") get clickable clarifying options rather than a wrong answer.
- **Assemble**: for a target product, build a fully cited dossier: the relevant
  PSG(s), extracted bioequivalence requirements, approved Drugs@FDA labeling,
  applicable FDA guidance, and a requirements checklist.
- **Watch**: crawl the FDA PSG database daily, detect new and revised guidances,
  match them against the company's product pipeline, and surface a cited summary
  of what changed.
- **White Paper**: given a brand-drug name and application number, fill the CRA
  White Paper template cell by cell. Each cell carries its provenance (source,
  locator, and when it was fetched). Cells the system cannot determine
  deterministically, such as patents, BE strategy, and every required-studies
  judgment, are left for an analyst with the evidence attached. They are never
  auto-answered.
- **Deficiency**: analyze a submission for the deficiencies an FDA reviewer is
  likely to raise, with the evidence behind each one.

All five share one **current product**, set from the scope-bar picker, a Watch
row, or a White Paper run. The product is held in the URL (`?rp=&appl=`) so a
view is shareable and survives reload.

### Compliance Studio (`/studio`)

Every surface above reads **public FDA material**. The Compliance Studio reads
**our own drafts**: an IDE-style workbench where a reviewer opens a CMC document,
reads it or has the assistant summarize a passage, runs it against ICH / USP /
21 CFR / internal SOPs, and records what they decided about each finding.

A finding is a span of the document, not a report line. It highlights in place,
and editing the text underneath it invalidates the claim. "Fixed" cannot be
recorded until the anchored text has actually changed.

It sits outside the shared shell: whole viewport, no product scope bar yet.
**It is UI and domain model only.** The document service, the compliance
pipeline, and the assistant are fixtures behind typed seams, and nothing
survives a refresh. See
[docs/COMPLIANCE_STUDIO.md](docs/COMPLIANCE_STUDIO.md).

## The core rule: cite the facts, talk like a person

This is the whole point, and it is enforced in code with tests, not by asking the
model nicely. Every sentence an Ask answer writes is one of three kinds.

1. **Source fact.** Says what FDA guidance requires, recommends, permits, or
   prohibits. It has to end with the passage numbers it came from, like `[1]` or
   `[1, 3]`, placed right before the final period. An uncited source fact is
   dropped before the analyst ever sees it. That is INV-1, and it lives in
   `generate/turn_gate.py`, not in the prompt text.
2. **Reasoning.** Our own reading, going past what the passages say. It carries
   no numbers and has to open with one of four fixed phrases, such as
   "My reading is ..." or "Reading the guidance together, ...". The exact list is
   pinned in `turn_gate.REASONING_FRAME_PREFIXES` and a test enforces it. An
   obligation or prohibition cannot hide inside a reasoning sentence: the gate
   reclassifies it back to a source fact, and then it needs a citation.
3. **Conversation.** Greetings, offers, transitions, a question back to the user.
   Plain text, no numbers, no FDA facts.

There is no code word for "not found". When the passages do not answer the
question, the model says so in its own words, names what it does have nearby, and
offers a next step. That reply carries no passage numbers at all.

What holds it together:

1. **Resolve the product first.** A question is pinned to one drug (canonical
   name plus 6-digit application number) *before* anything is retrieved. Shared
   FDA boilerplate cannot leak a wrong-drug citation.
2. **Retrieve scoped and current.** Retrieval is filtered to that product's
   ingredient and to the *current* PSG version, so a superseded passage cannot be
   cited as if it were live.
3. **Keep weak evidence out.** If the best match scores below
   `REFUSAL_SCORE_THRESHOLD` (default 0.30), those passages go to neither the
   synthesizer nor the guidance planner. The planner may pick a safe, allowlisted
   next step, but it cannot author factual answer text.
4. **One bounded AI role per turn.** A grounded question uses the synthesizer. A
   pre-synthesis product, form, scope, capability, or evidence-gap outcome uses
   the router-role guidance planner, which can only select an approved next step
   and existing option IDs. Product, form, scope, status, citation validation,
   and display copy stay in application code. Operational errors never enter the
   guidance path.
5. **Audit everything.** Every query, answered or not, writes exactly one
   `query_log` row.

The gate records one verdict per turn: `answer`, `partial`, `material_drop`,
`no_valid_citations`, `no_evidence`, or `conversational_decline`.

### How a question flows

The **Ask** path, from input to an audited answer. Assemble and White Paper
follow the same discipline over structured FDA sources.

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
    PRECHK -- "yes" --> SYNTH["Synthesizer LLM<br/>plain prose, v7 selective citation"]

    SYNTH --> VALID{"Every source fact cited to<br/>a retrieved passage?"}
    VALID -- "no" --> REFUSE["evidence gap / declined<br/>no second AI call<br/>INV-1 - uncited source facts dropped"]
    VALID -- "yes" --> ANSWER["answer<br/>answer + citation chips"]

    GUIDE --> COPY["Application renders trusted copy<br/>and existing options"]
    COPY --> FINAL["Finalize one audit row<br/>status, route, citations"]
    ERROR --> FINAL
    REFUSE --> FINAL
    ANSWER --> FINAL
```

The canonical system design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Runtime boundary

The repository target is OpenAI-only for model calls:

- Generation uses the OpenAI Responses API with `gpt-5.6-luna`, medium
  reasoning, and `store=false`. RegWatch supplies the conversation transcript
  on every request; it does not use OpenAI conversation state or
  `previous_response_id`.
- Embeddings use OpenAI `text-embedding-3-large` with
  `dimensions=1024`.
- Retrieval runs exact vector search in RegWatch's PostgreSQL/pgvector database.
  Approximate HNSW retrieval is disabled.
- Sessions, documents, vectors, audit rows, and all other application state stay
  in the database selected by RegWatch's `DATABASE_URL`; OpenAI does not own
  application state.

This describes the checked-in runtime target. Moving a live deployment to a new
PostgreSQL host, rotating provider secrets, backfilling the OpenAI embedding
profile, and deploying are separate operator actions and must be verified before
calling production migrated.

The authoritative FDA discovery manifest contains 140,438 source records.
Those are source records, not chunks or embeddings. Activation still requires
the corpus acceptance checks in
[`docs/AUTHORITATIVE_FDA_CORPUS.md`](docs/AUTHORITATIVE_FDA_CORPUS.md).

## Quick start

```bash
# install development tools and the OpenAI SDK
uv sync --extra dev --extra llm

# configure. Providers are required-explicit: use OpenAI for real model calls;
# offline tests and local smoke runs use the echo providers.
cp .env.example .env && $EDITOR .env

# initialize the DB and data directories
uv run regwatch init-db

# discover Amneal sponsor-name aliases from Drugs@FDA (no hand-coding)
uv run regwatch aliases --refresh

# seed the verified starter PSGs by application number
# (`regwatch ingest-all` loads the full catalog)
uv run regwatch seed

# discover the complete FDA-only replacement corpus without writing the DB
uv run regwatch authoritative-corpus-plan

# diagnostics remain available while a profile is incomplete
uv run regwatch authoritative-corpus-status

# production corpus builds use the dedicated OCR/Dagster worker and exact
# 512-shard runbook; do not launch the 140k build from an API machine
docker build -f Dockerfile.corpus-worker -t regwatch-corpus-worker .

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

To share the whole app (API plus UI) over one public link, run
`./scripts/share-demo.sh`. It builds both and opens a cloudflared tunnel. The UI
proxies `/api/*` to the backend, so only one origin is exposed.

## How it's built

One browser-visible origin, two Fly runtimes, one RegWatch-owned datastore,
and OpenAI for stateless model calls:

```mermaid
flowchart LR
    B["Browser"] --> V["Vercel / Next.js"]
    V --> GO["Fly.io / Go proxy"]
    GO --> PY["Fly.io / FastAPI"]
    GO --> PG[("RegWatch PostgreSQL + pgvector")]
    PY --> PG
    PY --> RESP["OpenAI Responses<br/>gpt-5.6-luna"]
    PY --> EMB["OpenAI Embeddings<br/>text-embedding-3-large / 1024"]
```

| Layer | Choice |
|---|---|
| Edge | Go proxy for auth, sessions, rate limits, query orchestration, and audit |
| RAG core | Python, FastAPI, deterministic retrieval and citation gates |
| Frontend | Next.js App Router and React |
| LLM | OpenAI Responses API, `gpt-5.6-luna`, medium reasoning, `store=false` |
| Embeddings | OpenAI `text-embedding-3-large`, 1024 dimensions |
| Retrieval | exact pgvector search; approximate HNSW mode disabled |
| State | RegWatch PostgreSQL selected by `DATABASE_URL`; no OpenAI application state |
| Deploy | Fly.io backend and Vercel frontend; deployment remains operator-controlled |

Provider and model access stays behind interfaces so tests can use deterministic
echo providers without external calls.

## Compliance invariants

These are enforced in code with tests. See
[`tests/test_invariants.py`](tests/test_invariants.py).

| | Invariant | Where it's enforced |
|---|---|---|
| INV-1 | Every source fact is traceable to a source + page | `generate/turn_gate.py` admits a claim only when its citation resolves to a passage that was actually retrieved, and drops uncited source facts; `process/extractor.py` quote-verbatim check |
| INV-2 | If retrieval is weak, do not answer from it | The score threshold blocks weak passages from the synthesizer and the guidance planner; the post-synthesis gate blocks unsupported claims |
| INV-3 | Operational only, no authoring and no judgment | Prompt design plus a structural check against `api/` for forbidden endpoints |
| INV-4 | Never report a run that didn't happen | `watch/alerts.py` skips any match whose `psg_version` is not in the DB |
| INV-5 | Verified provenance only | `WatchlistEntry` rejects sources outside `{drugsfda, anda_letter, manual}` |
| INV-6 | Every query is audited | `common/audit.py` writes a `query_log` row on every Q&A path |
| INV-7 | Cross-product integrity: never blend two applications' data | `whitepaper/populator.py` matches PSGs on exact application-number tokens (with `sources/psg.py`); `tests/test_whitepaper_populator.py` |
| INV-8 | Structured citations obey a strict token grammar and are honored only when backed | `common/citations.py` `validate_structured_citations`; `whitepaper/populator.py` collapses any unbacked cell to analyst input; `tests/test_citations.py` |
| INV-9 | PSG answers are always product-resolved and ingredient-filtered, so no cross-drug citation survives | `retrieve/resolver.py` plus `generate/grounded_qa.py`; `tests/test_cross_drug_leak.py` |

## API

Every product/data endpoint requires a login. `GET /health`, `GET /ready`, and
`GET /metrics` are operational endpoints outside the auth router; `/metrics` is
open by default and bearer-gated when `METRICS_TOKEN` is set. Full
request/response shapes are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Since the step-4 cutover the **Go proxy serves `/auth/*`, `/sessions`,
`/feedback`, `/settings`, and `/products` natively** at the public edge, and
since step 5 it also **orchestrates `POST /query` natively**: it persists the
audit row and calls Python's internal, token-gated `POST /internal/query/compute`
for the RAG work. The `/internal/` subtree is never exposed publicly. The
remaining structured-source endpoints relay to Python. The public wire contract
is identical either way, and the [`tests_contract/`](tests_contract/) suite
proves it across the boundary.

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

The auto-docs routes (`/docs`, `/redoc`, `/openapi.json`) are disabled. They
register outside the auth wall and would disclose the API surface anonymously.

### Auth

Sessions are DB-backed opaque tokens in an HttpOnly `regwatch_session` cookie
(SameSite=Lax; the DB stores only the token's sha256; passwords are bcrypt-hashed).
There is no self-signup. Users are provisioned from the CLI:

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
`CORS_ALLOW_ORIGINS_CSV`. TLS termination and OIDC/SSO are environment work; see
[`docs/PROD_READINESS.md`](docs/PROD_READINESS.md).

## Watchlist sources

The watchlist is built from three allowed identity inputs, in order. INV-5
rejects anything else, including model memory:

1. `drugsfda`: the official Drugs@FDA data-file snapshot, filtered to
   applications whose sponsor matches a discovered Amneal variant. Aliases
   come from `uv run regwatch aliases`, not hand-coding.
2. `anda_letter`: user-uploaded approval letters.
3. `manual`: explicit overrides.

## Eval

Two layers grade the answer pipeline:

- **`uv run python -m regwatch.eval.run_eval`** scores a curated gold set
  ([`src/regwatch/eval/gold_set.jsonl`](src/regwatch/eval/gold_set.jsonl)) of
  real, must-refuse, and must-clarify questions against the live corpus. The
  blocking floors are `recall_at_k >= 0.80` and `citation_precision >= 0.74`.
  These are regression floors, not claims that retrieval quality is complete.
  `refusal_accuracy` is still measured and printed but stopped blocking on
  2026-08-06, because Ask is deliberately moving away from refusing. The older
  `0.90 / 0.95 / 0.95` numbers are recorded as aspirational targets, never as
  gates. A run that loses more than 10 percent of its turns to transport
  failures exits without scoring, so an outage cannot pass on a shrunken
  denominator. It also exits nonzero on an empty store. In CI this runs as its
  own OpenAI-backed workflow,
  [`.github/workflows/openai-eval.yml`](.github/workflows/openai-eval.yml),
  which evaluates the Responses and 1024-dimension OpenAI embedding path.
- **[`tests/test_eval_gate.py`](tests/test_eval_gate.py)** is a deterministic,
  offline gate. It seeds a fixed corpus and a faithful LLM stub, so the full
  pipeline (resolve -> filter -> retrieve -> cite -> gate) is graded on every
  `uv run pytest`, including CI. Passing this fixture is not a live-corpus
  quality result.

Growing the gold set is a human process. Thumbs up/down from the Ask UI is the
candidate pool. Review them and promote good ones into `gold_set.jsonl` by hand.
Nothing is auto-ingested.

See [`docs/EVAL_STATUS.md`](docs/EVAL_STATUS.md) for the verified gold-set
counts, the latest live-artifact interpretation, and the still-provisional `0.30`
cutoff.

## Project layout

```
config/settings.py        pydantic-settings: all thresholds + secrets
src/regwatch/
  corpus/                 FDA discovery, atomic sync, status, embedding backfill
  ingest/                 psg_crawler, pdf_parser, pipeline, embedding writer
  process/                chunker, embedder, extractor, change_detector
  store/                  db, models, vector_store, pgvector_store
  retrieve/               retriever (stage 1), reranker (stage 2)
  sources/                exact five-family policy, adapters, and source router
  generate/               LLM provider interface, grounded_qa, prompts, turn_gate
  watch/                  watchlist, aliases, matcher, alerts
  assemble/               dossier builder
  whitepaper/             CRA White Paper populator + .docx writer
  eval/                   metrics, run_eval, gold_set.jsonl
  api/                    FastAPI surface
  auth/                   passwords (bcrypt), cookie sessions, require_user
  common/                 logging, audit, citations, text_normalize, conversation, ratelimit
migrations/               Alembic migration history (the single schema authority)
go/                       Go proxy: public edge + native auth/sessions/feedback/settings/products + native /query orchestration (sqlc store)
regwatch/frontend/        Next.js (App Router, TS) UI: one (shell) for the five scoped surfaces, plus /studio outside it
tests/                    smoke, invariants, eval gate, per-module
tests_contract/           cross-service contract suite: real Go proxy + uvicorn + Postgres
```

## Docs

Start with the Map of Content, [`docs/MAP.md`](docs/MAP.md). Highest-value entry
points:

- [Architecture](docs/ARCHITECTURE.md): canonical system design.
- [Authoritative FDA corpus](docs/AUTHORITATIVE_FDA_CORPUS.md): exact source
  boundary, 140,438-record snapshot, counts, checksums, runbook, activation, and
  rollback.
- [Non-technical guide](docs/NON_TECH_GUIDE.md): plain English for business and
  regulatory readers.
- [Production readiness](docs/PROD_READINESS.md): the POC-to-production path.
- [Decisions](docs/DECISIONS.md): append-only log of what was chosen and why.
- [CI/CD pipeline](docs/CI_CD.md): the CI gate (Python lint/type/test, audit,
  docker build, frontend, Go proxy, schema-drift, and the cross-service contract
  lane) plus a pre-push checklist. Read it before pushing so you do not fail CI.

## Docker

Full details in [`docs/DOCKER.md`](docs/DOCKER.md). The one image runs the API
and ingest jobs; Compose adds the Next.js UI and a local pgvector Postgres
service for development.

```bash
docker build -t regwatch:local .          # shared image
docker compose up api                      # API on http://localhost:8000
docker compose up --build api web          # API + UI (http://localhost:3000)
```

Compose mounts `./data` so raw and processed PDFs survive restarts and runs
Postgres via its `db` service. The compose stack defaults to the offline `echo`
providers for smoke use. For real model calls set both providers to `openai`
and configure the OpenAI fields documented in `.env.example`.
See [`docs/DEPLOY.md`](docs/DEPLOY.md).

## License

MIT.
