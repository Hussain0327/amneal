# CI/CD Pipeline and Pre-Push Checklist

The single source of truth is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
This doc explains what it gates on and gives the exact local command to satisfy
each job, so you catch failures before you push instead of after.

CI runs on **every push to `main`** and **every pull request**. There are eight
independent jobs; a red mark on any one blocks the merge. CD is
[`deploy.yml`](../.github/workflows/deploy.yml): a `workflow_run` follow-on that
fires after every GREEN `ci` run on `main` and ships the exact validated commit
to Fly (build + Trivy re-scan + `flyctl deploy`) -- see "CD: deploy.yml" below.
Vercel builds the frontend separately.

---

## TL;DR -- run this before every push

From the repo root. This mirrors the gating jobs in order; if it is all green,
CI almost certainly passes too.

```bash
# 1. Backend: format, lint, types, layering, tests + coverage floor
uv sync --extra dev --extra llm --extra local-embeddings
uv run ruff check src tests migrations tests_contract scripts
uv run black --check src tests migrations tests_contract   # NOTE: ruff does NOT format; black is a separate gate
uv run mypy src tests tests_contract
uv run lint-imports
uv run pytest -q --cov=src/regwatch --cov-fail-under=80

# 2. Python supply-chain audit (asserts uv.lock matches pyproject.toml via --frozen)
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

# 5. Frontend wire-type contract: regenerate and confirm BOTH committed copies are current
npm run gen:types                                   # needs the API importable (uv sync above)
git diff --exit-code -- openapi.json lib/api-types.ts   # nonzero => commit the regenerated files
cd ../..

# 6. Docker images + Trivy scan (slowest; see "Reproduce the Trivy scan" below)
docker build -t regwatch:ci .
docker compose config --quiet
docker build -t regwatch-web:ci regwatch/frontend
```

If you only touched backend Python, blocks 1-2 are the ones that matter (plus the
contract suite in job 8 if you touched anything `POST /query` relays through the
Go edge). Go changes: block 3. Frontend only: 4-5. API schema changes: 5. If you
changed dependencies or a Dockerfile, do 6. Two jobs have no block above because
they need a disposable pgvector Postgres and/or the proxy binary --
`store-schema-drift` (job 7) and `cross-service-contract` (job 8); their exact
repro commands are in their sections below.

---

## The eight jobs, one by one

### 1. `lint-type-test` (Python 3.12 only)

Runs against a real **Postgres + pgvector** service (`pgvector/pgvector:pg17`) with
`TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres`, so the
production datastore path is exercised. No version matrix anymore: prod runs only
3.12 (`python:3.12-slim` in the Dockerfile), so the 3.11 leg was dropped to halve
this (the heaviest) job's minutes.

| Step | Local command | Notes |
|---|---|---|
| ruff | `uv run ruff check src tests migrations tests_contract scripts` | Lint only. Does **not** reformat. `scripts/` is included so the flake8-bandit "S" rules cover the export/codegen helpers. |
| black | `uv run black --check src tests migrations tests_contract` | Formatting gate. Run `uv run black src tests migrations tests_contract` to fix. |
| mypy | `uv run mypy src tests tests_contract` | Strict on `src/`. CI caches `.mypy_cache` between runs. |
| lint-imports | `uv run lint-imports` | import-linter layering contracts (domain logic must not import I/O layers). |
| pytest | `uv run pytest -q --cov=src/regwatch --cov-fail-under=80` | Coverage floor is 80% (measured ~82%). Includes the deterministic eval gate and the INV-1..9 invariant tests. |

- Dep sync is `uv sync --extra dev --extra llm --extra local-embeddings` (there is
  no `orchestration` extra anymore -- Dagster left with R5).
- The `seed` and `eval --check-thresholds` steps run **only if `OPENAI_API_KEY` is
  set** as a repo secret. It was absent when verified on 2026-07-30, so the
  latest CI run skipped both provider-backed steps. Current live-corpus
  `run_eval` pass/fail is unknown; the previously cited `0.917` came from the
  separate advisory threshold sweep and was not `run_eval.refusal_accuracy`.
  Adding the secret activates paid live seed/eval work and makes those
  thresholds deployment-gating, so establish and review an explicit baseline
  before enabling it repo-wide. The watch cron uses separately scoped
  `WATCH_OPENAI_API_KEY`. See [`EVAL_STATUS.md`](EVAL_STATUS.md) and
  [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md). Locally, `pytest` runs the offline
  deterministic fixture without an API key.
- To exercise the pgvector tests locally, point `TEST_DATABASE_URL` at a local
  Postgres+pgvector. Without it, `test_pgvector_store.py` /
  `test_postgres_bootstrap.py` **skip** -- they pass locally but the prod path
  goes unverified, so do not treat a green local pytest as covering pgvector.

### 2. `python-audit`

Audits the **shipped production closure** (the slim image installs the `llm` extra
only; `local-embeddings`/torch never reaches prod and is intentionally out of
scope).

```bash
uv export --frozen --no-emit-project --no-dev --extra llm \
  --format requirements-txt --output-file requirements-audit.txt
uvx pip-audit -r requirements-audit.txt
```

- `--frozen` fails if `uv.lock` is out of sync with `pyproject.toml`. After any
  dependency change run `uv lock` and commit `uv.lock`.
- pip-audit runs **bare** -- there are currently no `--ignore-vuln` suppressions
  (the old chromadb ChromaToast ignores went away with the dependency itself). To
  add a Python ignore, edit this step in `ci.yml` (pip-audit takes ignores on the
  command line, not from `.trivyignore`).

### 3. `docker-build` (image build + Trivy scan)

```bash
docker build -t regwatch:ci .
docker compose config --quiet
docker build -t regwatch-web:ci regwatch/frontend
```

CI builds with buildx + the GitHub Actions layer cache (separate `api`/`web`
scopes; a cache export failure never fails the build) and prunes the local
buildkit store before scanning -- the 2026-07-20 ENOSPC incident. Then both
images are scanned by a **pinned** `aquasec/trivy:0.72.0`, gating on **FIXABLE
CRITICAL/HIGH** vulns (`--ignore-unfixed`) plus any embedded **secrets**.
Suppressions live in [`.trivyignore`](../.trivyignore). See the dedicated
section below -- this is the job that fails most often.

### 4. `frontend`

Working directory `regwatch/frontend`.

| Step | Local command |
|---|---|
| install | `npm ci` |
| prod-dep audit | `npm audit --omit=dev --audit-level=high` |
| lint | `npm run lint` |
| typecheck | `npx tsc --noEmit` |
| build | `npm run build` |
| test | `npm test` (the script is `vitest run`, non-watch) |

`npm audit` gates on **production** deps only (`--omit=dev`); dev-only build-tool
advisories do not block (they do not reach the served app).

### 5. `frontend-contract`

The wire types in `lib/api-types.ts` are generated from the FastAPI OpenAPI
schema, and `regwatch/frontend/openapi.json` is the committed contract snapshot.
CI regenerates **both** and fails if **either** committed copy is stale (it first
asserts both files are tracked, so an untracked copy cannot pass vacuously). The
frontend contract can therefore never silently drift from the backend in either
direction; `tests/test_openapi_contract.py` is the matching pytest-side guard.

```bash
cd regwatch/frontend
npm run gen:types                                    # runs gen:openapi (needs API importable) then openapi-typescript
git diff --exit-code -- openapi.json lib/api-types.ts   # must be clean
```

**If you change any API request/response model, run `gen:types` and commit the
regenerated `openapi.json` + `lib/api-types.ts` in the same change.**

### 6. `go-proxy`

The Go lane for everything under `go/` (the proxy that holds prod's public edge
plus the native store/API code). Runs against the same pgvector service container
as job 1. Local commands, from `go/`:

| Step | Local command | Notes |
|---|---|---|
| gofmt | `gofmt -l .` | Any filename printed = fail (this lane's `black --check`). |
| vet | `go vet ./...` | |
| sqlc diff | `sqlc diff` | Committed `*.sql.go` must equal what sqlc 1.31.1 generates. Hermetic -- no DB needed. |
| golangci-lint | `golangci-lint run` | CI pins v2.12.2. |
| sqlc vet | `psql "$TEST_DATABASE_URL" -f internal/store/schema.sql && sqlc vet` | db-prepare PREPAREs every query against a real Postgres carrying the committed snapshot -- a query naming a nonexistent column fails here, not at runtime. **Run before `go test`**: the tests drop and recreate `schema public`. |
| test | `go test ./...` | The store's opt-in tests use `TEST_DATABASE_URL`. |

### 7. `store-schema-drift`

Proves the committed Go store schema snapshot (`go/internal/store/schema.sql`)
still equals what Alembic head actually produces: bootstrap a throwaway pgvector
database the canonical way (`regwatch init-db`), re-run the exact generation
pipeline, and diff. A failure means a Python migration changed a step-4 table
without regenerating the Go side.

```bash
# needs a FRESH disposable pgvector Postgres on localhost:5432
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  EMBEDDING_PROVIDER=openai uv run regwatch init-db    # openai satisfies the dim assert with no key
bash scripts/gen-store-schema.sh \
  'postgresql://postgres:postgres@localhost:5432/postgres' /tmp/schema.live.sql
diff -u go/internal/store/schema.sql /tmp/schema.live.sql
```

CI runs `pg_dump` from the `pgvector/pgvector:pg17` image so the client lineage
matches the server; do the same locally if your `pg_dump` is older than 17. On
failure: regenerate with `scripts/gen-store-schema.sh` against a fresh `init-db`
database, run `sqlc generate`, and commit both.

### 8. `cross-service-contract`

The R1 cross-service harness: the **real** Go proxy binary + **real** uvicorn +
**real** Postgres, driven through the Go edge (`tests_contract/`, scenarios
S1-S23). Pytest fixtures own both subprocesses; the job only provides toolchains,
the disposable DB, and the prebuilt proxy binary.

```bash
cd go && go build -trimpath -o /tmp/regwatch-proxy ./cmd/proxy && cd ..
uv sync --extra dev --extra llm      # llm extra = prod-image parity (real openai SDK for the dead-provider scenarios)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  REGWATCH_PROXY_BIN=/tmp/regwatch-proxy uv run pytest tests_contract -q
```

(CI omits `-ldflags "-s -w"` on purpose so a core dump keeps symbols; the shipped
image strips.)

---

## CD: `deploy.yml`

[`deploy.yml`](../.github/workflows/deploy.yml) is the continuous-deployment
follow-on. It triggers on `workflow_run` completion of `ci` and runs **only**
when all four guards hold: the run concluded `success`, its head repository is
this repo (rejects the fork-PR pwn-request surface), the event was a `push`, and
the branch is `main`. It never runs for a pull request or a feature branch.

What it does, in order:

1. Checks out `workflow_run.head_sha` -- the **exact commit CI validated**, even
   if `main` has since advanced.
2. Rebuilds the API image and **re-scans it with the same pinned
   `aquasec/trivy:0.72.0` gate as CI**, so a CVE or secret the vuln DB learned
   about after CI ran still blocks the release.
3. Deploys with flyctl (binary pinned to `0.4.71`, not just the installer
   action), wrapped in `scripts/fly-deploy.sh`, which retries only transient Fly
   platform errors ("machine still starting") and fails fast on everything else.
4. Fly runs `fly.toml [deploy] release_command = "alembic upgrade head"` in a
   one-off machine **before** the rolling replace, so the live schema reaches the
   build's head before the app's boot-time stamp guard checks it.

Guard rails: a 45-minute job timeout, and a `deploy-fly` concurrency group with
`cancel-in-progress: false` -- an in-flight release is never killed mid-roll (that
could leave the app half-migrated); the next deploy queues instead. The only
required secret is `FLY_API_TOKEN`. Net effect: **every green `ci` run on `main`
auto-deploys** -- and a red CI run (including one turned red by setting
`OPENAI_API_KEY`, see job 1) ships nothing.

---

## Gotchas that have actually broken this gate

- **black is its own gate; ruff is not enough.** `ruff check` lints but does not
  format. Always run `uv run black src tests migrations tests_contract` before
  pushing Python or CI's `black --check` fails. (The Go-side twin: `gofmt -l`
  gates the `go-proxy` job the same way.)
- **Stale `api-types.ts` / `openapi.json`.** Editing a FastAPI schema without
  regenerating the wire types fails `frontend-contract` -- it now diffs BOTH the
  committed `openapi.json` snapshot and `lib/api-types.ts`. Run `gen:types` and
  commit both results.
- **Stale generated Go code.** Editing `go/internal/store` queries without
  `sqlc generate` fails `go-proxy`'s `sqlc diff`; a migration that touches a
  step-4 table without regenerating `go/internal/store/schema.sql` fails
  `store-schema-drift`. Regenerate and commit alongside the change.
- **`uv.lock` drift.** Changing `pyproject.toml` without `uv lock` fails
  `python-audit`'s `--frozen` export.
- **Setting the repo-wide `OPENAI_API_KEY` activates provider-backed seed/eval.**
  That result gates CD, but its current outcome is unverified. Run and review a
  controlled baseline before making it a permanent repo-wide gate; see job 1.
- **Trivy / web image (the recurring one).** See below.
- **pgvector tests skip without a DB.** A green local `pytest` may not have run the
  Postgres path; CI always does.
- **ASCII only in committed files.** No em-dashes or smart quotes (repo rule in
  `CLAUDE.md`). Not CI-enforced, but it breaks parsers and review tooling -- use
  `-` and straight quotes.

---

## Trivy: why the image scan keeps failing, and how to fix it fast

Two recurring root causes, both verified by reproducing the scan:

1. **esbuild ships a Go binary.** Every "Go stdlib" CVE in `.trivyignore` comes
   from `@esbuild/<platform>/bin/esbuild` (Type=gobinary) in the **web** image,
   pulled in transitively by vite/vitest. We do not build esbuild and cannot bump
   its embedded Go toolchain; only an upstream esbuild release can. esbuild runs as
   a local code transform, never as a network/TLS peer, so its net/* and crypto/tls
   CVEs are unreachable -- ignoring them is correct and consistent.

   **The API image now has a Go binary too, and it is OURS.** Since PR #93 the API
   image ships `/usr/local/bin/regwatch-proxy`, built from the Dockerfile's
   digest-pinned `golang:` stage -- and since the July 2026 flip that binary IS the
   public edge in prod. So a Go stdlib CVE can fire on the **API** image as well --
   and the remedy there is the opposite of the esbuild case: bump the pinned
   `golang:` digest in the Dockerfile and rebuild. Never add an API-image Go CVE to
   `.trivyignore`; we own that toolchain. Check which image the finding came from
   before reaching for the ignorefile.
2. **Trivy flips advisory primary ids (GHSA <-> CVE).** Trivy's legacy ignorefile
   matches the **exact** `VulnerabilityID` and does **not** resolve aliases. When
   Trivy's DB switches an advisory from its GHSA id to a CVE id (as it did for the
   vite/vitest findings), a previously-ignored finding silently resurfaces. Fix:
   list **both** id forms in `.trivyignore`.

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

Exit 0 = pass. To see what is failing and where, drop `--exit-code`/`--ignorefile`
and add `--format json --output report.json`, then inspect `Target`, `PkgName`,
`VulnerabilityID`, `FixedVersion`. The esbuild Go version is arch-independent, so an
arm64 local run matches amd64 CI (only the path differs: `linux-arm64` vs `linux-x64`).

### Adding a suppression

Edit [`.trivyignore`](../.trivyignore), put the CVE under the right commented block
with a one-line reachability justification, and **list both the CVE and GHSA id**
when an advisory has both. Re-run the scan above and confirm exit 0 before pushing.
The `aquasec/trivy` tag is pinned to `0.72.0` in **both** `ci.yml` and `deploy.yml`
(scanner-behavior drift must not land silently; bump both files together,
deliberately) -- but the pin does **not** prevent the alias-flip class: the vuln DB
is downloaded fresh at scan time regardless of the binary version.
