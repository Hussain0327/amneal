# DEPLOY - RegWatch PostgreSQL + Fly.io + Vercel runbook

Verified 2026-08-26 against `origin/main` at `0a13c4a`. Every `fly.toml`
excerpt, secret name, and rollback command in this file was checked against
the code, `fly.toml`, or a GitHub Actions workflow in that commit. For live
runtime values this doc deliberately does not print (deployed release,
active embedding profile, effective refusal threshold), see
`docs/PRODUCTION_TRUTH.md` or run the command this doc names next to the
claim.

This runbook describes the checked-in target. It does not prove that a live
production database has been migrated or that the application has been
deployed.

```text
browser -> Vercel -> Fly Go proxy -> Fly FastAPI
                                  |-> OpenAI Responses API
                                  |-> OpenAI Embeddings API
                                  |-> RegWatch PostgreSQL + pgvector
```

Generation is `gpt-5.6-terra` over the Responses API with medium reasoning and
`store=false`. Embeddings are `text-embedding-3-large` at 1024 dimensions.
Retrieval is exact pgvector search. RegWatch owns and supplies all application
state through `DATABASE_URL`; OpenAI does not store the application
conversation or database.

## 1. Database

Provision a PostgreSQL database with pgvector for RegWatch, then set
`DATABASE_URL` for both the Go and Python services. The replacement service
must satisfy these gates before production traffic moves:

1. TLS is required for every non-local connection.
2. The application role can run the schema migrations and read/write every
   RegWatch table used by the release.
3. The `vector` extension is installed and visible to the application role.
4. The OpenAI embedding profile is registered as
   `text-embedding-3-large`, 1024 dimensions, unit normalized.
5. Every serving chunk has an embedding for that profile before activation.
6. Exact retrieval and citation evaluation pass against the replacement
   database.
7. Backup, restore, and rollback procedures are rehearsed.

Do not reuse or copy an existing production `DATABASE_URL` into logs, prompts,
or committed files. Database provisioning, data copy, secret rotation, and the
final production switch require explicit operator approval.

## 2. Schema and migrations

The Fly deploy is the single migration authority. `fly.toml` sets
`[deploy] release_command = "regwatch release"`, which runs in a one-off machine
BEFORE the rolling replace. It applies `alembic upgrade head` and then executes
the full serving-readiness guard, so both migration failures and live
embedding-profile drift abort before any long-lived machine is replaced. There
is no manual pre-migration step on the normal path.

**Before the deploy that first carries `0027_chunk_filter_indexes`, check branch
headroom.** That migration builds three btree indexes on `chunk`, and the
Lakebase branch is capped at 512 MiB (`branch_logical_size_limit_bytes`,
tier-fixed); measured headroom has been as low as ~21 MiB, and hitting the cap
mid-build is what the 2026-08-18 incident looked like. The build is
transactional, so a failure rolls back and releases its space and the deploy
aborts before any long-lived machine is replaced -- but the deploy still fails,
so check first:

```bash
psql "$PROD_DB_URL" -c \
  "SELECT pg_size_pretty(pg_database_size(current_database())) AS used,
          pg_size_pretty(512 * 1024 * 1024 - pg_database_size(current_database())) AS headroom"
```

Estimate, not a measurement: at ~88k `chunk` rows with short text keys the three
indexes should total single-digit MB, so treat less than ~32 MiB of headroom
(a few times the finished size, to cover build workspace) as "reclaim first".
`scripts/reclaim_lakebase_space.py` is the reclaim path.

The deployed corpus schema includes `0023_authoritative_fda_corpus` and
`0024_fda_streaming_lifecycle`. This follow-up adds
`0025_fda_terminal_resolution`, so its release advances only the lifecycle and
run ledgers before terminal-tail repair and acceptance. No corpus migration
makes network calls or performs a corpus backfill.

App boot repeats the guard after the release preflight. The entrypoint runs
`regwatch init-db`, which compares the Alembic stamp to the head this image
expects and verifies the active serving profile. That repetition covers drift
in the interval after the one-off release check. To heal a schema mismatch,
from a checkout of the DEPLOYED commit on `main`:

```bash
DATABASE_URL="$PROD_DB_URL" uv run alembic upgrade head
```

**Never run that from an unmerged branch against prod.** That is the 2026-07-07
outage rule: a branch's head revision is not in the deployed image, so the boot
guard then kills every machine.

The daily Watch cron does NOT migrate, on purpose. If a migration is merged but
not deployed, the cron fails loudly instead of pushing the live schema ahead of
the running API machines.

### 2.1 Authoritative FDA corpus rollout

The frozen production manifest contains 140,438 source records under logical
SHA-256
`fae78c8eb6c5b601a5a52539ec7b62444d1eb7c745879d04ce1d031fa75c0c84`.
This is a manifest denominator, not a chunk or embedding count. Do not switch
serving retrieval during schema deployment.

**Scoped-activation amendment, 2026-08-18.** The complete-universe target
below (all 140,438 records backfilled and indexed before cutover) became
permanently unreachable under the Lakebase branch's 512 MiB cap
(`config/settings.py:508-517`). Activation no longer requires a
complete-universe run. Set `REGWATCH_SERVING_MANIFEST_SHA` to the logical
SHA-256 of an operator-named, explicitly scoped manifest, and API boot
checks full embedding coverage against that named manifest instead of the
140,438-record universe. Leaving it unset keeps the original
complete-universe-only behavior, which is not achievable at the current
branch size. Do not treat step 4 below as the only path to
`REGWATCH_RETRIEVAL_CORPUS=authoritative_fda`; a scoped manifest is the real
one.

The corrected production canary passed 21 / 21 and produced 499 chunks with
complete active-profile embeddings. The full backfill is now owned by a
supervised operator session. Do not run production Dagster jobs, change its
worker environment, or freeze a second manifest from a parallel session.
Retrieval remains on `legacy` throughout the build.

After this release is healthy, deploy `Dockerfile.corpus-worker` on separately
supervised compute. It uses bounded ephemeral scratch, durable S3-compatible raw
artifacts, Postgres-backed Dagster state, Tesseract OCR, and the same application
database and active embedding profile. Keep the Dagster webserver private.
Preflight with:

```bash
uv run regwatch authoritative-corpus-plan
uv run regwatch authoritative-corpus-status
```

The rollout ledger is now at these stages:

1. `authoritative_fda_canary_job`: complete at 21 / 21 with zero errors and 499
   chunks. Terminal outcomes never satisfy this strict canary gate.
2. `authoritative_fda_manifest_job`: complete. Do not rerun it while the
   backfill is active; the driver reads the newest complete-universe row, so a
   second freeze can swap the manifest under the owned run.
3. `authoritative_fda_shard_job`: backfill partitions 000-511 with that manifest
   SHA-256 and the active profile. This one job chunks shard N and then embeds
   shard N. Do NOT substitute the two single-asset jobs and run all chunking
   before all embedding: it leaves the building corpus chunked-but-unembedded
   for the whole embedding phase, turning any mid-run repair into a corpus-wide
   reconciliation. The single-asset jobs exist for per-shard repair only.
   Production deploys stay safe throughout: the boot guard counts only the
   ACTIVE serving namespace (see AUTHORITATIVE_FDA_CORPUS.md), so backfill rows
   cannot fail an app boot until `REGWATCH_RETRIEVAL_CORPUS` flips.
4. `authoritative_fda_acceptance_job`: after migration 0025 and backfill
   completion, require all 512 blocking checks and write indexed and audited
   terminal counts to the complete-universe success ledger.

The full schedule is intentionally absent, and weekly manifest discovery is
stopped by default. See [`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md)
for exact run configuration, retry semantics, storage/OCR limits, and evidence
to retain.

The complete-universe final status (`activation_ready: true`, zero policy
violations, all five families, exact `indexed + evidence-backed terminal ==
140438` parity) is the criteria as written in code for the unscoped path. It
is not achievable in the current Lakebase branch; see the 2026-08-18
amendment above. The scoped path's bar is full embedding coverage
(`embedded_chunks == chunks`) against the named `REGWATCH_SERVING_MANIFEST_SHA`
manifest. Either way, run the new-namespace retrieval/citation eval and a
serving canary before changing:

```bash
REGWATCH_RETRIEVAL_CORPUS=authoritative_fda
```

API boot independently checks readiness and refuses an unsafe cutover. Rollback
is configuration-only and does not delete corpus data:

```bash
REGWATCH_RETRIEVAL_CORPUS=legacy
```

The full acceptance criteria, snapshot checksums, failure semantics, and Google
Well-Architected/SRE control mapping are in
[`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md).

## 3. API on Fly.io

The slim image (no torch) is the production build.

### 3.1 App and config

The Fly app is `amneal`. `fly.toml` is COMMITTED at the repo root and is
authoritative, load-bearing config: two process groups, the migration
`release_command`, the step-5 flag pin. Do not regenerate it with `fly launch`. A
fresh checkout deploys with the committed file as-is.

Abridged excerpt. The real `fly.toml` is heavily commented and is the source of
truth:

```toml
app = "amneal"
primary_region = "iad"
kill_timeout = 30                        # drain in-flight SSE on deploys

[deploy]
  release_command = "regwatch release"  # migrates + asserts readiness BEFORE roll

[processes]
  app = "regwatch serve"      # dual-stack uvicorn on :8000
  proxy = "regwatch-proxy"    # Go proxy, holds the public port

[env]
  INGEST_EMBEDDING_PROVIDER = "openai"   # required-explicit; unset refuses to boot
  LLM_PROVIDER = "openai"                # required-explicit; unset refuses to boot
  PROFILE_HNSW_INDEX_REQUIRED = "false"  # exact pgvector scan, no HNSW index
  REGWATCH_ROUTE_CALL = "off"            # route/scope shadow observer, off the Ask path
  AUTH_COOKIE_SECURE = "true"
  CORS_ALLOW_ORIGINS_CSV = "https://amneal.vercel.app"
  SENTRY_ENVIRONMENT = "production"
  TRUST_PROXY_HEADERS = "true"     # Go login limiter keys on Fly-Client-IP
  REQUIRE_DATABASE_URL = "true"    # read by the GO PROXY only
  GO_NATIVE_QUERY = "true"         # step-5 pin: proxy serves POST /query natively
  REGWATCH_PROSE_SYNTHESIS = "true"      # v6 prose format; code default is false
  REGWATCH_SELECTIVE_CITATION = "true"   # v7 answer policy; code default is false
  UPSTREAM_URL = "http://app.process.amneal.internal:8000"

[http_service]
  processes = ["proxy"]
  internal_port = 8080
  force_https = true
  auto_stop_machines = false
  min_machines_running = 2
  [[http_service.checks]]     # GET /livez through the proxy, DB-free liveness

[checks.app_health]           # deploy-gates the now-private app group on :8000, GET /health
```

`INGEST_EMBEDDING_PROVIDER` and `LLM_PROVIDER` are the actual key names.
There is no `EMBEDDING_PROVIDER` key in `fly.toml` (that is a deprecated env
alias the code still accepts, not what the committed file sets), and there is
no Databricks value anywhere in it.

Three tests guard this file against well-meaning simplifications:
`tests/test_trust_proxy_fly_toml.py`, `tests/test_boot_command_drift.py`, and
`tests/test_dual_stack_bind.py`. Read the comments in `fly.toml` before touching
any guarded line.

### 3.2 Secrets

Secrets never go in `fly.toml`. Put comments ABOVE the command, never on a
continuation line: a `\` followed by spaces is an escaped space, not a line
continuation, so a trailing `#` silently truncates the command and every variable
after it is never set.

This is the deploy-critical subset: the secrets that boot or a normal deploy
depends on. `docs/CONFIG_REFERENCE.md` owns the full environment variable and
secret inventory; check there before assuming a name below is complete.

```bash
# DATABASE_URL                Lakebase DIRECT endpoint (see step 1.1). Mandatory:
#                             the app refuses to boot without it.
# OPENAI_API_KEY              gates both generation and embeddings. Missing it
#                             raises at LLM/embedding provider construction
#                             (src/regwatch/generate/llm.py:698-714,
#                             src/regwatch/process/embedder.py).
# RETRIEVAL_EMBEDDING_PROFILE picks the live vector space (deprecated alias:
#                             ACTIVE_EMBEDDING_PROFILE; the new name wins if
#                             both are set). INGEST_EMBEDDING_PROVIDER itself
#                             comes from fly.toml [env] ("openai") and is
#                             required-explicit: an unset value refuses to boot.
# INTERNAL_RAG_TOKEN          Go proxy -> Python /internal/query/compute auth
# METRICS_TOKEN               bearer gate on GET /metrics; unset = world-readable
# SENTRY_DSN                  error tracking; unset boots but logs a loud warning
fly secrets set \
  DATABASE_URL="postgresql://regwatch_app:...@<lakebase-host>:5432/<db>?sslmode=require" \
  OPENAI_API_KEY="sk-..." \
  RETRIEVAL_EMBEDDING_PROFILE="ep_<profile-id>" \
  INTERNAL_RAG_TOKEN="..." \
  METRICS_TOKEN="..." \
  SENTRY_DSN="https://...ingest.sentry.io/..." \
  -a amneal
```

`config/settings.py` also has `OPENAI_LLM_MODEL` (default `gpt-5.6-terra`),
`OPENAI_REASONING_EFFORT` (default `medium`), `OPENAI_EMBEDDING_MODEL`
(default `text-embedding-3-large`), and `OPENAI_EMBEDDING_DIMENSION` (default
`1024`). Set these only to override the code default, for example to pin the
LLM to a dated snapshot instead of a moving alias (see lever 0 in 6.1).

Neither `D1_ENFORCED` nor `D1_ALLOWED_LLM_MODELS` is a field in
`config/settings.py`. They are not "unset"; they do not exist as a config
surface at all. The only related code is `D1ResidencyError` in
`src/regwatch/generate/llm.py:429-436`, defined but never raised by any
provider. Do not set these names; they do nothing.

Of the three answer-policy flags, two are pinned `"true"` in `fly.toml`
`[env]` (`REGWATCH_PROSE_SYNTHESIS`, `REGWATCH_SELECTIVE_CITATION`, see 3.1)
and need no secret in normal operation. `REGWATCH_LIVE_DRAFT` is not pinned
anywhere; it is a Fly secret, set separately:

```bash
fly secrets set REGWATCH_LIVE_DRAFT="1" -a amneal
```

- `REGWATCH_PROSE_SYNTHESIS` is the v6 prose format.
- `REGWATCH_SELECTIVE_CITATION` is the v7 answer policy. It is only honored when
  the prose flag is also on.
- `REGWATCH_LIVE_DRAFT` streams the provisional draft over SSE.

**Roll any of the three back with `fly secrets set <name>=false` or
`fly secrets unset <name>`, not by setting a value to `""`.** The prose and
selective flags read a blank value as OFF, but `REGWATCH_LIVE_DRAFT` does not:
an empty string fails bool parsing and takes the process down at boot. Because
the first two are also pinned in `fly.toml` `[env]`, unsetting the secret
alone is not enough to turn them off for good; the `[env]` pin still applies
after the secret is gone. A Fly secret overrides `[env]` while it exists
(fastest incident lever), but a durable rollback also needs the `fly.toml`
line removed and redeployed.

Notes on what is deliberately NOT set, and one name that is not a real knob:

- `REGWATCH_ROUTE_CALL` is pinned `"off"` in `fly.toml` `[env]`, not merely
  unset (see 3.1). The route/scope shadow observer is off (see 6.3).
- `WHITEPAPER_TEMPLATE_URL` is unset, so `.docx` renders fall back to marker
  output. `/health` reports `whitepaper_template: absent`. That is current
  behavior, not a hypothetical. See 3.6.
- `D1_ENFORCED` and `D1_ALLOWED_LLM_MODELS` are not Settings fields and do
  nothing if set. See the note after the deploy-critical secrets block above.

`DATABASE_URL` is mandatory. Without it the app refuses to boot rather than
losing the audit trail. `REQUIRE_DATABASE_URL` in `fly.toml` `[env]` is a
separate thing, read by the Go proxy only: with it set, a proxy machine refuses
to serve auth when `DATABASE_URL` is missing. `SENTRY_DSN` is strongly
recommended: without it the app boots but logs a loud
`sentry_disabled_in_production` warning and 500s go only to stderr.

### 3.3 Deploy

The normal path is automatic. Every green `ci` run on `main` triggers
`deploy.yml`, which builds the image once, re-scans it with Trivy, pushes it to
`registry.fly.io/amneal:sha-<commit>`, and ships that exact image via
`scripts/fly-deploy.sh --image`. Fly runs the migration release command first
(see section 2). Read the current release with `fly releases -a amneal`; this
doc does not name a version, it drifts on every deploy.

Manual deploys (`fly deploy`, or `bash scripts/fly-deploy.sh` from the exact
commit) are for recovery only.

If a machine refuses to start with `stamped at alembic revision '00XX_...' but
this build expects '00YY_...'`, that is the boot guard. Run the
`alembic upgrade head` one-liner from section 2, from the deployed commit, then
deploy again.

### 3.4 Verify

```bash
curl -s https://amneal.fly.dev/health | python -m json.tool
```

A shape the current code can actually produce (`src/regwatch/api/main.py:337-460`;
`corpus_count` and the profile id are illustrative, not live values, do not
copy them):

```json
{
  "status": "ok",
  "components": {
    "db": {"ok": true, "dialect": "postgresql"},
    "vector_store": {"ok": true, "corpus_count": 87800},
    "llm": {"provider": "openai", "key_present": true},
    "embedding": {"provider": "openai", "profile": "ep_<profile-id>"}
  },
  "whitepaper_template": "absent",
  "warnings": []
}
```

What to check, and what a wrong value means:

- `db.dialect` must be `postgresql`. Anything else means the stack is on the
  wrong datastore.
- `embedding.provider` should read `openai` and `embedding.profile` should be
  the `ep_...` id named by the `RETRIEVAL_EMBEDDING_PROFILE` secret. A profile
  of `legacy` means the query path fell back to the frozen pre-cutover vector
  space, not the live one; `embedding.provider` is looked up from that
  profile's own row (`get_embedding_profile`), not read from
  `EMBEDDING_PROVIDER`, precisely so this check catches drift between the
  two.
- `llm.provider` should read `openai`. `key_present` is `true` only when both
  `OPENAI_API_KEY` and `OPENAI_LLM_MODEL` resolve to a non-empty string
  (`src/regwatch/api/main.py:396-405`); `openai`/`key_present: false` means
  the key or model name is missing, not that the provider is wrong.
- `corpus_count` must be non-zero. This doc does not name the current count;
  read it live from this endpoint.

Also hit `GET /ready`. It fails closed if the boot RLS sweep left any public
table unprotected.

### 3.5 Provision users

CLI only, no self-signup, password is prompted. Target an `app`-group machine:
the `proxy` machines run only the Go binary and have no Python CLI, and a bare
`fly ssh console` may land on one.

```bash
fly ssh console -s -C "regwatch create-user analyst@amneal.com --name 'CRA Analyst'"
# at the -s picker, choose a machine from the "app" process group
```

### 3.6 Notes

- **White-paper template.** The real CRA `.docx` template is gitignored and not
  baked into the image, so prod renders a fallback document stamped
  `(generated without the official CRA template file)` plus a
  `whitepaper_template_missing` warning. Never a silent or failed render. To turn
  on real-template fill on Fly, set `WHITEPAPER_TEMPLATE_URL` to a long-lived
  signed HTTPS URL for the file. The render path fetches and caches it on first
  use (`src/regwatch/whitepaper/template_fetch.py`), and the fetch is
  URL-generic, so a Databricks Volume URL works with no code change. Any fetch
  failure falls back loudly. Under compose, drop the file at
  `./data/templates/cra_white_paper_template.docx` instead, which is where
  `WHITEPAPER_TEMPLATE_PATH` already points.
- **`data/` inside the container is scratch.** Raw PDFs from ingest runs land
  there. Q&A and white-paper serving need only Postgres, so do not attach a
  volume unless you run ingest or watch on that machine.
- **Watch.** The production Watch path is the `watch-daily.yml` GitHub Actions
  cron at 07:17 UTC, the only scheduler. It hardcodes
  `INGEST_EMBEDDING_PROVIDER=openai` and `LLM_PROVIDER=openai` in its own env
  block and needs exactly three repository secrets: `WATCH_DATABASE_URL`,
  `OPENAI_API_KEY`, and `WATCH_ACTIVE_EMBEDDING_PROFILE` (validated against
  `^ep_[0-9a-f]{32}$` before it crawls anything; `.github/workflows/watch-daily.yml:70-128`).
  No Qwen or Databricks secret is read anywhere in this workflow. Current
  secret status is not something this checkout can prove; check with
  `gh secret list` or see [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md).

  Keep ad hoc `regwatch watch` runs for break-glass recovery, not as the normal
  schedule.

## 4. Frontend on Vercel

The Next.js app proxies `/api/*` server-side to the API
(`regwatch/frontend/next.config.mjs`), so the browser only ever talks to the
Vercel origin. The HttpOnly session cookie is set on and sent to the Vercel
domain and forwarded through the rewrite. That is why `AUTH_COOKIE_SECURE=true`
just works: Vercel terminates TLS.

1. <https://vercel.com/new>, then **Import** the GitHub repo.
2. **Root Directory**: click *Edit* and set `regwatch/frontend`. The build fails
   without this. Framework preset Next.js is auto-detected.
3. **Environment Variables** (Production): `API_PROXY_TARGET` =
   `https://amneal.fly.dev`. Server-side only, no `NEXT_PUBLIC_` prefix.
4. **Deploy**. Production URL is `https://amneal.vercel.app`.
5. If the final Vercel URL differs from `CORS_ALLOW_ORIGINS_CSV`, fix it. A Fly
   secret of the same name overrides `[env]` and applies in about 60 seconds:
   `fly secrets set CORS_ALLOW_ORIGINS_CSV=... -a amneal`. Make it durable
   afterwards by editing `fly.toml` and redeploying, then `fly secrets unset` the
   override so the committed file stays the source of truth.

CLI alternative:

```bash
cd regwatch/frontend
vercel link
vercel env add API_PROXY_TARGET production   # paste the API URL
vercel deploy --prod
```

## 5. Smoke checklist (run every deploy)

Do these in order. Stop at the first failure.

1. `curl https://<api-host>/health`: 200, `status: ok`, `db.ok: true`, embedding
   provider `openai` on the expected profile, `llm.provider: openai`,
   `corpus_count` > 0. See 3.4. Then `GET /ready`: `status: ready`.
2. Chunk count matches. `psql "$PROD_DB_URL" -c "select count(*) from chunk"`
   should agree with `corpus_count`, and
   `select version_num from alembic_version` should show the current head.
3. Open the Vercel URL. The login page renders with no console errors.
4. Wrong-password login gives "invalid email or password" and sets no cookie.
5. A provisioned analyst logs in and lands in the shell with Ask in view. Reload
   keeps the session.
6. Set product scope from the "Under review" bar: resolve an RLD name plus
   application number (`POST /resolve`). The scope pins to the canonical
   `{normalized_name, six-digit appl}` and the URL gains `?rp=&appl=`, which is
   shareable and survives reload. A mismatched application 422s and leaves the
   scope unset. `/resolve` writes no audit row.
7. Ask a question, for example "What bioequivalence studies does FDA recommend
   for albuterol sulfate metered aerosol?". You get a right-aligned user bubble,
   then an answer whose factual sentences carry `[1]`-style markers backed by a
   Sources list, and the chips open the FDA source PDF. Ask something the corpus
   does not cover: you should get a plain-language reply that says so and offers
   a next step, with no citation markers and no invented facts.
8. Populate a white paper: White Paper tab, RLD name plus NDA number. Cells fill
   with provenance (source, locator, fetched-at) and manual cells say "Analyst
   input required". A successful populate also sets product scope.
9. Download the docx. It opens in Word with the populated cells and the
   Provenance appendix.

If 5 through 9 pass, the deploy is good.

## 6. Operations

Day-2 runbook: rollback, uptime, the route/scope shadow rollout, the refusal
threshold, and the restore drill. Everything here is operator-driven. Agents and
CI never touch production data paths.

### 6.1 Rollback

Independent levers, least to most drastic. Pick the smallest one that covers the
failure.

**Lever 0, config flip.** Fastest: about 60 seconds, no redeploy, no CI cycle.
Fly secrets take precedence over `fly.toml` `[env]`, so a bad flag flip
reverts with a secret.

**There is no Databricks rollback.** `get_llm_provider`
(`src/regwatch/generate/llm.py:681-734`) accepts only `openai` or `echo`;
`if name == "databricks": raise ValueError("unknown LLM provider:
databricks")`, at line 732-733, fires on the first call after the flip. Do
not set `LLM_PROVIDER=databricks` during an incident. It does not repoint
generation, it takes every Q&A request down with an unhandled 500 on top of
whatever the original incident was. `get_embedding_provider`
(`src/regwatch/process/embedder.py:689-702`) is the same shape: only `echo`
and the OpenAI provider names are recognized, and there is no Qwen or
Databricks embedding class in the codebase to fall back to. If the incident
is genuinely "OpenAI itself is down or unreachable," there is no working
provider fallback today; that gap is open work, not a documented lever.

The levers that do exist, inside the OpenAI stack:

```bash
# Pin the LLM to a known-good dated snapshot instead of the moving
# "gpt-5.6-terra" alias, if the alias resolving to a new snapshot is itself
# the problem. OPENAI_LLM_MODEL is a Fly secret; fly.toml does not set it,
# so this always overrides the code default with no [env] pin to also strip.
fly secrets set OPENAI_LLM_MODEL=<dated-snapshot-id> -a amneal

# Turn off the step-5 native query path. The proxy relays POST /query to
# Python instead of orchestrating it in Go. GO_NATIVE_QUERY is pinned "true"
# in fly.toml [env] (see 3.1); a secret of the same name overrides it while
# the secret exists.
fly secrets set GO_NATIVE_QUERY=false -a amneal

# Turn off the v7 answer policy; the v6 prose format stays. Also pinned in
# fly.toml [env]; same override behavior.
fly secrets set REGWATCH_SELECTIVE_CITATION=false -a amneal

# Stop streaming the provisional draft over SSE. Not pinned in fly.toml, so
# unsetting the secret is enough.
fly secrets unset REGWATCH_LIVE_DRAFT -a amneal
```

Unset, do not set to empty. See the warning in step 3.2. Because
`GO_NATIVE_QUERY` and `REGWATCH_SELECTIVE_CITATION` are also pinned in
`fly.toml` `[env]`, the secret above is a fast override, not a durable
rollback: revert or edit `fly.toml` and redeploy to make the change stick past
a `fly secrets unset` or the next deploy that carries the current committed
file.

**Lever 1, app rollback (bad deploy, schema unchanged).** List releases, note the
image ref of the last good one, pin it:

```bash
fly releases --image                       # last good release's image ref
fly deploy --image <previous-image-ref>    # e.g. registry.fly.io/amneal:deployment-...
```

> **Image and config are version-coupled.** `fly deploy --image <old>` sends your
> CURRENT `fly.toml` with that old image, and two boundaries make that
> combination refuse to boot:
>
> - The phase-2 dual-stack listener (`docs/GO_PROXY_ROLLOUT.md`). `fly.toml` says
>   `[processes].app = "regwatch serve"`, and a pre-phase-2 image has no `serve`
>   subcommand, so every machine exits non-zero.
> - The step-5 pin. `fly.toml` `[env]` sets `GO_NATIVE_QUERY = "true"`, which
>   hands the pin to a proxy binary from before the CompleteQuery cutover.
>
> To roll back across either boundary, deploy the reverted CHECKOUT instead:
> `git revert` and push to main, or `bash scripts/fly-deploy.sh` from the
> reverted commit in an emergency, so image and config come from one commit. For
> step 5 you can also neutralize the pin first with lever 0. The same constraint
> makes `--strategy immediate` safe only when image and `fly.toml` are from one
> commit. Within a single phase this lever is unaffected.

The schema is alembic-stamped and checked on boot, so an older image that expects
an older head refuses to start rather than running against a newer schema. If the
bad deploy also migrated, an image rollback alone is not enough: restore data or
roll forward with a fix.

**Lever 2, data rollback (bad write or bad migration).** Restore the database
from a Lakebase backup, then restart the API (`fly apps restart amneal`). A
restore overwrites everything, so stop the API first and accept that sessions,
audit rows and every other write since that backup are gone. Verify with the
section 5 checklist before calling it done.

> **This procedure is not written down and has never been rehearsed on
> Lakebase.** The old text here described the Supabase dashboard backup flow,
> which no longer applies. Writing and rehearsing the Lakebase restore is an open
> item (6.5).

There is no local-datastore fallback. R5 deleted the SQLite/Chroma dual-mode, so
if Postgres is unusable, lever 2 is the only option short of standing up a new
Postgres instance.

### 6.2 Uptime

Point an external monitor at the one open endpoint:

- **URL:** `GET https://<api-host>/health`, no auth.
- **Expected:** HTTP 200 with `"status":"ok"` (see the body in step 3.4). When
  the DB or vector store is unreachable the API returns **503** with
  `"status":"unhealthy"`, so a plain HTTP-status monitor already catches real
  outages.
- **UptimeRobot** (free tier): HTTP(s) monitor on the URL, 5-minute interval, and
  optionally a keyword monitor that alerts when `"status":"ok"` is absent from
  the body. **healthchecks.io** alternative: a cron on any box you control,
  `curl -fsS --max-time 20 https://<api-host>/health >/dev/null && curl -fsS https://hc-ping.com/<your-uuid>`.
- **Alert threshold:** 2 consecutive failures, about 10 minutes at a 5-minute
  interval. Single blips happen during deploys.

**CI backstop, `.github/workflows/uptime-eval.yml`.** A scheduled GitHub Action
curls the production health URL every 30 minutes and fails the run when the
response is not 200 / `"status":"ok"`. It is driven entirely by the
`PROD_HEALTH_URL` repository secret. While that secret is unset the workflow
skips cleanly. It is currently unset. This complements the external monitor, it
does not replace it: GitHub cron schedules can lag or pause on inactive repos.

### 6.3 Route/scope shadow rollout (PR11b/PR11c)

This measures the conversational router before it is allowed to change anything.
The code default is `REGWATCH_ROUTE_CALL=off`, and `fly.toml` `[env]` pins it
to `"off"` explicitly (see 3.1), not merely leaving it unset.

With `shadow`, the extra route decision and deterministic scope compile are
recorded under `query_log.route_json.route_call`. The existing product resolver,
retrieval query, mode, filters, response, citations and session update all stay
authoritative. The reserved value `live` is intentionally shadow-equivalent right
now and must not be treated as a promotion.

The first controlled PR11b production window (2026-08-10) recorded 24 tagged
turns: 21 route successes and 3 invalid responses (12.5%), with zero provider
errors and zero unsafe corpus authorizations. All 15 explicit-corpus controls
compiled to bounded `EXACT_CORPUS`, and all 3 named beclomethasone controls
compiled to `EXACT_SCOPED`; the malformed cases and inconsistent greeting /
inheritance classifications still blocked promotion. PR11c changes only the
route prompt and its synthetic controls. Its v2 prompt remains observation-only
and must pass the same production procedure below before PR12 is considered.
The pre-merge route-only v2 battery passed 32/32 committed controls with zero
invalid responses, provider errors, or unsafe corpus proposals; production
shadow is still required because that battery did not exercise Ask integration,
audit persistence, or real traffic mix.

Before enabling it, probe the reasoning floor of the endpoint you actually run.
The committed default of 1200 max tokens was sized from an older observation of
about 761 reasoning tokens on a different served model. The endpoint serving
today is the authority. Set the cap comfortably above its measured floor plus the
small JSON body:

```bash
fly secrets set REGWATCH_ROUTE_CALL=shadow REGWATCH_ROUTE_MAX_TOKENS=1200 -a amneal
```

Start with a small traffic window and watch `/metrics`:

- `regwatch_route_shadow_calls_total{outcome=...}` separates `success`,
  `provider_error`, `invalid` and `request_error`
- `regwatch_route_shadow_failures_total` is the sum of unsuccessful calls
- `regwatch_route_shadow_compilations_total{status=...}` separates successful,
  failed and unattempted scope compiles

Alert only once there is enough traffic for the ratio to mean something: at least
20 calls in 15 minutes, and

```text
increase(regwatch_route_shadow_failures_total[15m]) /
clamp_min(sum(increase(regwatch_route_shadow_calls_total[15m])), 1) > 0.02
```

Also watch Ask latency p95 and OpenAI request rate. Shadow adds one sequential
model call per enabled turn even though it cannot change the answer.

Rollback needs no deploy:

```bash
fly secrets unset REGWATCH_ROUTE_CALL REGWATCH_ROUTE_MAX_TOKENS -a amneal
```

A provider, request, parse or catalog failure is audit-only and falls through to
today's deterministic turn. `D1ResidencyError` stays fail-closed. Never weaken
that exception to improve the shadow success rate.

### 6.4 Refusal-threshold revalidation

Two settings gate every answer in `grounded_qa.ask`. If the best retrieved
passage scores below the resolved floor, the turn declines with reason
`low_top_score` and never reaches the synthesizer. If some passages clear it
and some do not, only the ones above it are allowed to support an answer.
Either way, weak evidence cannot become citation cover.

**The floor is per-profile, not a single global number.**
`Settings.effective_refusal_threshold()` (`config/settings.py:120-147`)
resolves `REFUSAL_SCORE_THRESHOLD_BY_PROFILE`, a dict keyed by embedding
profile id, for the profile named in `RETRIEVAL_EMBEDDING_PROFILE`. An
absent entry falls back to the global `REFUSAL_SCORE_THRESHOLD`, default
`0.30`. This doc does not print the live resolved value; it changes with
which profile is active and is a Fly secret, not something in this
checkout. Read it two ways:

- `GET /settings` (authenticated; `go/internal/api/settings.go`) returns
  `refusal_score_threshold`, the resolved effective floor, computed the same
  way as the Python resolver (`go/internal/api/config.go:220-227`).
- `regwatch status` prints both `refusal_score_threshold` (effective, per
  profile) and `refusal_score_threshold_global` (the 0.30 fallback), so an
  operator sees whether a per-profile override is even in play.

A threshold is a property of one embedding model's score distribution.
Cosine distributions differ between spaces, so a calibration done against one
embedding profile does not transfer to another. Whenever
`RETRIEVAL_EMBEDDING_PROFILE` changes to a profile with no entry in
`REFUSAL_SCORE_THRESHOLD_BY_PROFILE`, treat the resulting 0.30 fallback as
provisional until it is recalibrated for that profile's geometry.

**The daily sweep is wired to the right space but cannot recalibrate by
itself.** The `watch-daily` job runs an advisory, non-gating sweep after the
crawl and uploads `threshold_sweep.json` as a workflow artifact (Actions run,
then **Artifacts**, then `threshold-sweep`). It inherits whichever profile
`WATCH_ACTIVE_EMBEDDING_PROFILE` names (`.github/workflows/watch-daily.yml:80-91`),
so once that secret matches the profile prod actually serves, its scores use
production geometry. The corpus labels and refusal cases still need the
evaluation work below before a sweep recommendation can be promoted as a
validated threshold.

The sweep is read-only with respect to the safety path. It never changes
`REFUSAL_SCORE_THRESHOLD` or `REFUSAL_SCORE_THRESHOLD_BY_PROFILE`, and never
fails the crawl (`continue-on-error: true`), so a hiccup or an odd
recommendation cannot block ingestion or alerting.

**The sweep's `current` is a hardcoded 0.30, not the live per-profile
value.** `threshold_sweep.py`'s `--current` option defaults to `0.30`
(`_CURRENT_DEFAULT`) and `watch-daily.yml` invokes the sweep with no
`--current` flag (`.github/workflows/watch-daily.yml:271-274`), so every
comparison in the report is against 0.30 regardless of what
`REFUSAL_SCORE_THRESHOLD_BY_PROFILE` actually resolves to for the active
profile in prod. Before trusting a report, check whether the effective floor
(`GET /settings` or `regwatch status`) is actually 0.30. If it is not, rerun
the sweep manually with `--current <effective-value>` before reading the
pathology flags as actionable.

**How to read `threshold_sweep.json`** (the same content prints as a table in the
step log):

- `distributions.must_answer` and `distributions.must_refuse` are the two
  per-question max-passage-cosine distributions. A cutoff is calibratable only
  when both groups have scored rows. A healthy threshold sits above the
  must-refuse max and at or below the must-answer min.
- `counts.must_clarify_excluded` counts resolver clarification cases, kept for
  audit but excluded from the cutoff curve.
- `recommendation.recommended` vs `recommendation.current` (the `--current`
  value the sweep was run with), with a rationale. The rule is: maximize
  refuse recall without refusing anything `current` already answers.
  `provisional`/`overlap: true` means the two distributions overlap, so no
  clean separator exists and the recommendation is a tradeoff, not a fix.
- Two pathology flags, which are the actual action triggers:
  `wrongly_refused_at_current` (must-answer questions already scoring below
  `current`) and `leaking_at_current` (must-refuse questions already scoring
  at or above `current`).

**Decision procedure.** A human reads the recommendation and both pathology
lists. Only if warranted, meaning a clean recommendation that differs from the
resolved effective floor or a non-empty pathology list, change the live value.
A profile-specific change goes in `REFUSAL_SCORE_THRESHOLD_BY_PROFILE` (a
JSON object keyed by profile id); a global change goes in
`REFUSAL_SCORE_THRESHOLD`:

```bash
fly secrets set REFUSAL_SCORE_THRESHOLD_BY_PROFILE='{"ep_<profile-id>": 0.NN}' -a amneal
```

There is no gate and no auto-tune: over-tuning this cutoff trades directly
against INV safety, so it is an explicit operator decision.

The last real sweep artifact is from 2026-07-30, watch run
[30531864530](https://github.com/Hussain0327/amneal/actions/runs/30531864530), in
the OpenAI 1536 space. Its six must-answer rows scored 0.812 to 0.896, but all
five must-refuse rows stopped before retrieval and had no cosine score, so it
produced no usable recommendation. Details in [`EVAL_STATUS.md`](EVAL_STATUS.md).

### 6.5 Staging and restore drill

A backup you have never restored is a hope, not a backup. We are currently in
that position: there is no rehearsed restore for Lakebase.

**What is needed, and is not built:**

1. A written `pg_dump` / `pg_restore` procedure against the Lakebase endpoint,
   plus a staging target to restore into.
2. A monthly run of it, roughly 30 minutes, with the date and result recorded.

Until that exists, the shape of the drill is:

1. Get a restorable copy into a staging database.
2. If the staging DB is stamped at an older revision, advance it before the
   restore is usable:

   ```bash
   DATABASE_URL='<staging-url>' uv run alembic upgrade head
   ```

3. Point a local API at staging and smoke it. It needs the same embedding
   profile and OpenAI credentials as prod, otherwise it boots into the legacy
   vector space and you are testing something prod does not run:

   ```bash
   DATABASE_URL='<staging-url>' \
   INGEST_EMBEDDING_PROVIDER='openai' LLM_PROVIDER='openai' \
   RETRIEVAL_EMBEDDING_PROFILE='ep_<profile-id>' \
   OPENAI_API_KEY='...' \
   uv run uvicorn regwatch.api.main:app --port 8099
   ```

   Then run the section 5 checklist against `localhost:8099`: step 1 directly,
   steps 5 through 8 by curl or a local frontend with
   `API_PROXY_TARGET=http://localhost:8099`.
4. Record date and result. Anything that fails here failed on staging, which is
   where you want it to fail.

The old `scripts/restore_drill.sh` and `scripts/migrate_to_supabase.py` were
deleted with the SQLite/Chroma dual-mode in R5. See git history if you rebuild a
scripted equivalent. The discipline they enforced still applies by hand: never
point a restore at the production endpoint by accident.
