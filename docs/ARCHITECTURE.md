# REGWATCH - System design and architecture

> An FDA regulatory-intelligence system for Amneal's generic-drug Clinical
> Regulatory Affairs (CRA) team. It surfaces, organizes, compares and cites
> public FDA data. By design it never authors submission content and never
> renders regulatory judgment.

> Last updated: 2026-08-13 for the authoritative FDA corpus. Live app, database
> and Fly values were last checked 2026-08-11; the replacement corpus discovery
> was checked against official FDA snapshots on 2026-08-13.

This is the canonical description of how REGWATCH is built. Read it once, then
use it as a reference. Companion docs: [`PROJECT_SPEC.md`](PROJECT_SPEC.md) (the
requirements), [`DEPLOY.md`](DEPLOY.md) (the production runbook),
[`whitepaper_schema.md`](whitepaper_schema.md) (the 46-cell White Paper schema),
[`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md) (the proposed
graph-assisted retrieval), and [`DECISIONS.md`](DECISIONS.md) (the decision log).

---

## Table of contents

1. [The prime directive](#1-the-prime-directive)
2. [Deployment topology](#2-deployment-topology)
3. [Product surfaces](#3-product-surfaces)
4. [Backend layout (the pipeline)](#4-backend-layout-the-pipeline)
5. [The API boundary](#5-the-api-boundary)
6. [The grounded Q&A engine](#6-the-grounded-qa-engine)
7. [Source layer](#7-source-layer)
8. [Ingest and processing pipeline](#8-ingest-and-processing-pipeline)
9. [Storage layer (Postgres + pgvector)](#9-storage-layer-postgres--pgvector)
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

## 1. The prime directive

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
policy. Version 7 replaced it in production on 2026-08-10. Enforcement did not
get weaker: an uncited source fact is still dropped. What changed is that the
reply around the facts is allowed to read like a colleague talking, instead of a
form letter. Section 6 has the detail.

Responses are also not an answer/refuse binary. The system distinguishes
grounded answers, summaries, clarifications, scope warnings, capability
information, evidence gaps and operational errors. When it cannot safely answer
a healthy turn, a constrained planner picks among next steps the application
already approved, instead of dead-ending or guessing.

---

## 2. Deployment topology

Four tiers, one browser-visible origin. Production is live on the Fly app
`amneal` (release v104, deployed 2026-08-10). The Go proxy holds the public
port; there are two Fly process groups, `proxy` and `app`, declared in
`fly.toml`. The schema self-migrates on every deploy through the Fly
`release_command` (section 9).

Generation and embeddings both run on Databricks Model Serving inside the
company's own Databricks tenant, and the production database is Databricks
Lakebase. That closes the data-residency question, D1: an analyst's question no
longer leaves the tenant on the normal path. See
[`DATABRICKS_ADOPTION_2026-07-28.md`](DATABRICKS_ADOPTION_2026-07-28.md) for the
adoption call and [`archive/DATA_RESIDENCY_D1.md`](archive/DATA_RESIDENCY_D1.md)
for the original threat model. Note that the adoption doc argued Supabase should
stay; that call was reversed and the move to Lakebase already happened.

Postgres and pgvector are the only datastore since R5. Locally and in CI the
same code runs against a disposable Postgres (`TEST_DATABASE_URL` for tests),
on the same schema as prod.

A TLS and SSO front door is still missing, so the app is not exposed externally.
See [`ROADMAP.md`](ROADMAP.md).

```
                       Browser (analyst)
                            |  HTTPS, HttpOnly session cookie
                            v
        +------------------------------------------+
        |  Vercel - Next.js 16 (App Router)        |   amneal.vercel.app
        |  server-side rewrite /api/* -> backend   |
        +--------------------+---------------------+
                             |  /api/:path*  ->  API_PROXY_TARGET/:path*
                             v
        +------------------------------------------+
        |  Fly.io - Go proxy (public :8080)        |   amneal.fly.dev
        |  auth, sessions, rate limits,            |   process group "proxy"
        |  native /query orchestration + audit     |   (sqlc over Postgres)
        +--------------------+---------------------+
                             |  6PN private network (IPv6)
                             v
        +------------------------------------------+
        |  Fly.io - FastAPI ("regwatch serve")     |   process group "app"
        |  stateless RAG core: resolve, retrieve,  |   dual-stack :8000
        |  synthesize, gate the citations          |
        +------+---------------------+-------------+
   SQLAlchemy  |                     |  OpenAI-compatible HTTPS
   / psycopg   v                     v
  +------------------------+   +-----------------------------------+
  | Databricks Lakebase    |   | Databricks Model Serving          |
  | Postgres + pgvector    |   | workspace.default.regwatch        |
  | rows, vectors, audit   |   |   gpt-oss-120b, every LLM role    |
  | in ONE database        |   | workspace.default.regwatch-embed  |
  | RLS deny-all           |   |   Qwen3 embeddings, 1024-dim      |
  +------------------------+   +-----------------------------------+
```

The Go proxy reaches the same Postgres directly (sqlc) for the surfaces it
serves natively.

### Single-origin proxy

The browser only ever talks to the Next.js origin. `next.config.mjs` declares:

```js
async rewrites() {
  return [{ source: "/api/:path*", destination: `${API_PROXY_TARGET}/:path*` }];
}
```

What that buys: no CORS in the browser path (everything is same-origin, and the
backend's CORS middleware is defense in depth for the credentialed cookie), no
public API URL in the client bundle, and one origin for the whole app.
`API_PROXY_TARGET` is a server-side env var. It defaults to
`http://127.0.0.1:8000` for local dev, pinned to IPv4 so Node does not waste a
failed `::1` attempt on every request.

### Environments

| Concern | Local / dev / CI | Production |
|---|---|---|
| Structured store | disposable Postgres | Databricks Lakebase Postgres, us-east-2 |
| Vector store | pgvector in the same instance | pgvector in the same Lakebase database |
| Embeddings | `echo` test provider (1536-dim) | Databricks Qwen3 at `workspace.default.regwatch-embed`, 1024-dim, profile `ep_2e7368b354d911ea3a013c3125e276c2` |
| LLM | `echo` test provider | Databricks `gpt-oss-120b` at `workspace.default.regwatch`, one endpoint for every role |
| `DATABASE_URL` | required, `TEST_DATABASE_URL` for tests | required |

The interactive app uses OpenAI only as a tested rollback; no analyst turn goes
there in current state. Scheduled Watch separately retains a scoped OpenAI key
for public-document change summaries and extraction, not embeddings.
`EMBEDDING_PROVIDER=openai` is still in `fly.toml` and is now dead weight on the
query path: retrieval picks its arm from
`ACTIVE_EMBEDDING_PROFILE`, and only the `legacy` arm ever reads
`EMBEDDING_PROVIDER` (section 9).

---

## 3. Product surfaces

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
| `(shell)/whitepaper/page.tsx` | `POST /whitepaper`, `POST /whitepaper/docx`, `/whitepaper/runs*` | CRA White Paper populator, exported as a filled `.docx` |
| `(shell)/watch/page.tsx` | `GET /watch/latest` | Recent change-detection alerts |
| `(shell)/deficiency/page.tsx` | `POST /deficiency/analyze` (202 plus background), `GET /deficiency/runs`, `GET /deficiency/runs/{id}` | Predicted submission deficiencies with evidence |
| `studio/page.tsx` | none yet (fixtures) | Compliance Studio, for reviewing our own CMC documents |
| `login/page.tsx` | `POST /auth/login` | Cookie-session login gate |
| `fixtures/page.tsx` | static | Demo inputs for testing |

Ask is a cited conversational chat: user bubbles on the right, a navy RW avatar
on the assistant side (gold is reserved for grounding), citation chips that open
an in-app evidence drawer with a Sources list as the no-JS fallback, clarify
pills, and a bottom-pinned composer. On wide viewports the avatar column becomes
a margin rail carrying the turn's provenance: time filed, audit number, and a
confidence dot that only validated answers ever wear.

### 3.1 Compliance Studio

Every other surface reads public FDA material. `/studio` reads our own drafts,
which is why it sits outside the shell: it takes the whole viewport and has no
scoped-product context.

It is UI and domain model only. `lib/studio-fixtures.ts` stands in for the
document service, the compliance pipeline and the assistant, and nothing survives
a refresh. The fixture shapes are the contract a real endpoint has to meet.

The idea worth carrying into the backend is that **a finding is a span, not a
report line**. Findings anchor to `(blockId, start, end)` plus the `excerpt`
those offsets resolved to, so they can highlight in place and be invalidated when
the analyst edits underneath. `Finding.start/end` is the immutable as-checked
anchor; `Mark.start/end` is the current render position, remapped on every edit.
Full design record: [`COMPLIANCE_STUDIO.md`](COMPLIANCE_STUDIO.md).

> **Direction, not built.** The intent is to collapse to two surfaces: Ask as
> the conversational one, and Studio as the document workspace that absorbs
> Assemble, Watch, White Paper and Deficiency. Nothing has moved. Studio has no
> backend, no persistence and no product scoping, so it cannot receive a working
> surface until those exist. Prerequisites are in [`ROADMAP.md`](ROADMAP.md).

### URL-scoped CurrentProduct and the "Under review" bar

One reference product is scoped across all five shell surfaces and mirrored into
the URL query (`?rp=<reference product name>&appl=<application number>`), so the
scope is shareable and survives a reload. `components/CurrentProductProvider.tsx`
reads that URL as the state of record. Re-scoping rewrites only the `rp` and
`appl` params, never the Ask page's `session`, so it cannot drop an open
conversation.

`components/ProductScopeBar.tsx` is the sticky "Under review" strip at the top of
every surface and the front-door setter for the whole pipeline. Pinning runs the
same deterministic resolve the White Paper uses, `POST /resolve` (section 5), so
the scope is always the canonical `{normalized_name, six-digit application
number}`. A 422 leaves the scope unset and shows the resolver's explanation
verbatim: refuse over guess. Three places set the scope, all writing that same
pair: the bar's picker, the White Paper on a successful populate, and a Watch
row.

Other cross-cutting frontend pieces: `app/layout.tsx` (the `AuthProvider` gate
plus fonts), `app/(shell)/layout.tsx` (sidebar, canvas, scope bar, history
sidebar), `app/icon.svg` (Amneal favicon), and `app/global-error.tsx` plus
`sentry.*.config.ts` (Sentry, off unless `NEXT_PUBLIC_SENTRY_DSN` is set).

---

## 4. Backend layout (the pipeline)

The backend (`src/regwatch/`) is a classic RAG data pipeline. Each package is
one stage with a clean boundary:

```
 sources/  ->  corpus/  ->  ingest/process/  ->  store/  ->  retrieve/  ->  generate/
 (exact five   (manifest,    (bounded parse,     (Postgres   (embed query,   (grounded LLM
  FDA families) atomic sync,  citable chunks,     + pgvector  vector top-k,   synthesis +
                coverage)     batch embeddings)  only)       rerank, scope)  citation gate)

 watch/      change detection plus digests and alerts over the same store
 assemble/   higher-order composer: a cited dossier (retrieve + generate)
 whitepaper/ higher-order composer: the 46-cell White Paper plus the .docx writer
 auth/       cookie sessions, bcrypt passwords, the require_user dependency
 common/     audit, citations, conversation memory, ratelimit, observability, logging
 eval/       offline gold-set metrics (the regression gate)
 api/        the FastAPI surface, the one boundary IT will wrap or replace
cli.py      operator commands (seed, ingest-all, create-user, watch, ...)
```

`config/` is a sibling top-level package, imported as `config.settings`. It holds
the single Pydantic `Settings` object every module reads.

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
Python verifies it. Since the step-5 cutover
([`GO_NATIVE_QUERY_ROLLOUT.md`](GO_NATIVE_QUERY_ROLLOUT.md), live 2026-07-24) Go
also orchestrates `POST /query`: it writes and finalizes the `query_log` audit
row and calls Python's internal, token-gated `POST /internal/query/compute` for
the RAG work. That endpoint is fail-closed: it 404s when `INTERNAL_RAG_TOKEN` is
unset, and the proxy never exposes the `/internal/` subtree.

On the Python side the only open probes are `GET /health`, `/ready` and
`/metrics`. Everything else is registered on a single router with a router-level
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
| POST | `/query` | yes | Grounded Q&A, orchestrated by Go since step 5; the RAG compute stays in Python |
| POST | `/query/stream` | yes | The same turn over SSE: progress frames, provisional draft tokens, one terminal validated result |
| POST | `/feedback` | yes | Thumbs up or down on one of the caller's own answers (Go) |
| POST | `/sources/search` | yes | Structured FDA source lookup |
| POST | `/resolve` | yes | Deterministic entity resolution (RLD plus application number) to a canonical spine; pins the scope bar without a populate |
| POST | `/assemble` | yes | Build a cited dossier |
| POST | `/whitepaper` | yes | Populate the CRA White Paper |
| POST | `/whitepaper/docx` | yes | Render a returned White Paper result as `.docx` |
| GET / POST / DELETE | `/whitepaper/runs*` | yes | Saved White Paper runs: list, detail, per-cell edit, finalize, reopen, render, delete |
| GET | `/watch/latest` | yes | Recent alerts, optional `since` filter |
| GET | `/psg/documents`, `/psg/documents/{id}/pdf` | yes | PSG reference library listing and inline PDF |
| GET | `/psg/documents/{id}/content`, `/psg/documents/{id}/docx` | yes | one PSG as studio blocks, and as a generated Word download |
| POST | `/deficiency/analyze` | yes | Start a deficiency analysis (202 plus background work) |
| GET | `/deficiency/runs`, `/deficiency/runs/{id}` | yes | Deficiency run list and detail |
| GET / POST | `/products` | yes | List or add watchlist products (Go) |
| DELETE | `/products/{id}` | yes | Soft-unwatch a product; the row is kept (INV-4) (Go) |
| GET | `/sessions` | yes | The caller's chat sessions (Go) |
| GET / DELETE | `/sessions/{id}` | yes | One session with messages, or delete it (Go) |
| GET | `/settings` | yes | Non-secret config (Go) |
| GET | `/health`, `/ready`, `/metrics` | open | Liveness, readiness and rollup counters |

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
4. `assert_embedding_runtime_available` refuses to boot a provider whose runtime
   dependencies are missing, rather than 500-ing on the first embed call.

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

```
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
   +- Stage 1: vector top-k (VECTOR_TOP_K = 50), scoped to the product (+ form)
   +- Stage 2: rerank and trim (RERANK_TOP_K = 8; reranker off by default, so
   |           this is just a slice)
   |
   +- top-1 score < REFUSAL_SCORE_THRESHOLD (0.30)?
   |     +- withhold every weak passage --------> GUIDANCE PLANNER
   |
   +- POST-RETRIEVAL GUARDS (defense in depth)
   |     * passages span more than one product -> GUIDANCE PLANNER
   |     * passages span more than one form ----> GUIDANCE PLANNER
   |
   +- SYNTHESIZER (temperature 0.0, SYNTHESIZER_MAX_TOKENS = 3000, shared by a
   |               reasoning model's thinking AND its answer)
   |
   +- parse the prose into sentences, each tagged source fact / reasoning /
   |  conversation (generate/prose_turn.py)
   +- gate every sentence against the passages actually sent this turn
   |  (generate/turn_gate.py)
   +- render only what the gate admitted, with markers the renderer writes ---> ANSWER
```

Operational failures (catalog, database, pipeline, provider) never enter the
guidance path. They return the audited error outcome with no extra AI call.

### The v7 answer policy, live in production

Three Fly flags, all on today:

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
open-field search. That is also why per-drug queries do not need HNSW
(section 9).

### Current-version scoping

`retrieve()` (`retrieve/retriever.py`) does more than embed and search. Before
querying the vector store it computes the set of current `psg_version` ids for
the filtered documents (`_current_version_ids_for_filters`) and constrains the
search to them, so a superseded chunk can never be cited even if it is still in
the index. A pure vector-only mode, used by unit tests that seed pgvector chunks
without a matching catalog row, is detected and skipped.

### Graph-assisted retrieval (foundation only)

Migration `0018_knowledge_graph` and `store/graph_store.py` derive a
deterministic Tier-1 hierarchy at chunk-write time: `application` to `psg_doc`
(`HAS_PSG`), `psg_doc` to `psg_section` (`HAS_SECTION`), ordered sections
(`FOLLOWS`), and graph nodes back to source chunks.

Chunks remain the only citable unit. The graph has no node embeddings and no
runtime query consumer, so the Ask path above is unchanged. The proposed
consumer, its budgets and its rollout gates are in
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

The Ask client targets `/query/stream` (SSE) first. The backend emits live
pipeline progress frames and then one terminal `result` frame carrying the same
validated `QueryResponse` that blocking `POST /query` returns. If the stream
closes before that frame, the client falls back to `POST /query` once.

Between progress frames the backend also emits provisional `token` delta frames,
a live draft the UI renders as it arrives. Those deltas are cosmetic by design:
INV-1 forbids treating answer text as authoritative before the gate has run, so
the validated terminal frame is the only answer of record. Both paths share one
serializer, so the wire shape cannot drift, and each turn still writes exactly
one audit row.

### Route and scope shadow (default off)

`REGWATCH_ROUTE_CALL=shadow` watches a proposed conversation-first router without
giving it any authority. One bounded `router` call proposes a
`standalone_question`, a `mode` and a `scope_hint`. Application code compiles the
hint against the deterministic resolver, the session state and an allowlisted
corpus catalog, then writes the result only to `route_json["route_call"]`. The
retrieval question, filters, mode, status, copy, citations and session patch all
continue through the existing path, and the model cannot emit filters, document
ids, version ids or an executable mode.

`off` makes no call at all. `live` is reserved and still behaves as `shadow`;
PR12 is the first change allowed to give it meaning. Failures fail open to the
existing turn, except `D1ResidencyError`, which is re-raised and stays fail
closed.

---

## 7. Source layer

`sources/policy.py` is the acquisition boundary. It admits exactly five source
families and validates the initial URL plus every redirect against reviewed,
family-specific FDA hosts and paths. The allowlist is code-reviewed, not an
environment variable. Retired API, DailyMed, NDC, shortage, and REMS acquisition
paths fail closed and are not registered in the router.

`sources/router.py` is deterministic and rules-first (regex and keywords, no
LLM). It fans a `SourceQuery` out only to the approved handlers behind the common
`SourceHandler` interface.

```
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

The replacement offline path is split into exact-manifest discovery,
document-at-a-time acquisition/chunking, and profile-scoped embeddings. Dagster
orchestrates canaries, 512 deterministic shard backfills, retries, and blocking
acceptance checks; Lakebase lifecycle rows remain the source of truth.
Deployment migrations never fetch or backfill source data.

```
official FDA snapshots/catalogs -> durable exact manifest + fingerprints
  -> family/path/redirect validation -> one bounded streamed temporary artifact
  -> SHA-256 + durable object upload -> acquired immutable version checkpoint
  -> PDF text / sandboxed OCR / HTML / inline parse -> atomic page-aware chunks
  -> unconditional temporary cleanup -> profile-scoped embedding shard
  -> 512-shard acceptance + retrieval/citation evaluation -> manual cutover
```

Key properties:

- **Discovery is read-only and reproducible.** The complete 2026-08-13 manifest
  has 140,339 source records and SHA-256
  `4e5c3708cb309489d9056580a7578b3047560f32aca0345df6ee26c3cd2a7c5e`.
- **Versioned, never overwritten.** A new content hash or processing fingerprint
  creates an immutable `fda_document_version`. Only current chunks remain
  searchable; version facts and content-addressed artifacts remain auditable.
- **Auditable two-stage publication.** An advisory lock serializes one canonical
  document. Acquisition first commits checksum/artifact provenance and pending
  state; one later transaction publishes the complete chunk set or records a
  bounded parse failure. Partial chunks are never searchable.
- **Bounded work.** Downloads, redirects, archive expansion, PDF bytes/pages/time,
  OCR pixels/CPU/memory/output, Dagster runs, and embedding batches are finite.
  Each shard processes one document at a time and every temporary artifact is
  unlinked in `finally`; request starts remain paced per FDA host.
- **Resumable phases.** Chunk and per-profile embedding states checkpoint
  independently. Both backfills restart from durable committed state and skip
  ready work even when Dagster itself restarts.
- **Safe reconciliation.** Documents missing from discovery are retired only
  after a zero-error, unfiltered complete-universe run. A developer's scoped or
  limited run cannot retire production data.
- **Reversible cutover.** `REGWATCH_RETRIEVAL_CORPUS=legacy` remains the default.
  `authoritative_fda` is accepted at API boot only when all five families, a
  successful full run, exact document parity, zero policy violations, and 100%
  embedding coverage are present.

The earlier PSG watch pipeline still drives daily change alerts. The replacement
corpus generalizes the searchable evidence store; its full backfill and schedule
must pass the activation runbook in
[`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md) before cutover.

---

## 9. Storage layer (Postgres + pgvector)

Postgres plus pgvector is the only datastore, everywhere. `DATABASE_URL` is
mandatory and the app refuses to boot without it, so there is no toggle and no
fallback left to document. (R5 removed the old SQLite/Chroma dual mode; see git
history for that design.)

Production runs on Databricks Lakebase in us-east-2, database
`databricks_postgres`, app role `regwatch_app`, with pgvector in the same
database. Rows, vectors and audit all live in one Postgres. Fly release 135
deployed migration `0023_authoritative_fda_corpus`; this follow-up adds
`0024_fda_streaming_lifecycle`. The partial canary increased the corpus from
5,494 to 5,841 chunks, all embedded on the active Qwen profile (5,841 / 5,841
verified 2026-08-14); the worker release and the 21 / 21 canary rerun for the
three unparsed documents precede the full backfill.

- `store/vector_store.py` is a thin wrapper over `store/pgvector_store.py`. Every
  public function (`similarity_search`, `add_chunks`, `collection_size`,
  `distinct_metadata_values`, `delete_chunks_for_doc_except_version`) talks to
  pgvector directly. Callers, meaning the retriever, ingest, watch, the resolver
  and the health probe, never see a backend choice.

- **Score convention.** `Hit.score` is cosine similarity in `[0, 1]`, computed as
  `score = 1 - cosine_distance / 2` (1.0 identical, 0.5 orthogonal, 0.0
  opposite). The refusal threshold uses this convention. The current `0.30` value
  is still provisional: it was tuned against the old OpenAI 1536-dim space, and
  the live space is now Qwen3 1024-dim, so that work does not transfer. See
  [`EVAL_STATUS.md`](EVAL_STATUS.md).

- **Embedding profiles (migration 0015) are how the vector space moves.** An
  embedder writes to a `chunk_embedding` table keyed by an immutable named
  profile (`vector(d)` up to 2000 dims, `halfvec` beyond). `legacy` means the
  original unversioned 1536-dim column. `ACTIVE_EMBEDDING_PROFILE` picks which
  profile serves retrieval, so a re-embed is blue/green: populate and benchmark a
  shadow profile, then flip. It is never an in-place rewrite.

- **Production runs a real profile, not `legacy`.** The active profile is
  `ep_2e7368b354d911ea3a013c3125e276c2` (created 2026-07-30), provider `qwen3`,
  1024 dimensions, served at `workspace.default.regwatch-embed`. All 5,494 chunks
  are embedded on it, 100 percent coverage, and it is the only row in
  `embedding_profile`. Because retrieval reads the profile, `EMBEDDING_PROVIDER`
  no longer affects the query path.

- **Provider and dimension are paired.** Startup fails fast on a provider/table
  mismatch (the `K6` assert in `assert_embedding_provider_dim`), which is why
  `local-bge-small` (384-dim) is rejected against the app datastore and stays
  available for offline and eval tooling only.

- **pgvector index strategy.** Per-drug queries, the common path, use a B-tree
  filter on `normalized_name` plus exact distance. That beats HNSW for filtered
  search; HNSW is for unfiltered queries.

- **RLS:** deny-all on every public table. The FDA corpus is org-shared by
  design, so per-user RLS only ever applies to the chat tables.

- **JSON columns** are `JSONB`. Single dialect since R5.

- **Schema bootstrap, two paths.** A fresh, empty database is created with
  `create_all` plus `alembic stamp head`, no history replay
  (`store/db.py::_init_postgres`). An existing database is migrated
  incrementally: `fly.toml`'s `[deploy] release_command = "alembic upgrade head"`
  runs pending migrations on a one-off machine before the app machines roll, so a
  newer image never boots against an older schema. That is the fix for the
  2026-06-18 incident. Tests run the same alembic path against
  `TEST_DATABASE_URL`, so dev, CI and prod share one migration history.

---

## 10. Data model

`store/models.py` holds the SQLModel definitions. Twenty-one Alembic migrations,
`0001` through `0021`, all in one Postgres.

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
| `fda_document` | Stable canonical identity for one of the five authoritative FDA source families |
| `fda_document_version` | Immutable source-byte and processing version, with artifact/page/chunk provenance |
| `fda_corpus_run` | Complete/scoped sync ledger, manifest hash, checkpoints, errors, and reconciliation facts |
| `spl_document` | Historical pre-corpus SPL provenance retained for saved-run compatibility; no new acquisition |
| `embedding_profile`, `chunk_embedding` | Named vector spaces and their vectors (migration 0015) |
| `graph_node`, `graph_edge`, `graph_node_chunk` | The Tier-1 hierarchy from migration 0018; write-only today |

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
| `eval_run` | Persisted eval scorecards (migration 0020) |

`query_log.route_json` records why each turn went the way it did (`reason`:
`multi_form`, `no_product`, `low_top_score`, `model_refusal`, `did_you_mean`,
`brand_lookup`, and so on), so an analyst or the eval harness can tell not just
that the system clarified or refused, but why. It also carries the gate's
per-claim ledger and, when route shadow is on, the shadow call's own record.
Token and cost columns are `NULL` when no LLM call happened or the provider
reported no usage, never a guessed number.

---

## 11. White Paper populator

`whitepaper/populator.py` is a higher-order feature built on the same retrieval
and structured-source machinery. Input: an RLD name plus an application number.

```
RLD name + appl_no
   |
   +- resolve a "spine" (the product identity) -- 422 SpineResolutionError on failure
   |
   +- for each of the 46 schema cells, by mode:
   |     * auto          -> deterministic fill, no LLM (counts, identifiers, dates)
   |     * evidence_only -> verbatim, cited FDA text (Orange Book / PSG / SPL rows)
   |     * manual        -> "analyst_input_required", never generated (INV-3)
   |
   +- write through Orange Book and SPL rows for durable provenance and freshness
   +- one whitepaper audit row, on success AND on a 422 resolution failure
```

- **Tri-state absence.** A blank cell, a "not found in FDA sources" and a "needs
  a human" are three distinct states, never collapsed.
- **The `.docx` render re-populates nothing.** `POST /whitepaper/docx`
  (`whitepaper/docx_writer.py`) renders the exact JSON the analyst already
  reviewed, with no live fetches and no LLM calls, after checking that
  `result.audit_id` belongs to the caller's own successful run. It validates the
  payload shape defensively: the `application_number` is interpolated into the
  `Content-Disposition` header, so anything looser than `[A-Z]{0,4}\d{6}` is
  rejected. It writes one `docx_rendered` audit row. When the Word template is
  absent (CI), the writer builds an equivalent document from scratch.

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

Scheduling has two deliberately separate control planes. GitHub Actions cron
`.github/workflows/watch-daily.yml` remains the sole scheduler for daily Watch
alerts. The private Dagster worker owns only the authoritative FDA replacement
corpus: canary, manifest, chunk shards, embedding shards, and acceptance. Its
weekly manifest schedule ships stopped and it has no automatic full-backfill or
retrieval-cutover schedule.

> **Operator action required, as of 2026-08-12.** The workflow is wired for the
> named Qwen profile and fails before checkout/crawl unless all six settings are
> present: `WATCH_ACTIVE_EMBEDDING_PROFILE`, base URL, token, model, revision and
> `WATCH_QWEN_EMBEDDING_DIMENSION`. Before ingest it runs the serving boot gate,
> which checks the registered immutable profile and its coverage/index readiness;
> after any attempted Watch run it independently requires zero pending chunks.
> Those six repository secrets are not provisioned yet. Until the owner sets
> them and verifies a manual dispatch, scheduled runs with a production database
> fail closed instead of silently writing outside the serving profile.
>
> Scheduled Watch now writes only the named Qwen profile. It deliberately does
> not refresh the legacy OpenAI vector column, so that arm needs a backfill before
> it can serve as a current-corpus embedding rollback.
>
> Historical context: the cron failed daily from 2026-08-07 through the morning
> of 2026-08-10, then its last observed pre-parity runs passed after
> `WATCH_DATABASE_URL` was updated. The first run of this workflow revision is
> expected to fail at profile preflight until the six secrets are provisioned.

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
- **Rate limiting.** Per user, 30 requests a minute (`RATE_LIMIT_PER_MINUTE`,
  `common/ratelimit.py`) on the expensive and outbound-FDA routes: `/query`,
  `/sources/search`, `/resolve`, `/assemble`, `/whitepaper`, `/whitepaper/docx`.
  The login brute-force guard, 10 per email per minute and 30 per IP per minute,
  runs in the Go proxy (`go/internal/api/ratelimit.go`). Limiters are in-memory
  and per process, so a multi-replica deploy needs distributed limiting at the
  gateway. That is open work, see [`ROADMAP.md`](ROADMAP.md).
- **Session ownership.** `/sessions/{id}`, `/feedback` and `/whitepaper/docx` all
  return 404, not 403, on a foreign or non-existent row, so the response never
  confirms that someone else's resource exists. Legacy NULL-owner sessions are
  adopted through a race-safe conditional `UPDATE` on the first authenticated
  `/query`; the loser of a race re-reads the committed owner and 404s.

---

## 15. Compliance invariants

The invariants are encoded in code and enforced by tests. They are the product's
regulatory differentiator.

| INV | Rule | Where enforced |
|---|---|---|
| INV-1 | **Grounding.** A stated FDA fact without a valid citation never reaches the analyst | `turn_gate`: an uncited source-fact claim is dropped, and a turn with nothing admitted refuses |
| INV-2 | **Refuse over guess.** Weak retrieval cannot become an answer | Top-1 score below 0.30 blocks the synthesizer and withholds the weak passages from the guidance planner too |
| INV-3 | **No regulatory judgment.** Never author strategy or classify paragraphs | Scope-warning guard; Orange Book tables store raw rows only; White Paper `manual` cells |
| INV-4 | **No fabrication.** A defensible change history | Versioned `psg_version`; watch digests and run log |
| INV-5 | **Verified provenance.** Every source is allowlisted | `ALLOWED_SOURCES`, `source` enums |
| INV-6 | **Audit everything.** One `query_log` row per turn, whatever the outcome | `log_query` in every branch |
| INV-7 to INV-9 | **Cross-drug and cross-form guards.** Never blend products or dosage forms | Pre- and post-retrieval product and form clarify guards |

The cross-drug and cross-form guards are doubled on purpose. The pre-retrieval
guard enumerates the product's current `(dosage_form, route)` combinations and
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
  `PROD_HEALTH_URL` GitHub secret for the CI smoke check.
- **Token and cost accounting:** `query_log.input_tokens`, `output_tokens` and
  `cost_usd` capture the turn's single Ask completion, synthesizer or guidance
  planner. `estimate_cost_usd` prices it from a settings table and leaves the
  column `NULL` rather than guessing. `latency_ms` (migration 0016) is the
  per-turn wall clock. Route-shadow usage lives under `route_json["route_call"]`
  and never replaces the turn totals.
- **Runbook:** rollback, restore drill and operations notes are in
  [`DEPLOY.md`](DEPLOY.md).
- **Docker:** the CRA template `.docx` is internal and gitignored, so it is never
  baked into an image. `WHITEPAPER_TEMPLATE_PATH` defaults to
  `/app/data/templates/cra_white_paper_template.docx` under the mounted data
  volume; an operator drops the file there per `DEPLOY.md`. Without it the writer
  falls back loudly and everything stays green.
- **Still open** (see [`ROADMAP.md`](ROADMAP.md)): exported request, latency and
  cost metrics; a real readiness probe covering database, vector store and LLM
  reachability; a Sentry DSN configured in prod; least-privilege database
  credentials; and a rehearsed restore drill.

---

## 17. Evaluation harness

`eval/` is the regression gate. It scores the system against a pinned gold set:

- `run_eval.py` and `metrics.py`: recall, citation precision, faithfulness,
  refusal accuracy and over-refusal for grounded Q&A.
- `whitepaper_metrics.py`: White Paper cell-level checks.

Faithfulness is kind-aware since PR #178: it measures the share of the gate's
admitted source-fact claims that carry a citation, and the older text-only rule
is still reported alongside it as `sentence_citation_rate`.

The blocking floors are **recall_at_k >= 0.80** and **citation_precision >= 0.74**
(`run_eval.py --check-thresholds`). They are a ratchet, not a quality bar: each
sits just below the first real measurement (eval run 1, 2026-08-05, on the active
Qwen3 profile), because the eval drives a live LLM and the numbers drift run to
run. The original 0.90 / 0.95 / 0.95 figures stay as aspirational targets,
reported beside each value and never blocking. `refusal_accuracy` is measured and
persisted but not gated: Ask is moving to a conversational layer that is not
meant to refuse, so failing a build for declining less often would be backwards.
A run that loses more than 10 percent of its turns to transport failures exits
without scoring, because it did not measure the system.

The committed gold sets are `gold_set.jsonl` (62 Q&A rows: 43 must-answer, 16
must-refuse, 3 must-clarify) and `whitepaper_gold.jsonl` (16 rows), scored
mechanically on `(short_name, page)` plus `expected_facts`. A deterministic
fixture also runs inside every `pytest`. [`EVAL_STATUS.md`](EVAL_STATUS.md) has
the evidence and the remaining gaps.

Open work ([`ROADMAP.md`](ROADMAP.md)): scored hard negatives and an LLM-as-judge
pass alongside the mechanical checks, then a provider-backed gate against a
controlled corpus snapshot. `answer_feedback` thumbs feed new candidate gold
items, closing the loop.

---

## 18. Configuration

One Pydantic `Settings` object (`config/settings.py`), fully `.env` driven.
Providers are pluggable (LLM: `databricks | openai | anthropic | echo`;
embeddings: `openai | qwen3 | local-bge-small | echo`). See
[`.env.example`](../.env.example) for the annotated surface. The load-bearing
knobs:

| Variable | Effect |
|---|---|
| `DATABASE_URL` | **Mandatory.** Postgres plus pgvector connection string; the app refuses to boot without it |
| `ACTIVE_EMBEDDING_PROFILE` / `EMBEDDING_SHADOW_PROFILE` | Which named profile serves retrieval, and which one is being populated for a blue/green re-embed. Prod runs `ep_2e7368b354d911ea3a013c3125e276c2` (Qwen3, 1024-dim) |
| `EMBEDDING_PROVIDER` | Only read on the `legacy` arm, so it no longer affects the production query path. `openai` (1536-dim) \| `qwen3` (OpenAI-compatible private endpoint) \| `local-bge-small` (384-dim, offline and eval tooling only) \| `echo` (test only) |
| `QWEN_EMBEDDING_BASE_URL` / `_TOKEN` / `_MODEL` / `_DIMENSION` / `_REVISION` | The in-tenant embedding endpoint the active profile calls |
| `LLM_PROVIDER` | `databricks` (prod: ONE endpoint, `DATABRICKS_LLM_MODEL`, serving every role) \| `openai` (role models below, the rollback path) \| `anthropic` \| `echo` (test only) |
| `DATABRICKS_LLM_BASE_URL` / `_TOKEN` / `_MODEL` | The in-tenant Model Serving endpoint. `_MODEL` has no default, because a placeholder would 404 every synthesis. Prod points at `workspace.default.regwatch`, which currently serves `gpt-oss-120b-080525` |
| `DATABRICKS_REASONING_EFFORT` (`low`) | Reasoning budget sent on every role. Prod runs `low` |
| `D1_ENFORCED` / `D1_ALLOWED_LLM_MODELS` | Residency tripwires: a boot guard against half-migrated config, plus a per-response served-model check. List BOTH the Unity Catalog alias and the served model id |
| `REGWATCH_PROSE_SYNTHESIS` / `REGWATCH_LIVE_DRAFT` / `REGWATCH_SELECTIVE_CITATION` | The v6 prose format, live draft streaming over SSE, and the v7 selective-citation policy. All three are on in prod |
| `ROUTER_MODEL` / `SYNTHESIZER_MODEL` / `EXTRACTOR_MODEL` | OpenAI role-specific models, used only on the OpenAI rollback path |
| `SYNTHESIZER_MAX_TOKENS` (3000) | Synthesis output cap, buffered and streamed alike. A reasoning model's thinking and its answer share it |
| `REGWATCH_ROUTE_CALL` (`off`) | Route and scope observation. `shadow` records one advisory call; `live` is reserved and still behaves as shadow |
| `REGWATCH_ROUTE_MAX_TOKENS` (1200) | Route-call budget. Probe the served reasoning floor before enabling shadow and keep the cap above it |
| `VECTOR_TOP_K` (50) / `RERANK_TOP_K` (8) / `RERANKER_ENABLED` (false) | Two-stage retrieval sizing |
| `REFUSAL_SCORE_THRESHOLD` (0.30) | The refuse-over-guess line (INV-2) |
| `AUTH_COOKIE_SECURE` / `AUTH_SESSION_TTL_HOURS` / `RATE_LIMIT_PER_MINUTE` | Auth and abuse controls |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` | Observability, off when unset |
| `API_PROXY_TARGET` (frontend, server side) | Where the Next.js proxy forwards `/api/*` |

`REGWATCH_ALLOW_TEST_PROVIDERS=1` is the only escape hatch that lets an `echo`
provider face a real corpus. Tests and CI only.

### The D1 residency guard

D1 asked one question: does an analyst's question ever leave the company
perimeter? Today it does not. Generation, query and corpus embeddings, and the
database all sit inside the company's Databricks tenant. D1 is closed.

The guard stays in the code, because a Unity Catalog alias can be repointed with
no deploy. Both halves of it are armed by `D1_ENFORCED`, and both are inert while
that is off:

- At boot, `D1_ENFORCED=true` refuses a half-migrated configuration. Generation
  on Databricks while query embedding still goes to OpenAI, or the reverse, leaks
  the question anyway.
- At runtime, every completion and stream is checked against
  `D1_ALLOWED_LLM_MODELS` using the model id the endpoint reports, not the name
  in config. The check also rejects partner-hosted families
  (`databricks-gpt*`, `databricks-claude*`, `databricks-gemini*`) even if someone
  allowlists them by hand, because those carry the partner's retention terms.
- An off-perimeter response raises a dedicated `D1ResidencyError`. The streaming
  path deliberately excludes it from the SSE fallback retry: a residency
  violation must never cause the question to be re-sent to the very endpoint the
  guard fences off.

The original threat model is archived at
[`archive/DATA_RESIDENCY_D1.md`](archive/DATA_RESIDENCY_D1.md).

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
