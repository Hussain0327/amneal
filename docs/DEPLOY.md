# DEPLOY - Supabase + Fly.io + Vercel runbook

This is the production cutover runbook, written to be executed top-to-bottom.
Live shape (Go proxy on the public edge since the 2026-07 phase-3 flip):

```text
browser -- https --> Vercel (Next.js, regwatch/frontend, https://amneal.vercel.app)
                        |  /api/* rewrite proxy (API_PROXY_TARGET)
                        v
                     Go proxy (Fly app "amneal", process group "proxy", public :8080)
                        |  6PN private network (UPSTREAM_URL)
                        v
                     FastAPI ("regwatch serve", dual-stack :8000, process group "app")
                        |  DATABASE_URL (psycopg v3, session pooler)
                        v
                     Supabase Postgres 17 + pgvector (structured store + chunks)

embeddings: OpenAI text-embedding-3-small (1536)
LLM: Databricks Model Serving since 2026-07-28 (LLM_PROVIDER=databricks,
     endpoint alias workspace.default.regwatch, served model gpt-oss-20b-080525,
     ONE model for all roles; called from the Python tier). OpenAI is the
     rollback path: `fly secrets set LLM_PROVIDER=openai` reverts in ~60s.
auth: custom cookie sessions (unchanged) -- Supabase Auth is NOT used
```

Postgres + pgvector is the only datastore (R5 deleted the SQLite/Chroma
dual-mode): `DATABASE_URL` is mandatory and the app refuses to boot without
it. `EMBEDDING_PROVIDER` must be `openai` (the `chunk` table is
`vector(1536)`; the API fails fast on a dimension mismatch). Moving off
OpenAI embeddings goes through the embedding-profile mechanism
(`ACTIVE_EMBEDDING_PROFILE` / `EMBEDDING_SHADOW_PROFILE`, migration 0015),
not by editing `EMBEDDING_PROVIDER`; prod still runs the legacy OpenAI
profile today.

Prerequisites on your machine: `uv`, `docker`, `flyctl`, `vercel` CLI
(optional -- the dashboard works too), repo checked out, and the production
secret values: `OPENAI_API_KEY` (embeddings + the LLM rollback path),
`DATABRICKS_LLM_BASE_URL` / `DATABRICKS_LLM_TOKEN` / `DATABRICKS_LLM_MODEL`
(the live LLM), and `INTERNAL_RAG_TOKEN` (auth for the Go proxy's internal
RAG relay to the Python tier).

---

## 1. Supabase project

1. Go to <https://supabase.com/dashboard> → **New project**.
   - Organization: yours. Name: `regwatch-prod`. Region: closest to the API
     host you'll pick in step 3 (e.g. `us-east-1`).
   - **Database password**: click *Generate a password*, save it in the
     password manager NOW — you need it for the connection string.
   - Click **Create new project** and wait for provisioning (~2 min).
2. Verify the vector extension: **Database → Extensions**, search `vector` —
   on current Supabase projects pgvector 0.8.x is already installed in the
   `extensions` schema. If it shows disabled, toggle it on (schema
   `extensions`). Our bootstrap also runs
   `CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions`, which
   no-ops when present.
3. Get the connection string: top bar **Connect** → tab **Session pooler**
   (NOT the transaction pooler on port 6543 — the long-lived API and the
   migration script need session mode). Copy the URI:

   ```text
   postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```

   Replace `[YOUR-PASSWORD]`; URL-encode any special characters in it
   (`@` → `%40`, `#` → `%23`, …). A bare `postgresql://` is fine — the app and
   the migration script normalize it to `postgresql+psycopg://` themselves.
4. Export it for the next step:

   ```bash
   export SUPABASE_DB_URL='postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres'
   ```

**RLS note (do not "fix" this):** the bootstrap enables row level security on
every `public` table with NO policies. That is deliberate deny-all for
Supabase's auto-exposed Data API roles (`anon`/`authenticated`) — the REST
surface sees nothing. Our API connects as the `postgres` role, which bypasses
RLS. Do not add policies and do not disable RLS from the dashboard.

## 2. Migrate the data (historical, SQLite + Chroma → Postgres + pgvector)

This step no longer applies: R5 deleted the SQLite/Chroma dual-mode, so there
is no local datastore left to cut over from — Postgres + pgvector is the only
datastore from a fresh checkout onward. `scripts/migrate_to_supabase.py` (the
one-time SQLite/Chroma-to-Supabase copier) and its `scripts/restore_drill.sh`
wrapper were deleted in the same change. For the original cutover procedure
and its verification steps, see git history (the pre-R5 revision of this
file). A PG-native `pg_dump`/restore drill is the noted open follow-up (see
§6.4).

## 3. API on Fly.io (primary)

The slim image (no torch) + `EMBEDDING_PROVIDER=openai` is the production
combination.

1. App + config. The Fly app is `amneal`, and `fly.toml` is COMMITTED at the
   repo root -- it is authoritative, load-bearing config (two process groups,
   the migration release_command, the step-5 flag pin). Do not regenerate it
   with `fly launch`; a fresh checkout deploys with the committed file as-is.
   Abridged excerpt -- the real `fly.toml` is heavily commented and is the
   source of truth:

   ```toml
   app = "amneal"
   primary_region = "iad"
   kill_timeout = 30                        # drain in-flight SSE on deploys

   [build]
     [build.args]
       INSTALL_LOCAL_EMBEDDINGS = "false"   # slim image: no torch

   [deploy]
     release_command = "alembic upgrade head"   # migrates BEFORE the roll

   [processes]
     app = "regwatch serve"      # dual-stack uvicorn on :8000
     proxy = "regwatch-proxy"    # Go proxy, holds the public port

   [env]
     EMBEDDING_PROVIDER = "openai"
     AUTH_COOKIE_SECURE = "true"
     CORS_ALLOW_ORIGINS_CSV = "https://amneal.vercel.app"
     SENTRY_ENVIRONMENT = "production"
     TRUST_PROXY_HEADERS = "true"     # Go login limiter keys on Fly-Client-IP
     REQUIRE_DATABASE_URL = "true"    # read by the GO PROXY only (see step 2)
     GO_NATIVE_QUERY = "true"         # step-5 pin: proxy serves POST /query natively
     UPSTREAM_URL = "http://app.process.amneal.internal:8000"

   [http_service]
     processes = ["proxy"]
     internal_port = 8080
     force_https = true
     auto_stop_machines = false
     min_machines_running = 2
     [[http_service.checks]]     # end-to-end GET /health through the proxy
       # interval/timeout/grace + method GET, path /health -- see fly.toml

   [checks.app_health]           # deploy-gates the now-private app group on :8000
     # processes ["app"], http GET /health -- see fly.toml
   ```

   Three tests guard this file against well-meaning "simplifications":
   `tests/test_trust_proxy_fly_toml.py`, `tests/test_boot_command_drift.py`,
   and `tests/test_dual_stack_bind.py`. Read the comments in `fly.toml`
   before touching any guarded line.

2. Secrets (never in `fly.toml`):

   ```bash
   fly secrets set \
     DATABASE_URL="$SUPABASE_DB_URL" \
     OPENAI_API_KEY="sk-..." \                        # embeddings + LLM rollback path
     LLM_PROVIDER="databricks" \                      # live LLM since 2026-07-28
     DATABRICKS_LLM_BASE_URL="https://<workspace-host>/serving-endpoints" \
     DATABRICKS_LLM_TOKEN="..." \
     DATABRICKS_LLM_MODEL="workspace.default.regwatch" \
     INTERNAL_RAG_TOKEN="..." \                       # Go proxy -> Python RAG relay auth
     SENTRY_DSN="https://...ingest.sentry.io/..." \   # B4: error tracking
     WHITEPAPER_TEMPLATE_URL="https://..." \          # signed Supabase Storage URL; see note below
     OPENFDA_API_KEY="..."        # optional
   ```

   `D1_ENFORCED` / `D1_ALLOWED_LLM_MODELS` are STAGED, deliberately unset:
   they arm the runtime served-model residency guard and stay unarmed until
   the embedding flip closes the last D1 leak.

   `DATABASE_URL` is mandatory everywhere since R5 (Postgres + pgvector is
   the only datastore), so if this secret is missing the app **refuses to
   boot** rather than losing the audit trail (B1). A `REQUIRE_DATABASE_URL`
   flag DOES still exist -- in `fly.toml` `[env]`, read by the GO PROXY only:
   with it set, a proxy machine refuses to serve auth when `DATABASE_URL` is
   missing. The Python side no longer reads it. `SENTRY_DSN` is strongly
   recommended: without it the app still boots but logs a loud
   `sentry_disabled_in_production` warning, and 500s go only to stderr (B4).

3. Deploy. The NORMAL path is automatic: every green `ci` run on `main`
   triggers `deploy.yml`, which rebuilds the image, re-scans it with Trivy,
   and ships via `scripts/fly-deploy.sh`. Fly then runs the committed
   `[deploy] release_command = "alembic upgrade head"` in a one-off machine
   BEFORE the rolling replace, so schema-advancing releases migrate
   themselves -- there is no manual pre-migration step on the normal path.

   Manual deploys (`fly deploy`, or `bash scripts/fly-deploy.sh` from the
   exact commit) are for recovery only. The entrypoint runs `regwatch
   init-db` on boot: it verifies the alembic stamp matches head and starts;
   on a mismatch it refuses to start (that's the signal image and schema came
   from different commits). Recovery for that refusal -- from a checkout of
   the DEPLOYED commit on `main`, never an unmerged branch (the 2026-07-07
   outage rule):

   ```bash
   DATABASE_URL="$SUPABASE_DB_URL" uv run alembic upgrade head
   ```

   then deploy again. A message like `stamped at alembic revision '0007_...'
   but this build expects '0008_...'` means exactly this command.

4. Verify:

   ```bash
   curl -s https://amneal.fly.dev/health | python -m json.tool
   ```

   Expect `"status": "ok"`, `db.ok true`, **`db.dialect "postgresql"`** (B1 —
   if you see `"sqlite"` here the prod stack is on the wrong datastore),
   embedding provider `openai`, **`llm.provider "databricks"`** (the
   2026-07-28 flip; `"openai"` here means the rollback secret is set),
   `llm.key_present true`, and a non-zero corpus count.

5. Provision users (CLI-only, no self-signup; password is prompted). Target
   an `app`-group machine: the `proxy` machines run only the Go binary and
   have no Python CLI, and a bare `fly ssh console` may land on one.

   ```bash
   fly ssh console -s -C "regwatch create-user analyst@amneal.com --name 'CRA Analyst'"
   # at the -s picker, choose a machine from the "app" process group
   ```

Notes:

- **White-paper template:** `CRA White Paper Template May 2026 - Raja.docx` is
  gitignored (internal artifact) and deliberately **not** baked into the image.
  The image defaults `WHITEPAPER_TEMPLATE_PATH` to
  `/app/data/templates/cra_white_paper_template.docx` (under the data volume),
  and the entrypoint creates that directory. To enable real-template fill:
    - **Compose:** drop the file at `./data/templates/cra_white_paper_template.docx`
      (the `./data:/app/data` mount makes it visible at the default path — no
      config change needed).
    - **Fly (this runbook attaches no volume):** set the
      `WHITEPAPER_TEMPLATE_URL` secret to a long-lived signed Supabase Storage
      URL for the template -- the render path lazily fetches and caches it on
      first use (`src/regwatch/whitepaper/template_fetch.py`); any fetch
      failure falls back loudly, never a 500. Alternatives: bake it into a
      *private* overlay image (`FROM regwatch:... ; COPY
      cra_white_paper_template.docx /app/data/templates/`) so it never enters
      this public repo, or attach a Fly volume at `/app/data/templates`.
  Absent the file, `/whitepaper/docx` returns a structurally-equivalent document
  stamped `(generated without the official CRA template file)` and logs a
  `whitepaper_template_missing` warning — never a silent or failed render.
- **`data/` inside the container is scratch** in Postgres mode (raw PDFs from
  ingest runs land there). Q&A/whitepaper serving needs only Postgres; don't
  attach a volume unless you run ingest/watch on this machine.
- **Watch:** the production Watch path is the `watch-daily.yml` GitHub Actions
  cron (the sole scheduler; Dagster was removed in R5). Configure
  `WATCH_DATABASE_URL`, `WATCH_OPENAI_API_KEY`, optional `OPENFDA_API_KEY`,
  optional `WATCH_HEALTHCHECK_URL` / `SLACK_WEBHOOK_URL`, and -- the moment a
  non-legacy embedding profile is promoted in prod -- the five parity secrets
  `WATCH_ACTIVE_EMBEDDING_PROFILE` and
  `WATCH_QWEN_EMBEDDING_{BASE_URL,TOKEN,MODEL,REVISION}`, all per
  `docs/SECRETS_RUNBOOK.md`. Keep ad hoc `regwatch watch` runs for break-glass
  recovery, not as the normal production schedule.

### 3-alt. API on Railway (HISTORICAL -- does not match the current prod shape)

This alternative predates the two-process-group deployment (Go proxy on the
public edge + private app group) and the committed `fly.toml`: Railway would
run the image CMD only -- uvicorn exposed directly, no Go edge, no
`GO_NATIVE_QUERY` path, no proxy-tier login limiting. Kept for reference; prod
is Fly.

```bash
railway init                       # link repo; Railway auto-detects the Dockerfile
railway variables --set DATABASE_URL="$SUPABASE_DB_URL" \
  --set OPENAI_API_KEY="sk-..." \
  --set EMBEDDING_PROVIDER=openai \
  --set AUTH_COOKIE_SECURE=true \
  --set CORS_ALLOW_ORIGINS_CSV="https://amneal.vercel.app"
railway up
```

In the Railway dashboard: service → **Settings → Networking → Generate
Domain** (note the URL for `API_PROXY_TARGET`), and **Settings → Health
check** → path `/health`. Build args: **Settings → Build** →
`INSTALL_LOCAL_EMBEDDINGS=false` (the default).

## 4. Frontend on Vercel

The Next.js app proxies `/api/*` server-side to the API
(`regwatch/frontend/next.config.mjs`), so the browser only ever talks to the
Vercel origin — the HttpOnly session cookie is set on and sent to the Vercel
domain and forwarded through the rewrite. This is why `AUTH_COOKIE_SECURE=true`
just works: Vercel terminates TLS.

1. <https://vercel.com/new> → **Import** the GitHub repo.
2. **Root Directory**: click *Edit* and set `regwatch/frontend` (the build
   will fail without this). Framework preset: Next.js (auto-detected).
3. **Environment Variables** (Production):
   - `API_PROXY_TARGET` = `https://amneal.fly.dev`.
     Server-side only — no `NEXT_PUBLIC_` prefix.
4. Click **Deploy**. Note the production URL
   (`https://amneal.vercel.app` in prod).
5. If the final Vercel URL differs from what you set in
   `CORS_ALLOW_ORIGINS_CSV` in step 3, update it:
   `fly secrets set` won't take env vars from `[env]` — edit `fly.toml` and
   `fly deploy` (or `railway variables --set ... && railway up`).

CLI alternative:

```bash
cd regwatch/frontend
vercel link
vercel env add API_PROXY_TARGET production   # paste the API URL
vercel deploy --prod
```

## 5. Smoke checklist (run every deploy)

Do these in order; stop at the first failure.

1. `curl https://<api-host>/health` → 200, `status: ok`, `db.ok: true`,
   embedding `openai`, `llm.key_present: true`, corpus count > 0.
2. Supabase **Table Editor**: `chunk` count equals the migration's verified
   count; `alembic_version` shows the current head.
3. Supabase **Advisors/Database**: RLS enabled on all public tables, no
   policies — expected (deny-all for the Data API); ignore "RLS enabled, no
   policy" infos.
4. Open the Vercel URL → login page renders (no console errors).
5. Wrong-password login → "invalid email or password"; no cookie set.
6. **Analyst logs in** (provisioned user) → lands inside the unified shell with
   the Ask chat in view (one sidebar, the "Under review" product-scope bar
   across all four surfaces — Ask, Assemble, Watch, White Paper); reload keeps
   the session (cookie survives).
7. **Sets product scope**: from the "Under review" bar's picker, resolve an RLD
   name + application number (POST `/resolve`) → the scope pins to the canonical
   `{normalized_name, six-digit appl}` and the URL gains `?rp=&appl=` (shareable,
   survives reload). A mismatched application 422s and leaves the scope unset
   (refuse over guess); `/resolve` writes NO audit row.
8. **Asks a question**: "What bioequivalence studies does FDA recommend for
   albuterol sulfate metered aerosol?" → right-aligned user bubble, then a cited
   answer with `[PSG_xxxxxx, p.N]` citation chips that open the FDA source (full
   snippets under the Sources disclosure); an off-corpus question still refuses.
9. **Populates a white paper**: White Paper tab → RLD name + NDA number (e.g.
   one of the seeded products) → cells fill with provenance
   (source/locator/fetched-at), manual cells say "Analyst input required" (a
   successful populate also sets product scope).
10. **Downloads the docx** → file opens in Word, populated cells and the
    Provenance appendix are present (rendered verbatim from the reviewed
    populate result).

If 6–10 pass, the deploy is good.

## 6. Operations

Day-2 runbook: rollback, uptime monitoring, and the monthly staging restore
drill. Everything here is operator-driven — agents and CI never touch
production data paths.

### 6.1 Rollback

Independent levers, least to most drastic. Pick the smallest one that
covers the failure.

**Lever 0 -- config flip (fastest; ~60s, no redeploy, no CI cycle).** Fly
secrets take precedence over `fly.toml` `[env]`, so a bad provider or flag
flip reverts with a secret:

```bash
fly secrets set LLM_PROVIDER=openai -a amneal    # revert the 2026-07-28 Databricks LLM flip
fly secrets set GO_NATIVE_QUERY=false -a amneal  # proxy relays POST /query to Python again
```

1. **App rollback (bad deploy, schema unchanged).** List releases, note the
   image ref of the last good one, pin it:

   ```bash
   fly releases --image                       # last good release's image ref
   fly deploy --image <previous-image-ref>    # e.g. registry.fly.io/amneal:deployment-...
   ```

   > **Image and config are VERSION-COUPLED since the phase-2 dual-stack
   > listener (docs/GO_PROXY_ROLLOUT.md).** `fly deploy --image <old>` sends
   > your CURRENT fly.toml with that old image. Across the phase-2 boundary
   > that combination does not boot: fly.toml says `[processes].app =
   > "regwatch serve"` and a pre-phase-2 image has no `serve` subcommand, so
   > every machine exits non-zero. To roll back ACROSS phase 2, deploy the
   > reverted CHECKOUT instead -- `git revert` + push to main (CI -> deploy.yml),
   > or `bash scripts/fly-deploy.sh` from the reverted commit in an emergency --
   > so the image and `[processes].app` come from the same commit. The same
   > constraint makes `--strategy immediate` safe ONLY when image and fly.toml
   > are from one commit. Within a single phase this lever is unaffected.
   >
   > The STEP-5 boundary added a second coupling of the same class: `fly.toml`
   > `[env]` pins `GO_NATIVE_QUERY = "true"` (PR #127), so `fly deploy --image
   > <old>` hands the pin to a proxy binary from before the CompleteQuery
   > cutover. To roll back across step 5, deploy the reverted CHECKOUT, or
   > neutralize the pin first with `fly secrets set GO_NATIVE_QUERY=false -a
   > amneal` (lever 0 -- secrets override `[env]`).

   (Railway: **Deployments → ⋮ → Redeploy** on the previous build.) The DB
   schema is alembic-stamped and verified on boot: an older app that expects
   an older head **refuses to start** rather than running against a newer
   schema. If the bad deploy also migrated the schema, an image rollback
   alone is not enough — restore data (lever 2) or roll forward with a fix.

2. **Data rollback (bad write / bad migration).** Supabase **Database →
   Backups** (daily on paid plans) → restore the last good backup, then
   restart the API (`fly apps restart amneal`). A restore overwrites
   the whole database — stop the API first, and accept that sessions, audit
   rows, and any other writes since that backup are lost. Verify with the §5
   smoke checklist before calling it done.

3. ~~**App-level fallback (Postgres unusable).**~~ **(removed in R5)** —
   unsetting `DATABASE_URL` to fall back to a local SQLite/Chroma copy was
   possible before R5 deleted that dual-mode; the app now refuses to boot
   without `DATABASE_URL`, so there is no local fallback lever. If Postgres
   is unusable, lever 2 (restore from a Supabase backup) is the only option
   short of standing up a new Postgres instance.

### 6.2 Uptime

Point an external monitor at the one open endpoint:

- **URL:** `GET https://<api-host>/health` (no auth).
- **Expected:** HTTP `200` with compact JSON shaped like

  ```json
  {"status":"ok","components":{"db":{"ok":true},"vector_store":{"ok":true,"corpus_count":1795},"llm":{"provider":"databricks","key_present":true},"embedding":{"provider":"openai"}},"warnings":[]}
  ```

  (the `vector_store` key reports pgvector, the only vector store since R5).
  When the DB or vector store is unreachable the API
  returns **503** with `"status":"unhealthy"`, so a plain HTTP-status monitor
  already catches real outages.
- **UptimeRobot** (free tier): HTTP(s) monitor on the URL, 5-minute interval;
  optionally a keyword monitor that alerts when `"status":"ok"` is *absent*
  from the body. **healthchecks.io** alternative: a cron on any box you
  control —
  `curl -fsS --max-time 20 https://<api-host>/health >/dev/null && curl -fsS https://hc-ping.com/<your-uuid>`.
- **Alert threshold:** 2 consecutive failures (~10 minutes at a 5-minute
  interval). Single blips happen during deploys; sustained failure pages.

**CI backstop — `.github/workflows/uptime-eval.yml`:** a scheduled GitHub
Action curls the production health URL every 30 minutes and fails the run
when the response is not 200 / `"status":"ok"`. It is driven entirely by a
repository secret — **Settings → Secrets and variables → Actions → New
repository secret**: name `PROD_HEALTH_URL`, value
`https://<api-host>/health`. While the secret is unset the workflow skips
cleanly (no failures, no invented URLs). This complements — does not
replace — the external monitor: GitHub cron schedules can lag or pause on
inactive repos.

### 6.3 Refusal-threshold revalidation

`REFUSAL_SCORE_THRESHOLD` (default `0.30`) gates the `low_top_score` refusal in
`grounded_qa.ask`: a question is refused when the best retrieved passage's cosine
score is below it. That `0.30` was calibrated in the **bge-384** cosine era.
Production now embeds with **OpenAI text-embedding-3-small (1536)** — a different
vector space with a different cosine-similarity distribution — so `0.30` is
**PROVISIONAL** until it is revalidated in the prod space. CI cannot do this:
CI runs pgvector against a disposable local Postgres (`TEST_DATABASE_URL`)
with the 1536-dim `echo` test provider, not real OpenAI embeddings.

**Where it runs:** the daily `watch-daily` job (§ the watch cron) now emits an
**advisory, non-gating** revalidation. After the crawl, it re-runs the gold set
through the real `ask` path in this job's prod embedding space
(`EMBEDDING_PROVIDER=openai` + the live `DATABASE_URL`) and uploads
**`threshold_sweep.json`** as a workflow artifact (Actions run → **Artifacts** →
`threshold-sweep`). It is read-only w.r.t. the safety path: it never changes
`REFUSAL_SCORE_THRESHOLD` and never fails the crawl — `continue-on-error: true`
means a sweep hiccup, or a recommendation that differs from `0.30`, can never
block ingestion or alerting.

**Latest verified artifact (2026-07-30):** watch run
[30531864530](https://github.com/Hussain0327/amneal/actions/runs/30531864530)
produced a real OpenAI-1536 + pgvector sweep. Its six must-answer rows scored
0.812-0.896, but all five must-refuse rows stopped before retrieval and had no
cosine score. The one must-clarify row correctly clarified and was
misclassified by the old harness. Thus the reported `0.917`
`current_decision_accuracy` was not `run_eval.refusal_accuracy`, and the old
`0.00` recommendation did not calibrate the cutoff. The corrected harness
excludes must-clarify rows and returns no recommendation without scored rows on
both sides. See [`EVAL_STATUS.md`](EVAL_STATUS.md).

**D1 note (deliberate residual):** this sweep AND the watch cron's change-day
ingest embeds still call OpenAI (via `WATCH_OPENAI_API_KEY`), even though prod
LLM inference moved to Databricks on 2026-07-28. That is the known remaining
D1 leak; the fix goes through the embedding-profile mechanism
(`ACTIVE_EMBEDDING_PROFILE` / `EMBEDDING_SHADOW_PROFILE`) once the Databricks
embedding endpoint is wired into the app.

**How to read `threshold_sweep.json`** (the same content is printed as a table in
the step log):

- `distributions.must_answer` and `distributions.must_refuse` — the two
  per-question max-passage-cosine distributions (`min`/`median`/`max`,
  `n_scored`). A threshold is calibratable only when both groups have scored
  rows. When they do, a healthy threshold sits **above** the must-refuse max and
  **at or below** the must-answer min.
- `counts.must_clarify_excluded` - resolver clarification cases retained for
  audit but excluded from the numeric cutoff curve.
- `recommendation.recommended` vs `recommendation.current` (0.30), with
  `recommendation.rationale`. The rule: pick the cutoff that **maximizes
  refuse_recall without refusing anything 0.30 currently answers**
  (`answer_retention` floor). `recommendation.provisional`/`overlap` is `true`
  when the two distributions overlap — no clean separator exists, so the
  recommendation is the best available tradeoff, not a fix.
- Two `0.30` **pathology flags** — these are the action triggers:
  - `recommendation.wrongly_refused_at_current` — must-answer questions whose
    best score is already `< 0.30` (over-refused TODAY in the prod space).
  - `recommendation.leaking_at_current` — must-refuse questions whose best score
    is already `>= 0.30` (leaking through TODAY).

**Decision procedure (human-in-the-loop):** a human reviews the recommendation
and the two pathology lists. **Only if warranted** — a clean (`overlap: false`)
recommendation that differs from `0.30`, or a non-empty pathology list — update
the live threshold by setting the `REFUSAL_SCORE_THRESHOLD` env var
(`fly secrets set REFUSAL_SCORE_THRESHOLD=... -a amneal`).
The sweep only **recommends**; it never changes the value. `0.30` stays
PROVISIONAL until a human has reviewed a prod-space sweep with scored positive
and negative distributions. There is no gate and no auto-tune — over-tuning the
refusal cutoff trades directly against INV safety, so it is an explicit
operator decision.

### 6.4 Staging + restore drill (monthly, ~30 min)

A backup you have never restored is a hope, not a backup.

**One-time setup:** create a second free Supabase project,
`regwatch-staging`, same region as prod (§1 steps 1–3; the free tier holds
this corpus comfortably). Save its session-pooler URL next to the prod one —
they differ by project ref.

**Monthly drill:**

1. Get a restorable copy into staging via the Backup path (the only
   recovery lever now — the SQLite/Chroma snapshot path and its
   `scripts/restore_drill.sh` wrapper were deleted in R5 along with the
   dual-mode datastore; see git history for the old snapshot-based drill):
   Supabase prod → **Database → Backups** → download the latest daily
   backup and restore it into `regwatch-staging` (dashboard restore, or
   `psql "<staging-url>" < backup.sql` run by the operator).

   A staging DB still stamped at an older revision (e.g. by last month's
   drill, run before a schema-advancing release) needs advancing before the
   restore is usable:

   ```bash
   DATABASE_URL='<staging-url>' uv run alembic upgrade head
   ```

   **Open follow-up:** a PG-native `pg_dump`/`pg_restore` drill script
   (replacing the deleted `restore_drill.sh`) is not yet built; until then
   this step is a manual dashboard restore.
2. Point a local API at staging and smoke it:
   `DATABASE_URL='<staging-url>' EMBEDDING_PROVIDER=openai uv run uvicorn
   regwatch.api.main:app --port 8099`, then run the §5 checklist against
   `localhost:8099` (step 1 directly; steps 6–9 via curl or a local
   frontend with `API_PROXY_TARGET=http://localhost:8099`).
3. Record date + result (a line in the team log is enough). Anything that
   fails here failed on staging — fix it now, not mid-incident.

**(removed in R5):** `scripts/restore_drill.sh` used to guard against pointing
the snapshot-restore path at the production project ref
(`xvhbfmoynibkcghazzxc`) before any network call, refusing to run against it
or against a bare non-loopback IP host. That script and its dedicated test
(`tests/test_restore_drill.py`) were deleted along with the SQLite/Chroma
migration tooling; the same "never target prod by accident" discipline still
applies operator-side to the manual dashboard restore above. See git history
for the guard's original implementation if a scripted equivalent is rebuilt.
