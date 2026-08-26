# SECRETS_RUNBOOK - the GitHub Actions secret surface

Last updated: 2026-08-26

What this is: rotation procedure, blast radius, and consumers for every GitHub
Actions secret this repo reads. `docs/CONFIG_REFERENCE.md` owns the full
environment variable, flag and secret inventory (sections 4 and 6 cover Fly
and Actions secrets specifically); read this instead when you need the full
picture. Fly app secrets are a separate surface: see
[`DEPLOY.md`](DEPLOY.md) step 3.2 and `CONFIG_REFERENCE.md` section 4.

Six workflow files exist: `ci.yml`, `deploy.yml`, `machine-monitor.yml`,
`openai-eval.yml`, `uptime-eval.yml`, `watch-daily.yml`. `ci.yml` reads no
secret directly; its `openai-eval` job passes `secrets: inherit` to
`openai-eval.yml` (`ci.yml:92`), which is how `OPENAI_API_KEY` reaches the
blocking eval. No workflow declares `environment:`, so only **repository**
secrets are in play.

Regenerate the inventory yourself before trusting any list below, including
this one:

```bash
grep -rhoE 'secrets\.[A-Z_]+' .github/workflows/*.yml | sort -u
```

That returns exactly seven names today: `FLY_API_TOKEN`, `OPENAI_API_KEY`,
`PROD_HEALTH_URL`, `SLACK_WEBHOOK_URL`, `WATCH_ACTIVE_EMBEDDING_PROFILE`,
`WATCH_DATABASE_URL`, `WATCH_HEALTHCHECK_URL`.

This doc does not claim which of the seven are currently set. A static
checkout cannot prove that. Run this yourself:

```bash
gh secret list -R Hussain0327/amneal
```

It prints names and updated-at, never values.

> **This runbook provisions secrets. It does NOT print, echo, or commit any
> secret VALUE.** Every command below sets a value from your local environment
> or a fresh token, by name only. Do not paste a secret into a terminal where
> it lands in shell history. Prefer the `gh secret set NAME < file` and stdin
> forms shown.

---

## 0. Prerequisites

- `gh` CLI authenticated against `Hussain0327/amneal` with a token that can
  write Actions secrets. In this repo `gh auth login` may be rejected because
  the token lacks `read:org`, so export a `repo`-scoped token per call
  instead:

  ```bash
  GH_TOKEN=$(git credential fill <<<$'protocol=https\nhost=github.com\n' | sed -n 's/^password=//p')
  export GH_TOKEN
  ```

- `fly` (flyctl) authenticated as a deployer of the `amneal` app. Needed only
  to MINT the deploy token.
- Your local `.env` populated with the production values. This runbook maps
  each secret to a local key by NAME. Confirm the names exist without reading
  values:

  ```bash
  grep -oE '^[A-Z_]+=' .env | tr -d '='
  ```

---

## 1. Secret to workflow inventory

| Secret | What it gates | Value source | Failure mode when absent |
|---|---|---|---|
| `WATCH_DATABASE_URL` | `watch-daily.yml` `env.DATABASE_URL` (line 73). Every real step: checkout, deps, `regwatch watch`, threshold sweep. | `.env` key **`DATABASE_URL`**, the Lakebase DIRECT endpoint | Job skips cleanly at the first step; no failure |
| `OPENAI_API_KEY` | `watch-daily.yml` (line 81) and `openai-eval.yml` (line 68, inherited into `ci.yml`'s `openai-eval` job). Gates both Responses generation and `text-embedding-3-large` embeddings on every path that reads it. | `.env` key **`OPENAI_API_KEY`** | Both preflight steps hard-fail loudly: `watch-daily.yml`'s "preflight OpenAI config" (lines 102-109) aborts before any crawl; `openai-eval.yml`'s "preflight OpenAI config" (lines 96-102) fails the job outright rather than skipping, which fails the required check `openai-eval / eval` |
| `FLY_API_TOKEN` | `deploy.yml`, the whole release path: registry push, `flyctl deploy`, the post-deploy machine check. Also `machine-monitor.yml`'s 10-minute app-machine check. | `fly tokens create deploy` (mint fresh, not in `.env`) | `deploy.yml` fails at the registry push; nothing ships. `machine-monitor.yml` skips cleanly and never checks machine state |
| `SLACK_WEBHOOK_URL` | `watch-daily.yml`: failure alert AND the success digest. `machine-monitor.yml`: failure/timeout alert. | Slack incoming webhook, no `.env` key | Optional. Every notify step is a graceful no-op |
| `WATCH_HEALTHCHECK_URL` | `watch-daily.yml`: success ping plus `/fail` ping. Dead-man's-switch for a cron that never STARTS. | healthchecks.io-style ping URL | Optional. Both pings no-op |
| `PROD_HEALTH_URL` | `uptime-eval.yml` probe of `GET /health` every 30 minutes. | `https://amneal.fly.dev/health` | Optional. The probe skips cleanly, no invented URL |
| `WATCH_ACTIVE_EMBEDDING_PROFILE` | `watch-daily.yml` `env.RETRIEVAL_EMBEDDING_PROFILE` (line 80). Must equal the exact profile id prod's `RETRIEVAL_EMBEDDING_PROFILE` Fly secret serves. | prod's live embedding profile id (`CONFIG_REFERENCE.md` section 8 for how to read it) | With `WATCH_DATABASE_URL` set, the preflight step "preflight Watch embedding profile" (lines 115-128) hard-fails if it is blank or does not match `^ep_[0-9a-f]{32}$`. `legacy` fails the shape check |

Other things worth knowing:

- `watch-daily.yml` hardcodes its own provider env block rather than
  inheriting anything (lines 70-93): `INGEST_EMBEDDING_PROVIDER="openai"`,
  `LLM_PROVIDER="openai"`, `PROFILE_HNSW_INDEX_REQUIRED="false"`,
  `REQUIRE_DATABASE_URL="true"`, `OPENAI_BASE_URL`, `OPENAI_LLM_MODEL`,
  `OPENAI_REASONING_EFFORT`, `OPENAI_EMBEDDING_MODEL`,
  `OPENAI_EMBEDDING_DIMENSION`, and `SENTRY_ENVIRONMENT="production"`.
  Changing a model default in `fly.toml` or `config/settings.py` does not
  change this block; edit the workflow file directly.
- `openai-eval.yml` does the same in its own `env:` block (lines 67-78). It
  derives `REGWATCH_PROSE_SYNTHESIS` and `REGWATCH_SELECTIVE_CITATION` from
  workflow inputs; `ci.yml` is what sets those inputs to score the production
  arm (`ci.yml:88-91`).
- There is **no** `SENTRY_DSN` Actions secret. Sentry is a **Fly** secret on
  the app (`CONFIG_REFERENCE.md` section 4, `DEPLOY.md` step 3.2). Do not add
  it here.
- The Fly app carries its own secret surface: `DATABASE_URL`,
  `OPENAI_API_KEY`, `RETRIEVAL_EMBEDDING_PROFILE`, `INTERNAL_RAG_TOKEN`,
  `METRICS_TOKEN`, `SENTRY_DSN`, `REGWATCH_LIVE_DRAFT`. Those are set with
  `fly secrets set`, never in Actions. See `DEPLOY.md` step 3.2 for the exact
  command and `CONFIG_REFERENCE.md` section 4 for what each one gates; do not
  assume this list is complete, treat `fly secrets list -a amneal` as
  authoritative.

---

## 2. Preflight checklist (before you set anything)

This pipeline writes the **shared production database**. Treat
`WATCH_DATABASE_URL` and `FLY_API_TOKEN` as prod-touching.

### 2.1 WATCH_DATABASE_URL must be the Lakebase DIRECT endpoint

- [ ] It is the **direct** endpoint
      (`ep-<id>.database.<region>.cloud.databricks.com`), **not** the
      `-pooler` host. The pooler is PgBouncer in transaction mode, which
      breaks the app's connection pool (`src/regwatch/store/db.py:390-391`).
- [ ] Special characters in the password are URL-encoded (`@` to `%40`, `#`
      to `%23`, and so on). A bare `postgresql://` prefix is fine, the app
      normalizes the scheme itself.
- [ ] It points at the **same database the API runs against**. The watch and
      the API must share one database. Cross-check the `corpus_count` field
      of `GET /health` against Watch's own logs after a run; do not assume a
      number from this doc. Pointing at the wrong copy is the failure class
      that broke the cron from 2026-08-07 to 2026-08-10.

TLS does not need a manual `?sslmode=require`: `_enforce_sslmode`
(`src/regwatch/store/db.py:195-217`) appends it for any non-local host.
Adding it yourself is harmless and an explicit value wins.

Sanity-check the SHAPE of your local value without revealing the password:

```bash
# prints only host:port and any query string, never the password
sed -n 's#.*@##p' <<<"$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)" \
  | sed -E 's#^([^/]+/[^?]*\??).*#\1#'
```

### 2.2 Schema-stamp guard

`watch-daily.yml` does **not** migrate. It runs `regwatch init-db` before
`regwatch watch`, which refuses to start if the live DB stamp does not equal
this checkout's Alembic head. If a migration is merged but not deployed, the
cron fails loudly instead of pushing the live schema ahead of the running API
machines. Before the first manual run, confirm `main` is deployed and the DB
is at head (`alembic heads` locally, `GET /health` in prod). Section 4's
dispatch run surfaces any mismatch as a loud failure, not a silent skip.

### 2.3 FLY_API_TOKEN re-enables auto-deploy

- [ ] **Setting `FLY_API_TOKEN` turns CD back on.** The next push to `main`
      that goes green in `ci` triggers `deploy.yml` and deploys to live prod.
- [ ] Check whether CD is currently enabled and whether `main` carries an
      un-deployed migration before you set this. `gh secret list` shows
      whether the secret exists; `fly releases -a amneal` shows the current
      release; do not trust a value written in this doc for either.
- [ ] Mint a **deploy-scoped** token (`fly tokens create deploy`), not an org
      or admin token.

---

## 3. Exact commands (no values shown)

Run from the repo root with `GH_TOKEN` exported (section 0). None of these
print a secret.

### 3.1 Required for the daily watch and the live eval

```bash
# WATCH_DATABASE_URL <- local DATABASE_URL (the Lakebase direct endpoint).
# Piped from .env so the value never lands in argv or shell history.
grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_DATABASE_URL -R Hussain0327/amneal

# OPENAI_API_KEY gates watch-daily.yml AND openai-eval.yml (the latter
# inherited by ci.yml's required "openai-eval / eval" check). Missing it
# hard-fails both.
grep -E '^OPENAI_API_KEY=' .env | head -1 | cut -d= -f2- \
  | gh secret set OPENAI_API_KEY -R Hussain0327/amneal
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

If your flyctl version wraps the `FlyV1 ...` token in log lines, mint it into
a restricted temp file instead, pipe that in, then shred it. Never under the
repo, never committed:

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
#   Slack -> Apps -> Incoming Webhooks -> Add to a channel -> copy the URL
#   into a restricted temp file, then:
gh secret set SLACK_WEBHOOK_URL -R Hussain0327/amneal < /path/to/slack_webhook.txt

# WATCH_HEALTHCHECK_URL <- a healthchecks.io ping URL (dead-man's-switch).
gh secret set WATCH_HEALTHCHECK_URL -R Hussain0327/amneal < /path/to/hc_url.txt

# PROD_HEALTH_URL <- https://amneal.fly.dev/health
gh secret set PROD_HEALTH_URL -R Hussain0327/amneal < /path/to/prod_health_url.txt
```

`PROD_HEALTH_URL` is not really secret, but storing it as one keeps
`uptime-eval.yml`'s "no invented URLs" contract intact and avoids hardcoding
a host in the workflow.

### 3.4 Required: match the Watch embedding profile to prod

Watch embeds new FDA chunks into the same named vector space serving reads
from. Set `WATCH_ACTIVE_EMBEDDING_PROFILE` to the exact profile id prod's
`RETRIEVAL_EMBEDDING_PROFILE` Fly secret carries (read it with
`fly secrets list -a amneal` for the name, `GET /settings` for the
effective value), never `legacy`:

```bash
printf '%s' '<profile-id-matching-prod>' \
  | gh secret set WATCH_ACTIVE_EMBEDDING_PROFILE -R Hussain0327/amneal
```

Confirm `OPENAI_API_KEY` is also set (3.1). Both are required before the
first successful crawl; a mismatch on the profile id does not fail the shape
check, it silently writes vectors nobody's queries will read.

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

- The **"skipped (secret not configured)"** step did NOT run. If you still
  see "WATCH_DATABASE_URL secret not set", the secret did not land (3.1).
- **"preflight OpenAI config"** passed. It hard-fails on an empty
  `OPENAI_API_KEY`.
- **"preflight Watch embedding profile"** passed, proving
  `WATCH_ACTIVE_EMBEDDING_PROFILE` is present and shaped like
  `ep_<32 lowercase hex>`.
- **"validate registered embedding profile"** (`regwatch init-db`) passed
  before crawl. This checks the immutable fingerprint and database readiness;
  it is not a live OpenAI request.
- **"regwatch watch"** exited 0: crawl, match, ingest, durable alerts,
  digest. A stamp-mismatch refusal here means deploy the pending migration
  first (2.2). It is the guard working, not a secret problem.
- **"verify embedding-profile coverage"** passed after Watch and reported
  zero pending chunks.
- The advisory **"threshold sweep"** may fail without failing the job
  (`continue-on-error: true`). By design, it never blocks the crawl.
- If Slack is set, **"slack digest on success"** posts when there are
  alerts. A quiet day posts nothing, which is success and not a wiring
  failure.
- If `WATCH_HEALTHCHECK_URL` is set, the success ping fired. To exercise the
  failure-alert path, do it out of band on a throwaway branch. Do not
  sabotage the prod-DB run to test alerting.

`deploy.yml` is `workflow_run`-triggered off a green `ci` on `main`, not
dispatchable. Its first execution is the next green push to `main`:

```bash
gh run list --workflow=deploy.yml -R Hussain0327/amneal --limit 1
gh run view <run-id> -R Hussain0327/amneal --log
```

`uptime-eval.yml` is dispatchable:

```bash
gh workflow run uptime-eval.yml -R Hussain0327/amneal
```

Expect "health OK". "PROD_HEALTH_URL secret not set" means the optional
secret is absent, so the probe skipped without failing.

---

## 5. Rollback

Each secret is independently removable. Deleting one immediately reverts
that workflow to its dormant path, except `OPENAI_API_KEY`, which has no
dormant path: deleting it turns both `watch-daily.yml` and the required
`openai-eval / eval` check into hard failures, not clean skips. None of
these deletions touch prod data.

```bash
# Disable CD (the next green main no longer deploys). Also stops
# machine-monitor.yml's check.
gh secret delete FLY_API_TOKEN -R Hussain0327/amneal

# Re-dormant the daily watch (job returns to the clean skip path):
gh secret delete WATCH_DATABASE_URL -R Hussain0327/amneal

# Silence alerting and uptime:
gh secret delete SLACK_WEBHOOK_URL -R Hussain0327/amneal
gh secret delete WATCH_HEALTHCHECK_URL -R Hussain0327/amneal
gh secret delete PROD_HEALTH_URL -R Hussain0327/amneal
```

- Deleting `WATCH_DATABASE_URL` is the cleanest kill-switch for the cron: the
  next scheduled run takes the green "skipped" path with no failure noise.
- Deleting `FLY_API_TOKEN` is the cleanest CD kill-switch. It does NOT roll
  back an already-shipped release. For that use the levers in
  [`DEPLOY.md`](DEPLOY.md) section 6.1.
- Deleting `OPENAI_API_KEY` does not silence anything; it turns the required
  eval check red and stops the daily watch from crawling at all. Only do this
  deliberately, and expect to fix it before the next PR can merge.
- **Rotation.** `fly tokens revoke` the old deploy token, mint a new one,
  re-run 3.2. For `OPENAI_API_KEY` and `WATCH_DATABASE_URL`, rotate the
  upstream credential, update `.env`, re-run the matching 3.1 command. There
  is no in-place edit: setting the same name overwrites.

---

## 6. Expected steady state

- `watch-daily` runs at 07:17 UTC, executes the real pipeline, writes durable
  alerts to the shared prod database, and pings healthchecks.io or Slack if
  those are configured.
- `deploy.yml` auto-deploys the exact CI-validated commit to the `amneal`
  Fly app on every green `main` push.
- `openai-eval` runs the blocking live eval on every push to `main` and PRs
  that touch the retrieval or synthesis path, scoring the production arm
  (`prose: true, selective: true, assert_prod_mode: true`, `ci.yml:88-91`).
- `machine-monitor` runs every 10 minutes and checks that no `app`-group Fly
  machine is stopped. Whether it is actually enabled at the repository level
  cannot be proven from this checkout; committed YAML is not evidence that
  monitoring is running.
- `uptime-eval` probes prod `/health` every 30 minutes when `PROD_HEALTH_URL`
  is set, otherwise it skips.

Related: [`DEPLOY.md`](DEPLOY.md) for the full deploy and incident runbook,
[`CI_CD.md`](CI_CD.md) for the `ci` gate these secrets sit downstream of,
[`CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md) for the full variable, flag and
secret inventory.
