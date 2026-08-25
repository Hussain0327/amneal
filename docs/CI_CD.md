# CI/CD Pipeline and Pre-Push Checklist

Last updated: 2026-08-12

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is the source of truth.
This doc says what each job gates on and gives the local command that satisfies
it, so you find the failure before you push instead of after.

CI runs on every push to `main` and on every pull request. There are nine jobs.
They run independently and any one of them going red blocks the merge.

CD is [`deploy.yml`](../.github/workflows/deploy.yml). It fires after every green
`ci` run on `main` and ships that exact commit to Fly. See "CD: deploy.yml"
below. Vercel builds the frontend separately.

---

## TL;DR, run this before every push

From the repo root. If all of this is green, CI almost certainly is too.

```bash
# 1. Backend: format, lint, types, layering, tests + coverage floor
uv sync --extra dev --extra llm --extra local-embeddings
uv run ruff check src tests migrations tests_contract scripts
uv run black --check src tests migrations tests_contract   # ruff does NOT format; black is its own gate
uv run mypy src tests tests_contract
uv run lint-imports
uv run pytest -q --cov=src/regwatch --cov-fail-under=80

# 2. Python supply-chain audit (--frozen also asserts uv.lock matches pyproject.toml)
uv export --frozen --no-emit-project --no-dev --extra llm \
  --format requirements-txt --output-file requirements-audit.txt
uvx pip-audit -r requirements-audit.txt

# 3. Go lane: format, vet, generated-code drift, lint, query-vs-schema, tests
cd go
test -z "$(gofmt -l .)"                             # any filename printed = fail
go vet ./...
sqlc diff                                           # committed *.sql.go must equal sqlc generate
golangci-lint run
psql "$TEST_DATABASE_URL" -f internal/store/schema.sql && sqlc vet   # BEFORE go test
go test ./...
cd ..

# 4. Frontend: lint, types, build, tests, prod-dep audit
cd regwatch/frontend
npm ci
npm audit --omit=dev --audit-level=high
npm run lint
npx tsc --noEmit
npm run build
npm test                                            # vitest run

# 5. Frontend wire types: regenerate and confirm BOTH committed copies are current
npm run gen:types                                   # needs the API importable (uv sync above)
git diff --exit-code -- openapi.json lib/api-types.ts   # nonzero => commit the regenerated files
cd ../..

# 6. Docker images + Trivy scan (slowest; see "Reproduce the Trivy scan" below)
docker build -t regwatch:ci .
docker compose config --quiet
docker build -t regwatch-web:ci regwatch/frontend
```

Backend Python only: blocks 1 and 2, plus the contract suite if you touched
anything `POST /query` relays through the Go edge. Go only: block 3. Frontend
only: 4 and 5. API schema change: 5. Dependency or Dockerfile change: 6.

Three jobs have no block above. `databricks-eval` needs live Databricks
credentials, and `store-schema-drift` and `cross-service-contract` need a
disposable pgvector Postgres and the proxy binary. Their repro commands are in
their own sections.

---

## The nine jobs

### `databricks-eval`

The live evaluation gate, declared first in `ci.yml`. It calls the reusable
workflow [`databricks-eval.yml`](../.github/workflows/databricks-eval.yml) with
`prose: false` and `selective: false`.

**Read that carefully: the blocking eval runs with both prompt flags off.** It
measures the v5 claims baseline, not the v6 prose plus v7 selective-citation
prompt production actually serves. The v6 and v7 arms exist but are run by hand
through `workflow_dispatch` (the `prose` and `selective` inputs) and upload their
scorecards under their own artifact names.

The job picks its arm at run time:

1. Databricks arm, if `QWEN_EMBEDDING_BASE_URL`, `QWEN_EMBEDDING_TOKEN` and the
   `DATABRICKS_SERVING_RUNTIME_VERSION` repo variable are all set. This is the
   arm that matters: it embeds with Databricks-hosted Qwen3, the same geometry
   production serves. Per [`EVAL_STATUS.md`](EVAL_STATUS.md) it has been
   configured since 2026-08-05 and runs on the same profile id prod uses.
2. Otherwise the job fails loudly ("eval could not run"). The legacy OpenAI
   arm was removed with the OpenAI provider on 2026-08-17.

The Databricks arm does four steps in a fixed order: register the embedding
profile (content-addressed, so it is idempotent), build the HNSW index, seed the
vector store, then run `run_eval --check-thresholds`. The index has to come
before the seed, because seeding activates the profile and the activation assert
demands a ready index.

Blocking thresholds live in `src/regwatch/eval/run_eval.py`: `recall_at_k` at
0.80 and `citation_precision` at 0.74. They are a ratchet against the first real
measurement, not a quality bar. `refusal_accuracy` is measured and printed but
does not block, by owner decision: the product is moving to a conversational Ask
layer that is not meant to refuse. If more than 10 percent of gold turns fail
transport, the run exits 3 instead of scoring, because it did not measure
anything.

Concurrency group `databricks-eval` with `cancel-in-progress: false`, so live
evals serialize. Two at once collide on shared Databricks workspace QPS and trip
that transport gate.

If branch protection pins required checks by job name, the name to add is
`databricks-eval / eval`.

You cannot run this locally without the Databricks credentials. The offline
stand-in is `tests/test_eval_gate.py`, which runs inside plain `pytest` on
hash-based echo embeddings and does not exercise real geometry or the 0.30
refusal threshold.

### `lint-type-test` (Python 3.12 only)

Runs against a real Postgres + pgvector service (`pgvector/pgvector:pg17`) with
`TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres`, so
the production datastore path is exercised. There is no version matrix: prod runs
3.12 only (`python:3.12-slim` in the Dockerfile), so the 3.11 leg was dropped to
halve this job's minutes.

| Step | Local command | Notes |
|---|---|---|
| ruff | `uv run ruff check src tests migrations tests_contract scripts` | Lint only. Does not reformat. `scripts/` is included so the flake8-bandit "S" rules cover the export/codegen helpers. |
| black | `uv run black --check src tests migrations tests_contract` | Formatting gate. Drop `--check` to fix. |
| mypy | `uv run mypy src tests tests_contract` | Strict on `src/`. CI caches `.mypy_cache` between runs. |
| lint-imports | `uv run lint-imports` | import-linter layering contracts: domain logic must not import I/O layers. |
| pytest | `uv run pytest -q --cov=src/regwatch --cov-fail-under=80` | Coverage floor 80 percent, measured around 82 per the comment in `ci.yml`. Includes the offline eval gate and the INV-1..9 invariant tests. |

Dependency sync is `uv sync --extra dev --extra llm --extra local-embeddings`.

The live eval used to live in this job. It moved out to `databricks-eval` so
every live eval serializes through one non-canceling concurrency group.

To exercise the pgvector tests locally, point `TEST_DATABASE_URL` at a local
Postgres with pgvector. Without it, `test_pgvector_store.py` and
`test_postgres_bootstrap.py` skip. They pass locally while leaving the prod path
unverified, so a green local pytest is not proof the pgvector path works.

### `python-audit`

Audits the shipped production closure. The slim image installs the `llm` extra
only, so `local-embeddings` and torch never reach prod and are deliberately out
of scope.

```bash
uv export --frozen --no-emit-project --no-dev --extra llm \
  --format requirements-txt --output-file requirements-audit.txt
uvx pip-audit -r requirements-audit.txt
```

- `--frozen` fails if `uv.lock` is out of sync with `pyproject.toml`. After any
  dependency change, run `uv lock` and commit `uv.lock`.
- pip-audit runs bare. There are no `--ignore-vuln` suppressions today. To add
  one, edit the step in `ci.yml`: pip-audit takes ignores on the command line,
  not from `.trivyignore`.

### `docker-build` (image build + Trivy scan)

```bash
docker build -t regwatch:ci .
docker compose config --quiet
docker build -t regwatch-web:ci regwatch/frontend
```

CI builds with buildx and the GitHub Actions layer cache (separate `api` and
`web` scopes; a cache export failure never fails the build), then prunes the
local buildkit store before scanning. That prune is there because of the
2026-07-20 ENOSPC incident. Both images are then scanned by a pinned
`aquasec/trivy:0.72.0`, gating on fixable CRITICAL/HIGH vulns
(`--ignore-unfixed`) plus any embedded secrets. Suppressions live in
[`.trivyignore`](../.trivyignore). This is the job that fails most often; see the
dedicated section below.

### `frontend`

Working directory `regwatch/frontend`, Node 20.

| Step | Local command |
|---|---|
| install | `npm ci` |
| prod-dep audit | `npm audit --omit=dev --audit-level=high` |
| lint | `npm run lint` |
| typecheck | `npx tsc --noEmit` |
| build | `npm run build` |
| test | `npm test` (the script is `vitest run`, non-watch) |

`npm audit` gates on production deps only. Dev-only build-tool advisories do not
block, because they never reach the served app.

### `frontend-contract`

The wire types in `lib/api-types.ts` are generated from the FastAPI OpenAPI
schema, and `regwatch/frontend/openapi.json` is the committed contract snapshot.
CI regenerates both and fails if either committed copy is stale. It first asserts
both files are tracked, so an untracked copy cannot pass vacuously.
`tests/test_openapi_contract.py` is the matching pytest-side guard.

```bash
cd regwatch/frontend
npm run gen:types                                    # gen:openapi (needs the API importable), then openapi-typescript
git diff --exit-code -- openapi.json lib/api-types.ts   # must be clean
```

If you change any API request or response model, run `gen:types` and commit the
regenerated `openapi.json` and `lib/api-types.ts` in the same change.

### `go-proxy`

The Go lane for everything under `go/`: the proxy that holds prod's public edge
plus the native store and API code. Runs against the same pgvector service
container as `lint-type-test`. From `go/`:

| Step | Local command | Notes |
|---|---|---|
| gofmt | `gofmt -l .` | Any filename printed = fail. This lane's `black --check`. |
| vet | `go vet ./...` | |
| sqlc diff | `sqlc diff` | Committed `*.sql.go` must equal what sqlc 1.31.1 generates. Hermetic, no DB needed. |
| golangci-lint | `golangci-lint run` | CI pins v2.12.2. |
| sqlc vet | `psql "$TEST_DATABASE_URL" -f internal/store/schema.sql && sqlc vet` | db-prepare PREPAREs every query against a real Postgres carrying the committed snapshot, so a query naming a column that does not exist fails here, not at runtime. Run this before `go test`: the tests drop and recreate `schema public`. |
| test | `go test ./...` | The store's opt-in tests use `TEST_DATABASE_URL`. |

### `store-schema-drift`

Proves the committed Go store schema snapshot (`go/internal/store/schema.sql`)
still equals what Alembic head produces. It bootstraps a throwaway pgvector
database the canonical way (`regwatch init-db`), re-runs the generation pipeline,
and diffs. A failure means a Python migration changed a step-4 table without
regenerating the Go side.

```bash
# needs a FRESH disposable pgvector Postgres on localhost:5432
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  EMBEDDING_PROVIDER=echo uv run regwatch init-db
bash scripts/gen-store-schema.sh \
  'postgresql://postgres:postgres@localhost:5432/postgres' /tmp/schema.live.sql
diff -u go/internal/store/schema.sql /tmp/schema.live.sql
```

`EMBEDDING_PROVIDER=echo` there is a bootstrap trick, not a statement about
prod. The legacy `chunk.embedding` column is `vector(1536)`, and naming the
1536-dim test provider satisfies the dimension assert without any credentials
(there is no default provider; an unset value refuses to boot).

CI runs `pg_dump` from the `pgvector/pgvector:pg17` image so the client lineage
matches the server. Do the same locally if your `pg_dump` is older than 17. On
failure: regenerate with `scripts/gen-store-schema.sh` against a fresh `init-db`
database, run `sqlc generate`, and commit both.

### `cross-service-contract`

The R1 cross-service harness: the real Go proxy binary, real uvicorn and real
Postgres, driven through the Go edge (`tests_contract/`, scenarios S1 to S23).
Pytest fixtures own both subprocesses; the job only supplies toolchains, the
disposable DB and the prebuilt proxy binary.

```bash
cd go && go build -trimpath -o /tmp/regwatch-proxy ./cmd/proxy && cd ..
uv sync --extra dev --extra llm      # llm extra = prod-image parity (real openai SDK for the dead-provider scenarios)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  REGWATCH_PROXY_BIN=/tmp/regwatch-proxy uv run pytest tests_contract -q
```

CI omits `-ldflags "-s -w"` on purpose so a core dump keeps symbols. The shipped
image strips.

---

## CD: `deploy.yml`

[`deploy.yml`](../.github/workflows/deploy.yml) is the continuous-deployment
follow-on. It triggers on `workflow_run` completion of `ci` and runs only when
all four guards hold: the run concluded `success`, its head repository is this
repo (which rejects the fork-PR pwn-request surface), the event was a `push`, and
the branch is `main`. It never runs for a pull request or a feature branch.

What it does, in order:

1. Checks out `workflow_run.head_sha`, the exact commit CI validated, even if
   `main` has since moved on.
2. Builds the API image once (from the `api` layer-cache scope CI's
   push-to-main run refreshed) and re-scans it with the same pinned
   `aquasec/trivy:0.72.0` gate CI used, so a CVE or secret the vuln DB learned
   about after CI ran still blocks the release.
3. Pushes the scanned image to `registry.fly.io/amneal:sha-<commit>` and deploys
   it by reference with flyctl (the binary is pinned to `0.4.71`, not just the
   installer action), wrapped in `scripts/fly-deploy.sh`, which retries only
   transient Fly platform errors such as "machine still starting" and fails fast
   on everything else. No remote build happens: the bytes Trivy passed are the
   bytes the machines boot, and the job logs `flyctl image show` after the roll.
4. Fly runs `fly.toml [deploy] release_command = "regwatch release"` in a
   one-off machine before the rolling replace. It migrates the live schema to
   the build's head and then runs the full serving-readiness guard, so profile
   coverage/configured-index/provider drift also aborts before any machine is
   replaced.

Guard rails: a 45-minute job timeout, and a `deploy-fly` concurrency group with
`cancel-in-progress: false`, so an in-flight release is never killed mid-roll
(that could leave the app half-migrated). The next deploy queues instead. The
only required secret is `FLY_API_TOKEN`. Net effect: every green `ci` run on
`main` auto-deploys, and a red one ships nothing.

---

## The other workflows (not merge gates)

- [`watch-daily.yml`](../.github/workflows/watch-daily.yml): the daily Watch run,
  cron `17 7 * * *` (07:17 UTC). It is the only production driver of the watch
  pipeline. It pins Qwen3 and requires the named profile plus base URL, token,
  model, revision and dimension. A pre-crawl `init-db` gate validates the
  registered profile; an `always()` post-ingest step asserts zero pending chunks.
  Those six `WATCH_*` repository secrets were still absent on 2026-08-12, so the
  workflow fails closed before crawl until the owner provisions them and
  verifies a manual dispatch.
- [`uptime-eval.yml`](../.github/workflows/uptime-eval.yml): curls
  `PROD_HEALTH_URL` every 30 minutes and asserts `"status": "ok"`. Skips cleanly
  while that secret is unset.

---

## Gotchas that have actually broken this gate

- **black is its own gate; ruff is not enough.** `ruff check` lints but does not
  format. Run `uv run black src tests migrations tests_contract` before pushing
  Python or `black --check` fails. The Go twin is `gofmt -l`.
- **Stale `api-types.ts` or `openapi.json`.** Editing a FastAPI schema without
  regenerating the wire types fails `frontend-contract`, which diffs both files.
  Run `gen:types` and commit both results.
- **Stale generated Go code.** Editing `go/internal/store` queries without
  `sqlc generate` fails `sqlc diff`. A migration touching a step-4 table without
  regenerating `go/internal/store/schema.sql` fails `store-schema-drift`.
- **`uv.lock` drift.** Changing `pyproject.toml` without `uv lock` fails
  `python-audit`'s `--frozen` export.
- **Two live evals at once.** They share one Databricks workspace, collide on
  QPS and trip the 10 percent transport gate. The `databricks-eval` concurrency
  group serializes CI's own runs; a hand-launched dispatch queues behind them.
- **Trivy on the web image.** The recurring one, see below.
- **pgvector tests skip without a DB.** A green local `pytest` may not have run
  the Postgres path. CI always does.
- **ASCII only in committed files.** No em dashes, no smart quotes (repo rule in
  `CLAUDE.md`). Not CI-enforced, but it breaks parsers and review tooling.

---

## Trivy: why the image scan keeps failing, and how to fix it fast

Two recurring root causes, both confirmed by reproducing the scan.

1. **esbuild ships a Go binary.** Every "Go stdlib" CVE in `.trivyignore` comes
   from `@esbuild/<platform>/bin/esbuild` (Type=gobinary) in the web image,
   pulled in transitively by vite and vitest. We do not build esbuild and cannot
   bump its embedded Go toolchain; only an upstream esbuild release can. esbuild
   runs as a local code transform and is never a network or TLS peer, so its
   net/* and crypto/tls CVEs are unreachable. Ignoring them is correct.

   **The API image also carries a Go binary, and that one is ours.** Since PR #93
   the API image ships `/usr/local/bin/regwatch-proxy`, built from the
   Dockerfile's digest-pinned `golang:` stage, and since the July 2026 flip that
   binary is the public edge in prod. So a Go stdlib CVE can fire on the API
   image too, and there the remedy is the opposite: bump the pinned `golang:`
   digest in the Dockerfile and rebuild. Never add an API-image Go CVE to
   `.trivyignore`. Check which image the finding came from before reaching for
   the ignorefile.
2. **Trivy flips advisory primary ids between GHSA and CVE.** The legacy
   ignorefile matches the exact `VulnerabilityID` and does not resolve aliases.
   When Trivy's DB switches an advisory from its GHSA id to a CVE id, as it did
   for the vite and vitest findings, a previously ignored finding silently comes
   back. Fix: list both id forms in `.trivyignore`.

### Reproduce the scan locally (exact CI command)

```bash
docker build -t regwatch:ci .
docker build -t regwatch-web:ci regwatch/frontend

# Scan an image exactly as CI does:
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.cache/trivy:/root/.cache/trivy" \
  -v "$PWD/.trivyignore:/root/.trivyignore" \
  aquasec/trivy:0.72.0 image \
  --exit-code 1 --ignore-unfixed --severity CRITICAL,HIGH \
  --ignorefile /root/.trivyignore \
  --scanners vuln,secret regwatch-web:ci          # swap regwatch:ci for the API image
```

Exit 0 = pass. To see what is failing and where, drop `--exit-code` and
`--ignorefile`, add `--format json --output report.json`, then inspect `Target`,
`PkgName`, `VulnerabilityID` and `FixedVersion`. The esbuild Go version is
arch-independent, so an arm64 local run matches amd64 CI. Only the path differs:
`linux-arm64` vs `linux-x64`.

### Adding a suppression

Edit [`.trivyignore`](../.trivyignore), put the CVE under the right commented
block with a one-line reachability justification, and list both the CVE and the
GHSA id when an advisory has both. Re-run the scan above and confirm exit 0
before pushing. The `aquasec/trivy` tag is pinned to `0.72.0` in both `ci.yml`
and `deploy.yml`, so bump them together and deliberately. The pin does not
prevent the alias-flip problem: the vuln DB is downloaded fresh at scan time
regardless of the binary version.
