# REGWATCH Docker Guide

This document records the Docker work added to REGWATCH and how to use it.

The goal is not Kubernetes or full production deployment yet. The goal is a
reliable local/container baseline that can run the Python API, Next.js UI,
ingest jobs, and Dagster orchestration without changing the core application
code.

## What Was Added

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the shared Python application image. |
| `regwatch/frontend/Dockerfile` | Builds the local Next.js UI image. |
| `.dockerignore` | Keeps secrets, local data, docs, caches, and local tooling out of the image context. |
| `compose.yaml` | Defines the API, UI, one-shot ingest, and Dagster services. |
| `docker/entrypoint.sh` | Creates container data directories and runs `regwatch init-db` before app start. |
| `docker/dagster/` | Dagster instance and workspace configuration for Compose. |
| `.github/workflows/ci.yml` | Adds a Docker image build check in CI. |
| `pyproject.toml` / `uv.lock` | Moves heavy local embedding dependencies behind the `local-embeddings` extra and Dagster behind the `orchestration` extra. |
| `src/regwatch/api/main.py` | Avoids running DB initialization twice when the entrypoint already ran it. |
| `src/regwatch/process/embedder.py` | Gives a clear error if local embeddings are requested without installing the extra. |

## Container Shape

One Python image is reused for app and orchestration jobs:

1. API service
2. Ingest service
3. Dagster code server, webserver, and daemon

The API is a long-running service. Ingest is intentionally a separate one-shot
command so a large 30-minute data load does not block API startup. Dagster V1
wraps the existing `regwatch seed` CLI as a manual `seed_corpus_job`.

```text
docker image: regwatch:local
  -> api                -> uvicorn regwatch.api.main:app
  -> ingest             -> regwatch seed
  -> dagster-code       -> dagster code-server
  -> dagster-webserver  -> dagster UI
  -> dagster-daemon     -> Dagster run/schedule daemon

docker image: regwatch-web:local
  -> web                -> npm run dev
```

## Quick Commands

Build the baseline image:

```bash
docker build -t regwatch:local .
```

Run the API:

```bash
docker compose up api
```

Run the full local stack:

```bash
docker compose up --build api web dagster-postgres dagster-code dagster-webserver dagster-daemon
```

Local endpoints:

```text
UI:      http://localhost:3000
API:     http://localhost:8000
Dagster: http://localhost:3001
```

Run the current one-shot seed ingest:

```bash
docker compose --profile ingest run --rm ingest
```

Validate Compose syntax:

```bash
docker compose config --quiet
```

Run the seed corpus from Dagster:

1. Open `http://localhost:3001`.
2. Select `seed_corpus_job`.
3. Launch materialization/run manually.

## Data Persistence

Compose mounts the host `./data` directory into the container at `/app/data`.

That means these survive container restarts:

- SQLite database
- Chroma vector store
- raw PDF files
- processed output files
- Dagster compute logs and local artifact storage

Dagster's run, event, and schedule metadata uses the `dagster-postgres` service
and the named `dagster-postgres` Docker volume. REGWATCH application data stays
in `./data`.

Container defaults:

```text
DATA_DIR=/app/data
CHROMA_DIR=/app/data/chroma
SQLITE_PATH=/app/data/regwatch.db
RAW_PDF_DIR=/app/data/raw
PROCESSED_DIR=/app/data/processed
API_HOST=0.0.0.0
API_PORT=8000
DAGSTER_HOME=/app/data/dagster/home
```

## Embedding Modes

The baseline Docker image defaults to:

```text
EMBEDDING_PROVIDER=echo
```

That is good for smoke tests because it avoids shipping PyTorch and avoids model
downloads. It is not acceptable for broad production ingest because it produces
low-quality deterministic test vectors.

For real PSG ingest inside Docker, set these in `.env`:

```text
INSTALL_LOCAL_EMBEDDINGS=true
EMBEDDING_PROVIDER=local-bge-small
```

Then rebuild:

```bash
docker compose build
```

After that, run ingest:

```bash
docker compose --profile ingest run --rm ingest
```

Do not load the full 2,000+ PSG corpus with `EMBEDDING_PROVIDER=echo`.

## Why Local Embeddings Became Optional

The first Docker build pulled large CUDA/NVIDIA packages through the
`sentence-transformers` / `torch` dependency path. That made the baseline API
image too heavy for a simple service smoke test.

The fix was:

1. Keep the application embedding provider pluggable.
2. Move `sentence-transformers`, `torch`, and `transformers` into the
   `local-embeddings` optional extra.
3. Keep Docker baseline builds on `--extra llm` only.
4. Use the PyTorch CPU index for Linux when the local embedding extra is
   installed.

This gives two useful modes:

- lightweight API image for health checks and API development
- heavier local-embedding image for actual local PSG ingest

## Startup Behavior

The entrypoint runs:

```bash
regwatch init-db
```

Then it exports:

```text
REGWATCH_DB_INITIALIZED=1
```

FastAPI checks that marker and skips its own duplicate `init_db()` call. This
prevents startup from doing the same migration/init work twice.

## Health Check

Compose checks:

```text
GET http://127.0.0.1:8000/health
```

The verified smoke test returned:

```json
{"status":"ok"}
```

## Current Verification

This Docker pass verified:

- `docker compose config --quiet`
- `docker build -t regwatch:local .`
- in-container `/health` smoke check
- formatting and type checks
- full pytest suite

The eval gate is green: `recall@k`, `citation_precision`, and `refusal_accuracy`
all meet threshold on the seeded corpus, and a deterministic offline eval gate
(`tests/test_eval_gate.py`) runs inside `uv run pytest`.

## The Next.js UI

The UI is the Next.js app in `regwatch/frontend/` and now runs as the Compose
`web` service. It talks to the API through a same-origin `/api` proxy
(`regwatch/frontend/next.config.mjs`). In Compose, `API_PROXY_TARGET` is set to
`http://api:8000`, so browser traffic still only talks to the Next.js origin.

The local container shape is:

```text
api container      -> FastAPI / Python evidence service
web container      -> Next.js / TypeScript UI (proxies /api -> api)
ingest container   -> scheduled or one-shot FDA data loads
dagster containers -> orchestration UI, code location, daemon, metadata DB
```

## Large Ingest Notes

For a 2,000 PSG / 1,200 drug load, the container shape is acceptable, but the
ingest command needs more production hardening before it should be trusted as a
routine job.

Needed next:

- real embedding provider, not `echo`
- resumable batches
- progress logging
- failure checkpoints
- source freshness timestamps
- retry/backoff per FDA source
- explicit prevention of broad ingest with test embeddings
- eventually a scheduled job or orchestrated worker instead of manual
  `docker compose run` or manual Dagster launches

## Not Done Yet

This is not a full production deployment story yet.

Still needed:

- secrets injection policy
- auth and authorization
- TLS termination
- deployment target decision
- backup and restore plan for `data/`
- observability and alerts
- resource limits
- image vulnerability scan
- Kubernetes manifests or Helm chart, if the hosting decision requires them
- decision on SQLite/Chroma vs Postgres/pgvector or managed vector storage
- automatic Dagster schedules, once the manual seed job is validated
