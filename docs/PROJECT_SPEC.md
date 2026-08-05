# PROJECT SPEC — Codename: REGWATCH

**A regulatory prep accelerator for a generic-drug Clinical Regulatory Affairs team.**
Spec author persona: Principal Software Engineer. Audience: Claude Code (implementer).
Status: v1.0, build-ready. Rename the codename freely.

> **Note — this is the original build spec.** It is preserved as the foundational
> design document. Where the shipped implementation has since evolved, the
> current-state docs win:
>
> - **UI:** now a **Next.js** App Router app in `regwatch/frontend/` — Streamlit is
>   fully retired (the `ui/app.py` and "Streamlit / three pages" references below
>   no longer reflect reality). The five surfaces (Ask, Assemble, Watch, White
>   Paper, Deficiency) render inside one unified shell with a URL-scoped CurrentProduct
>   (`?rp=&appl=`) and a shared "Under review" product-scope bar. **Ask** is a
>   cited conversational chat (citation chips, clarify pills, bottom-pinned
>   composer), not the editorial/three-page layout described below.
> - **LLM:** the OpenAI provider uses the **Responses API** with role-specific
>   models.
> - **Backend additions:** conversational sessions with per-turn audit,
>   current-version retrieval, entity-resolution hardening, a White Paper populator
>   (multi-source cited cells), `POST /resolve` (deterministic entity resolution,
>   not an LLM turn), DB-backed auth on every endpoint except `GET /health`, and a
>   dual-mode datastore (SQLite/Chroma by default; Postgres + pgvector when
>   `DATABASE_URL` is set). Alembic migrations through `0005`.
> - **Invariants:** Section 4 below codifies INV-1..6; three later invariants
>   (INV-7..9, cross-product / structured-citation / resolution-before-retrieval
>   guards) are now also enforced as tests — see the note in Section 4.
>
> See the root `README.md`, `docs/DECISIONS.md`, `docs/PROD_READINESS.md`, and
> `docs/ROADMAP.md` (consolidated open items) for what is true today.

---

## 0. How to read this spec

At the time it was written, this document was the source of truth. It is now an
archived foundational spec; current-state docs win when implementation has
evolved. The Compliance Invariants in Section 4 remain the enduring design
constraints unless a newer decision record explicitly supersedes them.

---

## 1. Context and problem

A generic-drug company files Abbreviated New Drug Applications (ANDAs). To file, the Clinical RA team must know, per target product, exactly what bioequivalence (BE) evidence the FDA expects. The FDA publishes this as Product-Specific Guidances (PSGs): roughly 2,400 documents, revised constantly (one recent biweekly batch added 23 and revised 48). Today a scientist manually checks the PSG database for relevant changes and manually assembles the requirements for each product from scattered FDA sources. That is slow, and in generics speed is the business: the first-to-file / first-approved applicant captures exclusivity windows worth real money.

The opportunity is to compress the team's own prep cycle, not to automate any FDA-facing regulatory judgment. Those are different things, and the boundary between them is both the compliance line and the strategy (Section 4).

## 2. Users

Non-technical regulatory professionals. They will not read JSON or run scripts. Every surface must be a plain-language question-in / cited-answer-out interaction. The single most important UX property is trust: a non-expert cannot detect a confident hallucination, so the system must show its source on every claim and refuse when it has none.

## 3. Goals and non-goals

**Goals**

- G1. **Watch:** detect new and revised PSGs/guidances, match them against the company's real product pipeline, and surface a cited summary of what changed.
- G2. **Assemble:** for a target product, auto-build an organized, fully cited requirements dossier (BE study design, RLD label, applicable guidances, dissolution method, requirements checklist).
- G3. **Cited Q&A:** answer plain-language questions over the corpus, every answer carrying source + page, or an explicit "not found."
- G4. An eval harness that proves the system is faithful (Section 10.11). This is the quality gate.

**Non-goals (do NOT build these)**

- N1. No drafting, generation, or suggestion of content intended for an FDA submission.
- N2. No regulatory decisions or recommendations ("you should run study X"). The system reports what guidance says; the human decides.
- N3. No internal, proprietary, or SOP data in this POC. Public FDA data only.
- N4. No autonomous actions (no filing, no submitting, no emailing FDA).
- N5. Not a production deployment. This is a demoable POC to hand to the internal IT/AI team.

## 4. Compliance invariants (HARD constraints, each must have a test)

These encode both FDA expectations (the Jan 2025 draft guidance treats AI that supports a regulatory decision very differently from AI that only improves operational efficiency) and hard-won lessons. They are invariants, not features.

- **INV-1 Grounding.** Every factual claim in any output must be traceable to a retrieved source passage with a document id and page number. No ungrounded claims, ever.
- **INV-2 Refuse over guess.** If retrieval does not surface a sufficiently relevant passage (Section 11), the system returns an explicit refusal ("Not found in the current FDA guidance corpus"). It never fabricates an answer.
- **INV-3 Operational only.** The system surfaces, organizes, compares, and cites public information. It never authors submission content and never renders a regulatory judgment.
- **INV-4 No fabricated execution.** The system must never report or narrate a process, run, or result it did not actually execute. A match that was not fetched does not exist.
- **INV-5 Verified provenance.** Pipeline and product facts come only from verified sources (Drugs@FDA, uploaded approval letters), never from a language model's memory.
- **INV-6 Auditability.** Every query, its retrieved sources with scores, the generated answer, and whether it refused are logged to a durable store (Section 10.10).

If a requested behavior would violate an invariant, do not implement it; flag it in `DECISIONS.md`.

> **Implementation status (current).** INV-1..6 above remain the enduring
> constraints and are tested in `tests/test_invariants.py`. Three additional
> invariants have since been added and are enforced as tests across the
> resolution, white-paper, and citation code: **INV-7..9** (cross-product
> integrity — never blend two applications' data; structured-citation grammar with
> a central validation guard that collapses any cell whose token is not backed; and
> product-resolution-before-retrieval to prevent cross-drug citation leak). They
> extend, and do not supersede, INV-1..6.

## 5. System overview

Two product functions on one shared backbone.

```
                         ┌─────────────────────────────────────────┐
 PUBLIC FDA DATA         │              BACKBONE                    │
 ───────────────         │                                          │
 PSG DB (scrape)  ─────▶  ingest ─▶ parse ─▶ chunk+tag ─▶ embed ──▶ vector store
 Guidance PDFs    ─────▶                         │                  (Chroma)
 Drugs@FDA (API)  ─────▶  watchlist build        ├─▶ extract BE ──▶ structured DB
 Drug labels(API) ─────▶                         │      fields        (SQLite)
 Orange Book(DL)  ─────▶                         └─▶ version diff ─▶ change log
                         └───────────────┬──────────────┬───────────┘
                                         │              │
                            ┌────────────▼───┐   ┌──────▼─────────────┐
                            │ RETRIEVE +     │   │ WATCH:             │
                            │ GROUNDED QA    │   │ match changes vs   │
                            │ (cited, refuse)│   │ watchlist ─▶ alert │
                            └───────┬────────┘   └──────┬─────────────┘
                                    │                   │
                            ┌───────▼────────┐   ┌──────▼─────────────┐
                            │ ASSEMBLE:      │   │ API (FastAPI) +    │
                            │ dossier builder│──▶│ UI (Streamlit)     │
                            └────────────────┘   └────────────────────┘
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

## 8. Repository structure

```
regwatch/
  pyproject.toml
  README.md
  CLAUDE.md                # operating instructions for Claude Code (mirror of Section 15)
  DECISIONS.md             # running log of choices made
  .env.example
  config/settings.py
  src/regwatch/
    ingest/   psg_crawler.py  guidance_fetcher.py  pdf_parser.py
    process/  chunker.py  embedder.py  extractor.py  change_detector.py
    store/    vector_store.py  db.py  models.py
    retrieve/ retriever.py  reranker.py
    generate/ llm.py  grounded_qa.py  prompts.py
    watch/    watchlist.py  matcher.py  alerts.py
    assemble/ dossier.py
    eval/     run_eval.py  metrics.py  gold_set.jsonl
    api/      main.py
    ui/       app.py
    common/   logging.py  audit.py  text_normalize.py
  tests/
  data/  raw/ (gitignored)  processed/
  scripts/  ops helpers (seeding is the `uv run regwatch seed` CLI command)
```

## 9. Data model (SQLite via SQLModel)

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

Vector store (Chroma) holds chunk text + metadata: `{doc_id, version_id, active_ingredient, normalized_name, dosage_form, route, recommended_date, source_url, page}`.

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

`vector_store.py` wraps Chroma (add, similarity_search_with_score, filter by metadata). `db.py` + `models.py` define schemas and CRUD. Acceptance: round-trip persistence across process restarts.

### 10.8 Retrieval (`retrieve/`)

`retriever.py`: embed query, top-k (default k=8) with metadata filtering (e.g., by drug/dosage form when specified), return chunks + scores. `reranker.py` (Phase 2): cross-encoder rerank top-k to top-n. Acceptance: on the gold set, recall@8 of the correct source meets the Section 12 target.

### 10.9 Generation (`generate/`)

`llm.py`: `LLMProvider` interface. `prompts.py`: a strict grounding system prompt — answer ONLY from provided context, cite every claim as `[<doc short-name>, p.<n>]`, and if context is insufficient, reply exactly with the refusal string. `grounded_qa.py`: orchestrates retrieve to generate, enforces the refusal path (Section 11), returns `{answer, citations[], refused}`, writes `query_log`. Acceptance: INV-1, INV-2, INV-6 hold under tests, including adversarial questions whose answer is not in the corpus.

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

> **Superseded — see the top-of-file note.** The UI is no longer Streamlit and is
> no longer three pages. It is a Next.js App Router app in `regwatch/frontend/`
> with four unified surfaces (Ask, Assemble, Watch, White Paper) sharing one
> shell, a URL-scoped CurrentProduct, and an "Under review" product-scope bar; Ask
> is a cited conversational chat. The original three-page intent below is
> preserved for design context.

Three pages: Ask (Q&A with inline sources), Assemble (pick a product to a brief), Watch (recent alerts). Sources are always visible and clickable. Acceptance: a non-technical user can ask a question, get a cited answer or a clear "not found," and open the source, with zero command line.

## 11. Citation and refusal spec (precise)

- Every answer ends with a **Sources** list: `[short-name, p.N](source_url)` for each cited chunk.
- Inline claims carry `[short-name, p.N]`.
- **Refusal trigger:** top retrieval score below `REFUSAL_SCORE_THRESHOLD` (configurable, tune on the gold set) OR the LLM, under the grounding prompt, determines context is insufficient. On trigger, return exactly: `"I can't find this in the current FDA guidance corpus. I won't guess on a regulatory question."` and set `refused=true`.
- The model is forbidden from using prior knowledge to fill gaps. Test this with questions whose answers are real-world true but absent from the corpus; the system must refuse.

## 12. Milestones and definition of done

- **Phase 0 — Scaffold.** Repo, config, stores, provider interfaces, CI (ruff/black/mypy/pytest), `CLAUDE.md`, `.env.example`. DoD: `uv run pytest` green on a smoke test; app boots.
- **Phase 1 — Ingest + extract (seed).** Crawl and parse PSGs for the three seed products; extract cited BE requirements. DoD: `uv run regwatch seed` populates `psg_document`, `psg_version`, `be_requirement` for albuterol, beclomethasone, romidepsin, every field cited; idempotent re-run.
- **Phase 2 — Retrieval + cited Q&A.** Embed, retrieve, grounded generation with citations and refusal; audit logging. DoD: INV-1/2/6 tests pass incl. adversarial refusals.
- **Phase 3 — Watch.** Build watchlist from Drugs@FDA; change detection; matcher; alerts. DoD: on the pasted batch, albuterol + beclomethasone are flagged with cited diffs; additions correctly ignored; INV-4/5 tests pass.
- **Phase 4 — Assemble.** Dossier builder + the two non-Q&A UI pages. DoD: a seed product yields a fully cited brief, reproducible via `/assemble`.
- **Phase 5 — Eval + demo polish.** Gold set, eval scorecard wired into CI with thresholds; Streamlit polish. DoD targets: recall@8 >= 0.90, citation precision >= 0.95, refusal accuracy >= 0.95, zero ungrounded claims on the gold set.

## 13. Testing strategy

Unit tests per module. Integration test for the full ingest to cited-answer path on a fixture PSG. A dedicated **invariants test suite** (`tests/test_invariants.py`) that asserts INV-1 through INV-6, including the refusal-on-out-of-corpus and no-duplicate-on-recrawl cases. CI fails on any invariant or eval-threshold violation.

## 14. Risks and mitigations

- **Scrape fragility (PSG page).** Inspect for a data endpoint; cache; pin selectors in one module; add a smoke test that alerts on layout change.
- **PDF extraction quality.** Validate page-map integrity; spot-check extractions in the gold set.
- **Hallucination.** Mitigated structurally by INV-1/2 and the eval harness, not by trust.
- **Name matching (salts, combinations, synonyms).** Centralize normalization; cover with tests on real combo cases.
- **IT/security on outbound calls.** An internally hosted tool making scheduled outbound requests to fda.gov may be blocked. Raise early; document the fix (run on a dev host or whitelist the FDA domains). Until then it runs locally on public data.

## 15. Instructions to Claude Code

- Build phase by phase (Section 12). After each phase, run the full test suite and the relevant DoD check before moving on.
- Treat Section 4 invariants as code with tests. If any requested behavior conflicts with them, stop and note it in `DECISIONS.md`.
- Keep `EmbeddingProvider` and `LLMProvider` behind interfaces; never hard-code a model in business logic.
- For scraping: inspect the live PSG page first to find a backing data endpoint before writing any parser; prefer it over a headless browser; be a polite crawler.
- Public FDA sources only. Do not touch internal or proprietary data. Do not invent product statuses; pull them from Drugs@FDA or the provided approval letter.
- Make reasonable default decisions where the spec is silent and log them. Ask the user only for: (a) any watchlist products beyond the three seeds, and (b) LLM/embedding provider preference if they have one. Otherwise proceed with the defaults here.
- Write clear commit messages mapped to phases. Keep functions small and typed. Prefer clarity over cleverness.

## 16. Seed data (verified)

Start ingestion with these three real, verified products. Albuterol and beclomethasone are inhalation aerosols already present in the recent revised-PSG batch; romidepsin is the injectable case.

| Active ingredient           | Dosage form / route              | RLD (brand)                          | Verified source                          |
| --------------------------- | -------------------------------- | ------------------------------------ | ---------------------------------------- |
| Albuterol sulfate           | Inhalation aerosol, metered      | ProAir HFA (Teva)                    | Drugs@FDA + public approval announcement |
| Beclomethasone dipropionate | Inhalation aerosol, metered      | QVAR (Teva)                          | Drugs@FDA + public approval announcement |
| Romidepsin                  | Injection, IV (single-dose vial) | reference per RLD, NDA 208574 (Teva) | ANDA 219099 approval letter              |

---

### Appendix A — Glossary

ANDA: Abbreviated New Drug Application. PSG: Product-Specific Guidance. BE: bioequivalence. RLD: Reference Listed Drug. CRL: Complete Response Letter. RTR: Refuse to Receive. eCTD: electronic Common Technical Document.

### Appendix B — Why this scope is correct

The system compresses the team's own prep time (retrieval, extraction, organization, comparison) and never touches FDA-facing regulatory authorship or judgment. That single boundary is simultaneously the compliance posture (operational efficiency, outside the FDA's credibility-assessment burden for decision-supporting AI) and the competitive strategy (faster internal cycle time, which is where generics are won). Every other capability either crosses that line or is a later extension on the same backbone.
