# REGWATCH Docker Guide

This document records the Docker work added to REGWATCH and how to use it.

The goal is a reliable local/container baseline that can run the Python API,
Next.js UI, and ingest jobs without changing the core application code.
Production (the Fly app `amneal`) ships this same API image; `docs/DEPLOY.md`
is the production runbook. (Dagster orchestration was removed in R5; GitHub
Actions cron is the sole scheduler.)

## What Was Added

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build: a digest-pinned `golang` stage compiles the static Go proxy binary (`regwatch-proxy`), then the `python:3.12-slim` stage builds the Python application; both ship in the one API image. |
| `regwatch/frontend/Dockerfile` | Builds the local Next.js UI image. |
| `.dockerignore` | Keeps secrets, local data, docs, caches, and local tooling out of the image context. |
| `compose.yaml` | Defines the API, UI, one-shot ingest, and pgvector `db` services. |
| `docker/entrypoint.sh` | Creates container data directories and runs `regwatch init-db` before app start (skipped for `alembic` and `regwatch-proxy` argvs -- see Startup Behavior). |
| `.github/workflows/ci.yml` | Builds both images in CI and gates them with a pinned Trivy scan (fixable CRITICAL/HIGH vulns + embedded secrets); `deploy.yml` re-scans the API image before every Fly release. |
| `pyproject.toml` / `uv.lock` | Moves heavy local embedding dependencies behind the `local-embeddings` extra. |
| `src/regwatch/api/main.py` | Avoids running DB initialization twice when the entrypoint already ran it. |
| `src/regwatch/process/embedder.py` | Gives a clear error if local embeddings are requested without installing the extra. |

## Container Shape

One Python image is reused for app and ingest jobs:

1. API service
2. Ingest service

The API is a long-running service. Ingest is intentionally a separate one-shot
command so a large 30-minute data load does not block API startup. Production
Watch is driven by `.github/workflows/watch-daily.yml`; there is no local
orchestration daemon (Dagster was removed in R5).

```text
docker image: regwatch:local
  -> api                -> regwatch serve   (dual-stack uvicorn; see docs/GO_PROXY_ROLLOUT.md)
  -> ingest             -> regwatch seed
  (also ships /usr/local/bin/regwatch-proxy -- no Compose service runs it)

docker image: regwatch-web:local
  -> web                -> npm run dev
```

Production differs here: on Fly this same API image runs TWO process groups
(`fly.toml [processes]`). The `proxy` group execs the static Go binary
`regwatch-proxy`, which holds the public edge on :8080 behind `[http_service]`
and relays over the private 6PN network to the `app` group, which runs
`regwatch serve` on :8000. Compose does not run the proxy locally -- the
Next.js dev server proxies `/api` straight to the `api` service instead. See
`docs/GO_PROXY_ROLLOUT.md` and `docs/DEPLOY.md`.

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
docker compose up --build api web
```

Local endpoints:

```text
UI:      http://localhost:3000
API:     http://localhost:8000
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

- raw PDF files
- processed output files

The structured store and the vectors both live in Postgres, not under `./data`
(the `db` Compose service, a pgvector-image Postgres, backed by the named
`db-data` Docker volume). Postgres + pgvector is the only datastore since R5 —
there is no SQLite/Chroma fallback. `DATABASE_URL` is mandatory; Compose
defaults it to the `db` service (`postgresql://postgres:postgres@db:5432/postgres`)
but you can point it at a Supabase session-pooler URL instead — see
`docs/DEPLOY.md`.

Container defaults:

```text
DATA_DIR=/app/data
RAW_PDF_DIR=/app/data/raw
PROCESSED_DIR=/app/data/processed
DATABASE_URL=postgresql://postgres:postgres@db:5432/postgres   # Compose default; Postgres + pgvector, mandatory
```

## Embedding Modes

Compose defaults to `EMBEDDING_PROVIDER=openai` (`INSTALL_LOCAL_EMBEDDINGS=true`
is still the build-arg default, so the image can also run local embeddings if
you override the env var). Since R5 the Compose `db` service is a
`vector(1536)` pgvector Postgres, and OpenAI's `text-embedding-3-small` is the
only bundled provider whose dimension matches — `local-bge-small` (384-dim) is
rejected at boot by the dimension assert (`assert_embedding_provider_dim` in
`store/pgvector_store.py`). `local-bge-small` remains available for
offline/eval tooling, not as the app datastore's embedding provider.

```text
INSTALL_LOCAL_EMBEDDINGS=true        # compose build-arg default
EMBEDDING_PROVIDER=openai            # compose environment default; plus OPENAI_API_KEY
```

For a slim no-torch stack — the production pairing, see `docs/DEPLOY.md` —
set:

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
**non-empty** pgvector corpus, the API refuses to boot with a `RuntimeError`
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
3. Keep the slim Docker baseline build on `--extra llm` only (no torch); add
   `--extra local-embeddings` only for the heavier image.
4. Use the PyTorch CPU index for Linux when the local embedding extra is
   installed.

This gives two useful modes:

- lightweight API image for health checks and API development
- heavier local-embedding image for actual local PSG ingest

## Startup Behavior

The entrypoint creates the container data directories, then runs:

```bash
regwatch init-db
```

and exports:

```text
REGWATCH_DB_INITIALIZED=1
```

FastAPI checks that marker and skips its own duplicate `init_db()` call. This
prevents startup from doing the same migration/init work twice.

Two argv shapes skip `init-db` entirely (plus an explicit
`REGWATCH_INIT_DB=false` override):

- `alembic ...` -- the Fly release_command (`alembic upgrade head`) exists to
  MOVE the schema stamp to head; the boot guard would otherwise refuse and
  abort the deploy before the migration ever ran.
- `regwatch-proxy` -- the Go proxy must boot DB-independent, so a proxy
  machine never crash-loops on the stamp guard while holding the public port.

## Health Check

Compose checks:

```text
GET http://127.0.0.1:8000/health
```

`/health` now returns component diagnostics -- a superset of the original
`{"status":"ok"}`, so the Compose healthcheck is unchanged:

```json
{"status":"ok","components":{"db":{"ok":true,"dialect":"postgresql"},
 "vector_store":{"ok":true,"corpus_count":123},
 "llm":{"provider":"openai","key_present":true},"embedding":{"provider":"openai"}},
 "whitepaper_template":"absent","warnings":[]}
```

The providers shown are the local Compose defaults. Production reports
`"llm":{"provider":"databricks","key_present":true}` -- since the Databricks
cutover every LLM role runs on the in-tenant `gpt-oss-20b` endpoint, while
embeddings stay `openai` (see `docs/DATABRICKS_ADOPTION_2026-07-28.md`). Keys
are conditional by design: `db` carries `dialect` on success XOR `error` on
failure, and `allow_test_providers` appears only when set.

It returns HTTP 503 with `"status":"unhealthy"` only when the DB or the vector
store is actually unreachable. An empty corpus is healthy (with a warning) so a fresh
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
(`tests/test_eval_gate.py`) runs inside `uv run pytest`. The separate LIVE eval
lane in CI is key-gated and deliberately OFF: setting the repo-wide
`OPENAI_API_KEY` un-skips it and it currently fails (`refusal_accuracy` 0.917
against the 0.95 floor), turning CI red and blocking CD -- see `docs/CI_CD.md`
and `docs/SECRETS_RUNBOOK.md`.

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
db container       -> Postgres + pgvector (structured store + vectors)
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
  `docker compose run` launches

## Not Done Yet For Production

This remains the local/container baseline. The active production runbook is
`docs/DEPLOY.md`; do not use older Docker-only notes as the production source
of truth when they conflict with that runbook. The consolidated list of open
items lives in `docs/ROADMAP.md`.

Several items from the original list are DONE and pruned: CI supply-chain
checks (pip-audit + npm audit + Trivy on both images in `ci.yml`, re-scanned in
`deploy.yml`), non-root container users (both Dockerfiles drop privileges),
managed Postgres/pgvector provisioning (Supabase serves prod), TLS termination
(Fly's edge, `force_https` + `AUTH_COOKIE_SECURE=true` in `fly.toml`), the
Kubernetes question (the hosting decision landed on Fly; no manifests needed),
and the daily watch cadence (`watch-daily.yml`, cron 07:17 UTC, is the live
prod scheduler).

Still needed (cross-referenced in `docs/ROADMAP.md`):

- an approved secrets-manager policy + a tested key-rotation drill (the secret
  inventory and provisioning runbook exist: `docs/SECRETS_RUNBOOK.md`)
- SSO/OIDC against the corporate IdP in front of the app-layer login (see
  `docs/PROD_READINESS.md` #1). The rate limiter is still per-process, so
  multi-replica needs gateway-level limiting.
- a rehearsed restore drill + least-privilege app DB credentials for the live
  Supabase Postgres
- load testing against the live deployment (the analyst smoke flows have run;
  a load test has not)
- observability depth: exported request/latency/cost metrics, confirmation
  that `SENTRY_DSN` is set in prod (a Fly secret; the app logs a loud warning
  when absent), an external uptime monitor beyond `uptime-eval.yml`, and
  product-facing alert delivery beyond the watch cron's optional Slack digest
  and the in-app `/watch/latest` feed
- resource limits (`compose.yaml` and `fly.toml` set none)
