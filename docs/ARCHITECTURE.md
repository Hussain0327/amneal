# REGWATCH — System Design & Architecture

> An FDA regulatory-intelligence system for Amneal's generic-drug Clinical
> Regulatory Affairs (CRA) team. It **surfaces, organizes, compares, and cites**
> public FDA data — and is architecturally constrained to *never* author
> submission content or render regulatory judgment.

This document is the canonical description of how REGWATCH is built. It is meant
to be read top-to-bottom once, then used as a reference. Companion docs:
[`PROJECT_SPEC.md`](PROJECT_SPEC.md) (the requirements/spec), [`DEPLOY.md`](DEPLOY.md)
(the production runbook), [`whitepaper_schema.md`](whitepaper_schema.md) (the
46-cell White Paper schema), and [`DECISIONS.md`](DECISIONS.md) (decision log).

---

## Table of contents

1. [The prime directive](#1-the-prime-directive)
2. [Deployment topology](#2-deployment-topology)
3. [Product surfaces](#3-product-surfaces)
4. [Backend layout (the pipeline)](#4-backend-layout-the-pipeline)
5. [The API boundary](#5-the-api-boundary)
6. [The grounded Q&A engine](#6-the-grounded-qa-engine)
7. [Source layer (rules-first router + handlers)](#7-source-layer-rules-first-router--handlers)
8. [Ingest & processing pipeline](#8-ingest--processing-pipeline)
9. [Storage layer (dual-mode)](#9-storage-layer-dual-mode)
10. [Data model](#10-data-model)
11. [White Paper populator](#11-white-paper-populator)
12. [Dossier assembler](#12-dossier-assembler)
13. [Change detection & alerts](#13-change-detection--alerts)
14. [Authentication & sessions](#14-authentication--sessions)
15. [Compliance invariants](#15-compliance-invariants)
16. [Observability & ops](#16-observability--ops)
17. [Evaluation harness](#17-evaluation-harness)
18. [Configuration](#18-configuration)
19. [Design principles (summary)](#19-design-principles-summary)

---

## 1. The prime directive

The single most important design idea, from which almost everything else
follows:

> **Every claim is traceable to a retrieved FDA passage, or the system refuses.**

This is enforced at three independent layers — at retrieval (a score threshold),
at synthesis (citation validation against the exact passages sent to the LLM),
and in the test suite (the INV-1..9 invariants, §15). REGWATCH is best understood
as a RAG system where the *safety rails are first-class, tested invariants*, not
prompt-engineering hopes.

A second framing decision shapes the UX: responses are **not** an answer/refuse
binary. The system distinguishes five outcomes — `answer`, `summary`, `clarify`,
`scope_warning`, `refused` — so that when it knows the product but not the intent
it **guides with clickable options** instead of guessing.

---

## 2. Deployment topology

Three tiers, one browser-visible origin. This is the **target topology** the code
and runbook ([`DEPLOY.md`](DEPLOY.md)) are built for; the dual-mode storage path
(§9) is implemented and rehearsable, but managed Postgres/pgvector is **not yet
provisioned**, and a gateway/TLS/SSO front-door is still open work (see
`docs/ROADMAP.md`). Locally and in CI the stack runs entirely on SQLite + Chroma.

```
                         Browser (analyst)
                               │  HTTPS, HttpOnly session cookie
                               ▼
            ┌──────────────────────────────────────┐
            │  Vercel — Next.js 14 (App Router)       │   amneal.vercel.app
            │  server-side rewrite  /api/* → backend  │   FREE tier
            └───────────────────┬────────────────────┘
                               │  /api/:path*  →  API_PROXY_TARGET/:path*
                               ▼
            ┌──────────────────────────────────────┐
            │  Fly.io — FastAPI (uvicorn)             │   amneal.fly.dev
            │  2× shared-cpu-1x, 1024 MB              │   ~$0–6/mo
            └───────────────────┬────────────────────┘
                               │  SQLAlchemy / psycopg
                               ▼
            ┌──────────────────────────────────────┐
            │  Supabase — Postgres + pgvector         │   FREE tier
            │  structured rows + 1536-dim vectors     │   one DB, RLS deny-all
            │  in the SAME database                   │
            └──────────────────────────────────────┘
```

### Single-origin proxy

The browser only ever talks to the Next.js origin. `next.config.mjs` declares:

```js
async rewrites() {
  return [{ source: "/api/:path*", destination: `${API_PROXY_TARGET}/:path*` }];
}
```

Consequences of this choice:

- **No CORS in the browser path** — same-origin to the analyst; the backend's
  CORS middleware exists only as defense-in-depth for the credentialed cookie.
- **No public API URL to configure** in the client bundle.
- **One tunnel/origin** exposes the whole app (the original cloudflared-over-:3000
  story; now Vercel + Fly).
- `API_PROXY_TARGET` is a *server-side* env var (defaults to `http://127.0.0.1:8000`
  for local dev — pinned to IPv4 so Node doesn't waste a failed `::1` attempt per
  request).

### Environments

| Concern | Local / dev / CI | Production |
|---|---|---|
| Structured store | SQLite (`data/regwatch.db`) | Postgres (Supabase) |
| Vector store | Chroma (`data/chroma`) | pgvector (same Postgres DB) |
| Embeddings | local `bge-small` (384-dim) | OpenAI `text-embedding-3-small` (1536-dim) |
| Selector | `DATABASE_URL` **empty** | `DATABASE_URL` **set** |

The entire stack swap is driven by one variable — see §9.

---

## 3. Product surfaces

The frontend (`regwatch/frontend/`) is a deliberately thin Next.js 14 App Router
app. All intelligence and all secrets live server-side. `lib/api.ts` is the fetch
wrapper; `lib/turns.ts` models a conversation turn. (Streamlit is fully retired;
this Next.js app is the only UI.)

All **four** product surfaces (Ask, Assemble, Watch, White Paper) render inside a
single `app/(shell)/` App Router route-group — **one sidebar, one canvas, one set
of design tokens, one scoped-product context**. The bare routes (`login`,
`fixtures`) sit *outside* the group and never inherit the shell.

| Route (`app/`) | Backend endpoint(s) | Purpose |
|---|---|---|
| `(shell)/page.tsx` | `POST /query` | Cited conversational **chat** + per-user history sidebar |
| `(shell)/assemble/page.tsx` | `POST /assemble` | Cited dossier for a target product |
| `(shell)/whitepaper/page.tsx` | `POST /whitepaper`, `POST /whitepaper/docx` | CRA White Paper populator → filled `.docx` |
| `(shell)/watch/page.tsx` | `GET /watch/latest` | Recent change-detection alerts |
| `login/page.tsx` | `POST /auth/login` | Cookie-session login gate (outside the shell) |
| `fixtures/page.tsx` | (static) | Demo/fixture inputs for testing (outside the shell) |

The **Ask** surface is a cited conversational chat, not the old editorial
document/ledger cards: right-aligned user bubbles, a gold RW assistant avatar,
citation chips that link to the FDA sources (full snippets behind a Sources
disclosure), clarify-option pills, a bottom-pinned composer, and Enter-to-send.

### URL-scoped CurrentProduct + the "Under review" scope bar

A single reference product is scoped across all four surfaces and mirrored into
the URL query (`?rp=<reference product name>&appl=<application number>`), so the
scope is **shareable and survives reload**. `components/CurrentProductProvider.tsx`
is the state of record (the URL itself); every surface reads it, and any surface
that re-scopes rewrites *only* the `rp`/`appl` params (never the Ask page's
`session`, so scoping never drops an open conversation).

`components/ProductScopeBar.tsx` is a slim sticky **"Under review"** strip across
the top of every surface (it replaced the old sidebar product badge) and is the
**front-door SETTER** for the whole pipeline. Pinning runs the same deterministic
resolve the White Paper uses — `POST /resolve` (§5) — so the scope is always the
canonical `{normalized_name, six-digit application number}`; a 422 leaves it
**unset** and shows the resolver's explanation verbatim (refuse over guess).

The scope is settable from **three** surfaces, all writing the same canonical
pair: the bar's picker, the White Paper on a successful populate, and a Watch row.

Cross-cutting frontend pieces: `app/layout.tsx` (the `AuthProvider` gate +
fonts), `app/(shell)/layout.tsx` (sidebar + canvas + scope bar + the history
sidebar via `SessionsProvider`/`CurrentProductProvider`), `app/icon.svg` (Amneal
favicon), `app/global-error.tsx` + `sentry.*.config.ts` (Sentry, off unless
`NEXT_PUBLIC_SENTRY_DSN` is set).

---

## 4. Backend layout (the pipeline)

The backend (`src/regwatch/`) is organized as a classic RAG data pipeline. Each
package is one stage with a clean boundary:

```
 sources/   →   ingest/   →   process/   →   store/   →   retrieve/   →   generate/
 (FDA APIs:     (crawl PSG    (chunk,        (Postgres   (embed query,    (grounded LLM
  7 handlers)    listings,     embed,         + pgvector   vector top-k,    synthesis +
                 parse PDFs)   extract BE)    OR SQLite    rerank, scope    citation
                                             + Chroma)     to product)      validation)

 watch/      change detection + digests/alerts over the same store
 assemble/   higher-order composer: cited dossier (built on retrieve + generate)
 whitepaper/ higher-order composer: 46-cell White Paper + .docx writer
 auth/       cookie sessions, bcrypt passwords, require_user dependency
 common/     audit, citations, conversation memory, ratelimit, observability, logging
 eval/       offline gold-set metrics (the regression gate)
 orchestration/  Dagster definitions (configured but DORMANT)
 api/        the FastAPI surface — the one boundary IT will wrap or replace
 cli.py      operator commands (seed, ingest-all, create-user, watch, …)
```

`config/` (a sibling top-level package, imported as `config.settings`) holds the
single Pydantic `Settings` object that every module reads.

---

## 5. The API boundary

`api/main.py` is the contract boundary — the clean surface the IT/AI team will
wrap or replace. Every response is reproducible in Postman from a `.env` and a
running instance.

### One authorization chokepoint

There are exactly two ways for a request to skip auth: `GET /health` and the
`/auth/*` routes. **Everything else** is registered on a single router with a
router-level dependency:

```python
auth_router = APIRouter(prefix="/auth", tags=["auth"])
protected   = APIRouter(dependencies=[Depends(require_user)])
```

This makes an accidentally-unauthenticated route *structurally* impossible — you
cannot add an endpoint to `protected` without inheriting `require_user`. FastAPI's
interactive docs (`/docs`, `/redoc`, `/openapi.json`) are disabled so the API
surface (every route, schema, and the cookie name) is not disclosed to anonymous
visitors through the proxy.

### Endpoint map

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/login` | open | Issue the `regwatch_session` cookie |
| POST | `/auth/logout` | open | Revoke server-side session + clear cookie |
| GET | `/auth/me` | ✅ | Current user |
| POST | `/query` | ✅ | Grounded Q&A (the conversational engine) |
| POST | `/feedback` | ✅ | Thumbs up/down on one of the caller's own answers |
| POST | `/sources/search` | ✅ | Structured FDA source lookup |
| POST | `/resolve` | ✅ | Deterministic entity resolution (RLD + appl no) → canonical spine; pins the scope bar without a populate |
| POST | `/assemble` | ✅ | Build a cited dossier |
| POST | `/whitepaper` | ✅ | Populate the CRA White Paper (RLD + appl no) |
| POST | `/whitepaper/docx` | ✅ | Render a returned White Paper result as `.docx` |
| GET | `/watch/latest` | ✅ | Recent alerts (optional `since` filter) |
| GET / POST | `/products` | ✅ | List / add watchlist products |
| GET | `/sessions` | ✅ | The caller's chat sessions |
| GET / DELETE | `/sessions/{id}` | ✅ | One session with messages / delete it |
| GET | `/settings` | ✅ | Non-secret config |
| GET | `/health` | open | Liveness + component diagnostics |

### `POST /resolve` is deliberately not an LLM turn

`/resolve` reuses the White Paper's spine resolver (`resolve_spine`, which calls
the same `_build_context`) to map an RLD name + application number to the
canonical `{normalized_name, application_number}` spine that the scope bar pins.
Because it is **deterministic entity resolution, not a synthesis turn**, it:

- writes **no `query_log` audit row** on either success or failure (nothing was
  answered — there is no "turn" to audit),
- returns **no answer text**, only the resolved spine, and
- **422s with the resolver's own detail** (and leaves the scope unset) when the
  pair doesn't resolve or the application doesn't match — refuse over guess.

It *is* rate-limited like `/query` / `/assemble` / `/whitepaper`, because it hits
live FDA sources the same way they do.

### Boot sequence (`_lifespan`)

1. **Sentry first** — initialized before `init_db` so a refused boot (e.g. a
   provider/dimension mismatch) is captured.
2. `init_db()` unless `REGWATCH_DB_INITIALIZED=1` (set when a Docker entrypoint
   ran `regwatch init-db` out-of-process); in that case the dimension fail-fast
   (`assert_embedding_provider_dim`) is re-asserted in-process anyway.
3. `_guard_test_providers` — refuse to boot an `echo` provider against a
   non-empty corpus (retrieval would silently degrade while citations still
   validate). An empty corpus is allowed (fresh checkout, pre-seed Docker boot).
4. `assert_embedding_runtime_available` — a provider whose runtime deps are
   missing (slim image + `local-bge-small`) must refuse to boot, not 500 on the
   first embed call.

### `/health` semantics

Returns a superset of `{"status": "ok"}` diagnosing db, vector store, LLM key
presence, and embedding provider. **503 only** when db or vector store is
unreachable. An empty corpus is *healthy with a warning*, so a fresh stack can
boot and the ingest service can seed.

---

## 6. The grounded Q&A engine

This is the architectural heart: `generate/grounded_qa.py`, function `ask()`. It
is **not** "embed → LLM → return." It is a deterministic decision pipeline in
which the LLM is the *last* resort and every branch writes exactly one audit row.

### Flow

```
question
   │
   ├─ scope-warning phrases? ──────────────────► REFUSE  (won't author strategy/judgment, INV-3)
   │
   ├─ ENTITY RESOLUTION FIRST  (resolve_product)        ← pin the product BEFORE semantic search
   │     • resolved   → pin normalized_name (canonicalized)
   │     • ambiguous  → CLARIFY  (which of these products?)
   │     • none       → did-you-mean (typo)? → brand→generic? → else REFUSE (no_product)
   │                    …or carry product from session if this looks like a follow-up
   │
   ├─ vague input?  ("Hello" + a drug filter, bare drug name) ──► CLARIFY with question templates
   │
   ├─ MULTI-FORM GUARD (pre-retrieval): drug spans >1 (dosage_form, route)?
   │     • question names the form unambiguously → pin it, proceed
   │     • else                                  → CLARIFY (which form?)
   │
   ├─ Stage 1: vector top-k   (VECTOR_TOP_K = 50), scoped to product (+ form)
   ├─ Stage 2: rerank → trim  (RERANK_TOP_K = 8;  reranker off by default → just a slice)
   │
   ├─ top-1 score < REFUSAL_SCORE_THRESHOLD (0.30)? ──► REFUSE before any LLM call (INV-2)
   │
   ├─ POST-RETRIEVAL GUARDS (defense in depth):
   │     • passages span >1 product? ───► CLARIFY
   │     • passages span >1 form?     ───► CLARIFY
   │
   ├─ LLM synthesis  (temperature 0.0, strict grounding system prompt, max 900 tok)
   │
   ├─ LLM returned the refusal sentinel? ──► REFUSE  (or CLARIFY if a drug was named by the user)
   ├─ validate every [short_name, p.N] citation against the passages actually sent
   ├─ body text but ZERO valid citations? ──► REFUSE  (INV-1: no ungrounded answers)
   └─ strip any fabricated citation markers from the prose ──► ANSWER (with verified citations)
```

### Why entity resolution precedes retrieval

FDA PSG documents share a lot of template boilerplate across drugs. If you let
pure vector ANN pick passages first, a generic boilerplate paragraph from the
*wrong* drug can score well and be cited as if it answered the question — and the
blend is invisible because citation labels are application-number-only
(`PSG_020503`). So the product is pinned *first*; retrieval then becomes a
**B-tree-filtered exact match on `normalized_name`** plus distance ranking, not
open-field ANN. This is also why per-drug queries don't need HNSW (§9).

### Current-version scoping

`retrieve()` (`retrieve/retriever.py`) does more than embed-and-search. Before
querying the vector store it computes the set of **current** `psg_version` ids for
the filtered documents (via `_current_version_ids_for_filters`) and constrains the
search to them, so a superseded chunk can never be cited even if it's still in the
index. (A pure vector-only mode — used by some unit tests that seed Chroma without
a SQLite catalog — is detected and skipped.)

### Conversation memory

`common/conversation.py` persists per-session filters (`ChatSession.active_filters_json`)
— the resolved product and any chosen `(dosage_form, route)`. A follow-up like
"What about dissolution?" inherits that context (`_looks_like_follow_up` +
`get_session_filters`), so it doesn't re-trigger product or form clarification.
Conversation memory is *never* treated as FDA evidence — only as deterministic
routing context.

### The five response states

| `status` | Meaning | Citations |
|---|---|---|
| `answer` | Grounded answer | ✅ validated |
| `summary` | Grounded answer to a "summarize/overview" request | ✅ validated |
| `clarify` | Product/form known-or-near but intent unclear → offer options | none (never fabricates) |
| `scope_warning` | Asked for strategy/judgment → explain the boundary | none |
| `refused` | Can't ground it (no product, low score, no valid citations) | none |

Every state runs through `_finish_turn`, which records the assistant message and
(on answerable states) updates the session's product filter.

### No real streaming yet (by design, for now)

The Ask chat client targets `/query/stream` (SSE) but **transparently falls back
to a blocking `POST /query`** — and the backend has **no `/query/stream` endpoint
today**, so every Ask turn is a single blocking call. The "thinking" ticker is
honest (real client-side phase labels), not a faked token stream. Real
token-by-token streaming is open work (`docs/ROADMAP.md`): it must still respect
INV-1 (no answer text emitted before a validated citation) and write exactly one
audit row per turn.

---

## 7. Source layer (rules-first router + handlers)

`sources/router.py` is a **deterministic, rules-first** router (regex + keyword
matching, no LLM) that fans a `SourceQuery` out to seven FDA source handlers
behind a common `SourceHandler` interface (Strategy pattern).

```
SourceQuery ─► route_sources()  (NDC pattern? "rems"? appl_no? "orange book"? …)
                   │   default when nothing matches: [DRUGSFDA, ORANGE_BOOK, PSG]
                   ▼
            for each routed source:  handler.search()   ← failures logged per-source, not fatal
                   │
                   ▼
            (routed_sources, list[SourceRecord])
```

The seven handlers (`sources/`):

| Handler | FDA source |
|---|---|
| `PsgHandler` | Product-Specific Guidance (the local indexed corpus) |
| `OrangeBookHandler` | Orange Book (TE codes, RLD/RS, patents, exclusivity) |
| `DrugsFdaHandler` | Drugs@FDA (approvals, sponsors) |
| `NdcHandler` | National Drug Code directory |
| `RemsHandler` | Risk Evaluation & Mitigation Strategies |
| `DailyMedHandler` | DailyMed SPL labeling |
| `ShortagesHandler` | Drug shortages / availability |

Adding an FDA source = one new handler + one routing rule. A single flaky FDA
endpoint never takes down a search — `search_sources` catches and logs per-source
exceptions and returns whatever the other handlers produced.

---

## 8. Ingest & processing pipeline

Offline/batch path that fills the stores (run via `regwatch` CLI commands and,
optionally, Dagster).

```
psg_crawler  →  pdf_parser  →  chunker  →  embedder  →  store.add_chunks
(A–Z letter     (extract       (split     (bge-small    (Chroma or pgvector)
 routes →        text +         into        OR OpenAI       +
 ~1,795 PSGs)    pages)         passages)   1536-dim)   psg_document/version rows

change_detector → content_hash compare → new PsgVersion on change → re-embed,
                  delete superseded chunks (delete_chunks_for_doc_except_version)

extractor → per-PSG BE requirement extraction (study type/design, dissolution,
            strengths, waivers) → BeRequirement rows, each field carrying a citation
```

Key properties:

- **Versioned, not overwritten.** A PSG content change creates a new
  `PsgVersion`; old chunks are removed from the *search index* but the version
  history stays in the structured store (INV-4: defensible "what changed when").
- **The full catalog** (~1,795 PSGs) is reachable via A–Z letter routes
  (`fetch_all_listings` / `ingest-all`), not just the ~70 the FDA landing index
  shows.
- **Known concurrency note:** the multi-worker ingest path has a small alembic
  init race (~8% transient "No context configured" errors) that is harmless — a
  single-threaded pass sweeps the remainder. Tracked as a future fix.

---

## 9. Storage layer (dual-mode)

The cleverest abstraction in the codebase. **One environment variable swaps the
entire persistence stack**, and no caller changes.

```
                         DATABASE_URL
                        ┌──────┴───────┐
                  empty │              │ set
                        ▼              ▼
        SQLite (structured) +    Postgres (structured) +
        Chroma (vectors)         pgvector (vectors, SAME DB)
```

- `store/vector_store.py` is a **facade**. Every public function
  (`similarity_search`, `add_chunks`, `collection_size`,
  `distinct_metadata_values`, `delete_chunks_for_doc_except_version`) dispatches
  on `_pg_mode()` (a non-empty `DATABASE_URL`) to either Chroma or
  `store/pgvector_store.py`. Callers — retriever, ingest, watch, resolver, API
  health — never know which backend is live.

- **Score-convention parity is the linchpin.** Both backends return
  `Hit.score` as cosine similarity in `[0, 1]`, computed as
  `score = 1 - cosine_distance / 2` (1.0 identical, 0.5 orthogonal, 0.0 opposite).
  Because the score means the same thing on both backends, the **refusal
  threshold (0.30) is calibrated once** and behaves identically in dev and prod.
  This is the #1 migration risk and is why the SQLite→Supabase cutover must be
  rehearsed against a snapshot before going live (that restore drill is open work
  — see `docs/ROADMAP.md`).

- **Embedding provider is paired to the vector dimension.** Production uses
  OpenAI `text-embedding-3-small` (1536-dim) — chosen over local `bge-small`
  (384-dim) specifically so the API image sheds torch and fits a cheap host. The
  pgvector chunk column is `vector(1536)`; startup **fails fast** on a
  provider/table dimension mismatch.

- **pgvector index strategy:** per-drug queries (the common path) use a B-tree
  filter on `normalized_name` + exact distance — which beats HNSW for filtered
  search; HNSW is for unfiltered queries. Supavisor transaction-mode pooling;
  Supabase session-pooler URI (port 5432).

- **RLS:** deny-all on every public table. The FDA corpus is org-shared by
  design, so per-user RLS only ever applies to chat tables as defense-in-depth.

- **JSON columns** become `JSONB` on Postgres and plain `JSON` on SQLite via a
  single `_json_column()` helper (`with_variant`), so the same Python types work
  on both dialects.

- **Schema bootstrap:** a fresh Postgres database is created via `create_all` +
  `alembic stamp head` (no history replay — the migration history 0001–0008 is
  SQLite-oriented, including SQLite-batch ops). SQLite/dev uses the normal alembic
  upgrade path. Constraints and composite indexes are declared in SQLModel
  `__table_args__` so `create_all`, alembic autogenerate, and the hand-written
  migrations all agree.

---

## 10. Data model

`store/models.py` — SQLModel definitions; eight Alembic migrations (0001–0008).
Embeddings live in the vector store; everything below is the structured store.

### Corpus & evidence

| Table | Role |
|---|---|
| `product` | A target product on the company watchlist (INV-5: verified sources only) |
| `psg_document` | A PSG as currently published by FDA (unique `appl_no`, `content_hash`) |
| `psg_version` | A captured version of a PSG; new row on every content change |
| `be_requirement` | Extracted BE requirements per PSG version; **every field carries a citation** |
| `ob_product` | Orange Book `Products.txt` row (White-Paper provenance; raw rows only, INV-3) |
| `ob_patent` | Orange Book `patent.txt` row (raw rows only) |
| `ob_exclusivity` | Orange Book `exclusivity.txt` row (raw rows only) |
| `spl_document` | DailyMed SPL document resolution (White-Paper provenance) |

The Orange Book tables store **raw rows only** — paragraph classification and
eligibility are never persisted (INV-3, no regulatory judgment).

### Conversation & audit

| Table | Role |
|---|---|
| `user` | Authenticated analyst; `email` unique+lowercased, bcrypt `password_hash`, role analyst/admin. CLI-provisioned, no self-signup |
| `auth_session` | Server-side login session; only the **sha256** of the cookie token is stored |
| `chat_session` | A durable conversation thread; `active_filters_json` holds deterministic routing context. Composite index `(user_id, updated_at)` for the sidebar |
| `chat_message` | One user/assistant turn; carries role, content, status, citations, audit_id |
| `query_log` | **The audit spine** (INV-6): every turn's mode, query, retrieved set, answer, citations, refused flag, status, `route_json`, model, and token/cost columns |
| `answer_feedback` | Thumbs up/down; one row per `(audit_id, user_id)`, re-rating replaces; CHECK `rating IN (-1,1)`. Candidate pool for future eval gold items |

`query_log.route_json` records *why* each turn went the way it did
(`reason`: `multi_form`, `no_product`, `low_top_score`, `model_refusal`,
`did_you_mean`, `brand_lookup`, …) — so an analyst or the eval harness can tell
not just *that* the system clarified/refused but *why*. Token/cost columns are
`NULL` when no LLM call happened or the provider reported no usage — never a
guessed number.

---

## 11. White Paper populator

`whitepaper/populator.py` is a higher-order feature built on the same retrieval
and structured-source machinery. Input: an **RLD name + application number**.

```
RLD name + appl_no
   │
   ├─ resolve a "spine" (the product identity) ── 422 SpineResolutionError on failure
   │
   ├─ for each of the 46 schema cells, by MODE:
   │     • auto          → deterministic fill, NO LLM   (counts, identifiers, dates)
   │     • evidence_only → verbatim, CITED FDA text     (Orange Book / PSG / SPL rows)
   │     • manual        → "analyst_input_required"     (NEVER generated, INV-3)
   │
   ├─ write-through Orange Book / SPL rows for durable provenance + freshness
   └─ one whitepaper audit row (on success AND on a 422 resolution failure)
```

- **Tri-state absence.** A blank cell, a "not found in FDA sources," and a "needs
  a human" are three *distinct* states — never collapsed.
- **The `.docx` render does zero re-population.** `POST /whitepaper/docx`
  (`whitepaper/docx_writer.py`) renders the **exact JSON** the analyst already
  reviewed — no live fetches, no LLM calls — after verifying `result.audit_id` is
  the caller's own successful White Paper run. It validates the payload shape
  defensively (the `application_number` is interpolated into the
  `Content-Disposition` header, so anything looser than `[A-Z]{0,4}\d{6}` is
  rejected). It writes one lightweight `docx_rendered` audit row.
- When the Word template is absent (CI), the writer builds an equivalent document
  from scratch.

The 46-cell schema is normative — see [`whitepaper_schema.md`](whitepaper_schema.md).

---

## 12. Dossier assembler

`assemble/dossier.py` (`POST /assemble`) composes a cited dossier for a target
product (active ingredient + optional dosage form + RLD). It reuses the grounded
Q&A engine internally with `bind_session=False` so its synthetic Q&A turns are
audited (INV-6) for attribution but **do not** appear in the analyst's chat
history sidebar. Output is markdown + structured sections + a refusal flag.

---

## 13. Change detection & alerts

`watch/` runs the change-detection loop over the same store:

- `change_detector` compares `content_hash` to detect revised PSGs and writes new
  `PsgVersion` rows with a cited `diff_summary`.
- `matcher` / `aliases` / `watchlist` map FDA records to the company watchlist
  (Amneal applicant aliases).
- `alerts` produces digest records surfaced by `GET /watch/latest` (and the
  `watch/` UI). The digest run history doubles as INV-4 evidence ("nothing was
  fabricated; here's the run log").

Scheduling: `orchestration/definitions.py` defines Dagster assets, but Dagster is
currently **dormant** — the scheduler can live anywhere once data is in Supabase
(cron on the deploy host running `regwatch watch`, a GitHub Actions scheduled
workflow against the remote DB, or Dagster if the job count grows). That choice
is tied to the deploy path and is deliberately deferred.

---

## 14. Authentication & sessions

`auth/` — cookie-session auth, designed so a future SSO swap touches one thin
boundary.

- **Login.** `POST /auth/login` verifies a bcrypt password and issues an opaque
  token in an **HttpOnly, SameSite=Lax** cookie (`regwatch_session`); `secure` is
  driven by `AUTH_COOKIE_SECURE` (true in prod over TLS). The DB stores only the
  **sha256** of the token (`auth_session.token_hash`). A fresh session row is
  created on every login (no session fixation).
- **Uniform-timing auth.** `authenticate()` burns a bcrypt verify in every branch
  (unknown email / wrong password / inactive user) and returns one message, so
  timing can't enumerate accounts.
- **No self-signup.** Accounts are provisioned via `regwatch create-user`.
- **`require_user` is the swap boundary.** Every protected route depends on it;
  swapping cookie-lookup for JWT/JWKS (Microsoft/Entra SSO, the planned
  fast-follow) is a localized change.
- **Rate limiting** (`common/ratelimit.py`): per-user **30/min**
  (`RATE_LIMIT_PER_MINUTE`) on the expensive / outbound-FDA routes — `/query`,
  `/sources/search`, `/resolve`, `/assemble`, `/whitepaper`, `/whitepaper/docx`;
  a separate **10/email/min** login brute-force guard. The limiter is in-memory
  / per-process, so a multi-replica deploy needs distributed (gateway) limiting —
  open work, see `docs/ROADMAP.md`.
- **Session ownership.** `/sessions/{id}` and `/feedback` and `/whitepaper/docx`
  all return **404, not 403**, on a foreign or non-existent row — the response
  never confirms that someone else's resource exists. Legacy NULL-owner sessions
  are adopted via a race-safe conditional `UPDATE` on first authenticated
  `/query` (the loser of a race re-reads the committed owner and 404s).

---

## 15. Compliance invariants

REGWATCH is governed by invariants encoded in code **and** enforced by tests.
They are the product's regulatory differentiator.

| INV | Rule | Where enforced |
|---|---|---|
| INV-1 | **Grounding** — no answer without a valid citation | `_validate_citations`, "body text but no citations → refuse" |
| INV-2 | **Refuse over guess** — weak retrieval refuses *before* the LLM | `top-1 score < 0.30` check |
| INV-3 | **No regulatory judgment** — never author strategy / classify paragraphs | scope-warning guard; OB tables store raw rows only; White-Paper `manual` cells |
| INV-4 | **No fabrication** — defensible change history | versioned `psg_version`; watch digests |
| INV-5 | **Verified provenance** — every source allowlisted | `ALLOWED_SOURCES`; `source` enums |
| INV-6 | **Audit everything** — one `query_log` row per turn, every outcome | `log_query` in every branch (answer/clarify/refuse/scope) |
| INV-7..9 | **Cross-drug / cross-form guards** — never blend products or dosage forms | pre- *and* post-retrieval product/form clarify guards |

The cross-drug/cross-form guards are doubled on purpose: a pre-retrieval guard
(enumerate the product's current `(dosage_form, route)` combos and clarify if
>1), and a post-retrieval guard (if the returned passages span >1 product or >1
form, clarify) that backstops any caller who bypassed the resolver.

---

## 16. Observability & ops

- **Sentry** (`common/observability.py`, `init_sentry`): on API + frontend,
  **off unless `SENTRY_DSN` / `NEXT_PUBLIC_SENTRY_DSN` is set**. PII-scrubbed —
  `include_local_variables=False` plus a `before_send` scrubber — so question and
  answer text never leave the system. Initialized before `init_db` so a refused
  boot is still captured. Frontend Sentry has source-map upload, replay, and
  tunneling explicitly disabled (no org/auth-token plumbing in the repo).
- **Health/uptime:** external pinger on `GET /health`; `PROD_HEALTH_URL` GitHub
  secret for CI smoke.
- **Token/cost accounting:** `query_log.input_tokens` / `output_tokens` /
  `cost_usd` capture the dominant (synthesizer) LLM call; `estimate_cost_usd`
  prices it from a settings table, leaving the column `NULL` rather than guessing.
- **Runbook:** rollback one-liner, restore drill, and operations notes live in
  [`DEPLOY.md`](DEPLOY.md) §6.
- **Docker:** the CRA template `.docx` is gitignored (internal), so it is never
  baked in. `WHITEPAPER_TEMPLATE_PATH` defaults to
  `/app/data/templates/cra_white_paper_template.docx` under the mounted data
  volume; an operator drops the file there (or, on Fly, bakes a private overlay)
  per `DEPLOY.md`. Absent it the writer falls back loudly — CI's docker build and
  `/whitepaper/docx` both stay green either way.
- **Open observability work** (`docs/ROADMAP.md`): exporting request/latency/cost
  metrics, a real readiness probe (DB + vector store + LLM reachability — distinct
  from `/health`'s liveness), and a Sentry DSN actually configured in prod.

---

## 17. Evaluation harness

`eval/` is the regression gate. It scores the system against a pinned gold set:

- `run_eval.py` / `metrics.py` — recall, citation precision, refusal accuracy,
  over-refusal rate for grounded Q&A.
- `whitepaper_metrics.py` — White Paper cell-level checks.

The CI gate holds **recall@8 ≥ 0.90, citation_precision ≥ 0.95,
refusal_accuracy ≥ 0.95** (`run_eval.py --check-thresholds`; a deterministic eval
gate also runs inside `pytest`). Measured calibration currently clears these
comfortably (recall/precision at 1.0, zero over-refusals). (One known
gold-semantics item — an absent product now drawing a *safe* brand-suggestion
clarify on the full corpus instead of a hard refuse — is a stale-gold fix, not a
regression: the gold set should accept refuse-OR-clarify for absent products.)

The current gold set is `gold_set.jsonl` (26 Q&A items) + `whitepaper_gold.jsonl`
(21 White-Paper rows), scored mechanically on `(short_name, page)` +
`expected_facts`. Open work (`docs/ROADMAP.md`): grow the gold set toward 30–50
and add an LLM-as-judge pass alongside the mechanical checks.

`answer_feedback` thumbs feed new candidate gold items, closing the loop.

---

## 18. Configuration

A single Pydantic `Settings` (`config/settings.py`), fully `.env`-driven. Providers
are pluggable (`openai | anthropic | echo`); see [`.env.example`](../.env.example)
for the annotated surface. The load-bearing knobs:

| Variable | Effect |
|---|---|
| `DATABASE_URL` | **The dual-mode switch** — empty = SQLite+Chroma; set = Postgres+pgvector |
| `EMBEDDING_PROVIDER` | `local-bge-small` (384-dim) \| `openai` (1536-dim, required in PG mode) \| `echo` |
| `LLM_PROVIDER` + `ROUTER_MODEL` / `SYNTHESIZER_MODEL` / `EXTRACTOR_MODEL` | Role-specific models (cheap classifier, capable synthesizer/extractor) |
| `VECTOR_TOP_K` (50) / `RERANK_TOP_K` (8) / `RERANKER_ENABLED` (false) | Two-stage retrieval sizing |
| `REFUSAL_SCORE_THRESHOLD` (0.30) | The refuse-over-guess line (INV-2) |
| `AUTH_COOKIE_SECURE` / `AUTH_SESSION_TTL_HOURS` / `RATE_LIMIT_PER_MINUTE` | Auth + abuse controls |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` | Observability (off when unset) |
| `API_PROXY_TARGET` (frontend, server-side) | Where the Next.js proxy forwards `/api/*` |

`REGWATCH_ALLOW_TEST_PROVIDERS=1` is the only escape hatch that lets an `echo`
provider face a real corpus (tests/CI only).

---

## 19. Design principles (summary)

1. **Refuse-or-cite is the prime directive.** Enforced at retrieval (score
   threshold), at synthesis (citation validation), and in tests (invariants).
2. **Deterministic before probabilistic.** Entity resolution, source routing,
   and clarify logic are rules-based and unit-testable; the LLM only synthesizes
   from pre-vetted, product-scoped passages.
3. **One toggle, two worlds.** `DATABASE_URL` swaps SQLite+Chroma ↔
   Postgres+pgvector with score-convention parity, so dev/test never touch the
   cloud and prod behaves identically.
4. **One auth chokepoint, one audit spine, one contract boundary.** Security
   (`require_user` router), traceability (`query_log`), and the IT handoff
   (`api/main.py`) each have exactly one place to reason about.
5. **Thin client, fat server, single origin.** All intelligence and all secrets
   stay behind the proxy; the browser sees one origin and a cookie.
6. **Versioned evidence, never overwritten.** PSGs are captured as versions; the
   search index holds the current answer while history stays auditable.
```
