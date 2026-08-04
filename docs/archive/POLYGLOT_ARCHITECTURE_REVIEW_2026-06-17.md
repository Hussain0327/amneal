# Polyglot Architecture Review — 2026-06-17

> **ARCHIVED** — the "no new language" verdict was **overruled** on 2026-07-10 by
> `docs/POLYGLOT_TARGET_2026-07-10.md` as owner-level architecture policy, not on
> measured performance. Retained because the §3 adopt-IF thresholds and the §6
> four-gate framework are still the bar any future runtime must clear.

**Question asked:** "Should RegWatch become more polyglot — where and what languages, for better system architecture and design?"

**Method:** Grounded multi-agent analysis — 6 subsystem mappers read the actual code, each candidate "seam" was evaluated by a language *advocate* and a keep-it-in-Python *skeptic*, then reconciled by a lead architect. Findings are tied to real files/subsystems.

---

## 1. Direct answer

**No — this system does not need a new language right now, and adding one would make the architecture *worse*, not better.**

The honest reading of the evidence:

- RegWatch is **overwhelmingly I/O-bound** (network crawl + LLM API calls + Postgres). The classic reasons to reach for Go/Rust — CPU throughput, massive concurrency, sub-ms latency — barely apply.
- It is **already healthily polyglot**: **Python** owns logic/ingest/LLM orchestration, **TypeScript** owns the UI + SSE consumer, **SQL/pgvector** owns retrieval and durability. That's the right split for this domain.
- It's maintained by a **1–2 person team over ~15k LOC** behind a 4-job CI gate and a 3-platform deploy (Fly + Vercel + Supabase). A 4th language multiplies CI, deploy surface, secrets, monitoring, cross-language contracts, and cognitive load.

Every seam that *looked* like a polyglot candidate fails on inspection:

| "Driver" | Reality |
|---|---|
| Crawler **concurrency** | **Illusory.** The throttle is a deliberate 250 ms/request politeness lock (`psg_crawler.py:_polite_pause`) + FDA/Akamai bot protection. No language fetches faster than a rate limit *you chose*. And you're not even using Python's async yet — `crawl_concurrency=4` (`settings.py:214`) is **dead config**. |
| PDF/HTML parse **memory-safety** | **Absent.** Inputs are first-party FDA PDFs, parsed in an *offline batch* isolated from the API; both engines fail gracefully; HTML is already a C parser (selectolax); the one untrusted vector (DailyMed SPL XML) is already XXE-mitigated in pure Python (`dailymed._safe_fromstring`). |
| LLM/embedding/reranker **CPU** | **Delegated.** Embeddings + synthesis are remote API calls in prod; torch is deliberately excluded from the prod image. A compiled language can't speed up a network round-trip. |
| Streaming **latency** | **Policy, not runtime.** INV-1 withholds the answer until citation validation — identical constraint in any language. |
| Python↔TS **contract safety** | **Real, but already in-language.** Both ends are Python + TypeScript; the fix is *tooling*, not a third language. |

The instinct ("more polyglot = better architecture") conflates **polyglot** with **well-bounded**. The leverage here is **boundaries and contracts inside Python + TS + SQL** — higher-impact and far cheaper than any new runtime.

---

## 2. Where a new language earns its place *now*

**None.** There is no "adopt-now" seam. Stating that plainly is the correct senior answer.

The closest thing to a "new stack member" worth adopting now isn't a language — it's a **tool already native to your TypeScript toolchain**: `openapi-typescript` for the API contract (see §4A). Runs on the Node already in CI, emits zero runtime code, adds no deploy surface.

---

## 3. Adopt-IF-threshold (pre-decide, build only when a metric is hit)

For each: the language, the seam, and the *exact* trigger. If the metric isn't hit, do nothing.

| Seam | Language IF triggered | Exact trigger (all conditions hold) |
|---|---|---|
| **PDF parsing** (`ingest/pdf_parser.py`) | **Rust** — isolated out-of-process worker behind the existing `parse_pdf(bytes)→ParsedPdf` contract | Corpus starts accepting **user-uploaded / third-party PDFs**, **AND** observed OOM+segfault+hang rate **> ~1%**, **OR** an unpatchable pypdf/pdfplumber CVE. *Try `pymupdf`/`pypdfium2` (Python) first.* |
| **Cross-encoder reranker** (`retrieve/reranker.py`) | **Not a general language** — a Python model-server sidecar or managed rerank API (Cohere/Voyage); off-the-shelf TEI/Triton only as an *operated dependency* | `RERANKER_ENABLED=true` in prod **AND** rerank p95 adds **>150–200 ms** to `/query`, **OR** in-process torch breaches the Fly RAM limit / cold start **>~5 s**. |
| **Crawler service** (`ingest/psg_crawler.py`, `sources/*`) | **Go** — separate Fly worker, HTTP/gRPC contract returning `PsgListing` | Only *after* async-Python is in place, **AND** crawl volume **>~50k items/run** OR shifts to continuous multi-source polling, **AND** HTTP-fetch fraction of ingest **>50%**, **AND** FDA politeness budget is genuinely raised. *Implausible for a bounded ~1,800-doc catalog.* |
| **Edge auth gate** (`frontend`) | **TypeScript** (Vercel Edge Middleware — *same language*) | App goes **broadly public / multi-tenant**, **AND** auth migrates from the opaque sha256→Postgres token to a **stateless signed token (JWT/PASETO)** the edge can verify without a DB hit. *Auth model must change first.* |
| **Realtime alert eventing** | **Don't pre-decide** | Only if **>1,000 concurrent SSE/WS subscribers** OR a required **<5 s** publish-to-user latency appears. Today it's a daily batch digest — nowhere close. |

The pattern: every threshold, when crossed, points *first* at Python or your existing TS. The few that point at a compiled language need both a workload shift *and* a policy/scale change the bounded FDA domain makes unlikely.

---

## 4. Polyglot done right — *without* new languages (the real wins)

These are the actual answer to "better system arch and design," in rough priority order:

**(A) Make the Python↔TS contract mechanical, not manual.** *Highest leverage — and it's a live bug.* Only **6 of 18 routes** carry a `response_model`; the 12 highest-payload routes return bare `dict[str, Any]`. `frontend/lib/api.ts` already **lies**: `QueryResponse` declares `suggestions`, `unanswered`, and a `status: 'conversational'` value the backend *never emits* (`_build_query_response`, `main.py:427`); `normalizeQuery()` papers over it.
- *Free, now:* add real Pydantic `response_model` to all routes; `status: str` → `Literal`; remove the phantom fields.
- *Gated:* build-time `app.openapi()` dump → `openapi-typescript` → regenerate `lib/api.ts` types + CI drift check. (`/query/stream` has no `response_model`, so give its terminal frame a shared model or keep it hand-typed.)

**(B) Wire the concurrency Python already planned.** Replace the serial `ingest_listings` loop (`pipeline.py:365`) and 26-letter `fetch_all_listings` loop (`psg_crawler.py:166`) with `httpx.AsyncClient` + `asyncio.Semaphore(crawl_concurrency)`, and turn `_polite_pause`'s global `threading.Lock`+`sleep` into an **async per-host token bucket**. Activates the dead `crawl_concurrency=4` knob; ~8–12 min → ~2–3 min batch, zero new surface.

**(C) Decompose the 1,217-LOC `grounded_qa.ask()`** into typed Protocol stages (resolve → guard → retrieve → synthesize → validate); isolate the INV-1/INV-2 citation kernel as a pure, property-tested (hypothesis) module. Refactor, not rewrite.

**(D) Make alerts durable in SQL.** ⚠️ `watch/alerts.py` writes JSONL to `processed_dir`, but `fly.toml` has **no `[[mounts]]`** — so alerts (the product's core output) are **wiped on every redeploy**. Add an `alert` SQLModel table (the `Alert` dataclass maps 1:1), insert on commit, serve `/watch/latest` from it; delivery = outbound `httpx`+`tenacity` POST (Slack/Resend).

**(E) Use SQL more declaratively** (SQL isn't a new language here). Replace the N+1 in `alerts._fetch_version_for_listing` with one `DISTINCT ON (psg_document_id) … ORDER BY captured_at DESC` query (also unifies the latest-version rule duplicated in `dossier.py`); add a partial unique index for alert de-dup. **Do not** add pl/pgSQL/triggers — that hides INV-4/5/6 from the Python test surface.

**(F) Real token streaming stays in Python.** Add `stream=True` via the OpenAI/Anthropic SDK `.stream()` in `generate/llm.py`; design a stream-then-validate UX. The TS consumer (`lib/api.ts consumeSse`) already handles incremental frames. No edge/Node tier needed.

**(G) Web/worker process split (same image, same language).** Carve ingest/crawl/whitepaper/eval onto a dedicated Python worker machine so heavy batch never contends with API latency. ⚠️ Also: the **daily watch pipeline appears not to run in prod at all** — activate the Dagster schedule (or replace with cron/GH Actions).

**(H) Single source of truth for DDL.** The idempotent chunk-table DDL is duplicated across `store/db.py` and `store/pgvector_store.py`, kept in sync by comments. Own it in one Alembic migration.

---

## 5. Target architecture

**Near-term — same three languages, better boundaries:**

```
┌─────────────────────────────────────────────────────────────┐
│ Vercel (TypeScript / Next.js 16 / React)                     │
│   • SPA + typed client (lib/api.ts) — TYPES GENERATED from   │
│     FastAPI OpenAPI (openapi-typescript, CI drift check)     │
│   • SSE consumer (consumeSse) — already streams incremental  │
│   • single /api/* rewrite proxy (no BFF, no middleware)      │
└───────────────┬─────────────────────────────────────────────┘
                │ HTTPS (one proxy, cookie-session)
┌───────────────▼─────────────────────────────────────────────┐
│ Fly.io — WEB process (Python / FastAPI)                      │
│   • auth, rate-limit, ownership, audit (INV-6)               │
│   • /query[/stream] ── ask() decomposed into typed stages    │
│   • LLM synthesis (remote OpenAI/Anthropic, stream=True)     │
│   • citation kernel = pure, property-tested module           │
├──────────────────────────────────────────────────────────────┤
│ Fly.io — WORKER process (same Python image, separate machine)│
│   • crawl/ingest: AsyncClient + Semaphore + token-bucket     │
│   • PDF parse (pdfplumber), embed (remote OpenAI)            │
│   • whitepaper build, eval                                   │
│   • driven by Dagster schedule / cron (ACTIVATED in prod)    │
└───────────────┬─────────────────────────────────────────────┘
                │ SQLAlchemy / pgvector
┌───────────────▼─────────────────────────────────────────────┐
│ Supabase Postgres + pgvector (SQL)                           │
│   • retrieval (cosine <=>, HNSW, ef_search) — already here   │
│   • NEW durable `alert` table (replaces ephemeral JSONL)     │
│   • set-based latest-version queries; DDL owned by Alembic   │
└──────────────────────────────────────────────────────────────┘
```

**12-month shape:** *identical language ownership.* The only additions that should ever appear — and only if their §3 thresholds fire — are a **Rust** isolated PDF worker (untrusted uploads + measured crash rate) and a **rerank inference service** (Python sidecar or managed API). A **Go** crawler stays parked behind a ~50k-item trigger. **Expected honest outcome at 12 months: still Python + TS + SQL.**

---

## 6. Decision framework — "when do we add a language?"

A new language must clear **all four** gates. If any fails, stay in-stack.

1. **Clean boundary** — a real process/service seam with a narrow, stable contract (not a function tangled into `store/`, `audit/`, `conversation/`). If it isn't isolated, fix the boundary *in-language* first — that's usually the whole win.
2. **Real, measured driver** — a *profiled* CPU hotspot **>50% of wall-clock**, OR a concurrency model Python genuinely can't express (I/O concurrency is **not** one — asyncio handles it), OR memory-safe parsing of **untrusted** input at volume, OR a decisively better ecosystem for that exact job. "Cleaner"/"modern" don't count.
3. **The driver moves with the language** — will it actually change the binding constraint? Rate limits, remote-API latency, and policy invariants (INV-1) are language-invariant; a faster runtime buys nothing.
4. **Worth the cost** — does the benefit exceed a new CI column + image + secrets + Sentry + on-call + a duplicated cross-language contract + cognitive load on a 1–2 person team? Prefer, in order: **(a)** better boundary in-language, **(b)** a C-backed Python lib (pymupdf, rapidfuzz, selectolax), **(c)** a managed service over HTTPS, **(d)** a new language as last resort.

**Default verdict: NO. Burden of proof is on the new language.**

---

## 7. Sequenced roadmap

**P0 — now (free, in-language, fixes real bugs):**
1. Add `response_model` to all 18 routes; `status: str` → `Literal`; remove phantom `suggestions`/`unanswered`/`conversational` fields in `lib/api.ts`. *(Fixes a live contract lie.)*
2. Durable `alert` Postgres table + Alembic migration; rewrite `write_digest`/`latest_digest_records` to insert/query it. *(Fixes alerts wiped on every redeploy.)*
3. Activate the daily watch pipeline in prod (Dagster schedule or cron/GH Actions). *(Currently not running in prod.)*
4. Collapse the `alerts` N+1 into one set-based `DISTINCT ON` query; consolidate the duplicated chunk DDL into one Alembic source.

**P1 — soon (boundaries/contracts, gated on need):**
5. `openapi-typescript` codegen + CI drift check (after P0 #1; gate on the next model-field change, or when `lib/api.ts` > ~25–30 interfaces).
6. Decompose `grounded_qa.ask()` into typed Protocol stages; extract the INV-1/INV-2 citation kernel as a pure, hypothesis-tested module.
7. Wire async crawl/ingest (AsyncClient + Semaphore + async token-bucket politeness; activate `crawl_concurrency`). Do it when full `ingest-all` > ~15 min.
8. Formalize a `RerankProvider` Protocol mirroring `EmbeddingProvider` so reranking is a config swap, not a rewrite.
9. Split web vs worker into separate Fly processes (same image).

**P2 — defer until a §3 threshold fires (pre-decided, not built):**
10. Real token streaming in Python (`stream=True` + stream-then-validate UX) — when first-token latency is a real complaint.
11. Rust isolated PDF worker — only on untrusted uploads + >1% crash rate (try pymupdf/pypdfium2 first).
12. Reranker inference service — only on `RERANKER_ENABLED` + measured p95/memory breach.
13. Go crawler service — only on >50k items/run or continuous polling + raised politeness budget.
14. Vercel Edge auth middleware (TS) — only after going public/multi-tenant AND migrating to stateless signed tokens.

---

## Bottom line

The best architectural move is **no new language**. Your system is already polyglot in the way that matters; the leverage is in tightening the seams you have — generate the Python↔TS contract, make alerts durable in SQL, wire the async concurrency Python already scaffolded, and split web/worker. Reserve Rust and Go for specific, measured thresholds the bounded, rate-limited, I/O-bound FDA domain is unlikely to cross. Adding a language now would fragment a small team's codebase to solve problems this system doesn't have.

### Real bugs this review surfaced (worth fixing regardless of the polyglot question)
1. **Alerts are wiped on every redeploy** — written to ephemeral JSONL with no `fly.toml [[mounts]]`. The product's core output isn't durable.
2. **The daily watch pipeline appears not to run in prod** — the Dagster schedule isn't activated.
3. **`lib/api.ts` declares fields the backend never sends** (`suggestions`, `unanswered`, `status:'conversational'`) — a silent contract drift.
4. **`crawl_concurrency=4` is dead config** — a knob that implies concurrency that doesn't exist.
