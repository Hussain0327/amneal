# SECRETS_RUNBOOK - the GitHub Actions secret surface

Last updated: 2026-08-17

What this is: the inventory of every Actions secret the workflows read, what each
one gates, and the exact commands to set them. Use it for rotation, a fork, or a
new repo. Fly app secrets are a separate surface and live in
[`DEPLOY.md`](DEPLOY.md) step 3.2.

Five workflows read secrets: `ci.yml`, `deploy.yml`, `watch-daily.yml`,
`uptime-eval.yml` and `databricks-eval.yml`. No workflow declares an
`environment:`, so only **repository** secrets are in play.

**Configured today (checked 2026-08-17 via `gh secret list`):**

```text
DATABRICKS_LLM_BASE_URL   DATABRICKS_LLM_TOKEN   FLY_API_TOKEN
QWEN_EMBEDDING_BASE_URL   QWEN_EMBEDDING_TOKEN
WATCH_DATABASE_URL        WATCH_ACTIVE_EMBEDDING_PROFILE
WATCH_QWEN_EMBEDDING_BASE_URL / _TOKEN / _MODEL / _REVISION / _DIMENSION
WATCH_OPENAI_API_KEY      (dead since 2026-08-17; delete it)
```

Everything else referenced in a workflow is unset. Section 1 says which, and what
that costs.

Current state in one paragraph: CD is live, `deploy.yml` ships every green `ci`
run on `main` to the Fly app. The owner fixed `WATCH_DATABASE_URL` on
2026-08-10, and all six Watch embedding-profile secrets were provisioned on
2026-08-12 (verified present 2026-08-17). Watch's LLM work reads the repo-wide
`DATABRICKS_LLM_*` names since 2026-08-17 (`WATCH_OPENAI_API_KEY` is dead).
The live Databricks eval lane runs off the same `DATABRICKS_LLM_*` and
`QWEN_EMBEDDING_*`.

> **This runbook provisions secrets. It does NOT print, echo, or commit any secret
> VALUE.** Every command below sets a value from your local environment or a fresh
> token, by name only. Do not paste a secret into a terminal where it lands in
> shell history. Prefer the `gh secret set NAME < file` and stdin forms shown.

---

## 0. Prerequisites

- `gh` CLI authenticated against `Hussain0327/amneal` with a token that can write
  Actions secrets. In this repo `gh auth login` may be rejected because the token
  lacks `read:org`, so export a `repo`-scoped token per call instead:

  ```bash
  GH_TOKEN=$(git credential fill <<<$'protocol=https\nhost=github.com\n' | sed -n 's/^password=//p')
  export GH_TOKEN
  ```

- `fly` (flyctl) authenticated as a deployer of the `amneal` app. Needed only to
  MINT the deploy token.
- Your local `.env` populated with the production values. This runbook maps each
  secret to a local key by NAME. Confirm the names exist without reading values:

  ```bash
  grep -oE '^[A-Z_]+=' .env | tr -d '='
  ```

- Confirm current state. `gh` returns names and updated-at, never values:

  ```bash
  gh secret list -R Hussain0327/amneal
  ```

---

## 1. Secret to workflow inventory

| Secret | What it gates | Value source | State |
|---|---|---|---|
| `WATCH_DATABASE_URL` | `watch-daily.yml` `env.DATABASE_URL`. Gates every real step: checkout, deps, `regwatch watch`, threshold sweep. Unset means the job skips cleanly. | `.env` key **`DATABASE_URL`**, the Lakebase DIRECT endpoint | **Set** |
| `DATABRICKS_LLM_BASE_URL` / `DATABRICKS_LLM_TOKEN` (secrets) + `DATABRICKS_LLM_MODEL` (variable) | `watch-daily.yml`, mapped to the job's `DATABRICKS_LLM_*` -- the SAME names `databricks-eval.yml` reads. Used for change summaries, optional BE extraction, and advisory-sweep synthesis; never for Watch embeddings. A preflight hard-fails when any is empty. Replaced `WATCH_OPENAI_API_KEY` on 2026-08-17 (the OpenAI provider was removed). | already provisioned for the eval workflow | **Set** (verified 2026-08-17) |
| `FLY_API_TOKEN` | `deploy.yml`, the whole release path: docker build, Trivy re-scan, `scripts/fly-deploy.sh`. Unset means the deploy step fails and nothing ships. | `fly tokens create deploy` (mint fresh, not in `.env`) | **Set** |
| `DATABRICKS_LLM_BASE_URL` / `DATABRICKS_LLM_TOKEN` | `databricks-eval.yml`, the live eval lane called by `ci.yml` and dispatchable by hand. | the Databricks workspace serving host and a token | **Set** |
| `QWEN_EMBEDDING_BASE_URL` / `QWEN_EMBEDDING_TOKEN` | `databricks-eval.yml`. Both must be present or the eval resolves to a non-Qwen arm. | the `regwatch-embed` serving endpoint and a token | **Set** |
| `SLACK_WEBHOOK_URL` | `watch-daily.yml`: failure alert AND the success digest. A quiet day posts nothing. Unset means both steps no-op. | Slack incoming webhook, no `.env` key | Not set |
| `WATCH_HEALTHCHECK_URL` | `watch-daily.yml`: success ping plus `/fail` ping. This is the dead-man's-switch for a cron that never STARTS, which the in-job failure step cannot catch. | healthchecks.io-style ping URL | Not set |
| `PROD_HEALTH_URL` | `uptime-eval.yml` probe. Unset means the 30-minute uptime probe skips. | `https://amneal.fly.dev/health` | Not set |
| `WATCH_ACTIVE_EMBEDDING_PROFILE` | `watch-daily.yml` `env.ACTIVE_EMBEDDING_PROFILE`. Must be prod's named `ep_...` profile; empty and `legacy` fail before crawl. | prod's `ACTIVE_EMBEDDING_PROFILE` | **Set** (2026-08-12, verified 2026-08-17) |
| `WATCH_QWEN_EMBEDDING_BASE_URL` / `_TOKEN` / `_MODEL` / `_REVISION` / `_DIMENSION` | `watch-daily.yml` Qwen provider configuration. All five are mandatory. The registered-profile gate checks fingerprint/readiness before crawl; post-ingest coverage must remain 100%. | mirror the Fly app's `QWEN_EMBEDDING_*` values exactly | **Set** (2026-08-12, verified 2026-08-17) |

Repository **variables** (not secrets, set under the same settings page) feed the
eval lane: `DATABRICKS_LLM_MODEL`, `QWEN_EMBEDDING_MODEL`,
`QWEN_EMBEDDING_DIMENSION`, `DATABRICKS_SERVING_RUNTIME_VERSION`. The runtime
version is deliberately stable, because it is part of the embedding-profile
fingerprint and a per-run value would mint a new profile id every build.

Other things worth knowing:

- `watch-daily.yml` also hardcodes non-secret env:
  `REQUIRE_DATABASE_URL=true`, `EMBEDDING_PROVIDER=qwen3`,
  `LLM_PROVIDER=databricks`, and `SENTRY_ENVIRONMENT=production`.
  `ACTIVE_EMBEDDING_PROFILE` chooses the named vector space; scheduled
  revisions leave the retired legacy vector column NULL.
- There is **no** `SENTRY_DSN` Actions secret. Sentry is a **Fly** secret on the
  app ([`DEPLOY.md`](DEPLOY.md) step 3.2). Do not add it here.
- The Fly app carries a much larger secret surface: `LLM_PROVIDER`,
  `DATABRICKS_LLM_*`, `ACTIVE_EMBEDDING_PROFILE`, `QWEN_EMBEDDING_*`,
  `INTERNAL_RAG_TOKEN`, `METRICS_TOKEN`, `D1_ALLOWED_LLM_MODELS` and the three
  `REGWATCH_*` answer-policy flags. Those are set with `fly secrets set`, never
  in Actions. See [`DEPLOY.md`](DEPLOY.md) step 3.2.
- The optional alerting/uptime secrets no-op cleanly when unset. The six Watch
  profile secrets are different: with `WATCH_DATABASE_URL` set, any missing
  value fails preflight before crawl. The three alerting secrets are what make a
  silent failure visible. Provision
  `SLACK_WEBHOOK_URL`, `WATCH_HEALTHCHECK_URL` and `PROD_HEALTH_URL` unless you
  have an equivalent external monitor.

---

## 2. Preflight checklist (before you set anything)

This pipeline writes the **shared production database**. Treat
`WATCH_DATABASE_URL` and `FLY_API_TOKEN` as prod-touching.

### 2.1 WATCH_DATABASE_URL must be the Lakebase DIRECT endpoint

- [ ] It is the **direct** endpoint
      (`ep-<id>.database.<region>.cloud.databricks.com`), **not** the `-pooler`
      host. The pooler is PgBouncer in transaction mode, which breaks both
      `regwatch watch`'s long-lived session and the Go proxy's prepared
      statements. See [`DEPLOY.md`](DEPLOY.md) step 1.1.
- [ ] Special characters in the password are URL-encoded (`@` to `%40`, `#` to
      `%23`, and so on). A bare `postgresql://` prefix is fine, the app
      normalizes the scheme itself.
- [ ] It points at the **same database the API runs against**. The watch and the
      API must share one database. Cross-check the chunk count against
      `/health`'s `corpus_count` (5,494 on 2026-08-11). Pointing at the wrong
      copy is the failure class that broke the cron from 2026-08-07 to
      2026-08-10.

TLS does not need a manual `?sslmode=require`: both tiers append it for any
non-local host (`store/db.py:_enforce_sslmode`). Adding it yourself is harmless
and an explicit value wins.

Sanity-check the SHAPE of your local value without revealing the password:

```bash
# prints only host:port and any query string, never the password
sed -n 's#.*@##p' <<<"$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)" \
  | sed -E 's#^([^/]+/[^?]*\??).*#\1#'
```

### 2.2 Schema-stamp guard

`watch-daily.yml` does **not** migrate. `regwatch watch` boots through
`_init_postgres`, which refuses to start if the live DB stamp does not equal this
checkout's alembic head. If a migration is merged but not deployed, the cron
fails loudly instead of pushing the live schema ahead of the running API
machines. Before the first manual run, confirm `main` is deployed and the DB is
at head. The section 4 dispatch run surfaces any mismatch as a loud failure, not
a silent skip.

### 2.3 FLY_API_TOKEN re-enables auto-deploy

- [ ] **Setting `FLY_API_TOKEN` turns CD back on.** The next push to `main` that
      goes green in `ci` triggers `deploy.yml` and deploys to live prod.
- [ ] CD is currently ENABLED and `main` equals prod (release v104, 2026-08-10).
      Whenever you RE-enable after a gap, check whether `main` carries an
      un-deployed migration first. The release command (`alembic upgrade head`)
      applies it automatically on the first auto-deploy. Make sure that is what
      you intend, or deploy manually first so the first automated run is a no-op.
- [ ] Mint a **deploy-scoped** token (`fly tokens create deploy`), not an org or
      admin token.

---

## 3. Exact commands (no values shown)

Run from the repo root with `GH_TOKEN` exported (section 0). None of these print
a secret.

### 3.1 Required for the daily watch

```bash
# WATCH_DATABASE_URL <- local DATABASE_URL (the Lakebase direct endpoint).
# Piped from .env so the value never lands in argv or shell history.
grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_DATABASE_URL -R Hussain0327/amneal

# Watch's LLM work reads the repo-wide DATABRICKS_LLM_BASE_URL/_TOKEN
# secrets and the DATABRICKS_LLM_MODEL variable -- the same trio the eval
# workflow uses, already provisioned (verified 2026-08-17). Nothing to set.
```

> If your `.env` values are quoted, strip the quotes before piping (add
> `| tr -d '"'` or `| tr -d "'"`). Run 2.1's shape check first so you know
> exactly what `cut -d= -f2-` yields.

### 3.2 Required for CD

```bash
# Mint a fresh deploy-scoped Fly token and store it without printing it.
fly tokens create deploy --app amneal \
  | gh secret set FLY_API_TOKEN -R Hussain0327/amneal
```

If your flyctl version wraps the `FlyV1 ...` token in log lines, mint it into a
restricted temp file instead, pipe that in, then shred it. Never under the repo,
never committed:

```bash
umask 077
tmp="$(mktemp)"                      # use your session scratchpad if you prefer
fly tokens create deploy --app amneal > "$tmp"
# (verify $tmp holds exactly the token line; trim any log preamble)
gh secret set FLY_API_TOKEN -R Hussain0327/amneal < "$tmp"
rm -P "$tmp" 2>/dev/null || rm -f "$tmp"
```

### 3.3 Optional: alerting and uptime (recommended)

```bash
# SLACK_WEBHOOK_URL <- a Slack incoming-webhook URL.
#   Slack -> Apps -> Incoming Webhooks -> Add to a channel -> copy the URL into a
#   restricted temp file, then:
gh secret set SLACK_WEBHOOK_URL -R Hussain0327/amneal < /path/to/slack_webhook.txt

# WATCH_HEALTHCHECK_URL <- a healthchecks.io ping URL (dead-man's-switch).
gh secret set WATCH_HEALTHCHECK_URL -R Hussain0327/amneal < /path/to/hc_url.txt

# PROD_HEALTH_URL <- https://amneal.fly.dev/health
gh secret set PROD_HEALTH_URL -R Hussain0327/amneal < /path/to/prod_health_url.txt
```

`PROD_HEALTH_URL` is not really secret, but storing it as one keeps
`uptime-eval.yml`'s "no invented URLs" contract intact and avoids hardcoding a
host in the workflow.

### 3.4 Required: provision the Watch embedding profile

**The workflow code is fail-safe; the owner provisioning is still incomplete.**

Prod promoted its Qwen3 embedding profile
(`ep_2e7368b354d911ea3a013c3125e276c2`, 1024 dim) on 2026-07-30. The workflow
now pins Qwen3, rejects empty/legacy/malformed profile IDs, requires all six
settings before checkout, validates profile fingerprint/coverage/index readiness
before crawl, and checks zero pending chunks after every attempted Watch run.
The base URL and token are presence-checked; validation does not spend an
inference request to authenticate them on a no-change day.

All six repository secrets were still unset on 2026-08-12. With
`WATCH_DATABASE_URL` configured, that now causes an immediate preflight failure
and no crawl. Set all six together:

```bash
# The promoted profile id, matching prod's ACTIVE_EMBEDDING_PROFILE exactly:
printf '%s' '<profile-id>' | gh secret set WATCH_ACTIVE_EMBEDDING_PROFILE -R Hussain0327/amneal

# Provider credentials, mirroring the Fly app's QWEN_EMBEDDING_* secrets.
# BASE_URL is the endpoint WITHOUT the trailing /embeddings (the provider appends it).
grep -E '^QWEN_EMBEDDING_BASE_URL=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_QWEN_EMBEDDING_BASE_URL -R Hussain0327/amneal
grep -E '^QWEN_EMBEDDING_TOKEN=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_QWEN_EMBEDDING_TOKEN -R Hussain0327/amneal
grep -E '^QWEN_EMBEDDING_MODEL=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_QWEN_EMBEDDING_MODEL -R Hussain0327/amneal
grep -E '^QWEN_EMBEDDING_REVISION=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_QWEN_EMBEDDING_REVISION -R Hussain0327/amneal
grep -E '^QWEN_EMBEDDING_DIMENSION=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_QWEN_EMBEDDING_DIMENSION -R Hussain0327/amneal
```

Every value must match prod exactly. The profile fingerprint covers model,
dimension, revision, instruction version, preprocessing version, dtype and
normalization, so any drift fails closed rather than writing vectors from a
different space into a profile that claims otherwise.

Qwen mode intentionally stops refreshing the legacy `chunk.embedding` OpenAI
column. Before an embedding rollback to `ACTIVE_EMBEDDING_PROFILE=legacy`,
backfill that arm so it includes revisions ingested after this change.

### 3.5 Confirm what was set

```bash
gh secret list -R Hussain0327/amneal
```

Names and updated-at only, never values.

---

## 4. Validate the first watch run

Do not wait for the 07:17 UTC cron. `watch-daily.yml` declares
`workflow_dispatch`, so trigger one manual run and read its logs. This is the
safe first write to the shared prod database.

```bash
gh workflow run watch-daily.yml -R Hussain0327/amneal

# Give Actions a few seconds to register the run, then follow it.
gh run list --workflow=watch-daily.yml -R Hussain0327/amneal --limit 1
gh run watch <run-id> -R Hussain0327/amneal --exit-status
gh run view <run-id> -R Hussain0327/amneal --log
```

What a good run looks like:

- The **"skipped (secret not configured)"** step did NOT run. If you still see
  "WATCH_DATABASE_URL secret not set", the secret did not land (3.1).
- **"preflight Databricks LLM config"** passed. It hard-fails on any empty value.
- **"preflight Watch embedding profile"** passed, proving all six values were
  present and the profile ID/dimension had valid shapes.
- **"validate registered embedding profile"** passed before crawl. This checks
  the immutable fingerprint and database readiness; it is not a live endpoint
  request.
- **"verify embedding-profile coverage"** passed after Watch and reported zero
  pending chunks.
- **"regwatch watch"** exited 0: crawl, match, ingest, durable alerts, digest. A
  stamp-mismatch refusal here means deploy the pending migration first (2.2). It
  is the guard working, not a secret problem.
- The advisory **"threshold sweep"** may fail without failing the job
  (`continue-on-error: true`). By design, it never blocks the crawl.
- If Slack is set, **"slack digest on success"** posts when there are alerts. A
  quiet day posts nothing, which is success and not a wiring failure.
- If `WATCH_HEALTHCHECK_URL` is set, the success ping fired. To exercise the
  failure-alert path, do it out of band on a throwaway branch. Do not sabotage
  the prod-DB run to test alerting.

`deploy.yml` is `workflow_run`-triggered off a green `ci` on `main`, not
dispatchable. Its first execution is the next green push to `main`, which per 2.3
should be a no-op redeploy:

```bash
gh run list --workflow=deploy.yml -R Hussain0327/amneal --limit 1
gh run view <run-id> -R Hussain0327/amneal --log
```

`uptime-eval.yml` is dispatchable:

```bash
gh workflow run uptime-eval.yml -R Hussain0327/amneal
```

Expect "health OK". "PROD_HEALTH_URL secret not set" means the optional secret is
absent, so the probe skipped without failing.

---

## 5. Rollback

Each secret is independently removable. Deleting one immediately reverts that
workflow to its dormant path. None of these deletions touch prod data.

```bash
# Disable CD (the next green main no longer deploys):
gh secret delete FLY_API_TOKEN -R Hussain0327/amneal

# Re-dormant the daily watch (job returns to the clean skip path):
gh secret delete WATCH_DATABASE_URL -R Hussain0327/amneal
gh secret delete WATCH_OPENAI_API_KEY -R Hussain0327/amneal  # dead since 2026-08-17

# Silence alerting and uptime:
gh secret delete SLACK_WEBHOOK_URL -R Hussain0327/amneal
gh secret delete WATCH_HEALTHCHECK_URL -R Hussain0327/amneal
gh secret delete PROD_HEALTH_URL -R Hussain0327/amneal
```

- Deleting `WATCH_DATABASE_URL` is the cleanest kill-switch for the cron: the
  next scheduled run takes the green "skipped" path with no failure noise.
- Deleting `FLY_API_TOKEN` is the cleanest CD kill-switch. It does NOT roll back
  an already-shipped release. For that use the levers in [`DEPLOY.md`](DEPLOY.md)
  section 6.1.
- **Rotation.** `fly tokens revoke` the old deploy token, mint a new one, re-run
  3.2. For `DATABRICKS_LLM_TOKEN` and `WATCH_DATABASE_URL`, rotate the upstream
  credential, update `.env`, re-run the matching 3.1 command. There is no
  in-place edit: setting the same name overwrites.

---

## 6. Expected steady state

This state begins only after section 3.4's six secrets are provisioned and
section 4's manual dispatch passes. Until then, the first run of this workflow
revision is expected to fail safely at profile preflight.

- `watch-daily` runs at 07:17 UTC, executes the real pipeline, writes durable
  alerts to the shared prod database, and pings healthchecks.io or Slack if those
  are configured. The last observed pre-parity runs passed after the database
  secret was corrected on 2026-08-10.
- `deploy.yml` auto-deploys the exact CI-validated commit to the `amneal` Fly app
  on every green `main` push.
- `databricks-eval` runs the live eval lane, serialized against the shared
  Databricks workspace so concurrent runs cannot collide on QPS.
- `uptime-eval` would probe prod `/health` every 30 minutes, but it is skipping:
  `PROD_HEALTH_URL` is unset.

Open item carried from section 3.4: provision all six Watch profile secrets and
record one successful manual dispatch before calling the operational fix done.

Related: [`DEPLOY.md`](DEPLOY.md) for the full runbook and operations,
[`CI_CD.md`](CI_CD.md) for the `ci` gate these secrets sit downstream of.
