# Operating instructions for Claude Code (REGWATCH)

This file mirrors Section 15 of the project spec. **Read this before touching
anything else.**

> **Implementation status. Last updated: 2026-08-11.**
> REGWATCH is built and running in production. It is not a greenfield build. The
> hard rules below are still in force. The phase-by-phase plan in the spec is
> history: phases 0-5 are done (ingest/extract, cited Q&A, Watch, Assemble,
> eval), plus auth, the White Paper populator, and a Next.js App Router frontend.
> Streamlit is retired.
>
> What is actually running today:
>
> - **Deploy.** Fly app `amneal`, release v104 (2026-08-10). A Go proxy holds the
>   public port and serves auth, sessions, feedback, settings and products
>   natively, and orchestrates `POST /query`. The Python RAG core sits behind it
>   on the private network. Frontend on Vercel. Polyglot migration is through
>   step 5.
> - **Database.** Databricks Lakebase Postgres, with pgvector in the same
>   database. Rows, vectors and the audit log all live there. Alembic head
>   `0020_eval_run`. Supabase is no longer the datastore. The old dual-mode
>   SQLite/Chroma path was deleted in R5.
> - **LLM.** Databricks Model Serving inside the company tenant. Serving alias
>   `workspace.default.regwatch`, served model `gpt-oss-120b-080525`. One model
>   serves every role: router, synthesizer, extractor. See
>   `docs/DATABRICKS_ADOPTION_2026-07-28.md`.
> - **Embeddings.** Databricks Model Serving as well, endpoint
>   `workspace.default.regwatch-embed`, Qwen3, 1024 dimensions, profile
>   `ep_2e7368b354d911ea3a013c3125e276c2`. All 5,494 chunks are embedded on that
>   profile (measured 2026-08-11).
> - **Data residency (D1) is closed.** Generation, embeddings and the database
>   are all inside the company's Databricks tenant. OpenAI is the rollback path
>   only.
> - **Answer policy.** v7 selective citation, live in prod. Cite the facts, talk
>   like a person.
>
> For current state and open work see `README.md`, `docs/ARCHITECTURE.md`,
> `docs/DEPLOY.md`, `docs/PROD_READINESS.md` and `docs/ROADMAP.md`.

## Hard rules

1. **Compliance Invariants are code, not guidelines.** INV-1 through INV-9
   (Section 4 of the spec) are enforced as tests: `tests/test_invariants.py` plus
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
  - `uv run pytest -q --cov=src/regwatch --cov-fail-under=80`
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
  `EMBEDDING_PROVIDER` has NO default (unset refuses to boot; 2026-08-14
  postmortem). Production uses a named embedding profile
  (`ACTIVE_EMBEDDING_PROFILE`, migration 0015): the Databricks-hosted Qwen3
  endpoint, 1024-dim, vectors in the `chunk_embedding` table. The `legacy`
  `chunk.embedding` column (1536-dim) is a frozen historical space: its OpenAI
  embedder was removed 2026-08-17, so rollback means a previously promoted
  profile, never the legacy column. `echo` is the test provider.
- **LLM.** `LLM_PROVIDER` has NO default either. Production runs `databricks`:
  one Model Serving endpoint (`DATABRICKS_LLM_MODEL`) for every role. `echo` is
  for tests. The OpenAI-API and Anthropic provider paths were removed
  2026-08-17 (the OpenAI SDK remains only as the Databricks wire transport).
- **Refusal threshold.** `REFUSAL_SCORE_THRESHOLD`, default 0.30. Passages
  scoring below it are withheld from the synthesizer before it runs. Note that
  0.30 was tuned on the old OpenAI vector space and has never been validated
  against the current Qwen3 space.
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
  drafter, an auto-emailer to FDA, a regulatory recommender). They cross the line
  in Section 4.
- Don't use a language model's memory to fill data gaps. Verified sources only.
