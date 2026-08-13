# REGWATCH Simple Technical Guide

Last updated: 2026-08-11

For a technical reader who wants the big picture before opening the code.

## What It Does

REGWATCH pulls in public FDA material, stores it as searchable and citable
evidence, and answers questions from that evidence. Facts it states about FDA
guidance carry a citation. When the evidence does not cover the question, it
says so in plain words instead of guessing.

## What Is Running Right Now

Verified against the live system on 2026-08-11.

- Backend on Fly.io, app `amneal`, release v104. TLS via `force_https`.
- Go edge service holds the public port. Python runs the RAG core behind it.
- Frontend on Vercel: Next.js App Router.
- Database: **Databricks Lakebase** Postgres in us-east-2, database
  `databricks_postgres`, app role `regwatch_app`. pgvector lives in the same
  database, so rows, vectors, and the audit log are all in one place.
  (An older doc, `DATABRICKS_ADOPTION_2026-07-28.md`, argued for keeping
  Supabase. That call was reversed and the move already happened.)
- Alembic head in the live DB is `0020_eval_run`, which matches the repo. Nothing
  pending.
- 5,494 chunk rows in the corpus.
- Generation model: **`gpt-oss-120b`** (served id `gpt-oss-120b-080525`),
  open weight, served from the company Databricks tenant at
  `workspace.default.regwatch`. One model does every role: router, synthesizer,
  extractor. `DATABRICKS_REASONING_EFFORT=low`.
- Embeddings: **Databricks Qwen3** at `workspace.default.regwatch-embed`, 1024
  dimensions, active profile `ep_2e7368b354d911ea3a013c3125e276c2`. All 5,494
  chunks are embedded on that profile, so coverage is 100 percent.
- No normal analyst turn uses OpenAI. Its provider remains the tested interactive
  LLM rollback; scheduled Watch has a separate scoped key for public-document
  change summaries and extraction, never embeddings.

Data residency is closed. Generation, embeddings, and the database all sit
inside the company's Databricks tenant, so a normal analyst question never
leaves for a third-party model API.

Main stack:

- Python 3.11+ / 3.12, FastAPI, SQLModel / SQLAlchemy
- Go edge service in `go/`
- Next.js (App Router, TypeScript) UI in `regwatch/frontend/`
- Postgres + pgvector as the only datastore. `DATABASE_URL` is mandatory and the
  app refuses to boot without it.
- `httpx` and `selectolax` for FDA crawling, `pdfplumber` and `pypdf` for PDFs
- Alembic migrations, currently through `0020`
- Docker / Compose for the local API and ingest baseline
- pytest, ruff, black, mypy

## The Two Flows

Ingest:

```text
FDA PSG index
  -> scrape PSG rows
  -> download PDF
  -> parse PDF page text
  -> chunk text with page metadata
  -> embed chunks
  -> store chunks in pgvector
  -> store document/version/BE fields in Postgres
```

Q&A:

```text
user question
  -> create or load chat session
  -> resolve follow-up context when safe
  -> resolve product scope
  -> retrieve scoped chunks
  -> withhold passages below the score threshold
  -> call the LLM with only the surviving passages
  -> gate the reply sentence by sentence, validate citations
  -> write one audit row with session_id and turn_id
  -> return the answer, a clarify prompt, or a plain-words decline
```

## Folder Map

```text
config/settings.py         runtime config from env

go/                        Go edge: auth, sessions, feedback, settings,
                           products, rate limits, POST /query orchestration

migrations/versions/       Alembic migrations (through 0020)

src/regwatch/
  ingest/                  crawl FDA PSGs, parse PDFs
  process/                 chunk, embed, extract BE fields, detect changes
  store/                   Postgres models/session + pgvector vector store
  retrieve/                product resolution, scoping, vector retrieval
  generate/                prompts, LLM providers, the answer gate, grounded Q&A
  watch/                   watchlist, alias discovery, matching, alerts
  assemble/                product dossier builder
  whitepaper/              cited White Paper populator + .docx export
  deficiency/              deficiency analysis runs
  sources/                 rules-first source router + FDA source handlers
  auth/                    cookie-session verification
  api/                     FastAPI endpoints
  eval/                    gold set, metrics, deterministic eval gate
  common/                  citations, audit, logging, conversation, rate limits

regwatch/frontend/         Next.js UI: five surfaces in one (shell) route group,
                           plus /studio outside it
tests/                     unit, integration, invariant, eval-gate tests
tests_contract/            cross-service suite over real Go + uvicorn + Postgres
docs/                      specs, decisions, plans, onboarding
```

Docker files at the top level: `Dockerfile` builds the shared Python image,
`compose.yaml` runs API + ingest, `.dockerignore` keeps data and secrets out of
the image.

## Read These First

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `config/settings.py`
4. `src/regwatch/store/models.py`
5. `src/regwatch/ingest/pipeline.py`
6. `src/regwatch/generate/grounded_qa.py`
7. `src/regwatch/generate/prompts.py`
8. `src/regwatch/generate/turn_gate.py`
9. `src/regwatch/retrieve/resolver.py`
10. `src/regwatch/common/citations.py`
11. `tests/test_invariants.py`

`store/models.py` explains most of the system on its own.

## The Data Model

Important tables:

- `Product`: verified watchlist product
- `PsgDocument`: current FDA PSG document record
- `PsgVersion`: a captured PSG version
- `BeRequirement`: extracted BE fields with citations
- `QueryLog`: durable audit row for every Q&A turn
- `User` and session tables: auth identities, opaque session tokens, per-user
  chat ownership (migration `0004`)
- `embedding_profile`: which vector space is live. There is exactly one row.

Each vector chunk carries `doc_id`, `version_id`, `normalized_name`,
`dosage_form`, `route`, `page`, `source_url`, and `appl_no`. That metadata is
what makes citations and scoped retrieval possible.

## Ingest

**`ingest/psg_crawler.py`** scrapes the FDA PSG index into `PsgListing` objects:
application number, active ingredient, normalized name, PSG type, route, dosage
form, recommended date, PDF URL. It can also filter listings to seed products.

**`ingest/pdf_parser.py`** turns PDF bytes into full text, a per-page text list,
and the parser engine name. Page boundaries matter because every citation needs
a page.

**`process/chunker.py`** splits page text into chunks and keeps the page number,
section path, and document metadata.

**`process/embedder.py`** defines the embedding interface. Providers:

- `qwen3`: Qwen3 over an OpenAI-compatible private endpoint. This is production,
  served by Databricks at 1024 dimensions.
- `openai`: `text-embedding-3-small`, 1536-dim. Rollback path only.
- `local-bge-small`: local sentence-transformers model, 384-dim, offline tooling
  only. The dimension check rejects it against the app datastore.
- `echo`: deterministic test provider, 1536-dim.

How the switch actually works, because it trips people up: retrieval picks its
provider from `ACTIVE_EMBEDDING_PROFILE`. Only the `legacy` value reads
`EMBEDDING_PROVIDER`. Production runs a real profile, so the
`EMBEDDING_PROVIDER=openai` line still sitting in `fly.toml` does nothing on the
query path. Do not read it as the live setting.

**`process/extractor.py`** uses the LLM to pull structured BE requirement fields
out of a PSG. Every populated field needs a citation with a page and a verbatim
quote, the quote has to actually appear on that page, and fields that fail are
dropped.

**`ingest/pipeline.py`** is the orchestrator: download PDF, upsert
`PsgDocument`, create a `PsgVersion` if content changed, parse, chunk, embed,
store in pgvector, run BE extraction, save `BeRequirement`.

## Retrieval

**`retrieve/resolver.py`** is the correctness file. FDA PSGs share boilerplate
across products, so unfiltered search will happily answer a beclomethasone
question with albuterol text. The fix is to resolve the product first and then
search only that product's chunks. Outcomes are `resolved`, `ambiguous`, or
`none`.

**`retrieve/retriever.py`** embeds the query and searches pgvector, with
metadata filters on `normalized_name`, `dosage_form`, `route`, and `psg_type`.
For product-specific PSG questions `normalized_name` is the one that matters.

**`retrieve/reranker.py`** is an optional cross-encoder hook, off by default
(`RERANKER_ENABLED`).

`REFUSAL_SCORE_THRESHOLD` defaults to 0.30. Passages scoring below it are
withheld before the synthesizer ever sees them.

## Generation: The v7 Answer Policy

This is the part that changed most recently, and it is live in production.

The old headline rule was "cite or refuse". It is gone. The rule now is **cite
the facts, talk like a person.** The prompt is `GROUNDED_QA_SYSTEM_V7` in
`generate/prompts.py`; the enforcement is `generate/turn_gate.py`.

Every sentence the model writes is one of three kinds.

1. **Source fact.** States what FDA guidance requires, recommends, permits, or
   prohibits. It must end with the passage numbers it came from, like `[1]` or
   `[1, 3]`, placed right before the final period. An uncited source fact is
   dropped by the gate. This is INV-1 and it lives in code, not in the prompt.
2. **Reasoning.** Our own reading, going past what the passages say. It carries
   no numbers and must open with one of four exact phrases:
   "The guidance does not state this directly; my reading is ...",
   "Reading the guidance together, ...", "My reading is ...", or
   "Beyond the guidance, ...". Those openers are pinned byte for byte to
   `turn_gate.REASONING_FRAME_PREFIXES` and a test enforces the match. An
   obligation or a prohibition cannot hide inside a reasoning sentence: a
   source-assertion lexicon reclassifies it back to a source fact.
3. **Conversation.** Greetings, offers, transitions, a question back to the
   user. Plain text, no numbers, no FDA facts.

**There is no sentinel and no code word for "not found".** v6 had one and it
broke every refusal in the battery. In v7, when the passages do not answer the
question the model says so in ordinary words, names what it does have nearby,
and offers a next step. That whole reply is plain prose with zero passage
numbers.

Gate verdicts in `turn_gate.py`:

| Verdict | Meaning |
|---|---|
| `answer` | every emitted claim was admitted |
| `partial` | something was dropped, immaterial, so render and disclose |
| `material_drop` | the drop would change the meaning, so reject the whole answer |
| `no_valid_citations` | nothing was admitted |
| `no_evidence` | the model declined |
| `conversational_decline` | admitted, but zero source facts (new in v7) |

Three feature flags are set on Fly and all three are on:
`REGWATCH_PROSE_SYNTHESIS` (v6 prose format, replaced the v5 claims JSON),
`REGWATCH_LIVE_DRAFT` (live token streaming of the provisional draft over SSE),
and `REGWATCH_SELECTIVE_CITATION` (the v7 policy above).

`generate/llm.py` defines the provider interface:

- **Databricks** (`DatabricksProvider`), production since 2026-07-28. Serving
  alias `workspace.default.regwatch`, one model for all roles. The alias pointed
  at `system.ai.gpt-oss-20b` until 2026-08-05, when it was repointed to
  `system.ai.gpt-oss-120b`. The served id is pinned in
  `tests/test_d1_guards.py`.
- **OpenAI**, rollback path. Uses the Responses API by default
  (`OPENAI_API_MODE=responses`; `chat` falls back to Chat Completions) with
  role-specific models: router `gpt-5-nano`, synthesizer and extractor
  `gpt-5.4-nano`, each falling back to `LLM_MODEL`. Reasoning models that reject
  `temperature` are retried without it.
- **Anthropic**, and `echo` for tests.

Business logic always calls `get_llm_provider(role=...)`. No model name is
hard-coded.

Residency guardrails that are still worth knowing: `D1_ALLOWED_LLM_MODELS` plus
a runtime served-model check in `llm.py`. It rejects a response served by a
model outside the allowlist, and it rejects partner-hosted families
(`databricks-gpt*`, `databricks-claude*`, `databricks-gemini*`) even if somebody
allowlists them by hand.

## Citations

`common/citations.py` is the one citation grammar for the whole codebase.

The model writes passage numbers like `[1]`. The gate maps each admitted claim
back to a real source and renders the final answer with locators:

```text
[PSG_020503, p.3]
```

Compound form for a claim backed by two sources:

```text
[PSG_020503, p.4; PSG_021730, p.4]
```

A `Sources:` trailer lists them at the end. One shared parser means generator
validation and eval scoring agree, fake citations can be stripped before the
answer goes out, and citation behavior does not drift between modules.

## Watch, Dossier, White Paper

**Watch** (`watch/`) tracks products and alerts on relevant PSG changes.
Products must come from verified sources, applicant aliases are discovered from
Drugs@FDA rather than guessed, PSG listings are matched against the watchlist,
and an alert is only emitted when the PSG version actually exists in the store.
The daily run is a GitHub Actions schedule, `.github/workflows/watch-daily.yml`.

**Dossier** (`assemble/dossier.py`) builds a Markdown research brief for a
product: matched PSGs, BE fields, citations and source links, RLD label info
from indexed Drugs@FDA approved labeling, a cited Q&A summary, and a checklist
scaffold. It is a research scaffold, not submission content.

**White Paper** (`whitepaper/`) is the shipped instance of multi-source
synthesis. It fuses approved Drugs@FDA metadata and labeling, Orange Book, PSG,
action-package evidence, and FDA BE guidance into a cited cell graph with
tri-state cells (`populated`,
`verified_absent` rendered as "No", `analyst_input_required`), records source
provenance with freshness timestamps, and exports a
`.docx` rendered from the exact reviewed result.

## Multi-Source Routing

Handlers live in `src/regwatch/sources/` behind a rules-first router
(`sources/router.py`) and are reachable via `POST /sources/search`: Drugs@FDA,
SBOA/action packages, PSG, FDA BE guidance, and Orange Book. The policy is exact:
requests for any legacy source are rejected, not silently routed to a fallback.
The replacement corpus parses both structured snapshot records and FDA
documents into citable, versioned chunks.

Still open: the main `POST /query` path runs PSG-scoped RAG only. It does not
yet synthesize the structured handlers, and the persist-and-cite plus freshness
pattern proven in the White Paper has not been applied to the Ask and Assemble
read paths. See `docs/ROADMAP.md`.

## API And UI

The Go edge owns the public port. It serves `/auth/*`, `/sessions*`,
`/feedback`, `/settings`, and `/products` natively, orchestrates `POST /query`
end to end (it writes the `query_log` audit row and calls Python only through
the token-gated internal `POST /internal/query/compute`), and relays everything
else to FastAPI.

Endpoints:

- `GET /health`, `GET /ready`: open liveness and readiness
- `GET /metrics`: Prometheus counters, open by default, bearer-gated when
  `METRICS_TOKEN` is set
- `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` (Go)
- `POST /query`: conversational, Go-orchestrated. Takes `session_id`/`user_id`,
  returns `session_id`/`turn_id`/`status`. Status is one of `answer`, `summary`,
  `clarify`, `scope_warning`, `meta`, `refused`, `error`.
- `POST /query/stream`: SSE. Sends `status` progress frames, provisional `token`
  delta frames (a cosmetic live draft), then one validated terminal
  `QueryResponse` `result` frame. The client falls back to `POST /query` if the
  stream dies before the result.
- `POST /feedback`: per-turn feedback against an audit row (Go)
- `POST /resolve`: deterministic entity resolution to
  `{normalized_name, six-digit application number}`. Not an LLM turn: no audit
  row, no answer text. A mismatch returns 422 with no scope set.
- `POST /sources/search`, `POST /assemble`
- `POST /whitepaper` plus the `/whitepaper/runs/*` read, edit, finalize, reopen,
  delete, and `.docx` export routes
- `POST /deficiency/analyze`, `GET /deficiency/runs`, `GET /deficiency/runs/{id}`
- `GET /watch/latest`, `GET /psg/documents`, `GET /psg/documents/{id}/pdf`,
  `GET /psg/documents/{id}/content`, `GET /psg/documents/{id}/docx`
- `GET /products`, `POST /products` (Go)
- `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` (Go). A
  `session_id` belonging to another user 404s.
- `GET /settings` (Go)

Everything else sits behind `require_user`. Auth is a DB-backed cookie session:
opaque token hashed at rest, bcrypt passwords, CLI-provisioned users, per-user
rate limiting (`RATE_LIMIT_PER_MINUTE`, default 30), and login brute-force caps
per email and per source IP. CORS is allow-listed with credentials via
`CORS_ALLOW_ORIGINS_CSV`.

### Frontend

Five surfaces (Ask, Assemble, Watch, White Paper, Deficiency) render inside one
`(shell)` route-group layout: one sidebar, one set of design tokens, one shared
"Under review" product-scope bar. The Compliance Studio (`/studio`) sits outside
that shell because it reads our own CMC drafts instead of public FDA material,
and it is fixture-backed.

- The current product is URL-scoped (`?rp=&appl=`), so it survives reload and is
  shareable. All five shell surfaces read it.
- Scope can be set from three places: the bar's resolve-backed picker, a
  successful White Paper populate, and a Watch row. Each writes the canonical
  `{normalized_name, six-digit application number}`.
- Ask is a cited conversational chat: right-aligned user bubbles, citation chips
  that link to FDA sources with full snippets in a Sources disclosure, clarify
  pills, a bottom-pinned composer, Enter to send.

It uses a typed client in `regwatch/frontend/lib/api.ts` that mirrors the
Pydantic models, and a same-origin `/api` proxy in `next.config.mjs`. Run it
with `cd regwatch/frontend && npm run dev`, or use `scripts/share-demo.sh` to
put the API and UI behind one public link.

## Eval

Files: `eval/gold_set.jsonl`, `eval/whitepaper_gold.jsonl`, `eval/metrics.py`,
`eval/run_eval.py`, `eval/verify_gold.py`.

Metrics: recall@k, MRR, citation precision, faithfulness (the share of admitted
source-fact claims that carry a citation), `sentence_citation_rate` (the older
text-only definition, kept for trend continuity), `fact_recall`, and refusal
accuracy.

`run_eval.py --check-thresholds` is the CI gate. It blocks on recall@8 >= 0.90,
citation_precision >= 0.95, and refusal_accuracy >= 0.95, and exits non-zero on
an empty store. Its provider-backed CI lane is key-gated.
`tests/test_eval_gate.py` is a deterministic offline twin: it seeds a fixed
corpus and a faithful LLM stub, so the check also fires inside plain
`uv run pytest`. The eval is mechanical and auditable on purpose; there is no
LLM-as-judge yet. See [`EVAL_STATUS.md`](EVAL_STATUS.md).

The gold set is verified before it is scored. `verify_gold` asserts that every
expected source quote really appears on that page, and `run_eval` refuses to
score a gold set that fails, before spending a single LLM call.

## Tests

The tests explain expected behavior better than comments do.

- `test_invariants.py`: compliance invariants
- `test_turn_gate.py`: the v7 sentence gate
- `test_cross_drug_leak.py`: product scoping before retrieval
- `test_citations.py`: citation grammar
- `test_resolver.py`: entity/product resolution
- `test_pipeline_idempotent.py`: ingest idempotency
- `test_grounded_qa_citations.py`: fake citation stripping
- `test_d1_guards.py`: residency allowlist and the served-model check
- `test_api.py`: endpoint behavior

## Safety Model

1. Use only trusted FDA and public source evidence.
2. Resolve product scope before searching chunks (INV-9: citations cannot cross
   drugs).
3. Retrieve with metadata filters, and withhold anything under the score
   threshold.
4. Give the LLM only the surviving passages.
5. Gate the reply sentence by sentence and validate every citation.
6. Drop uncited source facts. Reject the whole answer when the drop was
   material.
7. Write exactly one `query_log` row per query, answered or not.

The LLM is not trusted on its own. The code checks what it produced.

## Local Commands

```bash
uv sync --extra dev --extra llm
uv run pytest -q
uv run regwatch init-db
uv run regwatch create-user                    # auth gates every endpoint but /health
uv run regwatch aliases --refresh
uv run regwatch seed
uv run regwatch ingest-all                     # full A-Z PSG catalog crawl
uv run regwatch embedding-profile-list         # which vector space is live
uv run regwatch embedding-profile-coverage <profile_id>
uv run python -m regwatch.eval.run_eval
uv run python -m regwatch.eval.run_eval --check-thresholds   # CI eval gate
uv run uvicorn regwatch.api.main:app --reload
cd regwatch/frontend && npm install && npm run dev
```

## Known Open Items

- Not externally exposed yet. It needs an SSO plus TLS gateway.
- Least-privilege database credentials.
- A rehearsed restore drill.
- The daily Watch workflow is coded for Qwen/profile parity and fails before
  crawl if any of its six profile settings is absent. Those repository secrets
  are not provisioned yet; the owner must set them and verify one manual run.
  Scheduled Qwen ingestion no longer refreshes the legacy OpenAI vector arm, so
  backfill that arm before treating it as a current-corpus rollback.
- The 0.30 refusal threshold was validated against the old OpenAI vector space.
  The space is now Qwen3 at 1024 dimensions, so that validation no longer
  carries over.
- Compliance Studio is UI and domain model only. Its document service,
  compliance pipeline, and assistant are fixtures, and nothing survives a
  refresh.

## Mental Model

Three systems bolted together:

1. **Evidence builder**: fetches and stores FDA evidence.
2. **Evidence answerer**: retrieves the right evidence and writes a cited
   answer.
3. **Evidence guardrail**: checks every sentence, drops what is not supported,
   and audits everything.

Keep that in your head and the codebase reads much more easily.
