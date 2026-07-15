# CI/CD Pipeline and Pre-Push Checklist

The single source of truth is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
This doc explains what it gates on and gives the exact local command to satisfy
each job, so you catch failures before you push instead of after.

CI runs on **every push to `main`** and **every pull request**. There are five
independent jobs; a red mark on any one blocks the merge. Deployment is separate
(Fly release_command self-migrates; Vercel builds the frontend) -- see
[`DEPLOY.md`](DEPLOY.md).

---

## TL;DR -- run this before every push

From the repo root. This mirrors the gating jobs in order; if it is all green,
CI almost certainly passes too.

```bash
# 1. Backend: format, lint, types, layering, tests + coverage floor
uv sync --extra dev --extra llm --extra local-embeddings --extra orchestration
uv run ruff check src tests migrations
uv run black --check src tests migrations          # NOTE: ruff does NOT format; black is a separate gate
uv run mypy src tests
uv run lint-imports
uv run pytest -q --cov=src/regwatch --cov-fail-under=80

# 2. Python supply-chain audit (asserts uv.lock matches pyproject.toml via --frozen)
uv export --frozen --no-emit-project --no-dev --extra llm --extra orchestration \
  --format requirements-txt --output-file requirements-audit.txt
uvx pip-audit -r requirements-audit.txt \
  --ignore-vuln GHSA-f4j7-r4q5-qw2c --ignore-vuln CVE-2026-45829

# 3. Frontend: lint, types, build, prod-dep audit
cd regwatch/frontend
npm ci
npm audit --omit=dev --audit-level=high
npm run lint
npx tsc --noEmit
npm run build

# 4. Frontend wire-type contract: regenerate and confirm the committed copy is current
npm run gen:types                                   # needs the API importable (uv sync above)
git diff --exit-code -- lib/api-types.ts            # nonzero => commit the regenerated api-types.ts
cd ../..

# 5. Docker images + Trivy scan (slowest; see "Reproduce the Trivy scan" below)
docker build -t regwatch:ci .
docker compose config --quiet
docker build -t regwatch-web:ci regwatch/frontend
```

If you only touched backend Python, jobs 1-2 are the ones that matter. If you only
touched the frontend, jobs 3-4. If you touched the API schema, you must also do 4.
If you changed dependencies or a Dockerfile, do 5.

---

## The five jobs, one by one

### 1. `lint-type-test` (matrix: Python 3.11 and 3.12)

Runs against a real **Postgres + pgvector** service (`pgvector/pgvector:pg17`) with
`TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres`, so the
production datastore path is exercised, not just SQLite.

| Step | Local command | Notes |
|---|---|---|
| ruff | `uv run ruff check src tests migrations` | Lint only. Does **not** reformat. |
| black | `uv run black --check src tests migrations` | Formatting gate. Run `uv run black src tests migrations` to fix. |
| mypy | `uv run mypy src tests` | Strict on `src/`. |
| lint-imports | `uv run lint-imports` | import-linter layering contracts (domain logic must not import I/O layers). |
| pytest | `uv run pytest -q --cov=src/regwatch --cov-fail-under=80` | Coverage floor is 80% (measured ~82%). Includes the deterministic eval gate and the INV-1..9 invariant tests. |

- The `seed` and `eval --check-thresholds` steps run **only if `OPENAI_API_KEY` is
  set** as a repo secret. Locally they are skipped; `pytest` runs the offline
  deterministic eval gate, so you can validate without a key.
- To exercise the pgvector tests locally, point `TEST_DATABASE_URL` at a local
  Postgres+pgvector. Without it, `test_pgvector_store.py` /
  `test_postgres_bootstrap.py` **skip** -- they pass locally but the prod path
  goes unverified, so do not treat a green local pytest as covering pgvector.

### 2. `python-audit`

Audits the **shipped production closure** (the slim image: `llm` + `orchestration`
extras; `local-embeddings`/torch is intentionally out of scope).

```bash
uv export --frozen --no-emit-project --no-dev --extra llm --extra orchestration \
  --format requirements-txt --output-file requirements-audit.txt
uvx pip-audit -r requirements-audit.txt \
  --ignore-vuln GHSA-f4j7-r4q5-qw2c --ignore-vuln CVE-2026-45829
```

- `--frozen` fails if `uv.lock` is out of sync with `pyproject.toml`. After any
  dependency change run `uv lock` and commit `uv.lock`.
- The two `--ignore-vuln` ids suppress the chromadb ChromaToast advisory (pre-auth
  RCE in the standalone Chroma HTTP server -- we use the embedded client, so it is
  unreachable). Remove them when an upstream fix ships. To add a new Python ignore,
  edit this step in `ci.yml` (pip-audit takes ignores on the command line, not from
  `.trivyignore`).

### 3. `docker-build` (image build + Trivy scan)

```bash
docker build -t regwatch:ci .
docker compose config --quiet
docker build -t regwatch-web:ci regwatch/frontend
```

Then both images are scanned by Trivy, gating on **FIXABLE CRITICAL/HIGH** vulns
(`--ignore-unfixed`) plus any embedded **secrets**. Suppressions live in
[`.trivyignore`](../.trivyignore). See the dedicated section below -- this is the
job that fails most often.

### 4. `frontend`

Working directory `regwatch/frontend`.

| Step | Local command |
|---|---|
| install | `npm ci` |
| prod-dep audit | `npm audit --omit=dev --audit-level=high` |
| lint | `npm run lint` |
| typecheck | `npx tsc --noEmit` |
| build | `npm run build` |

`npm audit` gates on **production** deps only (`--omit=dev`); dev-only build-tool
advisories do not block (they do not reach the served app).

### 5. `frontend-contract`

The `/query`-family wire types in `lib/api-types.ts` are generated from the FastAPI
OpenAPI schema. CI regenerates them and fails if the committed copy is stale, so the
frontend contract can never silently drift from the backend.

```bash
cd regwatch/frontend
npm run gen:types                          # runs gen:openapi (needs API importable) then openapi-typescript
git diff --exit-code -- lib/api-types.ts   # must be clean
```

**If you change any API request/response model, run `gen:types` and commit the
regenerated `lib/api-types.ts` in the same change.**

---

## Gotchas that have actually broken this gate

- **black is its own gate; ruff is not enough.** `ruff check` lints but does not
  format. Always run `uv run black src tests migrations` before pushing Python or
  CI's `black --check` fails.
- **Stale `api-types.ts`.** Editing a FastAPI schema without regenerating wire types
  fails `frontend-contract`. Run `gen:types` and commit the result.
- **`uv.lock` drift.** Changing `pyproject.toml` without `uv lock` fails
  `python-audit`'s `--frozen` export.
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
   digest-pinned `golang:` stage. So a Go stdlib CVE can fire on the **API** image
   as well -- and the remedy there is the opposite of the esbuild case: bump the
   pinned `golang:` digest in the Dockerfile and rebuild. Never add an API-image Go
   CVE to `.trivyignore`; we own that toolchain. Check which image the finding came
   from before reaching for the ignorefile.
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
  aquasec/trivy:latest image \
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
Pinning the `aquasec/trivy` image tag does **not** prevent the alias-flip class --
the vuln DB is downloaded fresh regardless of the binary version.
