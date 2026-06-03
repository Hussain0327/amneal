# regwatch

A regulatory prep accelerator for a generic-drug Clinical Regulatory Affairs team.

It watches FDA Product-Specific Guidances (PSGs), matches changes against the
company's product pipeline, extracts cited bioequivalence requirements, and
answers plain-language questions over the FDA guidance corpus. Every answer
carries a source and a page, or an explicit "not found."

**This is a POC, not a production deployment.** It surfaces, organizes,
compares, and cites public FDA information. It does not author submission
content, render regulatory judgment, or take autonomous action. See
[Section 4 of the spec](#compliance-invariants) for the invariants that
encode this.

## What it does, exactly

- **Watch.** Crawl the PSG database, detect new and revised guidances, match
  them against a verified watchlist built from Drugs@FDA, surface a cited
  summary of what changed.
- **Assemble.** For a target product (active ingredient + dosage form +
  RLD), build a fully cited dossier: PSG(s), extracted BE requirements,
  RLD label from openFDA, applicable guidance via retrieval, dissolution
  method link, and a requirements checklist scaffold.
- **Ask.** Plain-language Q&A over the corpus. Inline `[short_name, p.N]`
  citations on every claim, exact-string refusal when the corpus does not
  contain the answer.

## Non-goals

- No drafting, suggestion, or generation of FDA submission content.
- No regulatory recommendations ("you should run study X"). The system
  reports what the guidance says; the human decides.
- No internal or proprietary data. Public FDA sources only.
- No autonomous action — no filing, no submitting, no email to FDA.
- Not production-deployable. Pluggable interfaces (`EmbeddingProvider`,
  `LLMProvider`) so the IT/AI team can swap models without touching
  business logic.

## Compliance invariants

These are code with tests, not guidelines. See `tests/test_invariants.py`.

| | Invariant | Where it's enforced |
|---|---|---|
| INV-1 | Every factual claim is traceable to a source + page | `process/extractor.py` quote-verbatim check; `generate/grounded_qa.py` citation validator |
| INV-2 | If retrieval is weak, refuse — never guess | Two-layer refusal: pre-LLM (top score below `REFUSAL_SCORE_THRESHOLD`) and post-LLM (refusal string or no valid citations) |
| INV-3 | Operational only — no authoring, no judgment | Prompt design + structural grep against `api/` for forbidden endpoint names |
| INV-4 | Never report a run that didn't happen | `watch/alerts.py` skips any match whose `psg_version` is not in the DB |
| INV-5 | Verified provenance only | `WatchlistEntry.__post_init__` rejects sources outside `{drugsfda, anda_letter, manual}` |
| INV-6 | Every query is audited | `common/audit.py` writes a `query_log` row on every Q&A path |

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ via `uv` |
| Scrape | `httpx` + `selectolax` (`pdfplumber` primary, `pypdf` fallback) |
| Chunking | Heading + page-aware recursive splitter, ~1000 tokens, ~150 overlap |
| Embeddings | Pluggable. Default: local `BAAI/bge-small-en-v1.5` via sentence-transformers |
| Vector store | ChromaDB, persistent on disk |
| Structured store | SQLite via SQLModel |
| Retrieval | Two-stage. Stage 1: `VECTOR_TOP_K=50` (wide). Stage 2: rerank → `RERANK_TOP_K=8`. Reranker off by default; when off, stage 2 is `passages[:rerank_top_k]` |
| LLM | Pluggable. Default `openai/gpt-4o-mini`. `anthropic` and `echo` (test-only) also supported |
| API | FastAPI |
| UI | Streamlit (POC) |
| Tooling | ruff, black, mypy strict on `src/`, pytest |

The LLM provider, model, and reranker are all behind interfaces. Nothing is
hard-coded in business logic.

## Quick start

```bash
# install
uv sync --extra dev --extra llm

# copy env, fill in OPENAI_API_KEY
cp .env.example .env
$EDITOR .env

# init DB + dirs
uv run regwatch init-db

# discover sponsor-name aliases from Drugs@FDA (no guessing)
uv run regwatch aliases --refresh

# seed the three verified seed products (Albuterol, Beclomethasone, Romidepsin)
uv run regwatch seed

# tests (smoke + invariants + eval metrics)
uv run pytest -q

# API
uv run uvicorn regwatch.api.main:app --reload

# UI (separate terminal)
uv run streamlit run src/regwatch/ui/app.py

# eval scorecard
uv run python -m regwatch.eval.run_eval
```

## API

```
POST  /query        grounded Q&A — {answer, citations[], refused}
POST  /assemble     cited dossier for {active_ingredient, dosage_form?, rld?}
GET   /watch/latest matched changes since cursor
GET   /products     watchlist
POST  /products     add a manual product (INV-5 enforced)
GET   /health       liveness
GET   /settings     non-secret config
```

OpenAPI docs at `/docs`. Every response is reproducible in Postman without
internal access.

## Watchlist sources

The watchlist is built from three allowed sources, in this order:

1. `drugsfda` — `api.fda.gov/drug/drugsfda.json`, filtered to applications
   whose `sponsor_name` matches any discovered Amneal variant. Aliases are
   discovered with `uv run regwatch aliases`, not hand-coded. On a recent
   run this returned 8 distinct variants including `AMNEAL EU LTD`,
   `AMNEAL IRELAND LTD`, `AMNEAL PHARMS NY`.
2. `anda_letter` — user-uploaded approval letters, asserted by the user.
3. `manual` — explicit overrides.

INV-5 rejects anything else, including model memory.

## Eval

`src/regwatch/eval/gold_set.jsonl` ships small (10 items: 6 real + 4
must-refuse). Production rollout expands to 30–50 per spec §10.11.

Thresholds (failed-CI when below): `recall@k ≥ 0.90`,
`citation_precision ≥ 0.95`, `refusal_accuracy ≥ 0.95`.

`run_eval.py` exits clean when the vector store is empty, so a fresh
checkout's CI passes; gates only fire once a seed has run.

## Layout

```
config/settings.py        pydantic-settings, all thresholds + secrets here
src/regwatch/
  ingest/                 psg_crawler, pdf_parser, pipeline
  process/                chunker, embedder, extractor, change_detector
  store/                  db, models, vector_store (Chroma wrapper)
  retrieve/               retriever (stage 1), reranker (stage 2, off by default)
  generate/               llm provider interface, grounded_qa, prompts
  watch/                  watchlist, aliases (Drugs@FDA discovery), matcher, alerts
  assemble/               dossier
  eval/                   metrics, run_eval, gold_set.jsonl
  api/                    FastAPI surface
  ui/                     Streamlit POC
  common/                 logging, audit, text_normalize
tests/                    smoke, invariants (INV-1..6), per-module
```

## Build phases

Built phase by phase. After each phase, the full test suite and the phase's
Definition of Done passed before moving on.

| Phase | Outcome |
|---|---|
| 0 | Scaffold, providers, CI, smoke tests |
| 1 | PSG crawler + PDF parser + chunker + embedder + cited BE extraction, idempotent |
| 2 | Two-stage retrieval + grounded Q&A with citations + refusal + audit |
| 3 | Drugs@FDA watchlist, alias discovery, fuzzy matcher, version diff, JSONL digest |
| 4 | Dossier builder + FastAPI + Streamlit |
| 5 | Eval harness + gold set + CI thresholds |

## What's not done

- No real prod deployment story. Streamlit is fine for the demo; the
  production UI is the IT team's call.
- The gold set is 10 items, not 30–50. It needs to be paired with what the
  seed actually ingests, not what was planned to be ingested.
- LLM-as-judge is not wired into the eval. Current metrics are mechanical
  (exact `(short_name, page)` matches). Good for the POC, will undercount
  semantically-equivalent answers.
- The cross-encoder reranker exists as a hook but is off by default. Turn
  on with `RERANKER_ENABLED=true` and tune `VECTOR_TOP_K` upward.
- Auto scheduling via APScheduler is a stub. POC runs ingest on demand.

## License

MIT.
