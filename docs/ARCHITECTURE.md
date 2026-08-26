# REGWATCH - System design and architecture

> An FDA regulatory-intelligence system for Amneal's generic-drug Clinical
> Regulatory Affairs (CRA) team. It surfaces, organizes, compares and cites
> public FDA data. By design it never authors submission content and never
> renders regulatory judgment.

This is the canonical description of how REGWATCH is built. It owns the stable
design: boundaries, pipeline stages, the citation gate, the data model, and the
INV-1 through INV-9 invariants.

It deliberately does not own values that move on a sub-monthly cadence. When you
need the live embedding profile id, the effective refusal floor, a flag state,
the deployed Alembic stamp, a chunk count or a release number, read the owner
instead:

- [`PRODUCTION_TRUTH.md`](PRODUCTION_TRUTH.md): what serves a request today.
- [`CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md): every environment variable,
  flag and secret.
- [`ROADMAP.md`](ROADMAP.md): open items and production gates.
- [`BUILT_BUT_DORMANT.md`](BUILT_BUT_DORMANT.md): code that exists but does not
  run.
- [`DECISIONS.md`](DECISIONS.md): what was decided, when and why.

Other companions: [`DEPLOY.md`](DEPLOY.md) (the production runbook),
[`whitepaper_schema.md`](whitepaper_schema.md) (the 46-cell White Paper schema),
[`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md) (the corpus
manifest and its activation gates), and
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md) (a proposal, not
shipped).

---

## Table of contents

1. [Orientation](#1-orientation)
2. [The prime directive](#2-the-prime-directive)
3. [Deployment topology](#3-deployment-topology)
4. [Product surfaces](#4-product-surfaces)
5. [The API boundary](#5-the-api-boundary)
6. [The grounded Q&A engine](#6-the-grounded-qa-engine)
7. [Source layer](#7-source-layer)
8. [Ingest and processing pipeline](#8-ingest-and-processing-pipeline)
9. [Storage and retrieval](#9-storage-and-retrieval)
10. [Data model](#10-data-model)
11. [White Paper populator](#11-white-paper-populator)
12. [Dossier assembler](#12-dossier-assembler)
13. [Change detection and alerts](#13-change-detection-and-alerts)
14. [Authentication and sessions](#14-authentication-and-sessions)
15. [Compliance invariants](#15-compliance-invariants)
16. [Observability and ops](#16-observability-and-ops)
17. [Evaluation harness](#17-evaluation-harness)
18. [Configuration](#18-configuration)
19. [Design principles](#19-design-principles)

---

## 1. Orientation

REGWATCH pulls in public FDA material, stores it as searchable and citable
evidence, and answers questions from that evidence. Anything it states about FDA
guidance carries a citation. When the evidence does not cover the question, it
says so in plain words instead of guessing.

Stack: Python 3.11+ with FastAPI, SQLModel and SQLAlchemy; a Go edge service in
`go/`; a Next.js App Router UI in `regwatch/frontend/`; Postgres with pgvector as
the only datastore; Alembic migrations; pytest, ruff, black and mypy.

### 1.1 The two flows

Everything else is detail on top of these two paths. Ingest, offline:

```text
FDA source index or manifest
  -> validate the URL and every redirect against the source policy
  -> download the document
  -> parse page text (PDF, HTML or inline)
  -> chunk with page metadata
  -> embed the chunks into a named embedding profile
  -> store chunks and vectors in Postgres, plus document/version rows
```

Q&A, per turn:

```text
user question
  -> create or load the chat session
  -> inherit follow-up context when it is safe to
  -> resolve the product BEFORE any semantic search
  -> retrieve chunks scoped to that product and its current versions
  -> withhold every passage below the refusal floor
  -> call the LLM with only the surviving passages
  -> gate the reply sentence by sentence and validate every citation
  -> write exactly one audit row
  -> return an answer, a clarify prompt, or a plain-words decline
```

### 1.2 Repository map

```text
config/settings.py         one Pydantic Settings object, read by every module

go/                        Go edge: auth, sessions, feedback, settings,
                           products, rate limits, and POST /query
                           orchestration when GO_NATIVE_QUERY is on

migrations/versions/       Alembic migrations; run `alembic heads` for the head

src/regwatch/
  sources/                 rules-first source router + FDA source handlers
  corpus/                  manifest, atomic sync, coverage accounting
  ingest/                  crawl FDA documents, parse PDFs
  process/                 chunk, embed, extract BE fields, detect changes
  store/                   Postgres models/session + pgvector vector store
  retrieve/                product resolution, scoping, vector retrieval
  generate/                prompts, LLM providers, the answer gate, grounded Q&A
  watch/                   watchlist, alias discovery, matching, alerts
  assemble/                product dossier builder
  whitepaper/              cited White Paper populator + .docx export
  deficiency/              deficiency analysis runs
  auth/                    cookie-session verification
  eval/                    gold set, metrics, the regression gate
  common/                  citations, audit, logging, conversation, rate limits
  api/                     the FastAPI surface, the one contract boundary
  cli.py                   operator commands (seed, ingest-all, create-user, ...)

regwatch/frontend/         Next.js UI: five surfaces in one (shell) route group,
                           plus /studio outside it
tests/                     unit, integration, invariant and eval-gate tests
tests_contract/            cross-service suite over real Go + uvicorn + Postgres
docs/                      this set
```

Each backend package is one stage of that pipeline with a clean boundary, in the
order listed. `config/` is a sibling top-level package, imported as
`config.settings`.

If you read only five files, read `store/models.py`, `config/settings.py`,
`generate/grounded_qa.py`, `generate/turn_gate.py` and `retrieve/resolver.py`.
`store/models.py` explains most of the system on its own.

---

## 2. The prime directive

One rule drives most of the design:

> **Cite the facts, talk like a person.**

Anything the system states as FDA guidance has to come from a passage retrieved
on that same turn, and it carries that passage's number. Everything else, our
own reading, an offer, a question back to the analyst, is plain prose with no
number on it. The model never gets to assert an FDA fact without evidence.

The rule is enforced in three independent places:

- **Retrieval.** A score threshold keeps weak passages out of synthesis.
- **The gate.** Every sentence that claims a fact is checked against the exact
  passages sent to the model, and an uncited fact sentence is dropped.
- **Tests.** The INV-1 through INV-9 invariants (section 15).

The old headline, "cite or refuse", described versions 5 and 6 of the answer
policy. Version 7 replaced it. Enforcement did not get weaker, an uncited source
fact is still dropped; the reply around the facts is now allowed to read like a
colleague talking instead of a form letter. Section 6 has the detail.

Responses are not an answer/refuse binary either. The system distinguishes
grounded answers, summaries, clarifications, scope warnings, evidence gaps and
operational errors. When it cannot safely answer a healthy turn, a constrained
planner picks among next steps the application already approved, instead of
dead-ending or guessing.

---

## 3. Deployment topology

The browser reaches the Next.js frontend, which proxies API requests to the Go
edge service. The Go service and the FastAPI application share one
RegWatch-owned PostgreSQL database with pgvector.

```text
Browser
   |
   v
Vercel / Next.js
   |
   v
Fly.io / Go proxy
   |
   v
Fly.io / FastAPI ---------> OpenAI Responses API   (generation)
   |
   +-----------------------> OpenAI Embeddings API  (vectors)
   |
   v
RegWatch PostgreSQL + pgvector
rows, vectors, sessions, audit; exact vector search
```

The Go and Python services use the same `DATABASE_URL`. Local and CI runs use an
equivalent disposable PostgreSQL database through `TEST_DATABASE_URL`.

### Model and state contracts

| Concern | Contract |
|---|---|
| LLM provider | OpenAI Responses API (`client.responses.create`), not Chat Completions |
| LLM model | one model serves every role: router, synthesizer, extractor |
| Reasoning effort | one global setting, not per-role |
| OpenAI response storage | disabled with `store=false` |
| Conversation state | transcript managed and replayed by RegWatch |
| Embedding provider | OpenAI Embeddings API |
| Embedding width | truncated to the profile width with the `dimensions` parameter |
| Retrieval | exact pgvector search (section 9.1) |
| Application state | RegWatch PostgreSQL, never OpenAI |

The model ids, the embedding model and its width are live values.
[`PRODUCTION_TRUTH.md`](PRODUCTION_TRUTH.md) owns them; `GET /settings` and
`regwatch status` report what a running process resolved. At the time of writing
that is `gpt-5.6-terra` for generation and `text-embedding-3-large` at 1024
dimensions for vectors, both set in `fly.toml` and `config/settings.py`.

There is no Databricks rollback path in code. `get_llm_provider` raises
`ValueError` when the provider name is `databricks` (`generate/llm.py`), and only
two embedding provider classes exist, `EchoEmbeddingProvider` and
`OpenAIEmbeddingProvider` (`process/embedder.py`). Any runbook that tells you to
repoint the provider at Databricks is wrong and would take generation down.

### Data residency, plainly

Generation and embeddings go to OpenAI, an external vendor, on every normal
question. Only the database stays inside the company tenant. There is no runtime
residency guard: no residency environment variable and no model allowlist exists
anywhere in `src/`, `config/` or `go/`, and the `D1ResidencyError` type defined
in `generate/llm.py` is never raised by any provider. Treat any claim that a
residency guard ships and is tested as false. See
[`BUILT_BUT_DORMANT.md`](BUILT_BUT_DORMANT.md).

---

## 4. Product surfaces

The frontend (`regwatch/frontend/`) is a deliberately thin Next.js 16 App Router
app. All intelligence and all secrets live server-side. `lib/api.ts` is the fetch
wrapper; `lib/turns.ts` models a conversation turn. It is the only UI.

Five product surfaces (Ask, Assemble, Watch, White Paper, Deficiency) render
inside a single `app/(shell)/` route group: one sidebar, one canvas, one set of
design tokens, one scoped-product context. The bare routes (`login`, `fixtures`,
`studio`) sit outside the group and never inherit the shell.

| Route (`app/`) | Backend endpoint(s) | Purpose |
|---|---|---|
| `(shell)/page.tsx` | `POST /query`, `POST /query/stream` | Cited conversational chat plus a per-user history sidebar |
| `(shell)/assemble/page.tsx` | `POST /assemble` | Cited dossier for a target product |
| `(shell)/whitepaper/page.tsx` | `POST /whitepaper`, `POST /whitepaper/runs/{run_id}/docx`, `/whitepaper/runs*` | CRA White Paper populator, exported as a filled `.docx` |
| `(shell)/watch/page.tsx` | `GET /watch/latest` | Recent change-detection alerts |
| `(shell)/deficiency/page.tsx` | `POST /deficiency/analyze` (202 plus background), `GET /deficiency/runs`, `GET /deficiency/runs/{id}` | Predicted submission deficiencies with evidence |
| `studio/page.tsx` | `GET /psg/documents*`, `POST /query`; the rest is fixtures | Compliance Studio, for reviewing our own CMC documents |
| `login/page.tsx` | `POST /auth/login` | Cookie-session login gate |
| `fixtures/page.tsx` | static | Demo inputs for testing |

Ask is a cited conversational chat: citation chips open an in-app evidence
drawer, with a Sources list as the no-JS fallback. Gold is reserved for
grounding. On wide viewports the avatar column becomes a margin rail carrying the
turn's provenance: time filed, audit number, and a confidence dot that only
validated answers ever wear.

### 4.1 Compliance Studio

Every other surface reads public FDA material. `/studio` reads our own drafts,
which is why it sits outside the shell: it takes the whole viewport and has no
scoped-product context.

Studio is a three-way split, not a fixture mock:

- **Real and wired.** The PSG reference rail and the chat assistant call real
  endpoints. `app/studio/page.tsx` imports `fetchPsgLibrary`, `fetchPsgContent`,
  `fetchPsgRequirements` and `askQuery` from `@/lib/api`.
- **Real but unwired.** `POST /studio/check` and `GET /studio/check/{run_id}`
  exist in `api/main.py`, run the deficiency engine over submitted blocks, and
  persist their runs. The frontend does not call them yet; `page.tsx` says so in
  a comment.
- **Fixtures.** The working-document set, the repository tree and the canned
  check results come from `lib/studio-fixtures.ts` and do not survive a refresh.
  Those fixture shapes are the contract the wiring has to meet.

The idea worth carrying into the backend is that **a finding is a span, not a
report line**. Findings anchor to `(blockId, start, end)` plus the `excerpt`
those offsets resolved to, so they can highlight in place and be invalidated when
the analyst edits underneath. `Finding.start/end` is the immutable as-checked
anchor; `Mark.start/end` is the current render position, remapped on every edit.
Full design record: [`COMPLIANCE_STUDIO.md`](COMPLIANCE_STUDIO.md).

> **Direction, not built.** The intent is to collapse to two surfaces: Ask as
> the conversational one, and Studio as the document workspace that absorbs
> Assemble, Watch, White Paper and Deficiency. Studio still has no persistence
> for its own documents and no product scoping, so it cannot receive a working
> surface until those exist. Prerequisites are in [`ROADMAP.md`](ROADMAP.md).

### URL-scoped CurrentProduct and the "Under review" bar

One reference product is scoped across all five shell surfaces and mirrored into
the URL query (`?rp=<name>&appl=<application number>`), so the scope is shareable
and survives a reload. `components/CurrentProductProvider.tsx` reads that URL as
the state of record; re-scoping rewrites only `rp` and `appl`, never the Ask
page's `session`, so it cannot drop an open conversation.

`components/ProductScopeBar.tsx` is the sticky "Under review" strip and the
front-door setter for the whole pipeline. Pinning runs the same deterministic
resolve the White Paper uses, `POST /resolve` (section 5), so the scope is always
the canonical `{normalized_name, six-digit application number}`. A 422 leaves the
scope unset and shows the resolver's explanation verbatim: refuse over guess.

---

## 5. The API boundary

`api/main.py` is the contract boundary, the surface the IT/AI team will wrap or
replace. Every response is reproducible in Postman from a `.env` and a running
instance.

### One authorization chokepoint

Since the step-4 cutover
([`POLYGLOT_TARGET_2026-07-10.md`](POLYGLOT_TARGET_2026-07-10.md)) the Go proxy
serves `/auth/*`, `/sessions*`, `/feedback`, `/settings` and `/products*`
natively at the public edge (`go/internal/api`) and mints the session cookie.
Python verifies it.

`POST /query` is conditional. When `GO_NATIVE_QUERY` is on, Go orchestrates the
turn: it writes and finalizes the `query_log` audit row and calls Python's
internal, token-gated `POST /internal/query/compute` for the RAG work
([`GO_NATIVE_QUERY_ROLLOUT.md`](GO_NATIVE_QUERY_ROLLOUT.md)). When it is off, Go
relays `/query` and Python runs the whole turn. **The Go default is `false`**
(`go/internal/api/config.go`), so a fresh local checkout relays to Python;
`fly.toml` pins it on. `POST /query/stream` is Python only in every
configuration; Go fronts it with `StreamGate`, which only rate-limits.

`/internal/query/compute` is fail-closed: it 404s when `INTERNAL_RAG_TOKEN` is
unset, and the proxy never exposes the `/internal/` subtree.

On the Python side four routes are declared directly on `app` and are therefore
outside the auth wall: `GET /health`, `GET /ready`, `GET /livez` and
`GET /metrics`. `/metrics` is open by default and returns 401 without a bearer
token once `METRICS_TOKEN` is set; `/health` and `/ready` are never gated that
way. Everything else is registered on a single router with a router-level
dependency:

```python
protected = APIRouter(dependencies=[Depends(require_user)])
```

That makes an accidentally unauthenticated route structurally impossible: you
cannot add an endpoint to `protected` without inheriting `require_user`.
FastAPI's interactive docs (`/docs`, `/redoc`, `/openapi.json`) are disabled, so
the route list, the schemas and the cookie name are not disclosed to anonymous
visitors through the proxy.

### Endpoint map

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/login` | open | Issue the `regwatch_session` cookie (Go) |
| POST | `/auth/logout` | open | Revoke the server-side session and clear the cookie (Go) |
| GET | `/auth/me` | yes | Current user (Go) |
| POST | `/query` | yes | Grounded Q&A; orchestrated by Go when `GO_NATIVE_QUERY` is on, otherwise run in Python. The RAG compute always stays in Python |
| POST | `/query/stream` | yes | The same turn over SSE: progress frames, provisional draft tokens, one terminal validated result. Python only |
| POST | `/feedback` | yes | Thumbs up or down on one of the caller's own answers (Go) |
| POST | `/sources/search` | yes | Structured FDA source lookup |
| POST | `/resolve` | yes | Deterministic entity resolution (RLD plus application number) to a canonical spine; pins the scope bar without a populate |
| POST | `/assemble` | yes | Build a cited dossier |
| POST | `/whitepaper` | yes | Populate the CRA White Paper |
| POST | `/whitepaper/runs/{id}/docx` | yes | Render a saved White Paper run as `.docx` |
| GET / POST / DELETE | `/whitepaper/runs*` | yes | Saved White Paper runs: list, detail, per-cell edit, finalize, reopen, render, delete |
| GET | `/watch/latest` | yes | Recent alerts, optional `since` filter |
| GET | `/psg/documents`, `/psg/documents/{id}/pdf` | yes | PSG reference library listing and inline PDF |
| GET | `/psg/documents/{id}/content`, `/psg/documents/{id}/docx`, `/psg/documents/{id}/requirements` | yes | One PSG as studio blocks, as a generated Word download, and as extracted requirements |
| GET | `/chemistry/structures` | yes | Ingredient chemistry plate for the resolved product |
| POST | `/deficiency/analyze` | yes | Start a deficiency analysis (202 plus background work) |
| GET | `/deficiency/runs`, `/deficiency/runs/{id}` | yes | Deficiency run list and detail |
| POST | `/studio/check` | yes | Run the deficiency engine over submitted Studio blocks (202 plus background work); not called by the UI yet |
| GET | `/studio/check/{run_id}` | yes | One Studio check run |
| GET / POST | `/products` | yes | List or add watchlist products (Go) |
| DELETE | `/products/{id}` | yes | Soft-unwatch a product; the row is kept (INV-4) (Go) |
| GET | `/sessions` | yes | The caller's chat sessions (Go) |
| GET / DELETE | `/sessions/{id}` | yes | One session with messages, or delete it (Go) |
| GET | `/settings` | yes | Non-secret config, including the effective refusal floor (Go) |
| GET | `/health`, `/ready`, `/livez`, `/metrics` | open | Diagnostics, readiness, liveness and rollup counters |

### `POST /resolve` is deliberately not an LLM turn

`/resolve` reuses the White Paper's spine resolver (`resolve_spine`) to map an
RLD name plus application number to the canonical
`{normalized_name, application_number}` spine the scope bar pins. It is
deterministic entity resolution, not a synthesis turn, so it writes no
`query_log` row (nothing was answered), returns no answer text, and 422s with the
resolver's own detail when the pair does not resolve. It is rate-limited like
`/query` because it hits live FDA sources the same way.

### Boot sequence (`_lifespan`)

1. **Sentry first**, before `init_db`, so a refused boot is still captured.
2. `init_db()` unless `REGWATCH_DB_INITIALIZED=1`, which a Docker entrypoint sets
   after running `regwatch init-db` out of process. Even then the dimension
   fail-fast (`assert_embedding_provider_dim`) is re-asserted in process.
3. `_guard_test_providers` refuses to boot an `echo` provider against a non-empty
   corpus: retrieval would silently degrade while citations still validate. An
   empty corpus is allowed, for a fresh checkout or a pre-seed Docker boot.
4. `assert_embedding_runtime_available` and `assert_llm_runtime_available` refuse
   a provider whose dependencies or credentials are missing, rather than 500-ing
   on the first embed or the first question.

`DATABASE_URL`, `INGEST_EMBEDDING_PROVIDER` and `LLM_PROVIDER` all refuse to boot
when unset. There is no implicit default for any of them, by decision after the
2026-08-14 backfill outage.

### `/health` semantics

`/health` returns a superset of `{"status": "ok"}`, diagnosing the database, the
vector store, LLM key presence and the embedding arm. The embedding block reports
the active profile id as well as the provider name, because profiles can share a
provider and only the id pins which vector geometry is live. It returns 503 only
when the database or the vector store is unreachable. An empty corpus is healthy
with a warning, so a fresh stack can boot and then be seeded.

---

## 6. The grounded Q&A engine

This is the heart of the system: `generate/grounded_qa.py`, function `ask()`. It
is not "embed, call the LLM, return". It is a deterministic pipeline where
application code owns every safety decision and every branch writes exactly one
audit row. Each healthy Ask turn gets exactly one AI role: either grounded
synthesis or constrained guidance, never both.

### Flow

```text
question
   |
   +- DETERMINISTIC ROUTE AND POLICY
   |     * scope, capability or vague input -> fixed status and reason
   |     * resolve the product BEFORE any semantic search
   |     * ambiguous or unknown product -> app-built options, or an evidence gap
   |     * enforce the product and multi-form guards
   |
   +- pre-synthesis non-answer? ---------------> GUIDANCE PLANNER (router role)
   |     * gets the question plus trusted route/product context and the options
   |     * picks one allowlisted next step and up to three existing option ids
   |     * the app validates the pick and renders its own trusted copy
   |
   +- Stage 1: vector top-k (VECTOR_TOP_K, default 50), scoped to product + form
   +- Stage 2: trim to RERANK_TOP_K (default 8); cross-encoder and MMR both off
   |
   +- top-1 score below the effective refusal floor?
   |     +- withhold every weak passage --------> GUIDANCE PLANNER
   |
   +- POST-RETRIEVAL GUARDS (defense in depth)
   |     * passages span more than one product or form -> GUIDANCE PLANNER
   |
   +- SYNTHESIZER (temperature 0.0; SYNTHESIZER_MAX_TOKENS covers a reasoning
   |               model's thinking AND its answer; one truncation retry doubles
   |               the budget to a hard ceiling)
   |
   +- parse the prose into sentences, tagged source fact / reasoning /
   |  conversation (generate/prose_turn.py)
   +- gate every sentence against the passages sent this turn (turn_gate.py)
   +- render only what the gate admitted, with markers the renderer writes --> ANSWER
```

Operational failures (catalog, database, pipeline, provider) never enter the
guidance path. They return the audited error outcome with no extra AI call.

The refusal floor is resolved by `Settings.effective_refusal_threshold()`: the
per-profile entry in `REFUSAL_SCORE_THRESHOLD_BY_PROFILE` wins, and a profile
with no calibrated entry falls back to the global `REFUSAL_SCORE_THRESHOLD`,
whose code default is 0.30. The live per-profile value is a Fly secret and is not
in this repository. Read it from `GET /settings` or `regwatch status`, never from
a document.

### The v7 answer policy

Three flags select the served answer policy. All three default to `false` in
`config/settings.py`, so a local run gets the older behavior unless you set
them. Two of them, `REGWATCH_PROSE_SYNTHESIS` and `REGWATCH_SELECTIVE_CITATION`,
are pinned `"true"` in `fly.toml`. `REGWATCH_LIVE_DRAFT` is not pinned there:
read its production state from `GET /settings` or the Fly secret list.
[`CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md) owns their current state.

| Flag | What it turns on |
|---|---|
| `REGWATCH_PROSE_SYNTHESIS` | The model writes prose instead of the old v5 claims JSON |
| `REGWATCH_LIVE_DRAFT` | The provisional draft streams token by token over SSE |
| `REGWATCH_SELECTIVE_CITATION` | The v7 policy below; only honored when the prose flag is also on |

The prompt is `GROUNDED_QA_SYSTEM_V7` in `generate/prompts.py`. It asks the model
to decide, sentence by sentence, which of three kinds it is writing.

1. **Source fact.** States what FDA guidance says, requires, recommends, permits
   or prohibits. It must end with the number(s) of the passage(s) it came from,
   placed right before the final period, like `[1]` or `[1, 3]`. An uncited
   source fact is dropped by the gate. That is INV-1, and it lives in code, not
   in the prompt.
2. **Reasoning.** Our own reading, going past what the passages say. It carries
   no number and must open with one of four exact phrases: "The guidance does not
   state this directly; my reading is ...", "Reading the guidance together, ...",
   "My reading is ...", "Beyond the guidance, ...". Those four are pinned byte
   for byte in `turn_gate.REASONING_FRAME_PREFIXES`, and a test holds the prompt
   and the code equal. An obligation or a prohibition may never hide inside a
   reasoning sentence: a source-assertion lexicon reclassifies it back to source
   fact, where it needs a citation.
3. **Conversation.** Greetings, offers, transitions, a question back to the user.
   Plain text, no numbers, no FDA facts.

**There is no sentinel and no code word for "not found".** v6 had one and it
broke 11 of 11 refusals in the battery. In v7, when the passages do not answer
the question, the model says so in ordinary words, names what it does have
nearby, and offers a next step. The whole reply is plain prose with no passage
numbers. Before that text is served, `turn_gate.render_decline` re-scans every
sentence against the materiality and source-assertion lexicons; if the guard
fires, the canned refusal copy is served instead of the model's text.

### The gate

`generate/turn_gate.py` is the reliability boundary: it is the only place
model-authored bytes become user-visible text, and it admits them one claim at a
time. `generate/prose_turn.py` parses the model's prose first, deterministically
and with no provider, database or settings access. Every ambiguity resolves in
the safe direction: a bracket that is not unambiguously a sentence-trailing
citation is never read as one, and the sentence carrying it is dropped rather
than rendered with a marker of uncertain meaning.

Model-authored markers are stripped; the renderer writes the canonical ones from
validated passages. A claim whose declared citations do not all resolve to a
passage sent this turn is dropped whole, never partially rewritten.

Verdicts:

| Verdict | What happened | What the analyst gets |
|---|---|---|
| `answer` | Every admitted claim validated | The answer |
| `partial` | Something was dropped, but nothing material | The answer plus a plain note that something was left out |
| `material_drop` | A dropped sentence carried obligation, permission or exception wording | The whole answer is rejected: the survivors could read as their own opposite |
| `no_valid_citations` | Nothing was admitted, or every source fact failed validation | A refusal |
| `no_evidence` | The model declined outright (the v5/v6 shape) | A refusal, or a clarify when the analyst named a real drug |
| `conversational_decline` | v7 found-nothing: everything admitted is uncited reasoning or conversation, and nothing was dropped | The model's own plain-words decline |

The gate's input and its decision are both persisted, as a per-claim ledger under
`query_log.route_json`, so the drop rate is measurable from real traffic instead
of inferred from a counter.

### Why entity resolution comes before retrieval

FDA PSG documents share a lot of template boilerplate across drugs. If pure
vector search picks passages first, a generic boilerplate paragraph from the
wrong drug can score well and be cited as if it answered the question, and the
blend is invisible because citation labels are application-number-only
(`PSG_020503`). So the product is pinned first. Retrieval then becomes a
B-tree-filtered exact match on `normalized_name` plus distance ranking, not
open-field search.

### Current-version scoping

`retrieve()` (`retrieve/retriever.py`) does more than embed and search. Before
querying the vector store it computes the set of current `psg_version` ids for
the filtered documents (`_current_version_ids_for_filters`) and constrains the
search to them, so a superseded chunk can never be cited even if it is still in
the index. A pure vector-only mode, used by unit tests that seed pgvector chunks
without a matching catalog row, is detected and skipped.

### Graph-assisted retrieval (foundation only)

Migration `0018_knowledge_graph` and `store/graph_store.py` derive a
deterministic Tier-1 hierarchy: `application` to `psg_doc`, `psg_doc` to
`psg_section`, ordered sections, and graph nodes back to source chunks.
Ingest-time population was retired; `regwatch graph-backfill` is the only
population path left. The tables are write-only, nothing reads them at runtime,
and chunks remain the only citable unit, so the Ask path above is unchanged. The
proposed consumer and its rollout gates are in
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md).

### Conversation memory

`common/conversation.py` persists per-session filters
(`ChatSession.active_filters_json`): the resolved product and any chosen
`(dosage_form, route)`. A follow-up like "What about dissolution?" inherits that
context (`_looks_like_follow_up` plus `get_session_filters`), so it does not
re-trigger product or form clarification. Conversation memory is never treated
as FDA evidence, only as deterministic routing context.

### The five response states

| `status` | Meaning | Citations |
|---|---|---|
| `answer` | Grounded answer | validated |
| `summary` | Grounded answer to a summarize or overview request | validated |
| `clarify` | Product or form is known or close, but intent is unclear, so the app offers options, optionally prioritized by the guidance planner | none |
| `scope_warning` | The analyst asked for strategy or judgment, so the app enforces the boundary and the planner picks a safe next action | none |
| `refused` | Cannot ground it (no product, low score, no valid citations), so explain the evidence gap | none |

Every state runs through `_finish_turn`, which records the assistant message and,
on answerable states, updates the session's product filter.

### Streaming boundary

The Ask client targets `/query/stream` (SSE) first. The backend emits pipeline
progress frames and then one terminal `result` frame carrying the same validated
`QueryResponse` that blocking `POST /query` returns. If the stream closes before
that frame, the client falls back to `POST /query` once. Between progress frames
it also emits provisional `token` deltas, a live draft the UI renders as it
arrives. Those deltas are cosmetic by design: INV-1 forbids treating answer text
as authoritative before the gate has run, so the terminal frame is the only
answer of record. Both paths share one serializer, so the wire shape cannot
drift, and each turn still writes exactly one audit row.

### Route and scope call

`REGWATCH_ROUTE_CALL` has three modes and is pinned `off` in `fly.toml`, which
keeps a blocking pre-retrieval model call off the critical Ask path.

- `off`: no call at all.
- `shadow`: one bounded `router` call proposes a `standalone_question`, a `mode`
  and a `scope_hint`. Application code compiles the hint against the
  deterministic resolver, the session state and an allowlisted corpus catalog,
  then writes the result only to `route_json["route_call"]`. Everything the
  analyst sees still comes from the existing path, and the model cannot emit
  filters, document ids, version ids or an executable mode.
- `live`: implemented, not enabled.
  `grounded_qa._compile_route_live_scope` turns the route decision into an
  executable scope, but only a `PRODUCT` scope and only from the no-product
  branch, after the did-you-mean and brand-lookup guards have come back empty.
  `CORPUS`, `CLARIFY` and `CONVERSE` scopes stay audit-only.

Failures fail open to the existing turn.

---

## 7. Source layer

`sources/policy.py` is the acquisition boundary. It admits exactly five source
families and validates the initial URL plus every redirect against reviewed,
family-specific FDA hosts and paths. The allowlist is code-reviewed, not an
environment variable. Retired API, label-database, NDC, shortage and REMS
acquisition paths fail closed and are not registered in the router.

`sources/router.py` is deterministic and rules-first (regex and keywords, no
LLM). It fans a `SourceQuery` out only to the approved handlers behind the common
`SourceHandler` interface.

```text
SourceQuery -> route_sources()  (appl_no? label? review? PSG/BE? Orange Book?)
                   |   default: [DRUGSFDA, ORANGE_BOOK, PSG, FDA_BE_GUIDANCE]
                   v
            for each routed source: handler.search()   <- failures logged per source, not fatal
                   |
                   v
            (routed_sources, list[SourceRecord])
```

| Handler | FDA source |
|---|---|
| `DrugsFdaHandler` | Official 12-table Drugs@FDA ZIP: applications, products, submissions, actions, and document links |
| `ActionPackageHandler` | SBOA/action-package review links classified from the same Drugs@FDA snapshot |
| `PsgHandler` | Product-Specific Guidance from the indexed PSG catalog |
| `FdaBeGuidanceHandler` | Five reviewed general FDA BE guidance documents |
| `OrangeBookHandler` | Official Orange Book ZIP: RLD, RS, TE codes, patents, and exclusivities |

A new source requires a reviewed policy-family addition, a family-specific URL
rule, a handler/discovery adapter, provenance and document types, and policy
tests. One flaky approved endpoint does not take down a structured search:
`search_sources` logs the family failure and returns the other approved results.
The corpus sync is stricter: any failed document makes the run failed and blocks
activation.

---

## 8. Ingest and processing pipeline

The offline path is split into exact-manifest discovery, document-at-a-time
acquisition and chunking, and profile-scoped embeddings. Dagster orchestrates
canaries, deterministic shard backfills, retries and blocking acceptance checks;
database lifecycle rows remain the source of truth. Deployment migrations never
fetch or backfill source data.

```text
official FDA snapshots -> frozen manifest + fingerprints -> policy validation
  -> bounded streamed artifact -> SHA-256 + object upload -> version checkpoint
  -> parse (PDF text, sandboxed OCR, HTML, inline) -> atomic page-aware chunks
  -> profile-scoped embedding shard -> acceptance + eval -> manual cutover
```

Key properties:

- **Discovery is read-only and reproducible.** The manifest is frozen and
  content-addressed by a logical SHA-256; its record count and hash live in
  [`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md).
- **Versioned, never overwritten.** A new content hash or processing fingerprint
  creates an immutable `fda_document_version`. Only current chunks stay
  searchable; version facts and artifacts stay auditable.
- **Auditable two-stage publication.** An advisory lock serializes one canonical
  document. Acquisition commits checksum and artifact provenance in a pending
  state; one later transaction publishes the complete chunk set or records a
  bounded parse failure. Partial chunks are never searchable.
- **Bounded work.** Downloads, redirects, archive expansion, PDF bytes, pages and
  time, OCR resources, Dagster runs and embedding batches are all finite. Every
  temporary artifact is unlinked in `finally`, and requests stay paced per host.
- **Resumable phases.** Chunk and per-profile embedding state checkpoint
  independently, so both backfills restart from durable committed state.
- **Terminal means formally resolved, not ignored.** Only exact 404 drift, and a
  retained PDF that still fails a reviewed parser after four durable attempts,
  may become terminal. Every other failure stays blocking, and a later successful
  parse recovers the same current version to indexed state.
- **Safe reconciliation.** Documents missing from discovery are retired only
  after a zero-error, unfiltered complete-universe run, so a developer's scoped
  run cannot retire production data.
- **Reversible cutover.** `REGWATCH_RETRIEVAL_CORPUS=legacy` is the code default.
  `authoritative_fda` is accepted at API boot only with all five families, a
  successful run, exact `indexed + terminal` manifest parity, zero policy
  violations, and 100% embedding coverage for indexed chunks.

### The complete-universe target is dead

Activation was originally gated on a complete-universe sync of the whole frozen
manifest. That target is permanently unreachable: the production Postgres branch
is capped at 512 MiB and the full universe does not fit (`config/settings.py`,
the `serving_manifest_sha` field).

The live path is a **curated manifest the operator names explicitly** through
`REGWATCH_SERVING_MANIFEST_SHA`. Set, activation counts against the durable
manifest with that exact logical SHA-256. Unset, the complete-universe rule
applies, which now means it cannot activate at all. A scoped sync can never
activate by accident either way.

The earlier PSG watch pipeline still drives daily change alerts. The replacement
corpus generalizes the searchable evidence store; its backfill and schedule must
pass the activation runbook in
[`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md) before cutover.

---

## 9. Storage and retrieval

Postgres plus pgvector is the only datastore, everywhere. `DATABASE_URL` is
mandatory and the app refuses to boot without it, so there is no toggle and no
fallback left to document. (R5 removed the old SQLite/Chroma dual mode; see git
history for that design.)

Production runs on Databricks Lakebase, with pgvector in the same database. Rows,
vectors and audit all live in one Postgres. The branch is capped at 512 MiB and
the cap is tier-fixed, not raisable; `scripts/reclaim_lakebase_space.py` is the
recovery tool. Check `pg_database_size` before any bulk write.

- `store/vector_store.py` is a thin wrapper over `store/pgvector_store.py` and
  `store/embedding_profiles.py`. Every public function talks to pgvector
  directly, so callers (the retriever, ingest, watch, the resolver, the health
  probe) never see a backend choice.

- **Score convention.** `Hit.score` is cosine similarity in `[0, 1]`, computed as
  `score = 1 - cosine_distance / 2` (1.0 identical, 0.5 orthogonal, 0.0
  opposite). The refusal threshold uses this convention. The 0.30 default was
  tuned against an older 1536-dimension vector space and has not been
  re-validated against the live one, which is why the per-profile override
  exists. See [`EVAL_STATUS.md`](EVAL_STATUS.md).

- **Embedding profiles (migration 0015) are how the vector space moves.** An
  embedder writes to a `chunk_embedding` table keyed by an immutable named
  profile (`vector(d)` up to 2000 dims, `halfvec` beyond). `legacy` means the
  original unversioned 1536-dim column on `chunk.embedding`.
  `RETRIEVAL_EMBEDDING_PROFILE` picks which profile serves retrieval, so a
  re-embed is blue/green: populate and benchmark a shadow profile, then flip. It
  is never an in-place rewrite.

- **Production serves a named profile, not `legacy`.** Because retrieval reads
  the profile, `INGEST_EMBEDDING_PROVIDER` steers only the write path and the
  legacy arm. The live profile id is a Fly secret;
  [`PRODUCTION_TRUTH.md`](PRODUCTION_TRUTH.md) owns it, `GET /health` reports the
  one a running process resolved, and `regwatch embedding-profile-list` reads the
  registry.

- **Provider and dimension are paired.** Startup fails fast on a provider/table
  mismatch (the `K6` assert in `assert_embedding_provider_dim`), and the corpus
  workers run the same geometry check as a preflight
  (`assert_embedding_write_config`) before any document work is spent.

- **RLS:** deny-all on every public table. The FDA corpus is org-shared by
  design, so per-user RLS only ever applies to the chat tables.

- **JSON columns** are `JSONB`. Single dialect since R5.

- **Schema bootstrap, two paths.** A fresh, empty database is created with
  `create_all` plus `alembic stamp head`, no history replay
  (`store/db.py::_init_postgres`). An existing database is migrated incrementally
  by `fly.toml`'s `[deploy] release_command = "regwatch release"`, which runs
  pending migrations and the serving-readiness guard on a one-off machine before
  the app machines roll: a newer image never boots against an older schema, and
  profile drift fails before the roll starts. Tests run the same alembic path
  against `TEST_DATABASE_URL`, so dev, CI and prod share one history.

### 9.1 Retrieval algorithm

Retrieval is an **exact pgvector scan**. Nothing about the ranking is
approximate, and that is a deliberate, enforced choice rather than a default.

`retrieve/mode.py` names the algorithm instead of inferring it. Before that
module existed, `similarity_search_profile` branched on `bool(clause)`, so "did a
metadata filter happen to be present" silently decided exact versus approximate
search on a regulatory-evidence path. Three modes exist:

- `EXACT_SCOPED`: exact scan over a product-scoped set. The compliance path.
- `EXACT_CORPUS`: exact scan over the whole current-version corpus, when no
  product resolved. It emits byte-identical SQL to `EXACT_SCOPED`; the
  distinction is a policy label recorded on the audit row.
- `ANN_RERANKED`: HNSW candidate generation plus a full-precision rerank.
  `assert_mode_permitted()` raises `ApproximateSearchNotPermitted`
  unconditionally, so this mode cannot execute.

For exact modes, `build_search_sql` issues `SET LOCAL enable_indexscan = off` and
orders by full-precision distance with a deterministic tiebreak. Bitmap scans on
the B-tree filter columns (`normalized_name`, `doc_id`, `version_id`, ...) stay
available, so a product-scoped query still narrows cheaply before the distance
sort. Every executed plan is recorded under `query_log.route_json["retrieval"]`
by `RetrievalPlan.as_route_json()`.

**Why there is no HNSW index on the serving arm.** Two independent reasons:

1. *Correctness.* pgvector applies metadata filters **after** the approximate
   scan, so a filtered HNSW query can silently drop matching rows. Measured
   against exact ground truth on this corpus, HNSW recall@8 averaged 0.984 and
   bottomed out at 0.125 on one query: usually perfect, occasionally
   catastrophic, and silent either way. On the common path the product filter
   already narrows the scan enough that exact distance is affordable.
2. *Capacity.* The Lakebase branch is capped at 512 MiB and headroom has been
   measured as low as ~21 MiB. The legacy HNSW index is roughly 42 MB on disk and
   reads only `chunk.embedding`, which is dead weight once a named profile
   serves. `store/pgvector_store.py` therefore builds that index **only** when
   the active profile is `legacy`; under a named profile the DDL is skipped, so a
   boot cannot silently reclaim the space an operator just freed.

`PROFILE_HNSW_INDEX_REQUIRED` is `false` in `fly.toml` for the same reason: exact
search needs full profile coverage, not an index.

**MMR diversity pass.** `retrieve/diversity.py` implements Maximal Marginal
Relevance over the already-retrieved pool, behind `REGWATCH_MMR_DIVERSITY`, which
is **off by default**. With it off, stage 2 is the unchanged first-`RERANK_TOP_K`
slice and production is bit-identical. With it on, the same *number* of passages
survives, but a candidate that repeats what is already selected loses to a
distinct one: eight near-clones of one paragraph are one piece of evidence, not
eight. The selection is
`lambda * rel(d) - (1 - lambda) * max sim(d, s) for s in selected`, with
`lambda = 0.7`. Similarity is token-set Jaccard over the chunk text, not cosine
over embeddings, so the module does no I/O and the flip costs no extra embedding
call or query. `lambda >= 1.0` returns the plain top-k slice, which makes it a
true no-op control arm for an A/B. One interaction to know before turning both
retrieval knobs on: the optional cross-encoder reranker (`RERANKER_ENABLED`, also
off by default) leaves a score on a different scale, and MMR re-ranks on cosine
after it.

Flipping either flag needs an eval A/B first; the harness is section 17.

---

## 10. Data model

`store/models.py` holds the SQLModel definitions. Migrations live in
`migrations/versions/`; run `alembic heads` for the current head and
[`PRODUCTION_TRUTH.md`](PRODUCTION_TRUTH.md) for the deployed stamp. Never
restate either from memory: they move every release.

### Corpus and evidence

| Table | Role |
|---|---|
| `product` | A target product on the company watchlist (INV-5: verified sources only) |
| `psg_document` | A PSG as currently published by FDA (unique `appl_no`, `content_hash`) |
| `psg_version` | A captured version of a PSG; a new row on every content change |
| `be_requirement` | Extracted BE requirements per PSG version; every field carries a citation |
| `ob_product` | Orange Book `Products.txt` row (raw rows only, INV-3) |
| `ob_patent` | Orange Book `patent.txt` row (raw rows only) |
| `ob_exclusivity` | Orange Book `exclusivity.txt` row (raw rows only) |
| `chunk` | The one citable unit, shared by both corpora. Legacy PSG rows carry a NULL `source_family`; authoritative FDA corpus rows carry a value |
| `fda_document` | Stable canonical identity for one of the five authoritative FDA source families |
| `fda_document_version` | Immutable source-byte and processing version, with artifact/page/chunk provenance |
| `fda_corpus_run` | Complete/scoped sync ledger, manifest hash, checkpoints, errors, and reconciliation facts |
| `spl_document` | Historical pre-corpus label provenance retained for saved-run compatibility; no new acquisition |
| `embedding_profile`, `chunk_embedding` | Named vector spaces and their vectors (migration 0015) |
| `graph_node`, `graph_edge`, `graph_node_chunk` | The Tier-1 hierarchy from migration 0018; write-only, populated only by the CLI `graph-backfill` |

The Orange Book tables store raw rows only. Paragraph classification and
eligibility are never persisted (INV-3, no regulatory judgment).

### Conversation, runs and audit

| Table | Role |
|---|---|
| `user` | Authenticated analyst; `email` unique and lowercased, bcrypt `password_hash`, role analyst or admin. CLI-provisioned, no self-signup |
| `auth_session` | Server-side login session; only the sha256 of the cookie token is stored |
| `chat_session` | A durable conversation thread; `active_filters_json` holds deterministic routing context. Composite index `(user_id, updated_at)` for the sidebar |
| `chat_message` | One user or assistant turn; role, content, status, citations, audit_id |
| `query_log` | The audit spine (INV-6): every turn's mode, query, retrieved set, answer, citations, refused flag, status, `route_json`, model, latency and token/cost columns |
| `answer_feedback` | Thumbs up or down; one row per `(audit_id, user_id)`, re-rating replaces. Candidate pool for future gold items |
| `alert` | Durable watch alerts surfaced by `GET /watch/latest` |
| `watch_run` | One row per watch run, the INV-4 evidence trail |
| `whitepaper_run`, `whitepaper_input` | Saved White Paper runs and their inputs |
| `deficiency_run`, `deficiency_kb` | Deficiency analysis runs, plus the embedded corpus of past deficiency text they search |
| `eval_run` | Persisted eval scorecards |

`query_log.route_json` records why each turn went the way it did (`reason`:
`multi_form`, `no_product`, `low_top_score`, `model_refusal`, `did_you_mean`,
`brand_lookup`, and so on), so an analyst or the eval harness can see why the
system clarified or refused, not only that it did. It also carries the gate's
per-claim ledger, the retrieval plan, and the route call's record when that is
on. Token and cost columns are `NULL` when no LLM call happened or the provider
reported no usage, never a guessed number.

---

## 11. White Paper populator

`whitepaper/populator.py` is a higher-order feature built on the same retrieval
and structured-source machinery. Input: an RLD name plus an application number.

```text
RLD name + appl_no
   |
   +- resolve a "spine" (the product identity), 422 SpineResolutionError on failure
   |
   +- for each of the 46 schema cells, by mode:
   |     * auto          -> deterministic fill, no LLM (counts, identifiers, dates)
   |     * evidence_only -> verbatim, cited FDA text (Orange Book / PSG / label rows)
   |     * manual        -> "analyst_input_required", never generated (INV-3)
   |
   +- write through Orange Book and label rows for durable provenance and freshness
   +- one whitepaper audit row, on success AND on a 422 resolution failure
```

- **Tri-state absence.** A blank cell, a "not found in FDA sources" and a "needs
  a human" are three distinct states, never collapsed.
- **The `.docx` render re-populates nothing.** `POST /whitepaper/runs/{run_id}/docx`
  (`whitepaper/docx_writer.py`) renders a saved run, fetched server side by
  `run_id`, with no live fetches and no LLM calls. Runs are org-shared, so the
  handler does not filter by caller. The `application_number` reaches the
  `Content-Disposition` header, so anything looser than `[A-Z]{0,4}\d{6}` is
  rejected. It writes one `docx_rendered` audit row, and builds an equivalent
  document from scratch when the Word template is absent (CI).

The 46-cell schema is normative: see [`whitepaper_schema.md`](whitepaper_schema.md).

---

## 12. Dossier assembler

`assemble/dossier.py` (`POST /assemble`) composes a cited dossier for a target
product: active ingredient, optional dosage form, RLD. It reuses the grounded
Q&A engine internally with `bind_session=False`, so its synthetic Q&A turns are
audited (INV-6) for attribution but do not appear in the analyst's chat history.
Output is markdown plus structured sections plus a refusal flag.

---

## 13. Change detection and alerts

`watch/` runs the change-detection loop over the same store:

- `change_detector` compares `content_hash` to spot revised PSGs and writes new
  `PsgVersion` rows with a cited `diff_summary`.
- `matcher`, `aliases` and `watchlist` map FDA records to the company watchlist
  (Amneal applicant aliases).
- `alerts` produces the digest records surfaced by `GET /watch/latest` and the
  Watch UI. The run history doubles as INV-4 evidence: nothing was fabricated,
  here is the run log.

Scheduling has two deliberately separate control planes. The GitHub Actions cron
`.github/workflows/watch-daily.yml` is the sole scheduler for daily Watch alerts.
The private Dagster worker owns only the authoritative FDA corpus, and its weekly
manifest schedule ships stopped, with no automatic backfill or cutover.

The Watch cron reads exactly three repository secrets: `WATCH_DATABASE_URL`,
`OPENAI_API_KEY`, and `WATCH_ACTIVE_EMBEDDING_PROFILE`, which must match the
`^ep_[0-9a-f]{32}$` shape and name the same profile production serves. Provider,
model and dimension are hardcoded in the workflow's own env block, not passed as
secrets. While `WATCH_DATABASE_URL` is unset the job skips cleanly instead of
failing. Provisioning steps are in
[`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md).

Before ingest, the workflow runs the serving boot gate, which checks the
registered immutable profile and its coverage readiness; after any attempted run
it independently requires zero pending chunks. Scheduled Watch writes only the
named serving profile and deliberately does not refresh the legacy vector column,
so that arm would need a backfill before it could serve as an embedding rollback.

---

## 14. Authentication and sessions

`auth/` is the verify half of the cookie-session contract. The mint half
(login, logout, me) moved to the Go proxy in step 4
(`go/internal/api/auth.go`), still designed so a future SSO swap touches one thin
boundary.

- **Login (Go).** `POST /auth/login` verifies a bcrypt password and issues an
  opaque token in an HttpOnly, SameSite=Lax cookie (`regwatch_session`). `secure`
  is driven by `AUTH_COOKIE_SECURE`, true in prod. The database stores only the
  sha256 of the token (`auth_session.token_hash`), and those are the same rows
  Python's `resolve_token` verifies, which is what makes the two runtimes one
  auth system. A fresh session row per login means no session fixation.
  Uniform-timing credential checks (a dummy bcrypt on an unknown email, one error
  message) live in the Go handler and are pinned by its contract tests.
- **No self-signup.** Accounts are provisioned with `regwatch create-user`.
- **`require_user` is the swap boundary.** Every Python protected route depends
  on it, so swapping cookie lookup for JWT/JWKS against the company IdP is a
  localized change on each side of the split.
- **Rate limiting.** Per user, `RATE_LIMIT_PER_MINUTE` (default 30) in
  `common/ratelimit.py`, on the expensive and outbound-FDA routes. Today that is
  `/query`, `/query/stream`, `/sources/search`, `/assemble`, `/whitepaper`,
  `/resolve`, `/whitepaper/runs/{id}/docx`, `/deficiency/analyze` and
  `/studio/check`. Grep `_enforce_query_rate_limit` in `api/main.py` for the
  current call sites.
  The login brute-force guard, 10 per email per minute and 30 per IP per minute,
  runs in the Go proxy (`go/internal/api/ratelimit.go`). Limiters are in-memory
  and per process, so a multi-replica deploy needs distributed limiting at the
  gateway. That is open work, see [`ROADMAP.md`](ROADMAP.md).
- **Session ownership.** `/sessions/{id}` and `/feedback`
  return 404, not 403, on a foreign or non-existent row, so the response never
  confirms that someone else's resource exists. Legacy NULL-owner sessions are
  adopted through a race-safe conditional `UPDATE` on the first authenticated
  `/query`; the loser of a race re-reads the committed owner and 404s.

---

## 15. Compliance invariants

The invariants are hard constraints, not features. They encode FDA expectations
(the Jan 2025 draft guidance treats AI that supports a regulatory decision very
differently from AI that only improves operational efficiency) and hard-won
lessons from this codebase. Each one is enforced in code and pinned by a test.

**This section is the only home of the invariant definitions.** Other documents
link here. If a requested behavior would violate an invariant, do not implement
it; record the conflict in [`DECISIONS.md`](DECISIONS.md).

**INV-1 Grounding.** Every factual claim in any output must be traceable to a
retrieved source passage with a document id and page number. No ungrounded
claims, ever.

*Amendment 1 (owner, 2026-08-07, implemented 2026-08-10).* Live un-gated prose
MAY stream to the client as an explicitly provisional draft on the dedicated
`draft` SSE channel, dual-gated by `REGWATCH_LIVE_DRAFT` and a per-request
opt-in, and available only in prose-synthesis mode. Nothing un-audited may be
presented as validated: draft frames carry no citations, no audit id and no
validated affordances, and the terminal `result` frame stays the only validated
artifact.

*Amendment 2 (v7 selective citation, live 2026-08-10).* "Factual claim" means a
sentence that says what FDA guidance requires, recommends, permits or prohibits.
Those sentences must carry their passage numbers, and
`generate/turn_gate.py` drops any that do not. Sentences that are our own
reading, or plain conversation, carry no numbers and assert no FDA facts. The
enforcement is unchanged: an uncited source fact still never reaches the user.
See section 6.

Enforced by `generate/turn_gate.py`: an uncited source-fact claim is dropped, and
a turn with nothing admitted refuses. Tested in `tests/test_invariants.py` and
`tests/test_inv1_real_geometry.py`. Do not delete the latter; echo-provider
geometry is degenerate and it is the only test that catches that.

**INV-2 Refuse over guess.** If retrieval does not surface a sufficiently
relevant passage, the system says so instead of answering. It never fabricates an
answer. Since v7 there is no fixed refusal string: the model says in ordinary
words that the corpus does not cover the question, names what it does have
nearby, and offers a next step. The API still marks the turn `refused` with an
empty citation list. Enforced by the refusal floor: a top-1 score below
`Settings.effective_refusal_threshold()` blocks the synthesizer and withholds the
weak passages from the guidance planner too. Tested in
`tests/test_invariants.py`.

**INV-3 Operational only.** The system surfaces, organizes, compares and cites
public information. It never authors submission content and never renders a
regulatory judgment. Enforced by the scope-warning guard in
`generate/grounded_qa.py`, by the Orange Book tables storing raw rows only, and
by White Paper `manual` cells. Tested in `tests/test_invariants.py`, which greps
the prompt and copy surfaces for banned advisory tokens.

**INV-4 No fabricated execution.** The system must never report or narrate a
process, run or result it did not actually execute. A match that was not fetched
does not exist. Enforced by versioned `psg_version` rows, the watch digests and
the `watch_run` log. Tested in `tests/test_invariants.py`: an alert must
reference a `psg_version` that was actually inserted.

**INV-5 Verified provenance.** Pipeline and product facts come only from
verified, allowlisted sources, never from a language model's memory. Enforced by
`sources/policy.py`, the `ALLOWED_SOURCES` set and the `source` enums. Tested in
`tests/test_invariants.py`, which pins the allowed set itself so widening it is a
deliberate, reviewed diff.

**INV-6 Auditability.** Every query, its retrieved sources with scores, the
generated answer and whether it refused are logged to a durable store. Enforced
by `log_query` in every branch of `ask()`, including error and guidance branches.
Tested in `tests/test_invariants.py`, including that rows from an authenticated
caller carry user identity.

**INV-7 Cross-product integrity.** Never blend two applications' data. Enforced
by exact application-number token matching in `whitepaper/populator.py` and
`sources/psg.py`, over the shared normalizer in `common/text_normalize.py`.
Tested in `tests/test_whitepaper_populator.py`.

**INV-8 Structured-citation grammar.** A structured citation token must parse and
must be backed by real evidence; a central guard collapses any cell whose token
is not backed. Enforced by `common/citations.py` and the White Paper populator's
guard. Tested in `tests/test_citations.py` and
`tests/test_whitepaper_populator.py`.

**INV-9 Product resolution before retrieval.** The product is resolved before any
semantic search, which is what stops a cross-drug citation leak. Enforced by
`retrieve/resolver.py` and the pre-retrieval product and form guards in
`generate/grounded_qa.py`. Tested in `tests/test_cross_drug_leak.py`,
`tests/test_multiform_clarify.py` and `tests/test_whitepaper_populator.py`.

INV-7 through INV-9 extend INV-1 through INV-6; they do not supersede them. The
cross-drug and cross-form guards are doubled on purpose. The pre-retrieval guard
enumerates the product's current `(dosage_form, route)` combinations and
clarifies when there is more than one. The post-retrieval guard clarifies when
the returned passages span more than one product or form, which backstops any
caller that bypassed the resolver.

---

## 16. Observability and ops

- **Sentry** (`common/observability.py`, `init_sentry`) on the API and the
  frontend, off unless `SENTRY_DSN` or `NEXT_PUBLIC_SENTRY_DSN` is set. PII is
  scrubbed (`include_local_variables=False` plus a `before_send` scrubber), so
  question and answer text never leave the system. Frontend Sentry has source-map
  upload, replay and tunneling disabled.
- **Health and uptime:** an external pinger on `GET /health`, plus the
  `PROD_HEALTH_URL` GitHub secret for the CI smoke check. Whether the
  repository-level monitoring workflows are enabled cannot be proven from a
  checkout; committed YAML is not evidence that monitoring runs.
- **Token and cost accounting:** `query_log.input_tokens`, `output_tokens` and
  `cost_usd` capture the turn's single Ask completion, synthesizer or guidance
  planner. `estimate_cost_usd` prices it from a settings table and leaves the
  column `NULL` rather than guessing. `latency_ms` is the per-turn wall clock.
- **Runbook:** rollback, restore drill and operations notes are in
  [`DEPLOY.md`](DEPLOY.md).
- **Docker:** the CRA template `.docx` is internal and gitignored, so it is never
  baked into an image. `WHITEPAPER_TEMPLATE_PATH` points at it under the mounted
  data volume; an operator drops the file there per `DEPLOY.md`, and without it
  the writer falls back loudly and everything stays green.
- **Still open** (see [`ROADMAP.md`](ROADMAP.md)): exported request, latency and
  cost metrics; a readiness probe covering database, vector store and LLM
  reachability; a Sentry DSN configured in prod; least-privilege database
  credentials; and a rehearsed restore drill.

---

## 17. Evaluation harness

`eval/` is the regression gate. It scores the system against a pinned gold set:

- `run_eval.py` and `metrics.py`: recall, citation precision, faithfulness,
  refusal accuracy and over-refusal for grounded Q&A.
- `whitepaper_metrics.py`: White Paper cell-level checks.

Faithfulness is kind-aware: it measures the share of the gate's admitted
source-fact claims that carry a citation, and the older text-only rule is still
reported alongside it as `sentence_citation_rate`.

The blocking floors live in `THRESHOLDS` in `src/regwatch/eval/run_eval.py` and
that dict is the source of truth. As committed they are `recall_at_k >= 0.80` and
`citation_precision >= 0.70`; the citation floor was lowered from 0.74 on
2026-08-11 by owner decision, with the measurement and the reasoning written out
in the comment above the dict. Read the dict, do not quote a number from a
document.

The floors are a ratchet, not a quality bar: each sits just below the first real
measurement of the arm it guards, because the eval drives a live LLM and the
numbers drift run to run. The separate `TARGETS` dict is aspirational, reported
beside each value and never blocking. `refusal_accuracy` is measured and
persisted but not gated: Ask is moving to a conversational layer that is not
meant to refuse. A separate blocking ceiling on end-to-end p95 latency per gold
question catches a change that buys recall with time, and a run that loses more
than 10 percent of its turns to transport failures exits without scoring, because
it did not measure the system.

The blocking eval scores the production arm: CI calls the reusable
`openai-eval.yml` workflow with `prose: true`, `selective: true` and
`assert_prod_mode: true`, asserted against `config/prod_mode.json`. The old
failure mode, where the gate scored v5 while production served v7, is closed.

The committed gold sets are `gold_set.jsonl` (62 Q&A rows: 43 must-answer, 16
must-refuse, 3 must-clarify) and `whitepaper_gold.jsonl` (16 rows), scored
mechanically on `(short_name, page)` plus `expected_facts`. A deterministic
fixture also runs inside every `pytest`. [`EVAL_STATUS.md`](EVAL_STATUS.md) has
the evidence and the gaps, [`CI_CD.md`](CI_CD.md) has the job wiring, and open
work (hard negatives, an LLM-as-judge pass, feedback-sourced gold items) is in
[`ROADMAP.md`](ROADMAP.md).

---

## 18. Configuration

One Pydantic `Settings` object in `config/settings.py` owns runtime
configuration. Real model providers are required-explicit: a missing provider
name or credential fails at startup, never at the first request.

[`CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md) is the complete annotated surface
and owns every current value. What follows is the shape a reader needs to
understand the design, not an inventory.

| Variable | Effect |
|---|---|
| `DATABASE_URL` | Mandatory RegWatch PostgreSQL + pgvector connection |
| `LLM_PROVIDER` | `openai` for real calls; `echo` for tests only. `databricks` raises |
| `OPENAI_API_KEY` | Credential shared by the Responses and Embeddings APIs |
| `OPENAI_LLM_MODEL`, `OPENAI_REASONING_EFFORT` | Generation model and one global reasoning effort |
| `INGEST_EMBEDDING_PROVIDER` | Provider for the ingest/backfill write path and the legacy retrieval arm. `openai` for real calls; `echo` for tests only |
| `RETRIEVAL_EMBEDDING_PROFILE` | The named profile the query path serves from |
| `OPENAI_EMBEDDING_MODEL`, `OPENAI_EMBEDDING_DIMENSION` | Embedding model and profile width |
| `REFUSAL_SCORE_THRESHOLD`, `REFUSAL_SCORE_THRESHOLD_BY_PROFILE` | Global INV-2 floor and its per-profile overrides |
| `RETRIEVAL_MODE` | Exact; the approximate mode is refused (section 9.1) |
| `PROFILE_HNSW_INDEX_REQUIRED` | `false`, because exact retrieval needs coverage, not an index |
| `GO_NATIVE_QUERY` | Whether Go orchestrates `POST /query`. Code default `false`, pinned true in `fly.toml` |
| `INTERNAL_RAG_TOKEN` | Gates `POST /internal/query/compute`; unset means 404 |

Two renames matter when you read older configuration:
`INGEST_EMBEDDING_PROVIDER` supersedes `EMBEDDING_PROVIDER`, and
`RETRIEVAL_EMBEDDING_PROFILE` supersedes `ACTIVE_EMBEDDING_PROFILE`. Both old
names still work as aliases and log a `FutureWarning`; when both are set the new
name wins. Production secrets still carry some of the old names.

The OpenAI request always sets `store=false`. RegWatch sends the applicable
transcript explicitly and does not configure an OpenAI conversation or
`previous_response_id`.

---

## 19. Design principles

1. **Cite the facts, talk like a person.** Enforced at retrieval (the score
   threshold), at the gate (per-claim citation validation), and in tests (the
   invariants). Uncited FDA facts never render; the prose around them can sound
   human.
2. **Deterministic authority, bounded probabilistic help.** Entity resolution,
   source routing, product/form/scope/status enforcement, the citation gate and
   display copy are all rules-based and unit-testable. The model either
   synthesizes from pre-vetted, product-scoped passages or picks a
   server-allowlisted guidance action. It cannot cross those contracts.
3. **One datastore, one score convention.** Postgres plus pgvector is the only
   backend. Dev and CI run it against a disposable local instance, never the
   cloud, and prod behaves the same because it is the same code path.
4. **One auth chokepoint, one audit spine, one contract boundary.** Security (the
   `require_user` router), traceability (`query_log`) and the IT handoff
   (`api/main.py`) each have exactly one place to reason about.
5. **Thin client, fat server, single origin.** All intelligence and all secrets
   stay behind the proxy. The browser sees one origin and a cookie.
6. **Versioned evidence, never overwritten.** PSGs are captured as versions. The
   search index holds the current answer while the history stays auditable.
7. **Explicit over inferred.** A provider, a retrieval mode or a serving manifest
   is named by an operator and recorded, never guessed from a side effect. Every
   outage this codebase has written down came from something being implicit.
