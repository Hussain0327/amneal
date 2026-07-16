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
command so a large 30-minute data load does not block API startup. Dagster
wraps two CLIs as asset jobs: a manual `seed_corpus_job` (`regwatch seed`) and a
`watch_digest_job` (`regwatch watch`) on a daily `watch_daily_schedule`
(`0 6 * * *` UTC). This is local orchestration only; production Watch is driven
by `.github/workflows/watch-daily.yml`, not by the Compose Dagster daemon.

```text
docker image: regwatch:local
  -> api                -> regwatch serve   (dual-stack uvicorn; see docs/GO_PROXY_ROLLOUT.md)
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

This SQLite + Chroma layout under `./data` is the local/container default.
Compose also wires a `DATABASE_URL` build/runtime variable (empty by default):
set it to a Postgres URL and the structured store **and** the vectors (pgvector)
move into that one Postgres database, leaving `./data` for raw/processed files
and logs. That Postgres/pgvector pairing is the production datastore path — see
`docs/DEPLOY.md`. (Managed Postgres/pgvector is not yet provisioned; see
`docs/ROADMAP.md`.)

Container defaults:

```text
DATA_DIR=/app/data
CHROMA_DIR=/app/data/chroma
SQLITE_PATH=/app/data/regwatch.db
RAW_PDF_DIR=/app/data/raw
PROCESSED_DIR=/app/data/processed
DATABASE_URL=                        # empty -> SQLite + Chroma; set -> Postgres + pgvector
DAGSTER_HOME=/app/data/dagster/home
```

## Embedding Modes

Compose defaults to the real local model:

```text
INSTALL_LOCAL_EMBEDDINGS=true        # compose build-arg default
EMBEDDING_PROVIDER=local-bge-small   # compose environment default
```

so an out-of-the-box `docker compose up --build` ships sentence-transformers
and produces real embeddings. (The bare Docker *image* — built without compose
— keeps an in-image `EMBEDDING_PROVIDER=echo` default: good for empty-corpus
smoke boots only, because echo produces low-quality deterministic test
vectors. The API refuses to boot `local-bge-small` on an image built without
sentence-transformers.)

For a slim no-torch stack — the production pairing, see `docs/DEPLOY.md` —
set both in `.env`:

```text
INSTALL_LOCAL_EMBEDDINGS=false
EMBEDDING_PROVIDER=openai            # plus OPENAI_API_KEY
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

This is now enforced at startup: when an `echo` embedding/LLM provider faces a
**non-empty** Chroma corpus, the API refuses to boot with a `RuntimeError`
explaining the fix (switch to a real provider, or set
`REGWATCH_ALLOW_TEST_PROVIDERS=1` for tests/CI). A fresh stack with an empty
corpus still boots on `echo`, so the ingest service can seed it — but the next
`api` start after that ingest will fail fast unless the providers are real. If
your mounted `./data` already contains a seeded corpus, set real providers (or
the override) before `docker compose up api`.

## Why Local Embeddings Became Optional

The first Docker build pulled large CUDA/NVIDIA packages through the
`sentence-transformers` / `torch` dependency path. That made the baseline API
image too heavy for a simple service smoke test.

The fix was:

1. Keep the application embedding provider pluggable.
2. Move `sentence-transformers`, `torch`, and `transformers` into the
   `local-embeddings` optional extra.
3. Keep the slim Docker baseline build on `--extra llm --extra orchestration`
   only (no torch); add `--extra local-embeddings` only for the heavier image.
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

`/health` now returns component diagnostics — a superset of the original
`{"status":"ok"}`, so the Compose healthcheck is unchanged:

```json
{"status":"ok","components":{"db":{"ok":true},"chroma":{"ok":true,"corpus_count":123},
 "llm":{"provider":"openai","key_present":true},"embedding":{"provider":"local-bge-small"}},
 "warnings":[]}
```

It returns HTTP 503 with `"status":"unhealthy"` only when the DB or Chroma is
actually unreachable. An empty corpus is healthy (with a warning) so a fresh
stack can boot and the ingest service can seed it. Key presence is reported as
a boolean only — never a value.

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

The UI is login-gated and a fresh stack has zero users, so provision one
before opening it (the password is prompted, never passed as an argument):

```bash
docker compose run --rm api regwatch create-user analyst@example.com --name "Analyst"
```

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

## Not Done Yet For Production

This remains the local/container baseline. The active production runbook is
`docs/DEPLOY.md`; do not use older Docker-only notes as the production source
of truth when they conflict with that runbook. The consolidated list of open
items lives in `docs/ROADMAP.md`.

Still needed (cross-referenced in `docs/ROADMAP.md`):

- secrets manager + documented/tested key rotation
- SSO / gateway-level auth + TLS termination in front of the app-layer login
  (cookie-session auth shipped; set `AUTH_COOKIE_SECURE=true` once TLS
  terminates; see `docs/PROD_READINESS.md` #1). The current rate limiter is
  in-memory/per-process, so multi-replica needs gateway-level limiting.
- managed Postgres/pgvector actually provisioned (the `DATABASE_URL` switch is
  code-ready) + migration from a clean snapshot + a rehearsed restore drill
- production deployment smoke/load testing behind the approved gateway
- observability and alerts (request/latency/cost metrics, a DB + vector store +
  LLM readiness probe, Sentry DSN configured in prod)
- resource limits
- CI supply-chain checks: dependency audit (pip-audit / uv) + image vuln scan
  (e.g. Trivy)
- Kubernetes manifests or Helm chart, if the hosting decision requires them
- verified production `watch-daily` run history, healthcheck pings, and any
  product-facing alert delivery beyond the in-app `/watch/latest` feed
