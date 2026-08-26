# REGWATCH Docker Guide

Last updated: 2026-08-26

This is the local and container baseline: run the Python API, the Next.js UI and
ingest jobs without changing application code. Production (the Fly app `amneal`)
ships this same API image. `docs/DEPLOY.md` is the production runbook.

## What is in the box

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build. A digest-pinned `golang` stage compiles the static Go proxy binary (`regwatch-proxy`), then the `python:3.12-slim` stage builds the Python app. Both ship in the one API image. |
| `regwatch/frontend/Dockerfile` | Builds the local Next.js UI image. |
| `.dockerignore` | Keeps secrets, local data, docs, caches and local tooling out of the image context. |
| `compose.yaml` | API, UI, one-shot ingest and pgvector `db` services. |
| `docker/entrypoint.sh` | Creates the data directories and runs `regwatch init-db` before app start. Skipped for `alembic` and `regwatch-proxy` argvs, see Startup Behavior. |
| `.github/workflows/ci.yml` | Builds all three images (api, corpus-worker, web) and gates each with a pinned Trivy scan (fixable CRITICAL/HIGH vulns plus embedded secrets). `deploy.yml` re-scans the API image before every Fly release. |
| `pyproject.toml` / `uv.lock` | Heavy local embedding dependencies sit behind the `local-embeddings` extra. |
| `src/regwatch/api/main.py` | Skips DB init when the entrypoint already ran it. |

## Container shape

One Python image serves both the app and the ingest job:

```text
docker image: regwatch:local
  -> api                -> regwatch serve   (dual-stack uvicorn; see docs/GO_PROXY_ROLLOUT.md)
  -> ingest             -> regwatch seed
  (also ships /usr/local/bin/regwatch-proxy; no Compose service runs it)

docker image: regwatch-web:local
  -> web                -> npm run dev
```

The API is long-running. Ingest is deliberately a separate one-shot command so a
30-minute data load never blocks API startup. Production Watch is driven by
`.github/workflows/watch-daily.yml`. There is no orchestration daemon.

Production differs in one important way. On Fly this same API image runs two
process groups (`fly.toml [processes]`). The `proxy` group execs the static Go
binary `regwatch-proxy`, which holds the public edge on :8080 behind
`[http_service]` and relays over the private 6PN network to the `app` group,
which runs `regwatch serve` on :8000. Compose does not run the proxy locally;
the Next.js dev server proxies `/api` straight to the `api` service instead. See
`docs/GO_PROXY_ROLLOUT.md` and `docs/DEPLOY.md`.

## Quick commands

```bash
docker build -t regwatch:local .                       # build the image
docker compose up api                                  # API only
docker compose up --build api web                      # full local stack
docker compose --profile ingest run --rm ingest        # one-shot seed ingest
docker compose config --quiet                          # validate compose syntax
```

Local endpoints:

```text
UI:      http://localhost:3000
API:     http://localhost:8000
```

## Data persistence

Compose mounts the host `./data` directory into the container at `/app/data`, so
raw PDFs and processed output survive container restarts.

The structured store and the vectors both live in Postgres, not under `./data`.
Locally that is the `db` Compose service, a pgvector Postgres backed by the named
`db-data` Docker volume. Postgres with pgvector has been the only datastore since
R5; there is no SQLite or Chroma fallback. `DATABASE_URL` is mandatory. Compose
defaults it to the `db` service, and you can point it at a hosted Postgres
instead. Production points at Databricks Lakebase; the database is the only
part of the stack that stays in the company's Databricks tenant, since
generation and embeddings now go to OpenAI on every normal request. See
`docs/DEPLOY.md`.

Container defaults:

```text
DATA_DIR=/app/data
RAW_PDF_DIR=/app/data/raw
PROCESSED_DIR=/app/data/processed
DATABASE_URL=postgresql://postgres:postgres@db:5432/postgres   # Compose default; mandatory
```

## Embedding modes

Two settings pick the vector space, and it helps to keep them apart:

- `INGEST_EMBEDDING_PROVIDER` (old alias `EMBEDDING_PROVIDER`, still accepted)
  names the provider for the ingest/backfill write path and the legacy retrieval
  arm. Only two provider classes exist: `EchoEmbeddingProvider` (the `echo`
  test provider) and `OpenAIEmbeddingProvider` (`openai`). Any other value
  raises at boot. `src/regwatch/process/embedder.py`.
- `RETRIEVAL_EMBEDDING_PROFILE` (old alias `ACTIVE_EMBEDDING_PROFILE`, still
  accepted) names a registered embedding profile. Anything other than the
  default `legacy` sends vectors to the profile-keyed `chunk_embedding` table
  instead.

**Local Compose defaults to the legacy arm with the test provider**:
`INGEST_EMBEDDING_PROVIDER=echo`, no profile, an offline smoke stack, fenced by
the `REGWATCH_ALLOW_TEST_PROVIDERS` boot guard against seeded corpora. The
legacy `chunk.embedding` column is `vector(1536)`, so any provider on this arm
has to be 1536-dim (`assert_embedding_provider_dim` in `store/pgvector_store.py`
refuses others at boot).

**Production does not run the legacy arm.** It runs a registered profile:
OpenAI `text-embedding-3-large`, truncated to 1024 dimensions via the
`dimensions` API parameter, named by the `RETRIEVAL_EMBEDDING_PROFILE` Fly
secret (`fly.toml` pins `INGEST_EMBEDDING_PROVIDER = "openai"`). Profile
vectors live in `chunk_embedding`, whose embedding column deliberately carries
no dimension typmod. The profile row's dimension is enforced by a database
trigger plus a per-profile expression index, which is what lets several vector
spaces coexist. The legacy column is a frozen historical space, not a
rollback: rollback means a previously promoted profile, never the legacy
column. `docs/PRODUCTION_TRUTH.md` carries the current serving profile id.

To run the production-shaped vector space locally you need a real
`OPENAI_API_KEY`, then two commands in the order `.github/workflows/openai-eval.yml`
uses:

```bash
export OPENAI_API_KEY=sk-...
PROFILE_ID=$(uv run regwatch embedding-profile-register \
  --provider openai --serving-runtime-version openai-api-v1 --id-only)
INGEST_EMBEDDING_PROVIDER=openai RETRIEVAL_EMBEDDING_PROFILE="$PROFILE_ID" \
  uv run regwatch seed
```

Registration is content-addressed and idempotent: rerunning it with the same
arguments returns the same profile id and writes nothing new. Start the API
against that profile the same way:

```bash
INGEST_EMBEDDING_PROVIDER=openai LLM_PROVIDER=openai \
  RETRIEVAL_EMBEDDING_PROFILE="$PROFILE_ID" OPENAI_API_KEY=sk-... \
  docker compose up api
```

Retrieval is an exact pgvector scan; there is no HNSW index requirement to
satisfy first (`PROFILE_HNSW_INDEX_REQUIRED` defaults to false).

There is exactly one build flavor since 2026-08-17: the slim no-torch image.
`docker compose build` takes no flavor argument (the old
`INSTALL_LOCAL_EMBEDDINGS` build arg is gone with the local bge provider).

Do not load the full PSG corpus with `INGEST_EMBEDDING_PROVIDER=echo`. That is enforced
at startup: when an `echo` embedding or LLM provider meets a non-empty pgvector
corpus, the API refuses to boot with a `RuntimeError` explaining the fix (switch
to a real provider, or set `REGWATCH_ALLOW_TEST_PROVIDERS=1` for tests and CI). A
fresh stack with an empty corpus still boots on `echo` so the ingest service can
seed it, but the next `api` start after that ingest fails fast unless the
providers are real. If your mounted `./data` already holds a seeded corpus, set
real providers before `docker compose up api`.

## Why the torch stack stays out of the image

The first Docker build pulled large CUDA and NVIDIA packages through the
`sentence-transformers` and `torch` dependency path, which made the baseline
API image far too heavy for a service smoke test. Those packages live behind
the `local-embeddings` extra, which since 2026-08-17 serves ONLY the
off-by-default cross-encoder reranker (`retrieve/reranker.py`,
`RERANKER_ENABLED`); the local bge embedding provider the extra was named for
was removed. The image installs `--extra llm` only, which is the OpenAI
Responses API transport (`openai>=2.53.0`, `pyproject.toml`), not a Databricks
transport; the Databricks LLM path was removed from `llm.py` entirely and
setting `LLM_PROVIDER` to that retired provider name now raises at boot. No
image flavor ever ships torch.

## Startup behavior

The entrypoint creates the container data directories, runs `regwatch init-db`,
and exports `REGWATCH_DB_INITIALIZED=1`. FastAPI checks that marker and skips its
own `init_db()` call, so the same work does not happen twice.

Four explicit argv shapes change the entrypoint's pre-command `init-db`
behavior, plus an explicit `REGWATCH_INIT_DB=false` override:

- `regwatch release`: the Fly release command migrates first and then runs the
  full serving guard itself. The entrypoint must not run that guard against the
  old stamp before the migration gets a chance to advance it. Fly's
  `RELEASE_COMMAND=1` marker enforces the same bypass if the command is wrapped.
- `alembic ...`: direct operator migration commands retain the same pre-guard
  bypass.
- `regwatch-proxy` (or any path ending in it): the Go proxy must boot
  DB-independent, so a proxy machine never crash-loops on the stamp guard
  while holding the public port.
- `dagster-daemon` / `dagster-webserver`: not a full bypass. Corpus
  maintenance intentionally creates pending vectors, so the public
  serving-profile completeness gate cannot run before the daemon that repairs
  them starts. Instead of skipping init entirely, the entrypoint runs
  `regwatch authoritative-corpus-init-db`, a maintenance-safe init that still
  verifies the exact Alembic head, RLS, and database connectivity, then skips
  the normal `regwatch init-db` call. `docker/entrypoint.sh`.

## Health check

Compose checks `GET http://127.0.0.1:8000/health`.

`/health` returns component diagnostics, a superset of the original
`{"status":"ok"}`, so the Compose healthcheck is unchanged. With the local
Compose defaults it looks like this:

```json
{"status":"ok","components":{"db":{"ok":true,"dialect":"postgresql"},
 "vector_store":{"ok":true,"corpus_count":123},
 "llm":{"provider":"openai","key_present":true},
 "embedding":{"provider":"openai","profile":"legacy"}},
 "whitepaper_template":"absent","warnings":[]}
```

Production also reports `"llm":{"provider":"openai","key_present":true}`: every
LLM role (router, synthesizer, extractor) runs on one model, `fly.toml` pins
`LLM_PROVIDER = "openai"`. The embedding component reports the live profile,
not the `INGEST_EMBEDDING_PROVIDER` setting, because only the legacy arm reads
that setting; reporting it instead would answer "openai" regardless of which
profile actually served the query. In prod it reads
`"embedding":{"provider":"openai","profile":"<the live RETRIEVAL_EMBEDDING_PROFILE id>"}`.
The profile's model name is left out on purpose: `/health` is the one
anonymous-reachable endpoint. `docs/PRODUCTION_TRUTH.md` carries the current
live profile id; do not hardcode it here.

Keys are reported as booleans only, never values, and appear conditionally: `db`
carries `dialect` on success or `error` on failure, and `allow_test_providers`
shows up only when set.

`/health` returns 503 with `"status":"unhealthy"` only when the DB or the vector
store is actually unreachable. An empty corpus is healthy, with a warning, so a
fresh stack can boot and the ingest service can seed it.

## The Next.js UI

The UI is the Next.js app in `regwatch/frontend/` and runs as the Compose `web`
service. It talks to the API through a same-origin `/api` proxy
(`regwatch/frontend/next.config.mjs`). In Compose, `API_PROXY_TARGET` is
`http://api:8000`, so browser traffic only ever talks to the Next.js origin.

The UI is login-gated and a fresh stack has zero users, so create one before
opening it. The password is prompted, never passed as an argument:

```bash
docker compose run --rm api regwatch create-user analyst@example.com --name "Analyst"
```

The local container shape:

```text
api container      -> FastAPI / Python evidence service
web container      -> Next.js / TypeScript UI (proxies /api to api)
ingest container   -> one-shot FDA data loads
db container       -> Postgres + pgvector (structured store + vectors)
```

## Large ingest notes

The container shape holds up for a full PSG load, and the guard rails that
matter are in place: the echo-provider boot refusal above, retry and backoff in
the crawler, and a scheduled daily run in `watch-daily.yml` rather than a human
launching `docker compose run`. The remaining ingest hardening is tracked in
`docs/ROADMAP.md`.

## Not done yet for production

This is the local and container baseline. The production runbook is
`docs/DEPLOY.md`. When an older Docker-only note conflicts with it, the runbook
wins. The consolidated open-item list lives in `docs/ROADMAP.md`.

Done and pruned from the old list here: CI supply-chain checks (pip-audit, npm
audit and Trivy on both images, re-scanned in `deploy.yml`), non-root container
users in both Dockerfiles, managed Postgres with pgvector (Databricks Lakebase
serves prod), TLS termination at Fly's edge (`force_https` plus
`AUTH_COOKIE_SECURE=true` in `fly.toml`), the Kubernetes question (hosting landed
on Fly, no manifests needed), and the daily watch cadence (`watch-daily.yml`,
cron 07:17 UTC).

Still needed, cross-referenced in `docs/ROADMAP.md`:

- an approved secrets-manager policy and a tested key-rotation drill. The secret
  inventory and provisioning runbook already exist in `docs/SECRETS_RUNBOOK.md`.
- SSO/OIDC against the corporate IdP in front of the app-layer login. The rate
  limiter is still per-process, so multiple replicas need gateway-level
  limiting. See `docs/ROADMAP.md`.
- a rehearsed restore drill and least-privilege app DB credentials on the live
  Lakebase Postgres.
- load testing against the live deployment. The analyst smoke flows have run; a
  load test has not.
- observability depth: exported request, latency and cost metrics; confirmation
  that `SENTRY_DSN` is set in prod (it is a Fly secret, and the app logs a loud
  warning when absent); an external uptime monitor beyond `uptime-eval.yml`; and
  product-facing alert delivery beyond the watch cron's optional Slack digest and
  the in-app `/watch/latest` feed.
- resource limits. Neither `compose.yaml` nor `fly.toml` sets any.
