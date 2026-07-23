# Open-model rollout: Qwen3 embeddings + Gemma generation

This rollout keeps retrieval orchestration, grounding, refusal, and audit
behavior in the existing Regwatch Python application. Only model inference is
split into independently deployed endpoints:

```text
documents ──> Qwen3-Embedding-4B endpoint ──> Postgres/pgvector profile
                                                       │
question ──> Qwen3-Embedding-4B endpoint ──> retrieval ─┤
                                                       ▼
                                      Gemma 4 31B IT endpoint ──> answer
```

The embedding and generation endpoints have separate credentials,
configuration, scaling, evaluation, and rollback. This does not add
application-level microservices.

## Runtime behavior

The Qwen provider uses an OpenAI-compatible embeddings API. Retrieval queries
receive the versioned regulatory instruction; document chunks are embedded as
raw text. Responses are rejected if their count, ordering, dimension, numeric
values, or unit normalization is invalid.

The Gemma provider uses a separate Databricks OpenAI-compatible Chat
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

# Gemma generation endpoint
LLM_PROVIDER=databricks
DATABRICKS_LLM_BASE_URL=https://<generation-endpoint-api-root>
DATABRICKS_LLM_TOKEN=<service-principal-token>
# The value sent in the Chat Completions `model` field, normally the Databricks
# serving endpoint name.
DATABRICKS_LLM_MODEL=<gemma-generation-endpoint-name>
GEMMA_THINKING_ENABLED=false
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

1. Deploy Qwen and Gemma as private, independently scaled endpoints. Keep the
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

## Gemma production sizing

Begin evaluation on one 80 GB H100 with a bounded maximum context length.
Fitting the checkpoint in memory is not a production throughput result.
Production sizing must measure concurrency, time to first token, tokens per
second, KV-cache pressure, failure behavior, and cost at the application's real
prompt lengths.

Treat a quantized 24 GB A10 deployment as an experiment, not an assumed
production fallback. Keep endpoint autoscaling and maximum context limits
independent from the Qwen embedding service.

Keep `GEMMA_THINKING_ENABLED=false` until the real serving endpoint's reasoning
payload/delimiters have been captured in an integration test; the default
prevents unverified thought formats from reaching users or audit records.
