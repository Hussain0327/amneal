# Databricks for regwatch: recommendation

## 1. What Databricks can actually do for regwatch

| Workload | Fit | What it replaces | Honest verdict |
|---|---|---|---|
| Inference: generation (synthesis + router + extractor) | **YES, do it** | OpenAI `gpt-5.4-nano` via `LLM_PROVIDER` | Config-only. `get_llm_provider` dispatches on `LLM_PROVIDER` (src/regwatch/generate/llm.py:814-856) and builds `DatabricksProvider` from three env vars (llm.py:819-846). One `fly secrets set`, one command to roll back. |
| Inference: query embedding | **YES, and it is the hard half** | OpenAI `text-embedding-3-small` on the Ask path | Not config-only. `assert_embedding_provider_dim` refuses a Qwen provider against the legacy space (src/regwatch/store/pgvector_store.py:277-283, verified). Requires register -> backfill 10,749 chunks -> HNSW -> promote via src/regwatch/cli.py:165-320. This is where D1 actually closes. |
| Vector store (AI Search / Mosaic Vector Search) | **NO** | pgvector `chunk` table | Filter clause caps at 1024 elements; retriever.py:218-223 (verified) ANDs a ~1,794-element `version_id $in` list on every production query. L2 metric vs the `1 - d/2` convention the 0.30 threshold is calibrated on (pgvector_store.py:596-602, verified). $204.40/mo floor for 0.5% of one unit. Async Delta Sync can leave superseded PSG versions citable. |
| OLTP store (Lakebase) | **NOT NOW** | Supabase Postgres | Technically credible (GA, PG17, pgvector 0.8.0 + hnsw) and the repo has near-zero Supabase SDK coupling. But it closes zero of D1, and it risks migration 0011's `CREATE EVENT TRIGGER` inside `release_command` (the 2026-07-07 outage class) plus the pgx session-pooler assumption (go/internal/store/pool.go:100-105). 208 MB database, 14 queries/day. No forcing function. |
| Corpus / ETL (UC Volumes, Delta, Lakeflow) | **NO (fix the real bug cheaply instead)** | ephemeral `data/` + GitHub Actions watch cron | The real defect is narrow: `parsed_text_path` points at an ephemeral disk and `_read_parsed_text` swallows the miss, degrading the cited diff on revisions. Total corpus text is ~9 MB. Fix with a nullable TEXT column, not a lakehouse. GitHub Actions has run 23/23 clean. |
| Eval (MLflow 3 + scorers) | **OPTIONAL, cheapest entry point** | src/regwatch/eval/run_eval.py printing a table | Four env vars, no migration, works from apps outside Databricks. Gives persisted run history for the legacy-vs-Databricks comparison. Keep INV metrics as deterministic code scorers, never LLM judges. |
| Hosting (Databricks Apps) | **NO** | Vercel frontend / Fly Go edge | Apps cannot be public and cannot bypass SSO. Kills it for analyst-facing UI. No documented Go support. |
| Governance (Unity Catalog lineage/audit) | **LATER, and never as a replacement** | nothing | UC lineage is data-access evidence. Answer provenance lives in `query_log`, which `answer_feedback` and `whitepaper_run` foreign-key into. Do not let a UC pitch imply the INV audit trail can retire. |
| Agent Bricks / Genie / external-model endpoints | **NO** | - | A managed agent cannot carry INV-1..INV-6 or write the audit rows. External models forward the prompt to OpenAI, which is the leak. |

## 2. The one thing that makes this worth doing

D1: analyst queries and the watchlist are confidential; the FDA corpus is public. Today every Ask leaks the verbatim question to OpenAI twice.

Both leaks are provider calls, not storage calls, and there are exactly two:

- `qv = embed_query(embedder, query)` at src/regwatch/retrieve/retriever.py:212 (verified; the only non-definition call site)
- the synthesizer at src/regwatch/generate/grounded_qa.py:1498

The Go edge adds no third egress: it does zero inference and zero vector math and POSTs the question to Python's `/internal/query/compute` (go/internal/api/ragclient.go:147-181). The reranker is local and off by default (config/settings.py:200).

**Does Databricks close both halves? Yes, on the code path, and only after both switches move.** `LLM_PROVIDER` and `EMBEDDING_PROVIDER`/`ACTIVE_EMBEDDING_PROFILE` are independent settings with nothing coupling them. Shipping the generation flip alone and calling D1 closed is exactly the security theater the requirement warns about; the internal status until the profile is promoted must read "D1 NOT CLOSED".

**What stays open, and it is legal, not engineering:**

- "Our data stays in Amneal's tenant" is **false**. Databricks Model Serving is serverless-only and the serverless compute plane runs in Databricks' account.
- Foundation Model APIs pay-per-token retains inputs and outputs **up to 30 days**, in-region and customer-isolated, for abuse monitoring. Databricks does commit not to train on inputs or outputs. Whether the 30-day window also applies to Custom Model Serving of self-registered weights is UNVERIFIED and must be obtained in writing.
- FMAPI compliance docs state Databricks "might process your data outside of the region and cloud provider where your data originated"; storage stays in-Geo, processing may not.

The only sentence we can defend: *"Analyst queries leave Amneal to Databricks as a contracted processor under a DPA, are not used to train any model, are retained at most 30 days for abuse monitoring, and never reach OpenAI."* If Amneal's bar is zero retention or processing-residency, FMAPI fails on its own terms and the answer is Custom Model Serving (about $1,022/mo per warm GPU_MEDIUM endpoint) or self-hosted vLLM inside Amneal's network. **Get that answer before the backfill.**

## 3. Recommended path

Databricks as the **inference plane only**. Supabase stays the one and only datastore, Fly keeps both process groups, Vercel keeps the frontend, GitHub Actions keeps CI and the watch cron. Foundation Model APIs pay-per-token against Databricks-hosted open-weight models, not Custom Model Serving.

### Step 0 - Workspace, legal gate, and two curls (no repo change)

**Amneal already runs Databricks** (owner-supplied, 2026-07-28). That removes vendor selection and net-new procurement from the critical path: the DPA exists, the security review happened, and "may our data go to Databricks" is already answered institutionally at the company level. It does NOT by itself answer three things, which is what this step is now for: (a) which tier and add-ons that existing workspace carries, (b) whether Foundation Model APIs are enabled on it or disabled by policy, and (c) whether the FMAPI abuse-retention window was in scope of the agreement Amneal signed. Ask the existing workspace owner, not procurement.

**Work.** Against the existing Amneal account, confirm or request: a workspace in AWS us-east-1 (match the Supabase region); Enterprise tier plus the Enhanced Security and Compliance add-on; enable the Compliance Security Profile (this flips the default-ON "partner-powered AI features" setting, which otherwise routes Assistant/Genie prompts to Azure OpenAI, to off); disable cross-Geo processing; serverless egress network policy in Restricted Access mode; service principal with a long-lived PAT (**not** M2M OAuth: `shared_databricks_openai_client` is lru_cached on (base_url, token) with no refresh, src/regwatch/common/llm_clients.py:31-53, so a 1-hour token goes stale in a long-lived Fly process).

Engineering does four curls and records raw responses:
- `POST https://<host>/serving-endpoints/chat/completions` and `POST https://<host>/ai-gateway/mlflow/v1/chat/completions` - which OpenAI root does this workspace actually serve? The SDK appends `/chat/completions` to `DATABRICKS_LLM_BASE_URL`, so a wrong root is a 404 on every call, not a boot failure.
- The matching `/embeddings` paths with `{"model": "...", "input": ["x"], "dimensions": 1024}` - exactly the payload the provider builds (src/regwatch/process/embedder.py:392-396).

Probe **two** embedding candidates, not one: `databricks-qwen3-embedding-0-6b` (Public Preview, instruction-aware, dims configurable to 1024) and `databricks-bge-large-en` (GA, 1024, documented as normalized). *[Graft from critique of the all-in design: staking an immutable profile fingerprint on a Preview endpoint is a change-control liability, and the Preview model's normalization behavior is undocumented while the GA one's is not.]*

Legal, in writing: (a) is "processor swap, <=30-day abuse retention, no training" acceptable for D1; (b) does that retention window apply to custom endpoints; (c) any GxP / 21 CFR Part 11 position. Because Amneal already runs Databricks, this is a scope question against an existing agreement ("does our DPA already cover Model Serving inference payloads?"), not a new vendor approval. That is a much shorter conversation, but it is still the gate: an existing agreement covering analytics workloads does not automatically cover shipping analyst queries to a hosted LLM.

**Gate.** Chat returns 200 with non-empty content. `response_format={"type":"json_object"}` returns 200 (llm.py:598-599 has no fallback; the router and extractor would raise). Embeddings return `data[0].embedding` of length exactly 1024, all finite, L2 norm within 1e-3 of 1.0 - the provider hard-rejects otherwise (embedder.py:463-479), which fails 100% of calls. Legal has said yes or no. **If legal says no, STOP; steps 1-9 are the wrong architecture.**

**Rollback.** Delete the service principal. Nothing shipped.

**Effort.** 2 days engineering. Calendar time is now workspace access plus a DPA scope check against an existing agreement, not vendor procurement.

### Step 1 - PR A: guards and tripwires, shipped inert

**Work.**
- Enforce an `https://` scheme in `_normalize_optional_provider_value` (config/settings.py:101-107, verified: it only calls `str(v).strip()` today). A typo'd `http://` base URL would ship confidential queries in plaintext.
- Change `databricks_llm_model` default from `"google/gemma-4-31B-it"` (a HuggingFace repo id, not a Databricks endpoint name) to `None`, so a half-configured operator fails loudly at the constructor (llm.py:823-833) instead of 404ing every synthesis into an audited refusal - which looks alive while refusing everything.
- Add `d1_enforced: bool = False` plus a boot allowlist of open-weight endpoint names, and a validator that refuses to construct Settings when `d1_enforced` is true and either (a) the model is off the allowlist, or (b) the pair is mixed (`llm_provider=databricks` with `active_embedding_profile=legacy`, or the inverse). `databricks-gpt-5-nano` and `databricks-claude-*` are one string edit away, identical at the call site (`model=self.model` verbatim, llm.py:592-597), and carry documented partner-retention paths. `LLM_PROVIDER` is one global switch, so a wrong string moves router, synthesizer and extractor at once.
- Add `"databricks"` to `_QWEN3_PROVIDER_NAMES` (embedder.py:38-46, verified: the sibling `_QWEN3_PROFILE_PROVIDER_NAMES` at line 47 already includes it), so `assert_embedding_runtime_available` emits its intended remediation instead of a late `ValueError`.
- Additive migration 0016: nullable `query_log.latency_ms`, written on the audit path. There is no latency column today, so no step in any plan can gate on p95 without it. *[Graft: both critiques flagged latency gates against a metric that does not exist.]*

**Do not** fabricate a Databricks price into `llm_model_prices`. The code contract is explicit ("An unknown model yields cost_usd NULL in the audit log - never a guessed price", config/settings.py:126-127, verified) and the $/DBU multiplier is UNVERIFIED. `cost_usd` stays NULL until Amneal has a real rate; then set `LLM_MODEL_PRICES` as one secret.

**Gate.** `black --check`, `ruff`, `mypy src tests tests_contract`, full pytest green. Deploy with `D1_ENFORCED` unset: `/health` and `/ready` byte-identical, one live Ask still answers on OpenAI.

**Rollback.** Revert the PR. Migration 0016 is a nullable ADD COLUMN, no rewrite, no lock.

**Effort.** 2-3 days.

### Step 2 - Flip generation (secrets only)

**Work.** `fly secrets set LLM_PROVIDER=databricks DATABRICKS_LLM_BASE_URL=<root from step 0> DATABRICKS_LLM_TOKEN=<sp-pat> DATABRICKS_LLM_MODEL=databricks-gemma-3-12b GEMMA_THINKING_ENABLED=false LLM_MODEL=databricks-gemma-3-12b`.

Model choice is not arbitrary. The shipped output sanitizer matches **Gemma delimiters only** - `<|channel>thought` and `<think>` (src/regwatch/generate/llm.py:420-422, verified) - and llm.py:451-453 warns that "some OpenAI-compatible servers expose reasoning in `content` instead of a separate field". A chain-of-thought model such as `databricks-gpt-oss-120b` uses different delimiters and would risk leaking reasoning into a cited regulatory answer; it also burns the `_SYNTH_MAX_TOKENS = 900` budget (grounded_qa.py:255, verified) on reasoning tokens, and `finish_reason == "length"` **raises** (llm.py:611-617), converting real answers into refusals. *[Graft from the critique of the all-in design.]*

`LLM_MODEL` is a zero-code fix for a cosmetic split brain: Go resolves `/settings` from env `LLM_MODEL`, defaulting to `gpt-5.4-nano` (go/internal/api/config.go:143), while Python records the Databricks endpoint. It is inert on Python's databricks branch. Without it the UI tells analysts they are still on OpenAI - to the exact stakeholders D1 exists for.

Known accepted regression: streaming becomes one blob. `_complete_stream` buffers the whole SSE stream before stripping delimiters and `stream()` yields a single delta (llm.py:648-733). Fixed in step 5.

**Gate.** `/ready` green. Then a **same-day relative A/B**: run src/regwatch/eval/run_eval.py against the live corpus on OpenAI and on Databricks and require Databricks to be no worse on all three metrics. Do **not** gate on run_eval's absolute thresholds - live `refusal_accuracy` is already 0.917 against the 0.95 floor (verified, .github/workflows/watch-daily.yml:74), so an absolute gate fails a working cutover. *[Graft from the all-in design.]* Plus 20 manual gold-set Asks with correct cite/refuse outcomes and zero `qa_provider_error`.

**Rollback.** `fly secrets set LLM_PROVIDER=openai`. One command, ~60s. Cheapest reversible step in the plan, which is why it comes before any data work: it load-tests the workspace, token, endpoint name and JSON-mode path under real traffic.

**Effort.** 1-2 days.

### Step 3 - Register and backfill a 1024-dim profile on PROD, serving nothing

**Work.** Do this directly against production, not a branch. Supabase branches start with **no data** from the parent project, so a "clone prod" branch benchmark cannot execute as commonly assumed. It is also unnecessary: `chunk_embedding` is a separate table (migration 0015, already live, 0 rows) and nothing reads it until `ACTIVE_EMBEDDING_PROFILE` changes. *[This corrects a fatal flaw in the minimal-inference design as originally drafted.]*

Set `QWEN_EMBEDDING_BASE_URL/TOKEN/MODEL/DIMENSION=1024/REVISION` (revision must be the Databricks served-model version, not a HuggingFace sha - it is fingerprinted into the immutable profile). Run the four shipped commands: `embedding-profile-register` (cli.py:165-207), `embedding-profile-backfill --batch-size 128` over all 10,749 chunks (cli.py:245-303), `embedding-profile-index --concurrently` (cli.py:305-320), `embedding-profile-coverage` (cli.py:223-243).

Backfill the **full** corpus, not a sample: at pay-per-token it costs about 1.4 DBU, and `assert_profile_ready_for_activation` refuses an incomplete profile so a sample cannot be benchmarked anyway.

1024 <= 2000 means `_index_spec` picks plain `vector(1024)` with a partial HNSW index (embedding_profiles.py:599-615), so the halfvec two-stage path and the pgvector >= 0.7.0 concern never come into play.

**Gate.** Coverage reports 10,749/10,749 with the index ready. Live app behavior provably unchanged: `/ready` 200, 5 Asks still retrieving from `chunk.embedding`.

**Rollback.** Nothing routes through the profile. To reclaim space, delete the profile's `chunk_embedding` rows first (FK is ON DELETE RESTRICT), then the `embedding_profile` row - it is immutability-trigger protected, so it is delete-or-keep, never edit.

**Effort.** 2 days, mostly wall clock.

### Step 4 - Retrieval quality gate (this is the real go/no-go)

**Work.** From a workstation process with `ACTIVE_EMBEDDING_PROFILE=<ep_id>` in its own env (no app change, no deploy), run the eval harness and `threshold_sweep` against prod, then repeat with `ACTIVE_EMBEDDING_PROFILE=legacy` as the control. Keep `LLM_PROVIDER` fixed across both so retrieval is the only variable.

The gold set is 26 items (verified) and `recall_at_k` is a binary hit@50 per item, so aggregate recall has 1/26 = 0.038 resolution and is close to blind to ranking degradation. **Gate on rank, not on hit:** for every gold item, record the rank position of the first expected source under both spaces and compare item-by-item; also compare the score distribution against the 0.30 refusal cutoff, since a uniform score shift silently converts answers into refusals. *[Graft: both critiques identified the aggregate-recall gate as below the instrument's resolution.]*

**Gate.** No gold item's first-expected-source rank degrades by more than 2 positions; `threshold_sweep` produces a recommended cutoff inside [0.20, 0.40] so `refusal_score_threshold = 0.30` either survives or gets one documented replacement value. **If this fails, stop.** Re-probe the GA `databricks-bge-large-en` (different instruction prefix, which is a profile-defining change and needs a fresh register + backfill), or accept the GPU cost of a larger embedder. Do not promote a profile that clears an absolute floor while losing ranking precision: retrieval degradation in this product surfaces as more refusals, which reads as "the model got more careful", not as a regression.

**Rollback.** N/A, read-only.

**Effort.** 3 days.

### Step 5 - PR B: real incremental streaming

**Work.** Replace the buffer-everything path (llm.py:648-733) with a safe-prefix emitter that holds back only the longest suffix that could still be a partial thought delimiter under llm.py:420-422 and flushes the rest as it arrives. Keep the existing exception fallback to buffered `complete()` (llm.py:714-730) so an endpoint without SSE still works. Extend the split-delimiter test in tests/test_databricks_llm_provider.py to assert more than one delta and that no delimiter fragment escapes.

**Gate.** The 9 existing Databricks provider tests plus the new multi-delta test; tests_contract stream test green against real Go + uvicorn + Postgres; visual check that the Ask UI types again.

**Rollback.** Revert. Falls back to single-delta, which is correct and INV-safe; only UX degrades.

**Effort.** 3 days.

### Step 6 - PR C: watch-cron parity (must land BEFORE step 7)

**Work.** .github/workflows/watch-daily.yml pins `EMBEDDING_PROVIDER: "openai"` (line 71, verified) and hard-fails preflight when `OPENAI_API_KEY` is empty (lines 97-101, verified: "Ingest embeds via OpenAI on change days"). It sets no `ACTIVE_EMBEDDING_PROFILE` and no `QWEN_EMBEDDING_*`. After promotion, a watch run that ingests a revised PSG builds profile targets from its **own** process env, so it commits chunk rows with no profile embedding. Coverage goes incomplete, and the next app boot refuses inside `assert_profile_ready_for_activation` during `regwatch init-db` (docker/entrypoint.sh:25-32), crash-looping every Python machine while the Go proxy - which deliberately skips init-db - keeps holding the public port and relaying into a dead upstream. The outage presents as edge 502/503, not as machines that fail to start.

This has not fired only because 23 consecutive runs added 0 and revised 0. It is a latent bomb that detonates on the first FDA update, weeks later, at a random deploy.

Fix: add `ACTIVE_EMBEDDING_PROFILE` and the `QWEN_EMBEDDING_*` set to the workflow env from GH secrets, mirroring prod exactly (any drift against the immutable profile fingerprint is itself a boot refusal - embedder.py:583-638 compares model, dimension, revision, instruction version, preprocessing version, dtype and normalization). Rewrite the preflight to require the Databricks token instead. Add a final step running `embedding-profile-coverage` that fails the run if `pending_chunks > 0`.

**Gate.** A manual `workflow_dispatch` run completes green, corpus unchanged, coverage step reports 0 pending.

**Rollback.** Revert the workflow. Safe only while prod is still `legacy`, which is why this precedes step 7.

**Effort.** 1 day.

### Step 7 - Promote the profile: D1 closes here

**Work.** Dry-run the boot guard from a workstation against prod first: `ACTIVE_EMBEDDING_PROFILE=<ep_id> QWEN_EMBEDDING_*=... DATABASE_URL=<prod> uv run regwatch init-db`. That runs the exact chain the container runs (db.py:718-726 -> pgvector_store.py:273-305), so a promotion that would crash-loop the fleet fails on a laptop instead.

Then `fly secrets set ACTIVE_EMBEDDING_PROFILE=<ep_id>` and deploy. **Deliberately leave `EMBEDDING_PROVIDER = "openai"` in fly.toml:57.** `retrieve()` selects the query embedder from the **profile**, not the global provider (retriever.py:206-212, verified), so the confidential analyst query goes to Databricks the instant the profile is active, while the global provider only affects ingest embedding of public FDA text and keeps `chunk.embedding` current as rollback insurance. This also keeps the rollback safe: setting `ACTIVE_EMBEDDING_PROFILE=legacy` while `EMBEDDING_PROVIDER=qwen3` would raise at boot (pgvector_store.py:277-283, verified). Keeping the global provider on openai means the rollback is a single, safe secret.

Then `fly secrets set D1_ENFORCED=true` to arm the allowlist and mixed-pair tripwires. Be honest internally: that tripwire prevents future regression, it does **not** cover the window between steps 2 and 7. During that window the correct status line is "D1 half closed: synthesis on Databricks, query embedding still on OpenAI."

**Gate.** Laptop dry-run exits 0. Post-deploy: `/ready` 200 on every app machine; 20 gold-set Asks correct; no `status=error` rows; `latency_ms` p95 from step 1's column no worse than 1.5x the pre-promotion week. Expect *some* increase: the profile path adds `FROM chunk_embedding ce JOIN chunk c` under the same `SET LOCAL enable_indexscan = off` filtered branch (embedding_profiles.py:756-766, verified) that the legacy path uses (pgvector_store.py:578-588, verified). Measure it; do not assume it.

**Rollback.** `fly secrets set ACTIVE_EMBEDDING_PROFILE=legacy` and unset `D1_ENFORCED` in the same call. Instant and genuinely safe: `chunk.embedding` and its 77 MB HNSW index are untouched by the entire profile lifecycle. Do not drop them for at least 30 days.

**Effort.** 1 day.

### Step 8 (optional, not D1) - durable parsed text

**Work.** `psg_version.parsed_text_path` points at `data/parsed_text` under `DATA_DIR=/app/data` with no `[mounts]` block in fly.toml and an ephemeral Actions runner as the sole ingest driver, so `_read_parsed_text` returns None and the cited diff degrades on every revision. Total corpus text is roughly 9-15 MB. Add a nullable TEXT column on `psg_version`, write parsed text there in the ingest pipeline, and fall back to it in `_read_parsed_text`. *[Graft from the lakehouse design's real finding, minus its delivery vehicle: this does not need Unity Catalog, Delta, Auto Loader or a Lakeflow job.]* Observed production impact so far is zero events (0 revised across 23 runs), so this is a latent-defect fix, not urgent.

**Gate.** Delete `data/` and confirm the diff still renders for a synthetic revision.

**Rollback.** `alembic downgrade -1`; nothing in the serving path reads it.

**Effort.** 0.5-1 day.

### Step 9 (one-way door, gate on a 30-day soak) - decommission OpenAI

**Work.** Change fly.toml:57 to `EMBEDDING_PROVIDER = "databricks-qwen3"` (already in `_QWEN3_PROVIDER_NAMES`), mirror it in the watch workflow, delete `WATCH_OPENAI_API_KEY`, `fly secrets unset OPENAI_API_KEY`. From this point ingest writes NULL into `chunk.embedding` rather than mixing geometries, so the legacy rollback decays with every new FDA revision.

**Gate.** 30 clean days in prod, at least one real FDA revision ingested end to end with coverage still complete, two consecutive green watch runs, eval green.

**Rollback.** Expensive by design: restoring legacy after this requires re-backfilling `chunk.embedding`, which needs an OpenAI key again. Do not bundle with anything else. Note the failure is silent if botched - `OpenAIEmbeddingProvider` raises lazily inside `_client_or_create`, not at construction, so a keyless rollback boots green and fails every Ask.

**Effort.** 1 day behind the soak.

**Critical path: about 3 weeks of engineering, gated by a DPA-scope check against Amneal's existing Databricks agreement that must run first.**

## 4. What we should NOT move

**The vector store stays in Supabase pgvector.** 10,749 chunks / 174 MB table / 77 MB HNSW index / 208 MB whole database / 99 queries in the last 7 days / 471 in 30 days / 23 consecutive daily crawls with 0 added, 0 revised, 0 errors. Databricks AI Search costs a $204.40/month floor (1 unit x $0.28/hr x 730h, billed 24/7 at zero traffic) to hold those vectors in a unit sized for 2,000,000 - 0.5% utilization, and a dev/staging/prod split is $613/month before storage. It also breaks three things: the 1024-element-per-filter-clause cap cannot express the ~1,794-element `version_id $in` list retriever.py:218-223 builds on every query; the L2 metric with undocumented score semantics invalidates the 0.30 threshold calibrated against `1 - d/2`; and async Delta Sync creates a window where superseded PSG versions remain citable, which for a regulatory product is the most expensive failure mode available. The prior "adopt iterative scan at ~100k rows or p95 > 50ms" trigger is at ~10% of its threshold.

**Supabase stays as the datastore.** Lakebase is a real product and a technically credible Postgres-for-Postgres swap - the repo has near-zero Supabase SDK coupling (the only string in src/ or go/ is a hostname comment) - but it contributes exactly zero to D1, and the leak is why we are here. Three concrete hazards if we did it anyway: migration 0011's `CREATE EVENT TRIGGER` needs privileges Lakebase's `databricks_superuser` is not documented to have and it would fail inside `alembic upgrade head` in `release_command`; Lakebase's `-pooler` endpoint is PgBouncer transaction mode, which breaks pgx's default `QueryExecModeCacheStatement` (go/internal/store/pool.go:100-105) as a total edge outage on the service that now holds the public port; and a half-move that split OLTP from vectors would break the single transaction that commits psg_version + psg_document content + chunk rows + be_requirement together (src/regwatch/ingest/pipeline.py:399-441). Revisit only if Supabase itself becomes a residency problem.

**The corpus stays out of Delta.** Landing 1,795 public FDA PDFs in Unity Catalog is 2-3 weeks of vendor plumbing to solve a 9-15 MB durability gap that a nullable column solves in half a day, and it creates a second source of truth that drifts silently while the compliance claim it exists to support quietly becomes false.

**The watch cron stays on GitHub Actions.** It is free, has run 23/23 clean, and carries a healthchecks.io dead-man's-switch that specifically catches a cron that never starts. A Lakeflow schedule can also silently never start. Moving it swaps one single point of failure for another at a new vendor, for zero D1 value.

**Hosting stays on Fly and Vercel.** Databricks Apps cannot be public and does not support anonymous access or SSO bypass.

## 5. Cost

Assumptions stated: 424 Asks/month (99 query_log rows in 7 days), per Ask roughly 5,400 input / 560 output tokens for router + synthesizer (8 passages after the rerank stage, `_SYNTH_MAX_TOKENS = 900` cap), ~50 tokens per query embedding. **The $/DBU multiplier is UNVERIFIED** - both Databricks pricing pages publish DBU quantities only. Below uses the widely circulated but unconfirmed $0.07/DBU and also shows $0.20/DBU.

| Line | Volume | DBU/month | @ $0.07 | @ $0.20 |
|---|---|---|---|---|
| Synthesis, `databricks-gemma-3-12b` (2.143 in / 7.143 out per M) | 2.29M in / 0.24M out | 6.6 | $0.46 | $1.32 |
| Query embedding, 1024-dim endpoint (0.286 per M) | 0.021M | 0.006 | $0.0004 | $0.001 |
| **Steady state** | 424 Asks | **~6.6** | **~$0.50** | **~$1.35** |
| At 10x (5,000 Asks/mo) | | ~78 | ~$5.50 | ~$15.60 |
| One-time backfill (10,749 chunks x ~450 tok) | 4.84M | 1.4 | $0.10 | $0.28 |

Baseline being replaced: `gpt-5.4-nano` at $0.05/M in and $0.40/M out is about $0.25/month, plus text-embedding-3-small at under a cent. **The inference swap is a delta of roughly +$0.30 to +$1.30/month at current volume.** Inference is not the cost story.

Unchanged: Supabase $0-25/month (208 MB fits the Free tier), Fly, Vercel, GitHub Actions.

What this avoids: Custom Model Serving of self-registered weights needs a warm replica per endpoint because documented cold start is "one to several minutes" with no SLA, at 20.00 DBU/hour = about $1,022/month per endpoint, $2,044/month for both - roughly 3,700x this plan for the identical two calls. AI Search adds $204.40/month floor.

**The cost that actually matters is unpriced and contractual.** Serverless egress control requires Enterprise tier; the Compliance Security Profile is a paid Enhanced Security and Compliance add-on. Those two are exactly the controls that make the D1 claim provable and auditable - a cheap workspace cannot produce the evidence. Get that quote in step 0, before any engineering. It is near-certainly larger than the entire inference bill. Engineering cost is about 3 weeks of one person.

## 6. Open questions / blockers for the user

1. **Does Amneal legal accept "processor swap under a DPA, up to 30 days abuse retention, no training on inputs, never reaches OpenAI" as satisfying D1?** If the bar is zero retention or in-tenant processing, this entire architecture is wrong and the answer is self-hosted vLLM in Amneal's network or Azure OpenAI in Amneal's own subscription with approved Modified Abuse Monitoring (which uniquely gives a machine-checkable `ContentLogging=false` artifact). This is the single gating question.
2. **Amneal already runs Databricks - at what tier, and who owns the workspace?** Enterprise tier plus the Enhanced Security and Compliance add-on are required for the provable-negative controls (serverless egress policy, Compliance Security Profile). An existing Standard/Premium workspace would need an upgrade, and that is the only remaining commercial question. Also confirm Foundation Model APIs are not disabled by account policy, and get a service principal we own rather than borrowing a human's PAT.
3. **Which region and workspace?** Recommend AWS us-east-1 to match the Supabase project. Confirm cross-Geo processing can be disabled at the account level.
4. **Is a GxP / 21 CFR Part 11 position required?** No Databricks GxP or Part 11 attestation was found in any public documentation. If a validated-system argument is needed, that pushes toward a pinned, self-registered model version in Unity Catalog (Custom Model Serving), which is the $2k/month path.
5. **Is the loss of token-by-token streaming acceptable for the 3 days between step 2 and step 5?** Alternative: hold step 2 until PR B is merged, at the cost of losing the cheap under-real-traffic test of the endpoint.
6. **Who owns the Databricks service principal and its 730-day token rotation?** The client is lru_cached on the token with no refresh; rotation requires a process restart.

## 7. Unverified claims

Nobody should plan on these.

**Vendor side:**
- Which OpenAI-compatible root the specific workspace serves. Databricks docs show both `https://<host>/serving-endpoints` and the Beta `https://<host>/ai-gateway/mlflow/v1`. A wrong root 404s every call. Resolved by step 0's curl.
- Whether a Databricks-served 1024-dim embedding endpoint honors the `dimensions` request field. FMAPI lists it as model-dependent. If ignored, the provider's exact-length check fails 100% of calls.
- Whether it returns L2-normalized vectors. Databricks documents normalization explicitly for `bge-large-en` (yes) and `gte-large-en` (no) and is **silent** for the Preview `qwen3-embedding-0-6b`. The provider hard-rejects norms outside 1e-3.
- Whether `stream_options={"include_usage": true}` and `response_format={"type":"json_object"}` are supported on the chosen endpoint. The stream path degrades safely; the JSON path used by the router and extractor does not and would raise.
- Whether the FMAPI 30-day abuse-retention window applies to Custom Model Serving of self-registered weights. Databricks makes no zero-retention statement for custom endpoints; absence of a claim is not a claim of absence.
- Whether partner-branded FMAPI endpoints (`databricks-gpt-5-*`, `databricks-claude-*`) make any network call to OpenAI/Anthropic infrastructure. Databricks asserts "hosted within the Databricks security perimeter" while simultaneously documenting partner retention regimes for those models. No equivalent to Microsoft's categorical "do NOT interact with any services operated by OpenAI" statement exists.
- The $/DBU multiplier, the serverless-jobs DBU rate, Lakebase CU pricing, and the AI Search DSU storage rate. All render behind JS selectors or 404.
- Whether pay-per-token FMAPI endpoints have any cold start.
- Where AI Gateway guardrail evaluation executes and whether it involves a separate model call that sees the prompt.
- Real Gemma thought-channel delimiters as emitted by an actual Databricks endpoint. The stripping regexes at llm.py:420-422 are written against an assumed format; keep `GEMMA_THINKING_ENABLED=false` until a real payload is captured in an integration test.

**Repo / environment side:**
- No test suite was executed in this analysis. Test coverage claims come from reading function names.
- Real per-query latency. `query_log` has no latency column today, which is why step 1 adds one. Retrieval was measured at 3.6-5.1s server-side for the production-shaped filtered exact scan, but whether that reflects steady state or a burst-credit-exhausted Micro instance is unknown. What is structural and does not vary: every production Ask carries a filter, which takes the `enable_indexscan = off` branch, so the 77 MB HNSW index has been scanned 3 times ever.
- Whether the ~1,794-element `version_id` array is actually emitted on every production path, or whether name resolution narrows it first.
- ~~Whether any Sentry `capture_exception` path carries verbatim question text off-tenant.~~ **AUDITED 2026-07-28, CLEAN.** Python `common/observability.py:59-78` sets `send_default_pii=False`, `max_request_body_size="never"`, `include_local_variables=False` (explicitly because `/query` frames hold question/answer/prompt), `LoggingIntegration(event_level=None)`, and `before_send=_scrub_event` cutting SQLAlchemy's `[SQL:`/`[parameters:` echo. Go `internal/obs/obs.go:69-85` installs no HTTP integration at all, sets `SendDefaultPII: false`, and `scrubEvent` cuts pgx's `failed to encode args` echo (the path carrying `query_text` and `answer_text` from `InsertQueryLog`). All six `capture_exception` sites in `grounded_qa.py` pass only the exception object. One residual, now fixed in PR A: a provider SDK error whose response body echoes prompt fragments would land in `exc.value`, since Python's scrubber only cut on `[SQL:`.
- Whether `WHITEPAPER_TEMPLATE_URL` is set as a Fly secret. The live Supabase project has 0 storage buckets and 0 objects, so if set it points somewhere else.
- Whether `CREATE EVENT TRIGGER` and `SECURITY DEFINER` trigger functions work on Lakebase. Only relevant if the Supabase decision is revisited.