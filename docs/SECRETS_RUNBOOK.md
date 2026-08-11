# SECRETS_RUNBOOK - the GitHub Actions secret surface

Last updated: 2026-08-11

What this is: the inventory of every Actions secret the workflows read, what each
one gates, and the exact commands to set them. Use it for rotation, a fork, or a
new repo. Fly app secrets are a separate surface and live in
[`DEPLOY.md`](DEPLOY.md) step 3.2.

Five workflows read secrets: `ci.yml`, `deploy.yml`, `watch-daily.yml`,
`uptime-eval.yml` and `databricks-eval.yml`. No workflow declares an
`environment:`, so only **repository** secrets are in play.

**Configured today (8 names, checked 2026-08-11):**

```text
DATABRICKS_LLM_BASE_URL   DATABRICKS_LLM_TOKEN   FLY_API_TOKEN
OPENFDA_API_KEY           QWEN_EMBEDDING_BASE_URL  QWEN_EMBEDDING_TOKEN
WATCH_DATABASE_URL        WATCH_OPENAI_API_KEY
```

Everything else referenced in a workflow is unset. Section 1 says which, and what
that costs.

Current state in one paragraph: CD is live, `deploy.yml` ships every green `ci`
run on `main` to the Fly app (release v104, 2026-08-10). The daily watch is live
and green. It failed every day from 2026-08-07 through the morning of 2026-08-10.
The owner updated `WATCH_DATABASE_URL` at 18:19 UTC on 2026-08-10 and both runs
since have passed, so if this cron starts failing again, the database this secret
points at is the first thing to check (see 2.1 and 2.2). The live Databricks eval
lane runs off `DATABRICKS_LLM_*` and `QWEN_EMBEDDING_*`.

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
| `WATCH_OPENAI_API_KEY` | `watch-daily.yml`, mapped to the job's `OPENAI_API_KEY`. Ingest embeds every chunk through it, and the advisory sweep uses it. A preflight step hard-fails the run when it is empty. | `.env` key **`OPENAI_API_KEY`**, stored under the WATCH_ name | **Set** |
| `FLY_API_TOKEN` | `deploy.yml`, the whole release path: docker build, Trivy re-scan, `scripts/fly-deploy.sh`. Unset means the deploy step fails and nothing ships. | `fly tokens create deploy` (mint fresh, not in `.env`) | **Set** |
| `DATABRICKS_LLM_BASE_URL` / `DATABRICKS_LLM_TOKEN` | `databricks-eval.yml`, the live eval lane called by `ci.yml` and dispatchable by hand. | the Databricks workspace serving host and a token | **Set** |
| `QWEN_EMBEDDING_BASE_URL` / `QWEN_EMBEDDING_TOKEN` | `databricks-eval.yml`. Both must be present or the eval resolves to a non-Qwen arm. | the `regwatch-embed` serving endpoint and a token | **Set** |
| `OPENFDA_API_KEY` | `watch-daily.yml` `env.OPENFDA_API_KEY`. Only raises the openFDA rate limit. | `.env` key **`OPENFDA_API_KEY`** | **Set** |
| `OPENAI_API_KEY` (repo-wide) | `databricks-eval.yml` job env. Separate from the WATCH_ copy on purpose: setting it turns on paid provider-backed work whose result gates CD. | an OpenAI project key approved for CI | Not set |
| `SLACK_WEBHOOK_URL` | `watch-daily.yml`: failure alert AND the success digest. A quiet day posts nothing. Unset means both steps no-op. | Slack incoming webhook, no `.env` key | Not set |
| `WATCH_HEALTHCHECK_URL` | `watch-daily.yml`: success ping plus `/fail` ping. This is the dead-man's-switch for a cron that never STARTS, which the in-job failure step cannot catch. | healthchecks.io-style ping URL | Not set |
| `PROD_HEALTH_URL` | `uptime-eval.yml` probe. Unset means the 30-minute uptime probe skips. | `https://amneal.fly.dev/health` | Not set |
| `WATCH_ACTIVE_EMBEDDING_PROFILE` | `watch-daily.yml` `env.ACTIVE_EMBEDDING_PROFILE`. Embedding-profile parity with prod. | prod's `ACTIVE_EMBEDDING_PROFILE` | **Not set, and this is the open hazard. See section 3.4.** |
| `WATCH_QWEN_EMBEDDING_BASE_URL` / `_TOKEN` / `_MODEL` / `_REVISION` | `watch-daily.yml` profile provider credentials. A preflight hard-fails when the profile is set but BASE_URL/TOKEN/MODEL are missing. A post-ingest step asserts the active profile still covers every chunk. | mirror the Fly app's `QWEN_EMBEDDING_*` secrets | **Not set. Same hazard.** |

Repository **variables** (not secrets, set under the same settings page) feed the
eval lane: `DATABRICKS_LLM_MODEL`, `QWEN_EMBEDDING_MODEL`,
`QWEN_EMBEDDING_DIMENSION`, `DATABRICKS_SERVING_RUNTIME_VERSION`. The runtime
version is deliberately stable, because it is part of the embedding-profile
fingerprint and a per-run value would mint a new profile id every build.

Other things worth knowing:

- `watch-daily.yml` also hardcodes non-secret env: `REQUIRE_DATABASE_URL=true`,
  `EMBEDDING_PROVIDER=openai`, `SENTRY_ENVIRONMENT=production`. Nothing to
  provision. The `EMBEDDING_PROVIDER` line says it mirrors prod, which is no
  longer true: prod embeds through the Qwen3 profile. Part of the same gap in
  3.4.
- There is **no** `SENTRY_DSN` Actions secret. Sentry is a **Fly** secret on the
  app ([`DEPLOY.md`](DEPLOY.md) step 3.2). Do not add it here.
- The Fly app carries a much larger secret surface: `LLM_PROVIDER`,
  `DATABRICKS_LLM_*`, `ACTIVE_EMBEDDING_PROFILE`, `QWEN_EMBEDDING_*`,
  `INTERNAL_RAG_TOKEN`, `METRICS_TOKEN`, `D1_ALLOWED_LLM_MODELS` and the three
  `REGWATCH_*` answer-policy flags. Those are set with `fly secrets set`, never
  in Actions. See [`DEPLOY.md`](DEPLOY.md) step 3.2.
- "Not set" means the workflow no-ops cleanly, so forks stay green. But the three
  alerting secrets are what make a silent failure visible. Provision
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

# WATCH_OPENAI_API_KEY <- local OPENAI_API_KEY, stored under the watch-scoped
# name. The repo-wide OPENAI_API_KEY is a separate decision: setting it turns on
# the paid provider-backed eval arm.
grep -E '^OPENAI_API_KEY=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_OPENAI_API_KEY -R Hussain0327/amneal
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
# OPENFDA_API_KEY <- local OPENFDA_API_KEY (only raises the openFDA rate limit).
grep -E '^OPENFDA_API_KEY=' .env | head -1 | cut -d= -f2- \
  | gh secret set OPENFDA_API_KEY -R Hussain0327/amneal

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

### 3.4 OPEN HAZARD: the watch cron has no embedding profile

**This is the one item in this runbook that is currently wrong in production.**

Prod promoted its Qwen3 embedding profile
(`ep_2e7368b354d911ea3a013c3125e276c2`, 1024 dim) on 2026-07-30. The watch cron
never followed. `WATCH_ACTIVE_EMBEDDING_PROFILE` and the four
`WATCH_QWEN_EMBEDDING_*` secrets are still unset, so the profile block in
`watch-daily.yml` is inert and the cron still embeds through
`WATCH_OPENAI_API_KEY` into the old legacy vector space.

What that costs, in order:

1. Today, on a no-change day, nothing. The failure only fires on the first day a
   real FDA revision lands.
2. On that day, ingest commits chunk rows carrying no embedding on the live
   profile, because ingest builds its profile targets from its OWN process env.
3. Profile coverage goes incomplete. The "verify embedding-profile coverage"
   step cannot catch it either, because that step is also gated on
   `ACTIVE_EMBEDDING_PROFILE` being set.
4. The next Python boot refuses inside `assert_profile_ready_for_activation`.
   It presents as edge 502s, weeks later, at an unrelated deploy: the Go proxy
   skips init-db by design, so it keeps holding the public port and relaying into
   a dead upstream.

**Setting the five secrets is necessary but NOT sufficient.**
`watch-daily.yml` maps `BASE_URL`, `TOKEN`, `MODEL` and `REVISION` but does
**not** map a dimension, and its preflight does not require one.
`config/settings.py` defaults `qwen_embedding_dimension` to 1536 while the
endpoint is 1024. The profile fingerprint covers dimension, so
`get_embedding_provider_for_profile` fails closed on the mismatch: the run fails
loudly rather than writing wrong-space vectors. Good failure mode, still a failed
run. Closing this properly means a workflow change that adds
`QWEN_EMBEDDING_DIMENSION` to both the env block and the preflight required set,
plus a `WATCH_QWEN_EMBEDDING_DIMENSION` secret. That change is tracked in
[`ROADMAP.md`](ROADMAP.md).

The workflow header was corrected on 2026-08-11 and now says the hazard is armed
rather than inert, so read it as current.

Setting the secrets is the owner's action. When the workflow gains its dimension
mapping, all of them go in together:

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
```

Every value must match prod exactly. The profile fingerprint covers model,
dimension, revision, instruction version, preprocessing version, dtype and
normalization, so any drift fails closed rather than writing vectors from a
different space into a profile that claims otherwise.

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
- **"preflight WATCH_OPENAI_API_KEY"** passed. It hard-fails on an empty key.
- **"preflight embedding-profile credentials"** and **"verify embedding-profile
  coverage"** passed. Both are currently inert because no profile is set (3.4).
  Once the profile secrets exist they become real gates.
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
gh secret delete WATCH_OPENAI_API_KEY -R Hussain0327/amneal

# Silence alerting and uptime:
gh secret delete SLACK_WEBHOOK_URL -R Hussain0327/amneal
gh secret delete WATCH_HEALTHCHECK_URL -R Hussain0327/amneal
gh secret delete PROD_HEALTH_URL -R Hussain0327/amneal
gh secret delete OPENFDA_API_KEY -R Hussain0327/amneal
```

- Deleting `WATCH_DATABASE_URL` is the cleanest kill-switch for the cron: the
  next scheduled run takes the green "skipped" path with no failure noise.
- Deleting `FLY_API_TOKEN` is the cleanest CD kill-switch. It does NOT roll back
  an already-shipped release. For that use the levers in [`DEPLOY.md`](DEPLOY.md)
  section 6.1.
- **Rotation.** `fly tokens revoke` the old deploy token, mint a new one, re-run
  3.2. For `WATCH_OPENAI_API_KEY` and `WATCH_DATABASE_URL`, rotate the upstream
  credential, update `.env`, re-run the matching 3.1 command. There is no
  in-place edit: setting the same name overwrites.

---

## 6. Expected steady state

- `watch-daily` runs at 07:17 UTC, executes the real pipeline, writes durable
  alerts to the shared prod database, and pings healthchecks.io or Slack if those
  are configured. Green since 2026-08-10 18:19 UTC.
- `deploy.yml` auto-deploys the exact CI-validated commit to the `amneal` Fly app
  on every green `main` push.
- `databricks-eval` runs the live eval lane, serialized against the shared
  Databricks workspace so concurrent runs cannot collide on QPS.
- `uptime-eval` would probe prod `/health` every 30 minutes, but it is skipping:
  `PROD_HEALTH_URL` is unset.

Open item carried from section 3.4: the watch cron still has no embedding
profile.

Related: [`DEPLOY.md`](DEPLOY.md) for the full runbook and operations,
[`CI_CD.md`](CI_CD.md) for the `ci` gate these secrets sit downstream of.
