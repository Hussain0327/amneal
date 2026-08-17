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

## Status

regwatch runs as a **limited internal pilot**, not a generally available product.
The deployed-system and corpus counters below were checked on 2026-08-13.

- **Deployed.** The API runs on Fly.io (app `amneal`, release v135). The frontend
  runs on Vercel. Postgres and pgvector are on **Databricks Lakebase**, in the
  company's own Databricks tenant. Rows, vectors, and the audit log all live in
  that one database. The currently serving legacy PSG corpus is 5,494 chunks.
- **The replacement authoritative FDA corpus pipeline is implemented but not
  activated.** A complete read-only discovery found **140,339 source records**:
  99,190 Drugs@FDA, 10,156 action-package, 1,795 PSG, 5 FDA BE-guidance, and
  29,193 Orange Book records. Those are documents, not chunks or embeddings.
  A first production canary indexed 18 / 21 records into 347 chunks and stopped
  on three parse failures. This follow-up adds streamed temporary files,
  durable object storage, sandboxed OCR, independent chunk/vector lifecycle,
  and 512-shard Dagster orchestration. The serving namespace stays on `legacy`
  until the canary reaches 21 / 21 and a successful full run reaches 100%
  profile coverage plus the activation gate. See
  [`docs/AUTHORITATIVE_FDA_CORPUS.md`](docs/AUTHORITATIVE_FDA_CORPUS.md).
- **Both model roles run on Databricks Model Serving**, in the same tenant.
  Generation is `gpt-oss-120b` (served id `gpt-oss-120b-080525`) behind the
  serving alias `workspace.default.regwatch`, and that one open-weight model
  handles every LLM role (`LLM_PROVIDER=databricks`). Embeddings are Qwen3 on
  `workspace.default.regwatch-embed`, 1024-dim, profile
  `ep_2e7368b354d911ea3a013c3125e276c2`. It covers all 5,841 chunks -- the
  5,494 serving legacy chunks plus the 347 canary chunks (5,841 / 5,841,
  verified against the production database on 2026-08-14). The interactive app sends no model traffic to OpenAI; its
  provider remains a tested LLM rollback. Scheduled Watch still uses its scoped
  OpenAI key for public-document change summaries and extraction, never for
  embeddings. The legacy OpenAI embedding arm is no longer refreshed by Watch
  and needs a backfill before it can be treated as a current-corpus rollback. See
  [`docs/DATABRICKS_ADOPTION_2026-07-28.md`](docs/DATABRICKS_ADOPTION_2026-07-28.md).
- **Data residency (D1) is closed.** Generation, query and corpus embeddings, and
  the database are all inside the company tenant, so an analyst question does not
  leave for a third-party model API on the normal path. The original write-up is
  archived at
  [`docs/archive/DATA_RESIDENCY_D1.md`](docs/archive/DATA_RESIDENCY_D1.md).
- **The daily Watch pipeline runs in production.** A GitHub Actions cron
  ([`.github/workflows/watch-daily.yml`](.github/workflows/watch-daily.yml)) runs
  `regwatch watch` each day and is the only driver of the daily pipeline in prod.
  It failed from 2026-08-07 through the morning of 2026-08-10 because
  `WATCH_DATABASE_URL` pointed at the wrong database. That was fixed on
  2026-08-10 and the last observed pre-parity runs were green. The first run of
  this workflow revision will intentionally fail before crawl until its six
  profile secrets are provisioned, as described below.
- **The polyglot migration**
  ([`docs/POLYGLOT_TARGET_2026-07-10.md`](docs/POLYGLOT_TARGET_2026-07-10.md))
  is through step 5. The Go proxy owns the public port and serves auth, sessions,
  feedback, settings, and products natively. Since 2026-07-24 it also
  orchestrates `POST /query` end to end: it persists the audit row and calls
  Python's internal, token-gated RAG compute endpoint. Python keeps the stateless
  RAG core. The SQLite/Chroma dual mode was deleted in R5, so Postgres plus
  pgvector is the only datastore. Remaining: legacy-path deletion, hardening step
  R3, and steps 6 through 9.
- **Not yet externally exposed.** The work between here and an external launch is
  an SSO plus TLS gateway, least-privilege database credentials, and a rehearsed
  restore drill. Tracked in [`docs/PROD_READINESS.md`](docs/PROD_READINESS.md)
  and [`docs/ROADMAP.md`](docs/ROADMAP.md).

**Known open risks.**

The Watch workflow now runs in Qwen/profile mode, requires all six profile
settings (including `WATCH_QWEN_EMBEDDING_DIMENSION`), validates the registered
profile before crawling, and asserts 100% coverage after an attempted ingest.
The six repository secrets are still unprovisioned as of 2026-08-12, so this
change fails closed before crawling until the owner sets them and verifies one
manual run. See [`docs/SECRETS_RUNBOOK.md`](docs/SECRETS_RUNBOOK.md) section 3.4.

The 0.30 refusal threshold was validated against the old OpenAI vector space. The
space is now Qwen3 at 1024 dimensions, so that validation no longer carries over.
It has never been checked against the space production actually serves.

**Deliberate scope** (boundaries, not gaps): the reranker is a hook that is off by
default; the eval gold set is a curated starter set, expanded by hand from real
answer feedback; and the persist-and-cite source-freshness path is proven on
White Paper before being generalized to Ask and Assemble.

## Quick start

```bash
# install (dev tools + the OpenAI-compatible SDK used as Databricks transport)
uv sync --extra dev --extra llm

# configure. Providers are REQUIRED-EXPLICIT (no defaults): production runs
# generation and embeddings on Databricks Model Serving (LLM_PROVIDER=databricks,
# EMBEDDING_PROVIDER=qwen3); offline tests use the echo providers. The
# Databricks/Qwen vars are in .env.example under LLM_PROVIDER.
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

One browser-visible origin, two runtimes on Fly, one datastore, and a Databricks
model plane inside the company tenant:

```mermaid
flowchart LR
    B["Browser<br/>(analyst)"] -->|"HTTPS + HttpOnly cookie"| V["Vercel<br/>Next.js 16 shell"]
    V -->|"/api/* rewrite"| GO["Fly.io - Go proxy (public port)<br/>auth + sessions + rate limits<br/>native /query orchestration + audit"]
    GO -->|"6PN private network"| PY["Fly.io - Python FastAPI<br/>stateless RAG core:<br/>resolve, retrieve, cite, gate"]
    GO --> PG[("Databricks Lakebase<br/>Postgres + pgvector<br/>one DB: rows, vectors, audit")]
    PY --> PG
    PY -->|"OpenAI-compatible API"| DBX["Databricks Model Serving<br/>gpt-oss-120b - all LLM roles<br/>qwen3 embeddings - 1024-dim"]
    CRON["GitHub Actions cron<br/>daily watch + ingest"] --> PG
```

| Layer | Choice |
|---|---|
| Edge / control plane | **Go** proxy (`go/`, module `github.com/Hussain0327/amneal/go`) holds the public port. Since the step-4 polyglot cutover it serves auth, sessions, feedback, settings, and product CRUD natively (sqlc over the same Postgres) and applies rate limiting plus `Fly-Client-IP` handling. Since the step-5 cutover it also orchestrates `POST /query` natively (persists the audit row, calls Python's internal RAG compute endpoint) and relays the remaining endpoints to Python. Migration plan: [`docs/POLYGLOT_TARGET_2026-07-10.md`](docs/POLYGLOT_TARGET_2026-07-10.md) |
| Backend (RAG core) | Python 3.11+ (managed by `uv`), FastAPI. The stateless retrieval, synthesis, and gating core behind the proxy: Ask, Assemble, White Paper, Watch, Deficiency, and query orchestration |
| Frontend | Next.js 16 (App Router, TypeScript) plus React 18 in `regwatch/frontend/`. The five scoped surfaces render in one `(shell)` route group with one sidebar and one product-scope bar; the Compliance Studio (`/studio`) sits outside it and is fixture-backed. Talks to the API through a same-origin `/api` proxy |
| LLM | **Databricks-hosted `gpt-oss-120b`** in prod (`LLM_PROVIDER=databricks`): served id `gpt-oss-120b-080525` on the Model Serving alias `workspace.default.regwatch`. One open-weight model serves ALL roles (router, synthesizer, extractor), which keeps analyst questions inside the company tenant (D1). A runtime served-model guard (`D1_ENFORCED` plus `D1_ALLOWED_LLM_MODELS`) rejects any response served by a model outside the allowlist once armed, and rejects partner-hosted families (`databricks-gpt*`, `databricks-claude*`, `databricks-gemini*`) even if someone allowlists them by hand. `LLM_PROVIDER` has NO default (an unset value refuses; 2026-08-14 postmortem): `databricks` is the only production value and `echo` is test-only. The OpenAI-API and Anthropic provider paths were removed 2026-08-17 — the OpenAI SDK remains solely as the Databricks wire transport, and LLM rollback means repointing `DATABRICKS_LLM_MODEL` at another verified endpoint |
| Embeddings | Profile-versioned and required-explicit. Prod today: **Databricks-hosted Qwen3**, 1024-dim, endpoint `workspace.default.regwatch-embed`, profile `ep_2e7368b354d911ea3a013c3125e276c2`, with the whole corpus embedded on it. Retrieval picks its arm from `ACTIVE_EMBEDDING_PROFILE`; `EMBEDDING_PROVIDER` has NO default (a process without it refuses to boot; 2026-08-14 postmortem) and `qwen3` is the only production value (`echo` is test-only). Profile vectors live in the profile-keyed `chunk_embedding` table, written blue/green into a named profile and never in place. The older `legacy` arm is the `vector(1536)` column on `chunk`; its OpenAI embedder was removed 2026-08-17, so it is a frozen historical space, not a rollback path — rollback is a previously promoted profile |
| Vector store | **pgvector** in the same Postgres, everywhere (Lakebase in prod, a disposable local/CI Postgres otherwise). No other vector backend since R5 |
| Structured store | **Postgres** via SQLModel (Lakebase in prod). `DATABASE_URL` is mandatory and the app refuses to boot without it. Schema changes ship as Alembic migrations |
| Retrieval | Two-stage. Stage 1: vector top-k 50 (`VECTOR_TOP_K`). Stage 2: rerank to top-k 8 (`RERANK_TOP_K`); reranker off by default |
| Ingest | Exact five-family FDA source policy; deterministic manifest; bounded and host-paced `httpx`; `selectolax`; `pdfplumber` with `pypdf` fallback; immutable source versions; per-document atomic chunk publication; deferred, resumable embedding batches |
| Deploy | One Fly.io app, **two process groups**: the Go proxy on the public port, the Python app (dual-stack `regwatch serve`) on an internal port behind it. DB and vectors on Lakebase, frontend on Vercel, LLM and embeddings on Databricks Model Serving, daily Watch via GitHub Actions cron. Alembic (run by the Fly release command) stays the single schema authority |
| Tooling | Python: ruff, black, mypy (strict on `src/`), pytest, import-linter layering contracts. Go: gofmt, go vet, golangci-lint, sqlc (generated store plus `sqlc vet` against a real schema). A cross-service contract suite (`tests_contract/`) boots the real Go proxy, uvicorn, and Postgres to prove the wire contract across the boundary |

The LLM provider, model, reranker, and embedding provider all sit behind
interfaces. Nothing is hard-coded in business logic.

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
  blocking floors are `recall_at_k >= 0.80` and `citation_precision >= 0.74`,
  ratcheted to the first real measurement on the Qwen3 arm (2026-08-05). They
  mean "no worse than the day we first measured", not "good enough".
  `refusal_accuracy` is still measured and printed but stopped blocking on
  2026-08-06, because Ask is deliberately moving away from refusing. The older
  `0.90 / 0.95 / 0.95` numbers are recorded as aspirational targets, never as
  gates. A run that loses more than 10 percent of its turns to transport
  failures exits without scoring, so an outage cannot pass on a shrunken
  denominator. It also exits nonzero on an empty store. In CI this runs as its
  own workflow,
  [`.github/workflows/databricks-eval.yml`](.github/workflows/databricks-eval.yml),
  which prefers the Databricks arm so it measures the geometry production
  actually serves.
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
  boundary, 140,339-record snapshot, counts, checksums, runbook, activation, and
  rollback.
- [Non-technical guide](docs/NON_TECH_GUIDE.md): plain English for business and
  regulatory readers.
- [Production readiness](docs/PROD_READINESS.md): the POC-to-production path.
- [Databricks adoption](docs/DATABRICKS_ADOPTION_2026-07-28.md): the inference-
  plane decision, cost model, and rollout state. Note that the "Supabase stays"
  call in that doc was later reversed: production Postgres is Lakebase now.
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
providers (smoke use only); for real embeddings/generation set
`EMBEDDING_PROVIDER=qwen3` and `LLM_PROVIDER=databricks` with their endpoint
values in `.env`. Production embeds and generates on Databricks.
See [`docs/DEPLOY.md`](docs/DEPLOY.md).

## License

MIT.
