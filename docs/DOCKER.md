# REGWATCH Docker Guide

This document records the Docker work added to REGWATCH and how to use it.

The goal of this pass was not Kubernetes or full production deployment. The
goal was a reliable local/container baseline that can run the Python API, the
temporary Streamlit UI, and ingest jobs without changing the application code.

## What Was Added

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the shared Python application image. |
| `.dockerignore` | Keeps secrets, local data, docs, caches, and local tooling out of the image context. |
| `compose.yaml` | Defines API, optional UI, and one-shot ingest services. |
| `docker/entrypoint.sh` | Creates container data directories and runs `regwatch init-db` before app start. |
| `.github/workflows/ci.yml` | Adds a Docker image build check in CI. |
| `pyproject.toml` / `uv.lock` | Moves heavy local embedding dependencies behind the `local-embeddings` extra. |
| `src/regwatch/api/main.py` | Avoids running DB initialization twice when the entrypoint already ran it. |
| `src/regwatch/ui/app.py` | Same DB initialization guard for Streamlit. |
| `src/regwatch/process/embedder.py` | Gives a clear error if local embeddings are requested without installing the extra. |

## Container Shape

One image is reused for three jobs:

1. API service
2. Streamlit UI service
3. Ingest service

The API and UI are long-running services. Ingest is intentionally a separate
one-shot command so a large 30-minute data load does not block API startup.

```text
docker image: regwatch:local
  -> api     -> uvicorn regwatch.api.main:app
  -> ui      -> streamlit run src/regwatch/ui/app.py
  -> ingest  -> regwatch seed
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

Run the optional Streamlit UI:

```bash
docker compose --profile ui up
```

Run the current one-shot seed ingest:

```bash
docker compose --profile ingest run --rm ingest
```

Validate Compose syntax:

```bash
docker compose config --quiet
```

## Data Persistence

Compose mounts the host `./data` directory into the container at `/app/data`.

That means these survive container restarts:

- SQLite database
- Chroma vector store
- raw PDF files
- processed output files

Container defaults:

```text
DATA_DIR=/app/data
CHROMA_DIR=/app/data/chroma
SQLITE_PATH=/app/data/regwatch.db
RAW_PDF_DIR=/app/data/raw
PROCESSED_DIR=/app/data/processed
API_HOST=0.0.0.0
API_PORT=8000
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

FastAPI and Streamlit check that marker and skip their own duplicate
`init_db()` call. This prevents startup from doing the same migration/init work
twice.

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

The eval gate still has one existing non-Docker failure in the local seeded
data: the beclomethasone gold question is refused, so refusal accuracy is below
threshold. Retrieval and citation precision remain green. That should be fixed
in the retrieval/eval path, not in Docker.

## How This Affects The Future TypeScript UI

The future TypeScript UI can be added as a separate service without changing
the API image.

Expected future shape:

```text
api container      -> FastAPI / Python evidence service
web container      -> Next.js / TypeScript UI
ingest container   -> scheduled or one-shot FDA data loads
```

When the TypeScript UI lands, the Streamlit service can be removed or kept as an
internal demo-only profile.

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
  `docker compose run`

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
