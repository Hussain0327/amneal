# PROJECT SPEC - Codename: REGWATCH

**A regulatory prep accelerator for a generic-drug Clinical Regulatory Affairs team.**
Spec author persona: Principal Software Engineer. Audience: Claude Code (implementer).
Status: v1.0, build-ready. Rename the codename freely.

> **This is the original build spec.** Last updated: 2026-08-11. It is kept for
> the design it locked in. Where the shipped system has moved on, the
> current-state docs win. What has changed since it was written:
>
> - **UI.** A Next.js App Router app in `regwatch/frontend/`. Streamlit is gone,
>   so ignore the `ui/app.py` and "three pages" text below. Five surfaces (Ask,
>   Assemble, Watch, White Paper, Deficiency) share one shell with a URL-scoped
>   CurrentProduct (`?rp=&appl=`) and an "Under review" product-scope bar. Ask is
>   a cited conversational chat. A sixth surface, Compliance Studio, reads our own
>   CMC drafts and is UI plus fixtures only.
> - **Models.** Production generation and embeddings both run on Databricks Model
>   Serving inside the company tenant: `gpt-oss-120b-080525` for every LLM role,
>   and a Qwen3 embedding endpoint at 1024 dimensions. OpenAI is the rollback
>   path, not current state.
> - **Storage.** One Postgres, Databricks Lakebase, with pgvector in the same
>   database. The SQLite/Chroma dual-mode path was deleted in R5. Alembic
>   migrations run to `0020_eval_run`.
> - **Backend additions.** Conversational sessions with per-turn audit,
>   current-version retrieval, entity-resolution hardening, a White Paper
>   populator with multi-source cited cells, `POST /resolve` (deterministic
>   entity resolution, not an LLM turn), and DB-backed auth on every endpoint
>   except `GET /health`. A Go proxy now holds the public port and orchestrates
>   `POST /query`.
> - **Answer policy.** "Cite or refuse" was the v5 and v6 rule. v7 selective
>   citation replaced it and is live in production: cite the facts, talk like a
>   person. See Section 11.
> - **Invariants.** Section 4 codifies INV-1..6. INV-7..9 were added later and are
>   also enforced as tests. See the note in Section 4.
>
> For what is true today see the root `README.md`, `docs/ARCHITECTURE.md`,
> `docs/DECISIONS.md`, `docs/PROD_READINESS.md` and `docs/ROADMAP.md`.

---

## 0. How to read this spec

This was the source of truth when it was written. It is now a historical spec:
current-state docs win wherever the implementation has moved on. The Compliance
Invariants in Section 4 are the exception. They still hold unless a newer
decision record explicitly supersedes them.

---

## 1. Context and problem

A generic-drug company files Abbreviated New Drug Applications (ANDAs). To file, the Clinical RA team must know, per target product, exactly what bioequivalence (BE) evidence the FDA expects. The FDA publishes this as Product-Specific Guidances (PSGs): roughly 2,400 documents, revised constantly (one recent biweekly batch added 23 and revised 48). Today a scientist manually checks the PSG database for relevant changes and manually assembles the requirements for each product from scattered FDA sources. That is slow, and in generics speed is the business: the first-to-file / first-approved applicant captures exclusivity windows worth real money.

The opportunity is to compress the team's own prep cycle, not to automate any FDA-facing regulatory judgment. Those are different things, and the boundary between them is both the compliance line and the strategy (Section 4).

## 2. Users

Non-technical regulatory professionals. They will not read JSON or run scripts. Every surface must be a plain-language question-in / cited-answer-out interaction. The single most important UX property is trust: a non-expert cannot detect a confident hallucination, so the system must show its source on every factual claim and say plainly when it has none.

## 3. Goals and non-goals

**Goals**

- G1. **Watch:** detect new and revised PSGs/guidances, match them against the company's real product pipeline, and surface a cited summary of what changed.
- G2. **Assemble:** for a target product, auto-build an organized, fully cited requirements dossier (BE study design, RLD label, applicable guidances, dissolution method, requirements checklist).
- G3. **Cited Q&A:** answer plain-language questions over the corpus. Every FDA fact carries its source and page. When the corpus does not cover the question, say so plainly instead of guessing.
- G4. An eval harness that proves the system is faithful (Section 10.11). This is the quality gate.

**Non-goals (do NOT build these)**

- N1. No drafting, generation, or suggestion of content intended for an FDA submission.
- N2. No regulatory decisions or recommendations ("you should run study X"). The system reports what guidance says; the human decides.
- N3. No internal, proprietary, or SOP data in this POC. Public FDA data only.
- N4. No autonomous actions (no filing, no submitting, no emailing FDA).
- N5. Not a production deployment. This is a demoable POC to hand to the internal IT/AI team.

N1 through N4 still hold and are enforced by the invariants. N5 has been overtaken:
the system is deployed and running (Fly app `amneal`, Vercel frontend, Lakebase
Postgres). It is not yet exposed outside the tenant, which still needs an SSO plus
TLS gateway. See `docs/PROD_READINESS.md`.

## 4. Compliance invariants (HARD constraints, each must have a test)

These encode both FDA expectations (the Jan 2025 draft guidance treats AI that supports a regulatory decision very differently from AI that only improves operational efficiency) and hard-won lessons. They are invariants, not features.

- **INV-1 Grounding.** Every factual claim in any output must be traceable to a retrieved source passage with a document id and page number. No ungrounded claims, ever.

  Amendment 1 (owner, 2026-08-07, implemented 2026-08-10): live un-gated prose
  MAY stream to the client as an explicitly provisional draft on the dedicated
  `draft` SSE channel, dual-gated by REGWATCH_LIVE_DRAFT and a per-request
  opt-in, and available only in prose-synthesis mode. Nothing un-audited may be
  PRESENTED AS VALIDATED: draft frames carry no citations, no audit id, and no
  validated affordances, and the terminal `result` frame stays the only
  validated artifact. See docs/superpowers/specs/2026-08-10-ask-sse-live-draft-design.md.

  Amendment 2 (v7 selective citation, live 2026-08-10): "factual claim" means a
  sentence that says what FDA guidance requires, recommends, permits or
  prohibits. Those sentences must carry their passage numbers, and
  `src/regwatch/generate/turn_gate.py` drops any that do not. Sentences that are
  our own reading, or plain conversation, carry no numbers and assert no FDA
  facts. The enforcement is unchanged: an uncited source fact still never reaches
  the user. See Section 11.
- **INV-2 Refuse over guess.** If retrieval does not surface a sufficiently relevant passage (Section 11), the system says so instead of answering. It never fabricates an answer. Since v7 there is no fixed refusal string: the model says in ordinary words that the corpus does not cover the question, names what it does have nearby, and offers a next step. The API still marks the turn `refused` with an empty citation list.
- **INV-3 Operational only.** The system surfaces, organizes, compares, and cites public information. It never authors submission content and never renders a regulatory judgment.
- **INV-4 No fabricated execution.** The system must never report or narrate a process, run, or result it did not actually execute. A match that was not fetched does not exist.
- **INV-5 Verified provenance.** Pipeline and product facts come only from verified sources (Drugs@FDA, uploaded approval letters), never from a language model's memory.
- **INV-6 Auditability.** Every query, its retrieved sources with scores, the generated answer, and whether it refused are logged to a durable store (Section 10.10).

If a requested behavior would violate an invariant, do not implement it; flag it in `DECISIONS.md`.

> **Implementation status, 2026-08-11.** INV-1..6 above still hold and are tested
> in `tests/test_invariants.py`. Three more invariants were added later and are
> enforced as tests across the resolution, white-paper and citation code:
> **INV-7** cross-product integrity, never blend two applications' data;
> **INV-8** structured-citation grammar, with a central guard that collapses any
> cell whose token is not backed; **INV-9** product resolution before retrieval,
> which is what stops a cross-drug citation leak. They extend INV-1..6, they do
> not supersede them.

## 5. System overview

Two product functions on one shared backbone. The shape below is the original
design. The stores and the UI named in it are gone: see `docs/ARCHITECTURE.md`
for the system as it runs today.

```
PUBLIC FDA DATA                 BACKBONE
---------------                 --------
PSG DB (scrape)     -->  ingest -> parse -> chunk+tag -> embed -> vector store
Guidance PDFs       -->                  |
Drugs@FDA (API)     -->  watchlist build |-> extract BE fields -> structured DB
Drug labels (API)   -->                  |
Orange Book (files) -->                  |-> version diff -> change log

                            |                          |
                            v                          v
                   RETRIEVE + GROUNDED QA        WATCH: match changes
                   (cited answers)               against the watchlist,
                            |                    then alert
                            v                          |
                   ASSEMBLE: dossier builder -----> API (FastAPI) + UI

cross-cutting: AUDIT LOG, EVAL HARNESS
```

## 6. Data sources (exact, with access method)

| Source                       | Use                                                          | Access                    | Endpoint / location                                         |
| ---------------------------- | ------------------------------------------------------------ | ------------------------- | ----------------------------------------------------------- |
| Product-Specific Guidances   | Core corpus; BE requirements                                 | **Scrape** (no API)       | accessdata.fda.gov/scripts/cder/psg/index.cfm + linked PDFs |
| Guidance documents (broader) | Phase 2 corpus expansion                                     | Scrape / download         | fda.gov guidance database                                   |
| Drugs@FDA                    | Build verified watchlist (sponsor = company), product status | **API**                   | api.fda.gov/drug/drugsfda.json                              |
| Drug labeling (SPL)          | RLD label for Assemble                                       | **API**                   | api.fda.gov/drug/label.json                                 |
| Drug shortages               | Competitive intel (later)                                    | **API**                   | api.fda.gov/drug/shortages.json                             |
| Complete Response Letters    | Deficiency-prep (v2, later)                                  | **API/dataset**           | openFDA CRL dataset                                         |
| Dissolution Methods DB       | Dissolution method for Assemble                              | **Scrape**                | accessdata.fda.gov/scripts/cder/dissolution                 |
| Orange Book                  | RLD / therapeutic-equivalence status                         | **Download** (data files) | FDA Orange Book data files                                  |

Notes:

- openFDA is free; set `OPENFDA_API_KEY` for higher rate limits, but the system must work without one (handle 429s with backoff).
- The PSG page is a script-driven (ColdFusion) app. Before writing the scraper, **inspect the live page's network traffic to find the backing data endpoint** (these DataTables-style pages usually have one returning JSON or HTML); prefer that over headless browsing. Fall back to Playwright only if the table is rendered client-side with no fetchable endpoint.
- Be a polite crawler: cache aggressively, rate-limit, set a descriptive User-Agent, respect robots.txt, never hammer.

## 7. Tech stack (decided)

- **Language:** Python 3.11+.
- **Env/packaging:** `uv`.
- **HTTP/scrape:** `httpx`, `selectolax` (or BeautifulSoup); `playwright` only if needed.
- **PDF parsing:** `pdfplumber` primary (preserves page numbers, needed for citations), `pypdf` fallback. PSGs are digital PDFs; no OCR.
- **Chunking:** recursive, heading-aware splitter with overlap; preserves metadata.
- **Embeddings:** behind an `EmbeddingProvider` interface. Default: local `BAAI/bge-small-en-v1.5` via `sentence-transformers` (no external calls, self-contained, strengthens the data-handling story). Swappable to a hosted provider via config.
- **Vector store:** `ChromaDB`, persistent on disk.
- **Structured store:** `SQLite` via `SQLModel` (SQLAlchemy + Pydantic).
- **LLM:** behind an `LLMProvider` interface. Default: a capable hosted chat model for the POC; the production model and any on-prem / small-model decision is explicitly deferred to the internal IT/AI team. Must be swappable via config and never hard-coded into business logic.
- **Reranker (Phase 2):** a cross-encoder (e.g., `bge-reranker-base`), optional, behind a flag.
- **API:** `FastAPI` + `uvicorn`.
- **UI:** `Streamlit` for the POC (fast to build, fine for a non-technical demo). Production UI is the IT team's call.
- **Scheduling (Watch):** `APScheduler` in-process for the POC, or a documented cron entry.
- **Testing:** `pytest`. **Linting/format:** `ruff` + `black`. **Typing:** `mypy` on `src/`.
- **Config:** `pydantic-settings`, all secrets/thresholds in env or `config/settings.py`, nothing hard-coded.

> **What actually shipped, 2026-08-11.** Python, `uv`, `httpx`, `pdfplumber`,
> FastAPI, pytest, ruff, black, mypy and `pydantic-settings` all stand. Five
> choices did not:
>
> - Vector store is pgvector inside the app's Postgres, not Chroma. R5 deleted the
>   Chroma path.
> - Structured store is that same Postgres, Databricks Lakebase in production, not
>   SQLite.
> - Embeddings run on a Databricks-hosted Qwen3 endpoint at 1024 dimensions.
>   `bge-small` is offline and eval tooling only.
> - The LLM decision was not deferred. Production runs Databricks Model Serving
>   inside the company tenant, `gpt-oss-120b-080525`, one model for every role.
> - UI is Next.js on Vercel, and a Go proxy holds the public port. Watch runs as a
>   GitHub Actions schedule, not APScheduler.

## 8. Repository structure

The original layout was one package per pipeline stage under `src/regwatch/`:
`ingest`, `process`, `store`, `retrieve`, `generate`, `watch`, `assemble`, `eval`,
`api`, `common`. That spine is still there. The tree printed here has drifted too
far to be useful, so it has been dropped. For the real folder map, including the
Go proxy and the Next.js frontend, see
[TECH_GUIDE_SIMPLE.md](TECH_GUIDE_SIMPLE.md).

## 9. Data model

Written for SQLite via SQLModel. The tables below survived; the database did not.
Everything now lives in one Postgres (Lakebase in production) under Alembic
migrations, currently at head `0020_eval_run`, with more tables than this list.

```
product
  id PK
  active_ingredient            # raw
  normalized_name              # lowercased, salt-stripped, sorted for combos
  dosage_form
  route
  rld_name
  rld_application_number
  company_status               # e.g. "approved", "tentative", "pipeline"
  source                       # "drugsfda" | "anda_letter" | "manual"
  source_url
  on_watchlist BOOL
  added_at

psg_document
  id PK
  active_ingredient
  normalized_name
  dosage_form
  route
  rld_or_rs_number
  psg_type                     # "draft" | "final"
  recommended_date
  source_url
  pdf_path
  content_hash
  first_seen_at
  last_seen_at

psg_version
  id PK
  psg_document_id FK
  content_hash
  recommended_date
  parsed_text_path
  captured_at
  diff_summary                 # nullable; cited summary of changes vs prior version

be_requirement
  id PK
  psg_document_id FK
  version_id FK
  study_type                   # e.g. fasting/fed BE, in vitro
  study_design
  strengths
  dissolution
  waiver_conditions
  additional_notes
  fields_json                  # full structured extraction
  citations_json               # per field: {page, quote}

query_log                      # INV-6
  id PK
  ts
  mode                         # "qa" | "assemble" | "watch"
  query_text
  retrieved_json               # [{chunk_id, score, doc_id, page}]
  answer_text
  citations_json
  refused BOOL
  model_name
```

The vector store holds chunk text plus metadata: `{doc_id, version_id, active_ingredient, normalized_name, dosage_form, route, recommended_date, source_url, page}`. Written for Chroma; it is pgvector in the same Postgres now, with profile vectors in `chunk_embedding`.

## 10. Component specifications

### 10.1 PSG crawler (`ingest/psg_crawler.py`)

Responsibility: enumerate PSGs (full set and the "newly added / newly revised" feeds), download each PDF, upsert `psg_document`, compute `content_hash`. Inputs: none (or a date filter). Output: rows in `psg_document`, PDFs in `data/raw/`. Key logic: discover the backing data endpoint first (Section 6); incremental mode that only fetches changed/new entries; idempotent upserts keyed on `(normalized_name, dosage_form, rld_or_rs_number)`. Acceptance: running twice produces no duplicates; the seed products (Section 16) are captured.

### 10.2 PDF parser (`ingest/pdf_parser.py`)

Responsibility: PDF to clean text **with per-page boundaries preserved** (citations depend on page numbers). Output: parsed text + page map. Acceptance: a parsed PSG yields text whose page numbers match the source PDF.

### 10.3 Chunker (`process/chunker.py`)

Heading/section-aware recursive splitting, target ~800-1200 tokens with ~15% overlap, every chunk tagged with full metadata incl. page. Acceptance: no chunk loses its source/page metadata.

### 10.4 Embedder (`process/embedder.py`)

`EmbeddingProvider` interface; default local bge-small. Batch, cache by content hash. Acceptance: provider swap requires only a config change.

### 10.5 Extractor (`process/extractor.py`)

Responsibility: from a PSG's text, extract structured BE requirements into `be_requirement`, **with a citation (page + verbatim quote) for every field**. Use the LLM with a strict, schema-constrained, JSON-only extraction prompt; reject any field that cannot be tied to a source span. Acceptance: every populated field has a non-empty citation; fields not present in the PSG are null, never invented.

### 10.6 Change detector (`process/change_detector.py`)

On re-crawl, if a `content_hash` changed, store a new `psg_version` and produce a `diff_summary` that is itself grounded (quote the changed passages, cite pages). Acceptance: an edited test PDF produces a new version and a cited diff; an unchanged PDF produces neither.

### 10.7 Stores (`store/`)

`vector_store.py` wraps the vector backend (add, similarity search with score, filter by metadata). `db.py` and `models.py` define schemas and CRUD. Acceptance: round-trip persistence across process restarts.

`vector_store.py` is still the seam every caller imports, but it is a facade over pgvector now (`store/pgvector_store.py`). The Chroma half of the old dual-mode dispatch went away in R5.

### 10.8 Retrieval (`retrieve/`)

`retriever.py`: embed query, top-k (default k=8) with metadata filtering (e.g., by drug/dosage form when specified), return chunks + scores. `reranker.py` (Phase 2): cross-encoder rerank top-k to top-n. Acceptance: on the gold set, recall@8 of the correct source meets the Section 12 target.

### 10.9 Generation (`generate/`)

`llm.py`: `LLMProvider` interface. `prompts.py`: the grounding system prompt. `grounded_qa.py`: orchestrates retrieve to generate, returns `{answer, citations[], refused}`, writes `query_log`. Acceptance: INV-1, INV-2, INV-6 hold under tests, including adversarial questions whose answer is not in the corpus.

`turn_gate.py` was added later and is now the reliability boundary. It is the only
place model-authored bytes become user-visible text, and it admits them one claim
at a time. The renderer, not the model, writes the citation markers. See Section
11.

### 10.10 Audit and logging (`common/`)

Structured logging; `audit.py` persists every interaction to `query_log`. Acceptance: every API/UI call yields exactly one durable audit row.

### 10.11 Eval harness (`eval/`)

`gold_set.jsonl`: 30-50 real CRA-style questions, each with expected source doc(s)/page and key answer facts, plus a set of out-of-corpus questions that MUST be refused. `metrics.py`: retrieval recall@k, citation precision (cited sources actually relevant), faithfulness (every answer claim supported by a cited source; LLM-as-judge plus exact-source spot checks), refusal accuracy. `run_eval.py`: CLI that prints a scorecard and fails CI if below thresholds (Section 12). Acceptance: the harness runs end to end and emits a scorecard.

### 10.12 Watchlist (`watch/watchlist.py`)

Build from Drugs@FDA by querying applications where the sponsor is the company, normalize names (`common/text_normalize.py`: lowercase, strip salt forms, sort multi-ingredient combos), seed/augment from uploaded approval letters and a manual list. INV-5: never populate from model memory. Acceptance: the three seed products appear with `source` set to a verified origin.

### 10.13 Matcher (`watch/matcher.py`)

Normalized + fuzzy match of crawled PSG active ingredients against the watchlist; handle combinations and synonyms. Output: matched changes with confidence. Acceptance: on the pasted revised-PSG batch, albuterol and beclomethasone match; unrelated additions do not.

### 10.14 Alerts (`watch/alerts.py`)

For each matched change, emit a cited summary (drug, what changed, source link, page). POC delivery: write to the Watch dashboard and a local digest file; pluggable for email/Slack later. INV-4: never emit a match that was not actually fetched.

### 10.15 Assemble / dossier (`assemble/dossier.py`)

Input: a product (active ingredient + dosage form + RLD). Gather and assemble into one cited brief: (a) matched PSG(s) and the extracted BE requirements with citations; (b) the RLD label via openFDA `drug/label`; (c) applicable guidances via retrieval; (d) dissolution method via the Dissolution DB scrape; (e) a requirements checklist scaffold derived from the PSG fields. Output: a structured Markdown brief (optionally rendered to PDF), every line source-linked. The checklist is a scaffold of what the PSG calls for; it does not assert what the company has done. Acceptance: for a seed product, produces a brief where every item links to a source and no field is fabricated.

### 10.16 API (`api/main.py`, FastAPI)

- `POST /query {question, filters?}` to `{answer, citations[], refused}`
- `POST /assemble {active_ingredient, dosage_form, rld}` to dossier
- `GET /watch/latest?since=` to matched changes
- `GET /products`, `POST /products` (watchlist CRUD)
- `GET /health`
  Acceptance: OpenAPI docs render; every response is reproducible in Postman without internal access.

### 10.17 UI (`ui/app.py`, Streamlit)

> **Superseded. See the top-of-file note.** The UI is not Streamlit and is not
> three pages. It is a Next.js App Router app in `regwatch/frontend/` with five
> surfaces (Ask, Assemble, Watch, White Paper, Deficiency) sharing one shell, a
> URL-scoped CurrentProduct, and an "Under review" product-scope bar. Ask is a
> cited conversational chat. The original three-page intent below is kept for
> design context.

Three pages: Ask (Q&A with inline sources), Assemble (pick a product to a brief), Watch (recent alerts). Sources are always visible and clickable. Acceptance: a non-technical user can ask a question, get a cited answer or a clear "not found," and open the source, with zero command line.

## 11. Citation and refusal spec

**What this section originally said:** every sentence carried `[short-name, p.N]`,
and a refusal returned one exact fixed string. That was the "cite or refuse" rule.
It described v5 and v6. v7 replaced it and is live in production. The fixed
refusal string no longer exists anywhere in the code.

**The rule today: cite the facts, talk like a person.** Every sentence the model
writes is one of three kinds.

1. **Source fact.** Says what FDA guidance requires, recommends, permits or
   prohibits. It must end with the numbers of the passages it came from, placed
   right before the final period, like `[1]` or `[1, 3]`. An uncited source fact
   is dropped by `src/regwatch/generate/turn_gate.py`. That is INV-1, and it is
   enforced in code, not in the prompt.
2. **Reasoning.** Our own reading, going past what the passages say. It carries no
   number and must open with one of four exact phrases, pinned byte for byte in
   `turn_gate.REASONING_FRAME_PREFIXES`: "The guidance does not state this
   directly; my reading is ...", "Reading the guidance together, ...", "My reading
   is ...", "Beyond the guidance, ...". An obligation or a prohibition may not
   hide inside a reasoning sentence. The source-assertion lexicon reclassifies it
   back to a source fact, which then needs a citation or gets dropped.
3. **Conversation.** Greetings, offers, transitions, a question back to the user.
   Plain text, no numbers, no FDA facts.

The model does not write the visible citation markers. It declares its sources per
claim, the gate validates them against the passages actually sent that turn, and
the renderer stamps the markers. A claim whose declared sources do not all resolve
is dropped whole, never partly rewritten. The API returns the validated set as
`citations[]`.

**When the corpus does not answer.** There is no sentinel and no code word. The
model says so in ordinary words, names what it does have nearby, and offers a next
step. The whole reply is plain prose with no passage numbers at all. v6 used a
fixed marker for this and it broke every refusal in the battery, which is why v7
removed it. The turn is still recorded as `refused` with an empty citation list.

**Gate verdicts** in `generate/turn_gate.py`: `answer`, `partial`,
`material_drop`, `no_valid_citations`, `no_evidence`, and
`conversational_decline`, which is new in v7 and fires when every admitted claim
is uncited.

**Retrieval floor.** Passages scoring below `REFUSAL_SCORE_THRESHOLD` (default
0.30) are withheld from the synthesizer before it runs, so it never sees the weak
evidence. The 0.30 value was tuned on the old OpenAI vector space and has not been
revalidated against the current Qwen3 space.

**Unchanged from the original spec.** The model may never use prior knowledge to
fill a gap. Test that with questions whose answers are real-world true but absent
from the corpus. Product scope is resolved before retrieval, so a citation cannot
cross drugs (INV-9). Every query writes exactly one `query_log` row, answered or
not.

## 12. Milestones and definition of done

All six phases are done: scaffold, ingest and extract for the three seed products,
retrieval and cited Q&A with audit logging, Watch, Assemble, and eval. The
demo-polish phase targeted Streamlit, which the Next.js frontend has since
replaced.

The Phase 5 quality bar is still the bar: recall@8 >= 0.90, citation precision
>= 0.95, refusal accuracy >= 0.95, and zero ungrounded claims on the gold set.
Measured numbers live in [EVAL_STATUS.md](EVAL_STATUS.md).

## 13. Testing strategy

Unit tests per module. Integration test for the full ingest to cited-answer path on a fixture PSG. A dedicated **invariants test suite** (`tests/test_invariants.py`) that asserts INV-1 through INV-6, including the refusal-on-out-of-corpus and no-duplicate-on-recrawl cases. CI fails on any invariant or eval-threshold violation.

## 14. Risks and mitigations

- **Scrape fragility (PSG page).** Inspect for a data endpoint; cache; pin selectors in one module; add a smoke test that alerts on layout change.
- **PDF extraction quality.** Validate page-map integrity; spot-check extractions in the gold set.
- **Hallucination.** Mitigated structurally by INV-1/2 and the eval harness, not by trust.
- **Name matching (salts, combinations, synonyms).** Centralize normalization; cover with tests on real combo cases.
- **IT/security on outbound calls.** An internally hosted tool making scheduled outbound requests to fda.gov may be blocked. Raise early; document the fix (run on a dev host or whitelist the FDA domains). Until then it runs locally on public data.

## 15. Instructions to Claude Code

`docs/CLAUDE.md` is the live version of this section. Read that one. What still
holds here:

- Treat the Section 4 invariants as code with tests. If a requested behavior conflicts with them, stop and note it in `DECISIONS.md`.
- Keep `EmbeddingProvider` and `LLMProvider` behind their interfaces. Never hard-code a model in business logic.
- For scraping: find the backing data endpoint on the live PSG page before writing any parser, prefer it over a headless browser, and be a polite crawler.
- Public FDA sources only. Do not touch internal or proprietary data. Do not invent product statuses; pull them from Drugs@FDA or the provided approval letter.
- Where the spec is silent, pick a sensible default and log it. Ask the user only about (a) watchlist products beyond the three seeds and (b) LLM or embedding provider preference.
- Keep functions small and typed. Prefer clarity over cleverness.

## 16. Seed data (verified)

Start ingestion with these three real, verified products. Albuterol and beclomethasone are inhalation aerosols already present in the recent revised-PSG batch; romidepsin is the injectable case.

| Active ingredient           | Dosage form / route              | RLD (brand)                          | Verified source                          |
| --------------------------- | -------------------------------- | ------------------------------------ | ---------------------------------------- |
| Albuterol sulfate           | Inhalation aerosol, metered      | ProAir HFA (Teva)                    | Drugs@FDA + public approval announcement |
| Beclomethasone dipropionate | Inhalation aerosol, metered      | QVAR (Teva)                          | Drugs@FDA + public approval announcement |
| Romidepsin                  | Injection, IV (single-dose vial) | reference per RLD, NDA 208574 (Teva) | ANDA 219099 approval letter              |

---

### Appendix A. Glossary

ANDA: Abbreviated New Drug Application. PSG: Product-Specific Guidance. BE: bioequivalence. RLD: Reference Listed Drug. CRL: Complete Response Letter. RTR: Refuse to Receive. eCTD: electronic Common Technical Document.

### Appendix B. Why this scope is correct

The system speeds up the team's own prep work: retrieval, extraction,
organization, comparison. It never touches FDA-facing authorship or judgment.
That one boundary does double duty. It keeps the tool on the operational-efficiency
side of the FDA's line, away from the credibility burden that falls on
decision-supporting AI. It is also where the money is, because in generics the win
comes from a faster internal cycle. Anything else either crosses the line or is a
later extension on the same backbone.
