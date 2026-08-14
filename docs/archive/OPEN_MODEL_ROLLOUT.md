# Open-model rollout: Qwen3 embeddings + Databricks generation

> **STATUS 2026-07-29:** this plan is partially executed, out of its written
> order, and parts of it are superseded.
> - The machinery (migration 0015 embedding profiles, `Qwen3EmbeddingProvider`,
>   `DatabricksProvider`) merged and deployed DORMANT on 2026-07-23 (#125).
> - Generation cut over FIRST, on 2026-07-28 -- and not to the planned
>   generation model: prod runs
>   gpt-oss-20b behind the Databricks endpoint alias
>   `workspace.default.regwatch` (served model id `gpt-oss-20b-080525`), one
>   model for ALL roles, `LLM_PROVIDER=databricks`. OpenAI is rollback only.
> - The embedding endpoint was created 2026-07-29:
>   `workspace.default.regwatch-embed`, Qwen3-Embedding-0.6B, 1024-dim,
>   pay-per-token (served id `qwen3-embedding-0-6b-112025`), verified by a
>   live call. App wiring is NOT built yet -- prod queries still embed on
>   OpenAI, and the config defaults below still describe the 4B/1536
>   Matryoshka plan.
> - The Qwen3-4B / 2560-dim sections and the generation GPU sizing section below
>   are historical planning kept for context; the shape actually adopted is
>   pay-per-token Databricks Model Serving.

This rollout keeps retrieval orchestration, grounding, refusal, and audit
behavior in the existing Regwatch Python application. Only model inference is
split into independently deployed endpoints:

```text
documents ──> Qwen3-Embedding-4B endpoint ──> Postgres/pgvector profile
                                                       │
question ──> Qwen3-Embedding-4B endpoint ──> retrieval ─┤
                                                       ▼
                                          generation endpoint ──> answer
```

The embedding and generation endpoints have separate credentials,
configuration, scaling, evaluation, and rollback. This does not add
application-level microservices.

## Runtime behavior

The Qwen provider uses an OpenAI-compatible embeddings API. Retrieval queries
receive the versioned regulatory instruction; document chunks are embedded as
raw text. Responses are rejected if their count, ordering, dimension, numeric
values, or unit normalization is invalid.

The generation provider uses a separate Databricks OpenAI-compatible Chat
Completions endpoint. Thinking is opt-in and synthesizer-only. Router and
extractor calls force it off, and thought content is removed before responses
or audit payloads leave the provider.

## Embedding dimensions

Qwen3-Embedding-4B natively produces 2,560-dimensional embeddings.
Regwatch's initial `QWEN_EMBEDDING_DIMENSION=1536` setting is a deliberate
Matryoshka truncation profile, not the model's native default.

Benchmark 1,536 and 2,560 dimensions before a full-corpus re-embedding. Use the
same representative 5,000–20,000 chunks and query judgments for both profiles,
and compare:

- Recall@5 and Recall@10
- MRR or nDCG
- refusal accuracy
- index memory and build time
- query latency

The profiles are separate vector spaces and must never be mixed. At 1,536
dimensions Regwatch builds a normal `vector` HNSW index. At 2,560 dimensions it
uses a `halfvec` HNSW candidate index and reranks candidates with the stored
full-precision vector; that path requires pgvector 0.7.0 or newer.

## Immutable profile metadata

Every registered embedding profile is fingerprinted from:

- provider
- model and immutable model revision
- dimension, dtype, and normalization
- query-instruction version
- preprocessing and chunking versions
- serving-runtime version

Each chunk embedding additionally stores its profile ID, source-content hash,
and embedding timestamp. A changed chunk is invalidated and becomes pending
for every profile. Changing any profile-defining field creates a new profile
ID rather than silently reusing incompatible vectors.

## Configuration

Use separate endpoint credentials and store tokens as deployment secrets:

```dotenv
# Keep the legacy provider during shadowing so rollback vectors stay current.
EMBEDDING_PROVIDER=openai

# Qwen embedding endpoint (independent from EMBEDDING_PROVIDER during shadowing)
QWEN_EMBEDDING_BASE_URL=https://<embedding-endpoint-api-root>
QWEN_EMBEDDING_TOKEN=<service-principal-token>
# For Databricks this is normally the deployed endpoint name; for a vLLM
# server it may be the served model name. It must match the immutable profile.
QWEN_EMBEDDING_MODEL=<qwen-embedding-endpoint-name>
QWEN_EMBEDDING_REVISION=5cf2132abc99cad020ac570b19d031efec650f2b
QWEN_EMBEDDING_DIMENSION=1536
QWEN_EMBEDDING_BATCH_SIZE=128
QWEN_EMBEDDING_QUERY_INSTRUCTION=Given a pharmaceutical regulatory question, retrieve FDA product-specific guidance passages containing the evidence needed to answer it.
QWEN_EMBEDDING_QUERY_INSTRUCTION_VERSION=regwatch-regulatory-retrieval-v1

# Retrieval routing
ACTIVE_EMBEDDING_PROFILE=legacy
# EMBEDDING_SHADOW_PROFILE=ep_<profile-id>

# Generation endpoint (live since 2026-07-28: gpt-oss-20b, one model for all roles)
LLM_PROVIDER=databricks
DATABRICKS_LLM_BASE_URL=https://<generation-endpoint-api-root>
DATABRICKS_LLM_TOKEN=<service-principal-token>
# The value sent in the Chat Completions `model` field, normally the Databricks
# serving endpoint name (in prod: the Unity Catalog alias workspace.default.regwatch).
DATABRICKS_LLM_MODEL=<generation-endpoint-name>
DATABRICKS_THINKING_ENABLED=false

# Truncation knobs (merged 2026-07-29 in PR #138 after the 2026-07-28 incident):
# "low" is the only reasoning level measured to finish under the 900-token cap
# on gpt-oss-20b; the cap itself is now an operator knob.
DATABRICKS_REASONING_EFFORT=low
SYNTHESIZER_MAX_TOKENS=3000

# D1 runtime served-model guard (shipped in PR #138, deployed UNARMED).
# When armed, _check_d1_enforcement (config/settings.py) refuses to boot unless
# generation AND query embedding have BOTH left OpenAI -- flip
# ACTIVE_EMBEDDING_PROFILE off "legacy" before arming. The allowlist is checked
# against two strings, so it must carry BOTH the configured endpoint alias and
# the model id the endpoint reports per response (D1ResidencyError otherwise).
# D1_ENFORCED=true
# D1_ALLOWED_LLM_MODELS='["workspace.default.regwatch","gpt-oss-20b-080525"]'
```

`ACTIVE_EMBEDDING_PROFILE=legacy` keeps retrieval on the existing
`chunk.embedding` column. A non-legacy value must be registered, fully
backfilled, compatible with the configured Qwen endpoint, and have a valid
HNSW index; startup refuses an unsafe promotion. `EMBEDDING_SHADOW_PROFILE`
identifies the candidate profile populated alongside new ingestion without
routing user queries to it.

Keep the legacy provider configured through the initial promotion if immediate
rollback coverage matters. After confidence is established,
`EMBEDDING_PROVIDER=qwen3` stops paid legacy embedding writes; Regwatch stores
`NULL` in the unversioned legacy vector column rather than mixing Qwen vectors
with the historical OpenAI space. A later rollback to `legacy` would then
require backfilling those missing legacy vectors first.

## Profile operations

Apply the additive database migration first:

```bash
DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head
```

The profile commands use the configured Qwen endpoint and Qwen settings:

```bash
uv run regwatch embedding-profile-register \
  --serving-runtime-version vllm-0.10.2
uv run regwatch embedding-profile-list
uv run regwatch embedding-profile-coverage <profile-id>
uv run regwatch embedding-profile-backfill <profile-id> --batch-size 128
uv run regwatch embedding-profile-index <profile-id>
```

Use `--limit` on backfill for a representative evaluation sample and
`--no-concurrently` only for disposable/local index builds. Backfill is
resumable: completed chunk/profile rows are durable checkpoints, so rerunning
skips them.

## Rollout sequence

> **2026-07-29:** executed order differs from the plan below. Step 7 (the
> generation cutover) happened FIRST, on 2026-07-28, to gpt-oss-20b. Step 1's
> embedding half exists since 2026-07-29 (`workspace.default.regwatch-embed`,
> pay-per-token). Steps 2-6 are the in-flight work, targeting the 1024-dim
> Qwen3-Embedding-0.6B endpoint; step 3's 2560-dim comparison applies only if
> a 4B endpoint is ever revisited.

1. Deploy the embedding and generation models as private, independently scaled
   endpoints. Keep the
   existing providers active while validating credentials and network paths.
2. Apply the migration, register the 1,536-dimensional profile with the exact
   serving-runtime/deployment version, and backfill only a representative
   sample.
3. Change `QWEN_EMBEDDING_DIMENSION` to `2560`, register a second profile, and
   backfill the same evaluation sample. Run the retrieval and refusal
   comparison before choosing a production dimension.
4. Restore the chosen Qwen settings, complete that profile's backfill, verify
   coverage, and build its HNSW index.
5. Set `EMBEDDING_SHADOW_PROFILE` to the chosen profile while leaving
   `ACTIVE_EMBEDDING_PROFILE=legacy`. Validate shadow coverage, quality,
   latency, and cost under normal ingestion.
6. Promote retrieval by setting `ACTIVE_EMBEDDING_PROFILE` to the chosen
   `ep_...` ID and restarting the application. Startup verifies profile
   identity, full coverage, and index readiness. Roll back immediately by
   restoring `legacy` while legacy dual-writing remains enabled.
7. Cut generation over separately with `LLM_PROVIDER=databricks`. Rollback is
   independent of the embedding profile.

## Generation production sizing (HISTORICAL)

> **2026-07-29:** superseded. Prod adopted Databricks pay-per-token Model
> Serving for both generation (gpt-oss-20b) and embeddings
> (Qwen3-Embedding-0.6B), so no GPU fleet is owned or sized by this project.
> Kept for context in case a provisioned-throughput or self-hosted deployment
> is ever revisited.

Begin evaluation on one 80 GB H100 with a bounded maximum context length.
Fitting the checkpoint in memory is not a production throughput result.
Production sizing must measure concurrency, time to first token, tokens per
second, KV-cache pressure, failure behavior, and cost at the application's real
prompt lengths.

Treat a quantized 24 GB A10 deployment as an experiment, not an assumed
production fallback. Keep endpoint autoscaling and maximum context limits
independent from the Qwen embedding service.

Keep `DATABRICKS_THINKING_ENABLED=false` until the real serving endpoint's reasoning
payload/delimiters have been captured in an integration test; the default
prevents unverified thought formats from reaching users or audit records.
