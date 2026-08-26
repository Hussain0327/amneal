# Operating instructions for Claude Code (REGWATCH)

This file states the hard rules from `docs/ARCHITECTURE.md` sections 2 and 15
(the prime directive and the compliance invariants) in agent-facing form.
**Read this before touching anything else.**

> **Implementation status.**
> REGWATCH is built and running in production. It is not a greenfield build.
> The hard rules below are still in force. Phases 0-5 are done (ingest/extract,
> cited Q&A, Watch, Assemble, eval), plus auth, the White Paper populator, and a
> Next.js App Router frontend. Streamlit is retired.
>
> This file does not restate volatile runtime facts (the deployed Alembic
> stamp, the live Fly release, which provider is serving today, flag states).
> Those rot the moment they are copied here. Read them from their single owner
> instead:
>
> - `docs/PRODUCTION_TRUTH.md`: what actually serves a query today, the live
>   provider and model, the request paths, and how to re-verify each one.
> - `docs/CONFIG_REFERENCE.md`: every environment variable, feature flag and
>   secret, including which layer (code default, `fly.toml`, Fly secret,
>   Actions secret) wins.
>
> For the design that does not change week to week, see `docs/ARCHITECTURE.md`.
> For open work, see `docs/ROADMAP.md`. For what shipped and why, see
> `docs/DECISIONS.md`.

## Hard rules

1. **Compliance Invariants are code, not guidelines.** INV-1 through INV-9
   (`docs/ARCHITECTURE.md` section 15) are enforced as tests: `tests/test_invariants.py` plus
   the per-feature invariant tests (`test_cross_drug_leak.py`,
   `test_multiform_clarify.py`, `test_citations.py`,
   `test_whitepaper_populator.py`, which cover INV-7/8/9). Under v7, INV-1 means
   this: a sentence that states what FDA guidance says must carry the passage
   numbers it came from, and `src/regwatch/generate/turn_gate.py` drops it if it
   does not. Our own reasoning and ordinary conversation carry no numbers and
   assert no FDA facts. The gate, not the prompt, is the enforcement. If a
   requested behavior would violate any invariant, stop and record it in
   `DECISIONS.md` instead of implementing it.
2. **No internal or proprietary data in this POC.** Public FDA sources only. No
   SOPs, no internal pipeline data, no submission drafts.
3. **No autonomous regulatory action.** The system surfaces, organizes, compares
   and cites. It does not author, decide, file or send.
4. **No fabrication.** Never narrate a run that did not happen. A PSG that was not
   actually fetched does not exist.
5. **Provider interfaces are sacred.** `EmbeddingProvider` and `LLMProvider` (in
   `src/regwatch/process/embedder.py` and `src/regwatch/generate/llm.py`) gate
   every model call. Never hard-code a model name in business logic.

## Before you push

- Run the same gate CI runs. `docs/CI_CD.md` maps every CI job to its local
  command and is the authority. The Python lane today is:
  - `uv run ruff check src tests migrations tests_contract scripts`
  - `uv run black --check src tests migrations tests_contract`
  - `uv run mypy src tests tests_contract`
  - `uv run pytest -q -n 4 --dist loadfile --cov=src/regwatch --cov-fail-under=80 --durations=25`
- `TEST_DATABASE_URL` must point at a disposable Postgres with pgvector before
  pytest will run at all; there is no SQLite/Chroma fallback (`tests/conftest.py`).
  Example: `TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5499/regwatch_py_test`.
- Note the scopes. `mypy src` alone is not what CI checks, and `black --check`
  fails on formatting `ruff` lets through. Run `black` after your last Python
  edit, not before.
- Keep functions small, typed and clear.

## How to scrape

- Before writing any PSG parser, **inspect the live PSG page**
  (`https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm`) for a backing data
  endpoint. DataTables-style pages usually have one returning JSON or HTML. Prefer
  that endpoint over headless browsing. Use Playwright only as a last resort.
- Be a polite crawler: descriptive `User-Agent`, retry with exponential backoff,
  cache aggressively, never hammer.

## Defaults you can take without asking

- **Embeddings.** Everything goes through `EmbeddingProvider`, and
  `INGEST_EMBEDDING_PROVIDER` has NO default (unset refuses to boot; 2026-08-14
  postmortem; the old name `EMBEDDING_PROVIDER` still works as a deprecated
  alias). Production uses a named embedding profile
  (`RETRIEVAL_EMBEDDING_PROFILE`, migration 0015). Only two provider classes
  exist: `OpenAIEmbeddingProvider` for real calls and `EchoEmbeddingProvider`
  for tests; there is no Databricks or Qwen provider class. There is
  deliberately NO HNSW index on the live arm; the Lakebase branch is capped at
  512 MiB and an index roughly doubles vector storage, so retrieval exact-scans
  the corpus. See `docs/PRODUCTION_TRUTH.md` for the live model and dimension,
  and `docs/CONFIG_REFERENCE.md` for the variable names and precedence.
- **LLM.** `LLM_PROVIDER` has NO default either. `LLM_PROVIDER=databricks`
  raises `ValueError` in code; there is no working Databricks rollback path.
  `echo` is for tests. Production runs `openai` over the Responses API
  (`client.responses.create`, `store=False`, not Chat Completions), with
  `openai_reasoning_effort` defaulting to `medium`. See
  `docs/PRODUCTION_TRUTH.md` for the live model name.
- **Refusal threshold.** `Settings.effective_refusal_threshold()` resolves a
  per-profile floor from `REFUSAL_SCORE_THRESHOLD_BY_PROFILE`, falling back to
  the global `REFUSAL_SCORE_THRESHOLD` default 0.30. Passages scoring below the
  effective floor are withheld from the synthesizer before it runs. The live
  per-profile value is a Fly secret, not readable from this repo; read it from
  `GET /settings` or `regwatch status`, never print it as a fixed number. The
  Ask confidence band's High cut is derived from the same effective floor
  (floor plus a third of the headroom to 1.0), never a fixed number.
- **Vector store.** pgvector, in the same Postgres database as the structured
  store. There is no other vector backend since R5.
- **Structured store.** Postgres via `DATABASE_URL` (Lakebase in prod, a
  disposable local or CI Postgres otherwise). `DATABASE_URL` is mandatory: the app
  refuses to boot without it.

## When to ask the user

Only for:

- Watchlist products beyond the three seeds (albuterol, beclomethasone,
  romidepsin).
- LLM or embedding provider preference, if they have one.

Otherwise: pick a default, log it in `DECISIONS.md`, and proceed.

## Don't

- Don't drop `INV-*` tests.
- Don't add behaviors that "should be easy to add later" (a hosted submission
  drafter, an auto-emailer to FDA, a regulatory recommender). They cross hard
  rule 3 above.
- Don't use a language model's memory to fill data gaps. Verified sources only.
