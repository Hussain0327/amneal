# Plan: Multi-source RegWatch — Router → Handlers → Synthesizer, Next.js UI, in-house LLM

> **Archived planning doc.** The Next.js migration described here has landed,
> Streamlit has been retired, and the current UI lives in `regwatch/frontend/`.
> Keep this file as history only; use `README.md`, `docs/ARCHITECTURE.md`,
> `docs/PROD_READINESS.md`, and `docs/DEPLOY.md` for current state.

> **Original status when written:** planning only. That status is now historical;
> the rebuild has since shipped. The text below remains the original "what we
> need to do before we start" reference, preserved for context.
>
> Current path note: the Next.js UI now lives in `regwatch/frontend/`, not the
> originally planned `web/` path used later in this document.

---

## Context — why we're doing this

`regwatch` today is a single-source RAG tool: `POST /query` calls
`generate.grounded_qa.ask()`, which retrieves PSG (Product-Specific Guidance) text
chunks from Chroma and answers with `[short_name, p.N]` citations or refuses. It
works, but three things are wrong:

- **Broken.** Two retrieval defects. (1) *Cross-drug leak* — `ask()` only filters
  by drug if the caller passes a filter, and `assemble/dossier.py:228` calls it
  with **no filter**, so a query about drug A can pull chunks about drug B.
  (2) *Pulls everything* — there is no routing; every question hits the one RAG
  path even when the answer lives in structured FDA data (shortage status,
  TE codes, NDCs). The fix is a router that touches only the relevant source and a
  PSG retriever with a **mandatory active-ingredient filter**. Neither counts as
  done until the **eval goes green** (the citation parser + gold set), which is the
  gate.
- **Unclear to non-technical users.** The system answers or refuses, binary. It
  never restates what it understood and never asks a clarifying question — it can
  silently hand back a wrong-drug answer. Fix: **clarify-over-guess** and
  **restate-what-it-understood**, enforced in the synthesizer and surfaced in the
  UI as clickable options + a plain-language interpretation line.
- **Ugly.** Streamlit (`ui/app.py`). Fix: a **Next.js (TypeScript)** UI — clean
  question box, cited answer, sources as clickable links, an interpretation line,
  and clarifying prompts the user clicks instead of retyping.

### Target architecture

```
Next.js UI  ──HTTP──>  FastAPI (unchanged language)
                          │
                          ▼
                       Router          rules first (keyword→source), LLM tool-select later
                          │            decides which source(s) answer; flags ambiguity
                          ▼
            Handlers (one per source, NO LLM in any handler)
              • PSG        → retriever WITH mandatory ingredient filter   (RAG / Chroma)
              • Drugs@FDA  → SQL over loaded openFDA application rows
              • Orange Book→ SQL over product/patent/exclusivity tables
              • NDC        → SQL lookup
              • Shortages  → SQL over openFDA shortage rows
              • REMS       → SQL over scraped REMS rows
                          │   (returns typed rows + chunks as unified Evidence)
                          ▼
            ONE in-house Synthesizer LLM
              reads the actual chunks + rows; writes the cited answer,
              OR asks a clarifying question, OR refuses. In-house only
              (Azure tenant or local) — queries never leave Amneal.
                          │
                          ▼
            Compliance layer (existing INV-1..6 + new INV-7..9)
```

**The load-bearing fact:** only **PSG** is RAG (chunk/embed/retrieve). The other
five are **structured data you query, not text you embed** — adding them grows row
count, not retrieval load.

### Decisions locked in this planning round
- **Scope:** plan the entire vision (all 5 sources) now; build later in phases.
- **UI:** Next.js App Router (TypeScript).
- **In-house LLM:** **undecided** — design behind the existing `LLMProvider`
  interface; default recommendation is a single configurable OpenAI-compatible
  provider (covers Azure OpenAI / vLLM / Ollama by config). **Must be decided
  before Phase 4.** See Open Decisions.

---

## Open decisions / prerequisites (resolve before the relevant phase)

| # | Decision | Needed by | Recommendation |
|---|---|---|---|
| D1 | In-house LLM target (Azure OpenAI vs local vLLM/Ollama) | Phase 4 | One generic OpenAI-compatible provider (`base_url`+`key`+`model`); switch backends by config only |
| D2 | openFDA API key (raises rate limits) | Phase 6 (Drugs@FDA/NDC/Shortages) | Set `OPENFDA_API_KEY`; loaders already read it |
| D3 | Orange Book download URL + cadence | Phase 6 (Orange Book) | FDA "Orange Book Data Files" ZIP; monthly refresh via CLI command |
| D4 | REMS data source — verify what's actually downloadable (no openFDA endpoint; REMS@FDA files / Public Dashboard) | Phase 6 (REMS) | Spike first; if only HTML, scrape with `selectolax` (already a dep) |
| D5 | Next.js hosting / how it reaches the in-house API (same network) | Phase 5 | `web/` folder in this repo; calls FastAPI via `NEXT_PUBLIC_API_BASE`; CORS allowlist in settings |
| D6 | Commit the 4 uncommitted health-fixes now in the working tree before starting | Phase 0 | `grounded_qa.py`, `eval/metrics.py`, `watch/watchlist.py`, `process/embedder.py` + their tests — land these first |

---

## Build phases (eval gate green is required to exit each phase)

| Phase | Outcome | Gate |
|---|---|---|
| **0** | **Citation grammar centralized** (refactor, zero behavior change). Extract `common/citations.py`; repoint `grounded_qa` + `eval/metrics` to it. | Full suite + `run_eval --check-thresholds` green |
| **1** | **Cross-drug leak fixed.** PSG handler skeleton enforces a mandatory `normalized_name` filter; `dossier.py` passes the ingredient filter. New INV-9 test. | Suite green; INV-9 proves PSG is always ingredient-filtered |
| **2** | **Orchestration scaffold (PSG-only).** `orchestrate/` package: `types.py`, `router.py` (PSG + refusal rules), `pipeline.answer_query`, `synthesizer.py` with an **echo-safe deterministic fallback**. Repoint `/query` to `answer_query`. | All existing API + invariant tests still pass (via echo path + preserved patch points) |
| **3** | **Synthesizer UX.** clarify-over-guess + restate-what-it-understood via a JSON LLM contract; CORS; new `/query` response fields (`interpretation`, `clarify`, `status`). INV-7/INV-8 tests + a clarify gold item. | Suite green incl. new invariants |
| **4** | **In-house LLM provider** (D1) behind `LLMProvider`; becomes the synthesizer default; secrets in settings. | Suite green under echo; manual smoke against the real endpoint |
| **5** | **Next.js UI** replaces Streamlit feature-for-feature (Ask / Assemble / Watch) + interpretation line + clickable clarify options + clickable source links. | Manual end-to-end against the API; UI lint/build |
| **6** | **Structured sources, one at a time:** Drugs@FDA → NDC → Shortages → Orange Book → REMS. Each = table + loader + handler + router keywords + CLI command + ≥1 gold item. | **Run the eval gate after EACH source**; recall/precision must not regress |
| **7** | **De-dup HTTP helper** — lift the tenacity retry/backoff + paginated fetch out of `watch/watchlist.py` + `watch/aliases.py` into `connectors/_http.py`; repoint both. | Watch tests pass |

Rationale for order: PSG is the RAG core and the hardest to get right, and the
existing code/tests already cover it — fix it first. The three openFDA APIs are
cheap broad coverage next. Orange Book (download+parse) then REMS (scrape, most
fragile, narrowest use) come last.

---

## New module layout

```
src/regwatch/
├── common/
│   └── citations.py            # NEW — single citation grammar (de-dups the regex)
├── orchestrate/                # NEW package — the Router→Handlers→Synthesizer unit
│   ├── types.py                # RouteDecision, Evidence, HandlerResult, SynthResult, ClarifyOption
│   ├── router.py               # decide(question, filters) -> RouteDecision   (rules first)
│   ├── pipeline.py             # answer_query(question, *, filters, k) -> SynthResult  (+ INV-6 audit)
│   ├── synthesizer.py          # synthesize(...) -> SynthResult  (the ONE LLM; echo-safe fallback)
│   └── handlers/
│       ├── base.py             # Handler Protocol + HandlerResult
│       ├── psg.py              # wraps retrieve() WITH mandatory ingredient filter
│       ├── drugsfda.py  ndc.py  shortages.py  orange_book.py  rems.py   # SQL/lookup, NO LLM
└── connectors/                 # NEW package — loaders (download/scrape → SQL tables)
    ├── _http.py                # extracted httpx + tenacity retry/backoff (de-dup, Phase 7)
    ├── drugsfda_loader.py  ndc_loader.py  shortages_loader.py
    ├── orange_book_loader.py   # ZIP of tilde-delimited files
    └── rems_loader.py          # scrape

web/                            # NEW — Next.js App Router TypeScript UI (replaces ui/app.py)
```

`generate/grounded_qa.ask()` stays intact — the synthesizer **reuses** its
citation-validation and refusal machinery rather than replacing it.

---

## Core data shapes (`orchestrate/types.py`)

- **RouteDecision** `{sources: list[str], confidence: float, ambiguous: bool,
  entities: {active_ingredient, normalized_name, dosage_form, route, appl_no, ndc,
  te_code}, matched_keywords: dict, method: "rules"|"llm_fallback"}`
- **Evidence** — unified for PSG chunks AND structured rows:
  `{kind, cite_token, source_url, snippet, [PSG: short_name,page,chunk_id,doc_id,
  version_id], [structured: row_key, table, fields]}`
- **HandlerResult** `{source, evidence: list[Evidence], ok: bool, note}`
- **ClarifyOption** `{label, value: dict}` — `value` is filters to resend (e.g.
  `{"active_ingredient": "albuterol sulfate"}`), so the user clicks instead of retyping.
- **SynthResult** — a **structural superset of `QAResult`** (same 6 field
  names/types: `answer, citations, refused, model_name, audit_id, retrieved`) **plus**
  `interpretation: str|None`, `clarify: list[ClarifyOption]`, `status:
  "answer"|"clarify"|"refused"`. Being a superset means `api/main.py` and
  `eval/metrics.evaluate` (which read `.retrieved/.citations/.refused/.answer`) keep
  working unchanged.

---

## Citation grammar — centralize then extend (`common/citations.py`)

This is the single most important refactor for the eval gate to stay meaningful.
The regex `r"\[([A-Za-z0-9_./-]+),\s*p\.(\d+)\]"` is **currently duplicated** in
`generate/grounded_qa.py:55` and `eval/metrics.py:24`. Move it to one module both
import, then add structured-source tokens:

| Source | Inline token | Validated against |
|---|---|---|
| PSG (existing) | `[PSG_020503, p.3]` | retrieved chunk `(short_name, page)` |
| Drugs@FDA | `[DAF:208574]` | application_number returned by handler |
| Orange Book | `[OB:208574/001]` | `(appl_no, product_no)` row key |
| NDC | `[NDC:0093-1234-56]` | package_ndc row key |
| Shortages | `[Shortage:<id>]` | shortage row key |
| REMS | `[REMS:<program_id>]` | REMS program row key |

Each structured token carries the **primary key the handler actually returned**, so
the synthesizer cannot cite a row it didn't retrieve — extending INV-1's
anti-fabrication guarantee to structured sources. `metrics.faithfulness()` switches
to a union `ANY_CITE` so a structured-only answer isn't scored 0% faithful;
`metrics._match_source()` gains a `(kind, row_key)` branch.

**Compatibility:** add the new `Citation` fields (`kind="psg"` default, `row_key`,
`table`) **with defaults at the end** so `QueryCitation(**c.__dict__)`
(`api/main.py:89`) and `asdict(c)` (audit) keep working.

---

## Cross-drug leak fix (Phase 1 — the headline correctness fix)

- **PSG handler** (`orchestrate/handlers/psg.py`) builds the filter **internally,
  never optional**: `filters = {"normalized_name": canonical_name(ingredient)}`
  (+ `dosage_form` if known), then calls the existing `retrieve()` /
  `rerank_passages()`. Use `normalized_name` (the exact metadata key written at
  `ingest/pipeline.py:149`, matched with `$eq` by `retriever._build_where`), **not**
  the raw `active_ingredient` display string. Works against the current index — no
  re-ingest needed (the metadata is already there).
- **No ingredient extracted → clarify, do not guess.** The pipeline returns
  `SynthResult(status="clarify")` whose options are the distinct in-corpus drugs
  (`PsgDocument.normalized_name`) or `list_watchlist()`. Only refuse if the corpus
  is genuinely empty (preserves INV-2 + the existing empty-corpus refusal test).
- **`assemble/dossier.py:228`** — one-line args change: pass
  `filters={"normalized_name": canonical_name(active_ingredient)}` (the module
  already imports `canonical_name`).

---

## Synthesizer (`orchestrate/synthesizer.py`) — the one LLM

New prompts in `generate/prompts.py` (extend `GROUNDED_QA_SYSTEM`). LLM returns
**JSON** via the existing `response_format="json"` hook:

```json
{ "status": "answer|clarify|refuse",
  "interpretation": "You're asking about the BE study design for albuterol sulfate inhalation aerosol.",
  "answer": "<prose with inline citation tokens>",
  "clarify_options": [{"label": "...", "value": {"active_ingredient": "..."}}],
  "refusal_reason": "<short>|null" }
```

Prompt rules: answer ONLY from provided evidence (INV-1/3); every claim carries a
citation token; **always** emit a one-line `interpretation` (restate); if answerable
by >1 entity or evidence spans drugs the user didn't disambiguate → `clarify` with
2–5 options (never guess); insufficient evidence → `refuse`.

**Python-side guards (do not trust the LLM blindly), reusing `grounded_qa` logic:**
- **Echo-safe fallback (mandatory):** tests run under `LLM_PROVIDER=echo`
  (`conftest.py:24`), where JSON parse yields `{"echo": ...}`. On parse failure, fall
  back to the deterministic path (refuse if no evidence; else validate citations like
  today). Without this, every test breaks.
- Re-apply the existing refusal gates: answer-with-body-but-no-valid-citations →
  refuse (`grounded_qa.py:208`); strip fabricated markers from prose
  (`grounded_qa.py:222`). Structured token "valid" = `row_key` present in handler
  evidence.
- **INV-2 stays pre-LLM:** if PSG is the only source and top score <
  `refusal_score_threshold`, refuse without calling the synthesizer
  (`grounded_qa.py:167`). Structured sources' weak-signal = "0 rows" → `ok=False`; if
  all selected handlers are empty, pipeline refuses pre-LLM.
- **Provider resolution patch-point (highest risk):** existing tests
  `monkeypatch.setattr(qa_mod, "get_llm_provider", ...)`. The synthesizer must resolve
  its provider via `generate.grounded_qa.get_llm_provider` (or keep the PSG path
  flowing through `ask()`) so those patches still intercept. Verify with
  `test_query_refuses_on_empty_corpus`.
- **INV-6:** `pipeline.answer_query` calls `log_query(mode="qa", ...)` exactly once on
  every path (answer/clarify/refuse); log the `RouteDecision` for auditability.

---

## New compliance invariants (new `tests/test_orchestration_invariants.py`)

- **INV-7 Router isolation** — `answer_query` invokes only handlers in
  `decision.sources` (spy on each handler; assert non-selected ones never run).
- **INV-8 Every structured claim cites a real row** — a stubbed answer citing
  `[OB:999/999]` not in evidence is stripped/collapsed, exactly like the PSG
  `PSG_FAKE` case (`test_grounded_qa_citations.py`).
- **INV-9 PSG always ingredient-filtered** — spy on `retriever.retrieve`; assert the
  PSG handler always passes a non-empty `normalized_name` filter, and that with no
  ingredient it returns `ok=False` → pipeline yields `status="clarify"` (the
  cross-drug-leak regression test).
- Extend **INV-6** audit test to cover the clarify and answer paths through
  `answer_query`.

---

## Eval gate extension (`eval/`)

- Keep all 11 existing gold items unchanged (they pin INV-1/2 behavior).
- Add structured-source gold items with `expected_sources` keyed by
  `kind`+`row_key`; add a **router-precision** subset tagged with which sources
  should fire; add a **clarify** item (`must_clarify: true`).
- `GoldItem` gains structured `expected_sources` + `must_clarify`;
  `evaluate()` treats `must_clarify` like `must_refuse` (grade the decision, don't
  penalize recall/precision); `_match_source` gets the structured branch.
- `run_eval.py:104` passes `ask_callable=lambda q: answer_query(q)` (signature
  adapter — `answer_query` is keyword-only). Keep the empty-store clean-skip
  (`run_eval.py:95`). Thresholds stay the gate: recall@k ≥ 0.90,
  citation_precision ≥ 0.95, refusal_accuracy ≥ 0.95.

---

## New-source recipe (Phase 6 — apply once per source)

1. **Table** — SQLModel class in `store/models.py` (auto-registered; `init_db()`
   imports the module and runs `create_all`). No migration system — defining +
   importing the class is enough; existing DBs gain the table on next `init-db`.
2. **Loader** — `connectors/<source>_loader.py` reusing the httpx + tenacity pattern
   (today duplicated in `watch/watchlist.py:58` & `watch/aliases.py:34`). Map rows →
   SQLModel, upsert on a natural key, normalize the ingredient column with
   `canonical_name()`/`stripped_name()` so handlers join on the user's drug name.
3. **Handler** — `orchestrate/handlers/<source>.py`, NO LLM. `session_scope()` +
   `select()` filtered by `decision.entities`; return typed `Evidence` with the
   source's `cite_token` + public FDA `source_url`.
4. **Router keywords** — add to `router.py` rule table.
5. **CLI** — `@app.command("load-<source>")` in `cli.py` (mirrors `cmd_seed`/`cmd_aliases`).
6. **Eval** — ≥1 gold item; run the gate.

**Per-source specifics:**
- **Drugs@FDA** (`api.fda.gov/drug/drugsfda.json`) — already integrated in
  `watch/watchlist.fetch_drugsfda_for_company`; reuse its pagination/404-break/Lucene
  logic. Table `DrugsFdaApplication`. Cite `[DAF:<application_number>]`.
- **NDC** (`api.fda.gov/drug/ndc.json`) — large; paginate. Table `NdcProduct`. Pure
  lookup, never embedded. Cite `[NDC:<package_ndc>]`.
- **Shortages** (`api.fda.gov/drug/drugshortages.json`) — table `DrugShortage`
  (generic_name, status, presentation, reason, update_date). Cite `[Shortage:<id>]`.
- **Orange Book** — NOT an API: download ZIP, parse `products.txt`/`patent.txt`/
  `exclusivity.txt` (tilde-delimited, header row first). Three tables joined on
  `(appl_no, product_no)`. Cite `[OB:<appl_no>/<product_no>]`. No per-row URL —
  construct the OB product page URL; persist a download date. **The connector recipe
  must allow non-JSON loaders** (don't force it through the openFDA helper).
- **REMS** — NOT an API (D4 spike first). Scrape with `selectolax` (existing dep);
  table `RemsProgram` (program_name, active_ingredient, rems_url,
  requirements_summary, captured_at). Cite `[REMS:<program_id>]`. Parse failure →
  `ok=False` (clarify/refuse), never fabricate (INV-4).

---

## API & UI

- **`api/main.py`:** add `CORSMiddleware` (allowlist from a new
  `cors_allow_origins` setting, default `http://localhost:3000`); repoint `/query`
  to `answer_query`; extend `QueryResponse` with optional `interpretation`,
  `clarify`, `status` (existing clients ignore new fields). Avoid naming any field
  `draft_*`/`submit_*` (INV-3 grep test).
- **`web/` (Next.js App Router, TS):** replaces `ui/app.py` feature-for-feature —
  **Ask** (question box → cited answer, sources as clickable links, interpretation
  line, clickable clarify options that resend filters), **Assemble** (dossier),
  **Watch** (alerts + watchlist). Talks to FastAPI via `NEXT_PUBLIC_API_BASE`.
  Mirror the current Streamlit screens in `ui/app.py` for parity.

---

## Critical files

**Modify:** `src/regwatch/generate/grounded_qa.py` (import central regex; synthesizer
reuses validation), `src/regwatch/eval/metrics.py` (central regex + structured
match), `src/regwatch/api/main.py` (CORS, repoint `/query`, response fields),
`src/regwatch/assemble/dossier.py:228` (filtered `ask`), `src/regwatch/store/models.py`
(5+ new tables), `src/regwatch/cli.py` (loader commands), `config/settings.py`
(in-house LLM, CORS, Orange Book/REMS URLs), `src/regwatch/eval/gold_set.jsonl` +
`run_eval.py`.

**Create:** `src/regwatch/common/citations.py`, the `src/regwatch/orchestrate/`
package, the `src/regwatch/connectors/` package, `web/` (Next.js), and
`tests/test_orchestration_invariants.py`.

**Reuse (do not reinvent):** `retrieve.retrieve` + `retriever._build_where`,
`retrieve.reranker.rerank_passages`, `store.db.session_scope`/`init_db`,
`common.text_normalize.{canonical_name,stripped_name,split_ingredients,is_combo}`,
`generate.llm.LLMProvider` + `get_llm_provider`, `watch/{watchlist,aliases}` openFDA
pagination/retry, `store.vector_store.{add_chunks,similarity_search}`.

---

## Risks

- **Patch-point break (highest):** synthesizer must resolve the provider via the
  symbol the tests patch (`qa_mod.get_llm_provider`), or `test_invariants`,
  `test_grounded_qa_citations`, `test_api` break.
- **Echo JSON:** deterministic fallback is mandatory (tests run under echo).
- **`Citation` field order:** new fields need defaults at the end.
- **`run_eval` callable:** wrap `answer_query` in a `lambda q:` (keyword-only args).
- **No DB migrations:** new tables appear only after re-running `init-db`
  (non-destructive on SQLite).
- **Orange Book / REMS are not openFDA:** non-JSON loaders; keep the connector recipe
  general.
- **INV-3 grep test:** don't introduce endpoint/field names containing
  `/draft`, `/submit`, `file_anda`, `generate_submission`.

---

## Verification (per phase + final)

- **Per phase, the gate:** `uv run ruff check src tests` · `uv run black --check src
  tests` · `uv run mypy src` · `uv run pytest -q` · `uv run python -m
  regwatch.eval.run_eval --check-thresholds` (clean-skips on empty store; enforces
  recall@k ≥ 0.90, citation_precision ≥ 0.95, refusal_accuracy ≥ 0.95 once seeded).
- **Cross-drug leak (Phase 1):** INV-9 asserts PSG retrieval is always
  ingredient-filtered and that a no-ingredient query clarifies rather than answers.
- **Routing (Phase 2/3):** INV-7 asserts only selected handlers run; router-precision
  gold subset grades source selection.
- **Structured citations (Phase 6):** INV-8 + structured gold items assert every
  `[OB:…]`/`[NDC:…]`/… token resolves to a retrieved row.
- **End-to-end (Phase 5):** `uv run uvicorn regwatch.api.main:app --reload` + the
  Next.js dev server (`web/`); manually confirm: cited answer renders with clickable
  source links, the interpretation line shows, a deliberately ambiguous question
  returns clickable clarify options (no guess), and an out-of-corpus question refuses
  with the exact refusal text.
- **In-house LLM (Phase 4):** smoke a real query against the configured in-house
  endpoint and confirm no external network egress.
