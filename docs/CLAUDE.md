# Operating instructions for Claude Code (REGWATCH)

This file mirrors Section 15 of the project spec. **Read this before touching anything else.**

> **Implementation status (2026-06-16):** REGWATCH is built and shipped on `main`,
> not a greenfield build. The hard rules below are still in force, but the
> phase-by-phase "How to build" plan is historical — Phases 0–5 are done
> (ingest/extract, cited Q&A, Watch, Assemble, eval), plus auth, the White Paper
> populator, the dual-mode SQLite/Postgres+pgvector datastore path, and a Next.js
> App Router frontend (Streamlit is fully retired). For current state and open
> work see `README.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`,
> `docs/PROD_READINESS.md`, and `docs/ROADMAP.md`.

## Hard rules

1. **Compliance Invariants are code, not guidelines.** INV-1 through INV-9 (Section 4 of the spec) are enforced as tests — `tests/test_invariants.py` plus the per-feature invariant tests (`test_cross_drug_leak.py`, `test_multiform_clarify.py`, `test_citations.py`, `test_whitepaper_populator.py`, which cover INV-7/8/9). If a requested behavior would violate any invariant, stop and record it in `DECISIONS.md` instead of implementing.
2. **No internal / proprietary data in this POC.** Public FDA sources only. No SOPs, no internal pipeline data, no submission drafts.
3. **No autonomous regulatory action.** The system surfaces, organizes, compares, and cites. It does not author, decide, file, or send.
4. **No fabrication.** Never narrate a run that did not happen. A PSG that was not actually fetched does not exist.
5. **Provider interfaces are sacred.** `EmbeddingProvider` and `LLMProvider` (in `src/regwatch/process/embedder.py` and `src/regwatch/generate/llm.py`) gate every model call. Never hard-code a model name in business logic.

## How to build

- Build phase by phase (Section 12 of the spec):
  - Phase 0 — Scaffold
  - Phase 1 — Ingest + extract (seed)
  - Phase 2 — Retrieval + cited Q&A
  - Phase 3 — Watch
  - Phase 4 — Assemble
  - Phase 5 — Eval + demo polish
- After each phase: run the full test suite (`uv run pytest`) and check the phase's DoD before moving on.
- Map commit messages to phases. Keep functions small, typed, and clear.

## How to scrape

- Before writing any PSG parser, **inspect the live PSG page** (`https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm`) for a backing data endpoint. DataTables-style pages usually have one returning JSON or HTML. Prefer that endpoint over headless browsing. Use Playwright only as a last resort.
- Be a polite crawler: descriptive `User-Agent`, retry with exponential backoff, cache aggressively, never hammer.

## Defaults you can take without asking

- Embedding provider: local `BAAI/bge-small-en-v1.5` via `sentence-transformers`.
- LLM provider: pluggable, default `openai` with model from `LLM_MODEL` (an `anthropic` provider also ships). The `echo` provider is for tests.
- Refusal threshold: configurable via `REFUSAL_SCORE_THRESHOLD` (tuned on the gold set).
- Vector store: ChromaDB persistent client at `data/chroma`.
- Structured store: SQLite at `data/regwatch.db`.

## When to ask the user

Only for:
- Watchlist products beyond the three seeds (albuterol, beclomethasone, romidepsin).
- LLM / embedding provider preference if they have one.

Otherwise: pick a default, log it in `DECISIONS.md`, and proceed.

## Don't

- Don't drop `INV-*` tests.
- Don't add behaviors that "should be easy to add later" (a hosted submission drafter, an auto-emailer to FDA, a regulatory recommender). They cross the line in Section 4.
- Don't use a language model's memory to fill data gaps. Verified sources only.
