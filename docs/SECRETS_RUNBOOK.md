# SECRETS_RUNBOOK -- the GitHub Actions secret surface

This runbook was written when the repo had **zero** Actions secrets and the
capabilities below were dormant. That is no longer the state: the secrets ARE
configured (13 secret names are referenced across the four workflows --
`ci.yml`, `deploy.yml`, `watch-daily.yml`, `uptime-eval.yml`). No workflow
declares an `environment:`, so only **repository** secrets are in play. Keep
this runbook for RE-provisioning (rotation, a fork, a new repo) and as the
inventory of what each secret gates. Current state:

1. **Daily watch is LIVE.** `watch-daily.yml` (cron 07:17 UTC) runs the real
   crawl -> match -> ingest -> durable alerts -> digest pipeline against the
   shared prod DB via `WATCH_DATABASE_URL` + `WATCH_OPENAI_API_KEY`. While
   `WATCH_DATABASE_URL` is unset the job skips cleanly -- that is the
   fork/kill-switch behavior (see §5), not the prod state.
2. **CD is LIVE.** `deploy.yml` fires after every green `ci` run on `main` and
   ships the exact validated commit (docker build + Trivy re-scan +
   `scripts/fly-deploy.sh`) using `FLY_API_TOKEN`. As of 2026-07-29 14:41 UTC
   it auto-deployed `f579030` successfully -- `main` == prod.
3. **The repo-wide `OPENAI_API_KEY` is not configured.** Verified secret names
   on 2026-07-30 were `FLY_API_TOKEN`, `OPENFDA_API_KEY`,
   `WATCH_DATABASE_URL`, and `WATCH_OPENAI_API_KEY`. Without the repo-wide key,
   `ci.yml` skips its provider-backed seed + eval steps. Their current pass/fail
   result is unverified; the previously cited `0.917` came from a separate
   threshold-sweep metric and was not `run_eval.refusal_accuracy`. Establish a
   controlled baseline before making paid live evaluation deployment-gating.

> **This runbook provisions secrets. It does NOT print, echo, or commit any secret
> VALUE.** Every command below sets a value from your local environment or a fresh
> token, by name only. Do not paste a secret into a terminal where it lands in
> shell history; prefer the `gh secret set NAME < file` / stdin forms shown.

---

## 0. Prerequisites

- `gh` CLI authenticated against `Hussain0327/amneal` with a token that can write
  Actions secrets. Per this repo's setup, `gh auth login` may be rejected (token
  lacks `read:org`); export a `repo`-scoped token per call instead:

  ```bash
  GH_TOKEN=$(git credential fill <<<$'protocol=https\nhost=github.com\n' | sed -n 's/^password=//p')
  export GH_TOKEN
  ```

- `fly` (flyctl) authenticated as a deployer of the `amneal` Fly app (needed only
  to MINT the deploy token; the token itself is what CI uses).
- Your local `.env` populated with the production values (this runbook maps each
  secret to a local key by NAME). Confirm the key names exist without reading
  values:

  ```bash
  grep -oE '^[A-Z_]+=' .env | tr -d '='
  ```

- Confirm the current state (names + updated-at only; `gh` never returns
  values):

  ```bash
  gh secret list -R Hussain0327/amneal
  ```

---

## 1. Secret -> workflow inventory

Every `${{ secrets.X }}` reference across the four workflows, what it gates, where
its value comes from, and whether it is required.

| Secret | Workflow / step it gates | Value source (local key / command) | Req? |
|---|---|---|---|
| `WATCH_DATABASE_URL` | `watch-daily.yml` -> `env.DATABASE_URL`; gates ALL real steps (checkout, deps, `regwatch watch`, threshold sweep). Unset => job skips. | `.env` key **`DATABASE_URL`** -- the prod Supabase **session pooler** URI WITH `?sslmode=require` | **Required** (watch) |
| `WATCH_OPENAI_API_KEY` | `watch-daily.yml` -> mapped to the job's `OPENAI_API_KEY`: `regwatch watch` ingest embeds every chunk (`EMBEDDING_PROVIDER=openai`); also the advisory threshold sweep. The "preflight WATCH_OPENAI_API_KEY" step hard-fails the run when it is unset/empty. | `.env` key **`OPENAI_API_KEY`** (the production key), stored under the WATCH_ name | **Required** (watch) |
| `OPENAI_API_KEY` (repo-wide) | `ci.yml` -> provider-backed seed + `eval --check-thresholds`. **Currently unset:** the latest CI run skipped both steps, so pass/fail is unverified. Enabling it adds paid live work whose result gates CD; baseline it first. | OpenAI project key approved for CI, if this gate is enabled | Not configured |
| `FLY_API_TOKEN` | `deploy.yml` -> the whole release path: docker build, Trivy re-scan, then `bash scripts/fly-deploy.sh` (the retrying flyctl wrapper). Unset => deploy step fails; no release. | **`fly tokens create deploy`** (mint fresh; NOT in `.env`) | **Required** (CD) |
| `WATCH_ACTIVE_EMBEDDING_PROFILE` | `watch-daily.yml` -> `env.ACTIVE_EMBEDDING_PROFILE`; embedding-profile parity with prod. While prod runs the `legacy` profile this is unset and the profile block is inert. | the promoted profile id (matches prod's `ACTIVE_EMBEDDING_PROFILE`) | Staged |
| `WATCH_QWEN_EMBEDDING_BASE_URL` / `_TOKEN` / `_MODEL` / `_REVISION` | `watch-daily.yml` -> profile provider credentials. Guarded by "preflight embedding-profile credentials" (hard-fails when the profile is set but BASE_URL/TOKEN/MODEL are missing) and "verify embedding-profile coverage" (post-ingest assertion that the active profile still covers every chunk). | the Databricks/Qwen serving-endpoint credentials, mirroring prod | Staged |
| `OPENFDA_API_KEY` | `watch-daily.yml` -> `env.OPENFDA_API_KEY`; only raises the openFDA rate limit. | `.env` key **`OPENFDA_API_KEY`** | Optional |
| `SLACK_WEBHOOK_URL` | `watch-daily.yml` -> "notify slack on failure" AND "slack digest on success" (a successful run posts the day's alert digest; a quiet day posts nothing). Unset => both steps no-op. | Slack incoming-webhook URL (no `.env` key; provision in Slack) | Optional |
| `WATCH_HEALTHCHECK_URL` | `watch-daily.yml` -> success ping + `/fail` ping (dead-man's-switch for a cron that NEVER starts). Unset => both pings no-op. | healthchecks.io-style ping URL (no `.env` key; provision in healthchecks.io) | Optional |
| `PROD_HEALTH_URL` | `uptime-eval.yml` -> "probe /health" (`if: env.PROD_HEALTH_URL != ''`). Unset => 30-min uptime probe skips. | `https://amneal.fly.dev/health` | Optional |

Notes:

- `watch-daily.yml` also hard-codes non-secret env (`REQUIRE_DATABASE_URL=true`,
  `EMBEDDING_PROVIDER=openai`, `SENTRY_ENVIRONMENT=production`) -- nothing to
  provision there.
- There is **no** `SENTRY_DSN` Actions secret. Sentry is configured as a **Fly**
  secret on the app (see `docs/DEPLOY.md` step 3.2), not in CI. Do not add it here.
- The GitHub list above is not the whole secret surface. The **Fly** app also
  carries `LLM_PROVIDER`, `DATABRICKS_LLM_BASE_URL` / `DATABRICKS_LLM_TOKEN` /
  `DATABRICKS_LLM_MODEL`, `INTERNAL_RAG_TOKEN`, and -- staged, deliberately
  unset until the embedding flip -- `D1_ENFORCED` / `D1_ALLOWED_LLM_MODELS`.
  Those are provisioned with `fly secrets set`, never in Actions; see
  `docs/DEPLOY.md` step 3.2.
- "Optional" means the workflow no-ops cleanly without it (forks stay green). But
  the three optional **alerting** secrets are what make a silent failure visible --
  provision `SLACK_WEBHOOK_URL` and/or `WATCH_HEALTHCHECK_URL` and `PROD_HEALTH_URL`
  unless you have an equivalent external monitor.

---

## 2. PREFLIGHT / SAFETY CHECKLIST (do this BEFORE setting anything)

This pipeline writes the **shared production database** (durable alerts, stream 1).
Treat `WATCH_DATABASE_URL` and `FLY_API_TOKEN` as prod-touching. Verify each box.

### 2.1 WATCH_DATABASE_URL must be the SESSION POOLER URL with sslmode=require

- [ ] The value is the **session pooler** endpoint, not the transaction pooler.
      Session pooler is host `aws-0-<region>.pooler.supabase.com` on **port 5432**.
      The transaction pooler (**port 6543**) must NOT be used: `regwatch watch`
      holds a long-lived session and runs alembic-stamp checks; transaction-pooling
      mode breaks both. (`docs/DEPLOY.md` step 1.3.)
- [ ] The URL ends with **`?sslmode=require`**. The `watch-daily.yml` header and
      stream 2 require TLS to the pooler.
- [ ] Any special characters in the password are **URL-encoded** (`@`->`%40`,
      `#`->`%23`, ...). The app normalizes the scheme to `postgresql+psycopg://`
      itself, so a bare `postgresql://` prefix is fine.
- [ ] It is the **production** project (ref `xvhbfmoynibkcghazzxc`), matching the
      app the API runs against -- the watch and the API must share one DB. (If you
      are wiring a staging crawl instead, that is a different decision; this runbook
      targets the prod cron.)

You can sanity-check the SHAPE of your local value without revealing the password:

```bash
# prints only the host:port and the sslmode query -- never the password
sed -n 's#.*@##p' <<<"$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)" \
  | sed -E 's#^([^/]+/[^?]*\??).*#\1#'
```

Expect something like `aws-0-<region>.pooler.supabase.com:5432/postgres?` and you
should already know `sslmode=require` is appended.

### 2.2 Schema-stamp guard (read before enabling the cron)

`watch-daily.yml` does **not** migrate. `regwatch watch` boots through
`_init_postgres`, which **refuses to start** if the live DB stamp != this
checkout's alembic head. If a migration was merged but not yet deployed, the cron
fails loudly rather than advancing the live schema ahead of the running API
machines (the 2026-06-18 crash class). Before the first manual run, confirm `main`
is deployed and the DB is at head (the §3 dispatch run will surface any mismatch as
a loud failure, not a silent skip).

### 2.3 FLY_API_TOKEN re-enables auto-deploy -- confirm main == live prod

- [ ] **Setting `FLY_API_TOKEN` turns CD back on.** The very next push to `main`
      that goes green in `ci` will trigger `deploy.yml` -> `flyctl deploy` to
      **live prod**.
- [ ] **CD is currently ENABLED and `main` == prod** (as of 2026-07-29 14:41
      UTC: `deploy.yml` auto-deployed `f579030` successfully, applying
      migration 0016 `query_log.latency_ms`). The general rule still applies
      whenever you RE-enable after a gap: confirm whether `main` carries an
      un-deployed migration before setting the token. The release_command
      (`alembic upgrade head`) will apply it automatically on the first
      auto-deploy -- make sure that is what you intend, or deploy manually
      first (`docs/DEPLOY.md` step 3) so the first automated run is a no-op.
- [ ] Mint a **deploy-scoped** token (`fly tokens create deploy`), not a full
      org/admin token -- least privilege for CI.

---

## 3. EXACT commands (no values shown)

Run from the repo root with `GH_TOKEN` exported (§0). None of these print a secret.

### 3.1 Required for the daily watch

```bash
# WATCH_DATABASE_URL <- local DATABASE_URL (the prod session-pooler URL, sslmode=require).
# Piped from .env so the value never appears as an argv/history entry.
grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_DATABASE_URL -R Hussain0327/amneal

# WATCH_OPENAI_API_KEY <- local OPENAI_API_KEY (production key), stored under
# the watch-scoped name. The repo-wide OPENAI_API_KEY is a separate decision:
# setting it activates ci.yml's paid provider-backed seed/eval and makes that
# result deployment-gating. Establish and review a controlled baseline first.
grep -E '^OPENAI_API_KEY=' .env | head -1 | cut -d= -f2- \
  | gh secret set WATCH_OPENAI_API_KEY -R Hussain0327/amneal
```

> If your `.env` values are quoted, strip the surrounding quotes before piping
> (e.g. add `| tr -d '"'` / `| tr -d "'"`). Re-run §2.1's shape check first so you
> know exactly what `cut -d= -f2-` yields.

### 3.2 Required for CD

```bash
# Mint a fresh deploy-scoped Fly token, then store it WITHOUT printing it.
# `fly tokens create deploy` prints the token to stdout; pipe it straight into gh.
fly tokens create deploy --app amneal \
  | gh secret set FLY_API_TOKEN -R Hussain0327/amneal
```

If your flyctl version prints a `FlyV1 ...` token wrapped in extra log lines, mint
it into a restricted-permission temp file in the scratchpad instead, pipe that file
in, then shred it -- never under the repo, never committed:

```bash
umask 077
tmp="$(mktemp)"                      # use your session scratchpad if you prefer
fly tokens create deploy --app amneal > "$tmp"
# (manually verify $tmp holds exactly the token line; trim any log preamble)
gh secret set FLY_API_TOKEN -R Hussain0327/amneal < "$tmp"
rm -P "$tmp" 2>/dev/null || rm -f "$tmp"
```

### 3.3 Optional -- alerting + uptime (recommended)

```bash
# OPENFDA_API_KEY <- local OPENFDA_API_KEY (only raises the openFDA rate limit).
grep -E '^OPENFDA_API_KEY=' .env | head -1 | cut -d= -f2- \
  | gh secret set OPENFDA_API_KEY -R Hussain0327/amneal

# SLACK_WEBHOOK_URL <- a Slack incoming-webhook URL you create in Slack.
#   Slack -> Apps -> Incoming Webhooks -> Add to a channel -> copy the URL into a
#   restricted temp file, then:
gh secret set SLACK_WEBHOOK_URL -R Hussain0327/amneal < /path/to/slack_webhook.txt

# WATCH_HEALTHCHECK_URL <- a healthchecks.io ping URL (dead-man's-switch).
gh secret set WATCH_HEALTHCHECK_URL -R Hussain0327/amneal < /path/to/hc_url.txt

# PROD_HEALTH_URL <- the live amneal Fly app /health URL, e.g. https://amneal.fly.dev/health
gh secret set PROD_HEALTH_URL -R Hussain0327/amneal < /path/to/prod_health_url.txt
```

`PROD_HEALTH_URL` is not strictly secret, but storing it as a secret keeps
`uptime-eval.yml`'s "no invented URLs" contract intact and avoids hard-coding a host
in the workflow.

### 3.4 Staged -- embedding-profile secrets (do NOT set until the flip)

When prod's `ACTIVE_EMBEDDING_PROFILE` flips off `legacy` (the Qwen3 move, see
`docs/OPEN_MODEL_ROLLOUT.md` and `docs/DATA_RESIDENCY_D1.md`), the watch cron
must flip IN THE SAME CHANGE -- otherwise its ingest writes chunks the prod
profile does not cover and "verify embedding-profile coverage" fails. Five
secrets, set together:

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
# Optional; only if prod pins a revision:
# gh secret set WATCH_QWEN_EMBEDDING_REVISION -R Hussain0327/amneal < /path/to/revision.txt
```

Until then leave all five unset: the profile block in `watch-daily.yml` is inert
while `WATCH_ACTIVE_EMBEDDING_PROFILE` is absent, and the cron keeps embedding
via `WATCH_OPENAI_API_KEY` (the last deliberate D1 residual -- see
`docs/DATA_RESIDENCY_D1.md`).

### 3.5 Confirm what was set (names only -- `gh` never returns values)

```bash
gh secret list -R Hussain0327/amneal
```

Expect the required pair (`WATCH_DATABASE_URL`, `WATCH_OPENAI_API_KEY`),
`FLY_API_TOKEN`, and whichever optionals you provisioned. The list shows names +
updated-at, never values.

---

## 4. VALIDATE the first watch run (workflow_dispatch)

Do NOT wait for the 07:17 UTC cron. `watch-daily.yml` declares `workflow_dispatch`,
so trigger one manual run and read its logs -- this is the safe first write to the
shared prod DB.

```bash
# Kick a manual run on the default branch.
gh workflow run watch-daily.yml -R Hussain0327/amneal

# Find the run id (give Actions a few seconds to register it), then watch it.
gh run list --workflow=watch-daily.yml -R Hussain0327/amneal --limit 1
gh run watch <run-id> -R Hussain0327/amneal --exit-status

# Inspect the log -- you want the REAL pipeline, not the skip path.
gh run view <run-id> -R Hussain0327/amneal --log
```

Success criteria:

- The **"skipped (secret not configured)"** step did NOT run (its `if:` is now
  false). If you still see "WATCH_DATABASE_URL secret not set", the secret did not
  land -- re-check §3.1.
- The **"preflight WATCH_OPENAI_API_KEY"** step passed. It hard-fails the run when
  the key is unset/empty -- a failure here means §3.1's second command did not land.
- The **"preflight embedding-profile credentials"** and **"verify
  embedding-profile coverage"** steps passed. While prod runs the `legacy`
  profile (no `WATCH_ACTIVE_EMBEDDING_PROFILE` set) both are inert; once the
  profile flips (§3.4) they become real gates -- missing credentials or a chunk
  the active profile does not cover fails the run loudly.
- The **"regwatch watch"** step ran and exited 0 (crawl -> match -> ingest -> durable
  alerts -> digest). A stamp-mismatch refusal here means deploy the pending
  migration first (§2.2); it is the guard working, not a secret problem.
- The advisory **"threshold sweep"** step may fail without failing the job
  (`continue-on-error: true`) -- that is by design and never blocks the crawl.
- If Slack is set, the **"slack digest on success"** step posts the day's alert
  digest when there are alerts (a quiet day posts nothing -- that is success, not
  a wiring failure).
- If you provisioned `WATCH_HEALTHCHECK_URL`, the success ping fired; if Slack is
  set, no failure post was sent on a clean run. To exercise the failure-alert path
  deliberately, do it OUT OF BAND (e.g. a temporary bad value on a throwaway branch
  run) -- do not sabotage the prod-DB run to test alerting.

For `deploy.yml`: it is `workflow_run`-triggered off a green `ci` on `main`, not
`workflow_dispatch`. Its first execution is the **next green push to `main`**, which
(per §2.3) is a no-op re-deploy. Watch that run:

```bash
gh run list --workflow=deploy.yml -R Hussain0327/amneal --limit 1
gh run view <run-id> -R Hussain0327/amneal --log    # flyctl deploy step now has a token
```

For `uptime-eval.yml`: dispatch it once to confirm the probe path (it also has
`workflow_dispatch`):

```bash
gh workflow run uptime-eval.yml -R Hussain0327/amneal
```

Expect "health OK"; "PROD_HEALTH_URL secret not set" means the optional secret is
absent (probe skipped, no failure).

---

## 5. ROLLBACK

Each secret is independently removable; deletion immediately reverts the workflow to
its dormant/no-op path (none of these deletions touch prod data).

```bash
# Disable CD (next green main will no longer deploy; deploy step fails loudly):
gh secret delete FLY_API_TOKEN -R Hussain0327/amneal

# Re-dormant the daily watch (job returns to the clean skip path):
gh secret delete WATCH_DATABASE_URL -R Hussain0327/amneal
gh secret delete WATCH_OPENAI_API_KEY -R Hussain0327/amneal

# Silence alerting / uptime again:
gh secret delete SLACK_WEBHOOK_URL -R Hussain0327/amneal
gh secret delete WATCH_HEALTHCHECK_URL -R Hussain0327/amneal
gh secret delete PROD_HEALTH_URL -R Hussain0327/amneal
gh secret delete OPENFDA_API_KEY -R Hussain0327/amneal
```

Notes:

- Deleting `WATCH_DATABASE_URL` is the cleanest kill-switch for the cron: the next
  scheduled run takes the green "skipped" path with no failure noise.
- Deleting `FLY_API_TOKEN` is the cleanest CD kill-switch. It does **not** roll back
  an already-shipped release -- for that use the Fly image-pin / data-restore levers
  in `docs/DEPLOY.md` §6.1.
- **Rotation** (compromise or routine): `fly tokens revoke` the old deploy token,
  mint a new one, and re-run §3.2 (`gh secret set` overwrites in place). For
  `WATCH_OPENAI_API_KEY` / `WATCH_DATABASE_URL`, rotate the upstream credential,
  update `.env`, and re-run the matching §3.1 set command -- there is no in-place
  "edit", setting the same name overwrites.

---

## 6. After provisioning -- expected steady state

- `watch-daily` runs daily at 07:17 UTC, executes the real pipeline, writes durable
  alerts to the shared prod DB, and (if configured) pings healthchecks.io / posts to
  Slack on failure.
- `deploy.yml` auto-deploys the exact CI-validated commit to the `amneal` Fly app on
  every green `main` push.
- `uptime-eval` probes prod `/health` every 30 minutes and fails (alerting via the
  Actions run) when it is not `200 / "status":"ok"`.

Cross-references: `docs/DEPLOY.md` (full cutover + §6 operations: rollback, uptime,
restore drill), `docs/CI_CD.md` (the `ci` gate these secrets sit downstream of).
