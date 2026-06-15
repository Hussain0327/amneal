# DEPLOY — Supabase + Fly.io/Railway + Vercel runbook

This is the production cutover runbook, written to be executed top-to-bottom.
Target shape:

```text
browser ── https ──> Vercel (Next.js, regwatch/frontend)
                        │  /api/* rewrite proxy (API_PROXY_TARGET)
                        ▼
                     API (FastAPI, slim Docker image, Fly.io or Railway)
                        │  DATABASE_URL (psycopg v3, session pooler)
                        ▼
                     Supabase Postgres 17 + pgvector (structured store + chunks)

embeddings: OpenAI text-embedding-3-small (1536) · LLM: OpenAI (existing config)
auth: custom cookie sessions (unchanged) — Supabase Auth is NOT used
```

Mode rule (no separate toggle): `DATABASE_URL` empty → SQLite + Chroma exactly
as today. `DATABASE_URL` set → Postgres + pgvector, and `EMBEDDING_PROVIDER`
must be `openai` (the `chunk` table is `vector(1536)`; the API fails fast on a
dimension mismatch).

Prerequisites on your machine: `uv`, `docker`, `flyctl` (or `railway`),
`vercel` CLI (optional — the dashboard works too), repo checked out, and the
production `OPENAI_API_KEY`.

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

## 2. Migrate the data (SQLite + Chroma → Postgres + pgvector)

Run from the repo on the machine that has the production `data/` directory.

1. **Quiesce writers, then snapshot** the live corpus (the script must NEVER
   point at live paths). Stop the API, any `regwatch watch`/ingest run, and
   the Dagster services first — a plain file copy of an open SQLite database
   (`data/regwatch.db`, and Chroma's internal `chroma.sqlite3`) taken
   mid-write can be torn or silently miss the latest rows, and step 4's
   verification table compares the TARGET against this SNAPSHOT, never
   against live `data/` — a stale or torn snapshot would pass it.

   ```bash
   # with the API / watcher / Dagster stopped:
   mkdir -p /tmp/regwatch-snapshot
   sqlite3 data/regwatch.db ".backup /tmp/regwatch-snapshot/regwatch.db"
   cp -R data/chroma /tmp/regwatch-snapshot/chroma
   ```

   `sqlite3 .backup` produces a transactionally consistent copy (a plain `cp`
   of the db + journal can capture them at different instants). Copy
   `data/chroma` only while nothing has the Chroma client open — Chroma has
   no equivalent online-backup hook.

2. Make sure deps are synced and the OpenAI key is available (re-embedding
   every chunk costs real money but text-embedding-3-small is cheap —
   ~$0.02 per 1M tokens; the full PSG corpus is a few dollars at most):

   ```bash
   uv sync --extra llm
   export OPENAI_API_KEY=sk-...
   ```

3. Rehearse the relational copy first (no embedding spend):

   ```bash
   uv run python scripts/migrate_to_supabase.py \
     --sqlite /tmp/regwatch-snapshot/regwatch.db \
     --chroma /tmp/regwatch-snapshot/chroma \
     --database-url "$SUPABASE_DB_URL" \
     --skip-embed
   ```

   Expect: per-table `copied N rows` lines, a verification table where every
   row says `OK`, and exit code 0.

4. Real run (wipes the rehearsal rows, then copies + re-embeds everything):

   ```bash
   uv run python scripts/migrate_to_supabase.py \
     --sqlite /tmp/regwatch-snapshot/regwatch.db \
     --chroma /tmp/regwatch-snapshot/chroma \
     --database-url "$SUPABASE_DB_URL" \
     --truncate
   ```

   The chunk step prints `chunks: N/total re-embedded + inserted` as it goes.
   The run is only a success if the final verification table is all `OK`
   (including `chunk (chroma -> pgvector)`) and it prints
   `migration complete — all counts verified`. ANY mismatch exits nonzero —
   rerun with `--truncate`; never ship a partial copy.

5. Spot-check in Supabase: **Table Editor** → `psg_document`, `chunk`,
   `user` — row counts must match the verification table.

Idempotency rules: the script refuses a non-empty target without
`--truncate`; `--truncate` makes reruns safe. Sequences are reset
(`setval(max(id)+1)`) after the copy, so new inserts can't collide with
copied ids.

## 3. API on Fly.io (primary)

The slim image (no torch) + `EMBEDDING_PROVIDER=openai` is the production
combination.

1. Create the app (one-time). From the repo root:

   ```bash
   fly launch --no-deploy --name regwatch-api --region iad
   ```

   Replace the generated `fly.toml` contents with:

   ```toml
   app = "regwatch-api"
   primary_region = "iad"

   [build]
     [build.args]
       INSTALL_LOCAL_EMBEDDINGS = "false"   # slim image: no torch

   [env]
     EMBEDDING_PROVIDER = "openai"
     AUTH_COOKIE_SECURE = "true"            # API is behind HTTPS
     API_HOST = "0.0.0.0"
     API_PORT = "8000"
     # The Vercel production origin(s); keep this tight.
     CORS_ALLOW_ORIGINS_CSV = "https://regwatch.vercel.app"

   [http_service]
     internal_port = 8000
     force_https = true
     auto_stop_machines = false   # cookie sessions are DB-backed, but keep warm
     min_machines_running = 1

     [[http_service.checks]]
       interval = "30s"
       timeout = "10s"
       grace_period = "30s"
       method = "GET"
       path = "/health"
   ```

   (`fly.toml` is environment config, not committed app code — keep it out of
   the repo or commit it, your call; nothing in the image depends on it.)

2. Secrets (never in `fly.toml`):

   ```bash
   fly secrets set \
     DATABASE_URL="$SUPABASE_DB_URL" \
     OPENAI_API_KEY="sk-..." \
     SENTRY_DSN="https://...ingest.sentry.io/..." \   # B4: error tracking
     OPENFDA_API_KEY="..."        # optional
   ```

   `DATABASE_URL` is mandatory in production: `fly.toml` sets
   `REQUIRE_DATABASE_URL = "true"`, so if this secret is missing the app
   **refuses to boot** rather than silently running on an ephemeral SQLite
   disk and losing the audit trail (B1). `SENTRY_DSN` is strongly recommended:
   without it the app still boots but logs a loud `sentry_disabled_in_production`
   warning, and 500s go only to stderr (B4).

3. Deploy:

   ```bash
   fly deploy
   ```

   The entrypoint runs `regwatch init-db` on boot: on the already-migrated
   Postgres it verifies the alembic stamp matches head and starts; on a
   mismatch it refuses to start (that's the signal you deployed code without
   migrating, or vice versa).

   **Schema-advancing releases** (a new file in `migrations/versions/` since
   the last deploy) need the database advanced FIRST — from any machine with
   a repo checkout (the alembic env resolves `DATABASE_URL` itself):

   ```bash
   DATABASE_URL="$SUPABASE_DB_URL" uv run alembic upgrade head
   ```

   The same one-liner is the recovery for the boot refusal above: a message
   like `stamped at alembic revision '0007_…' but this build expects
   '0008_…'` means exactly this command, then deploy again.

4. Verify:

   ```bash
   curl -s https://regwatch-api.fly.dev/health | python -m json.tool
   ```

   Expect `"status": "ok"`, `db.ok true`, **`db.dialect "postgresql"`** (B1 —
   if you see `"sqlite"` here the prod stack is on the wrong datastore),
   embedding provider `openai`, `llm.key_present true`, and a non-zero corpus
   count.

5. Provision users (CLI-only, no self-signup; password is prompted):

   ```bash
   fly ssh console -C "regwatch create-user analyst@amneal.com --name 'CRA Analyst'"
   ```

Notes:

- **White-paper template:** `CRA White Paper Template May 2026 - Raja.docx` is
  gitignored and not in the image, so `/whitepaper/docx` generates the
  structurally-equivalent document from scratch. To ship the real template,
  add a `COPY` for it (or mount a volume) and point `WHITEPAPER_TEMPLATE_PATH`
  at it.
- **`data/` inside the container is scratch** in Postgres mode (raw PDFs from
  ingest runs land there). Q&A/whitepaper serving needs only Postgres; don't
  attach a volume unless you run ingest/watch on this machine.
- **Watch/Dagster** stay out of scope for this deploy; run `regwatch watch`
  ad hoc via `fly ssh console` if needed.

### 3-alt. API on Railway (alternative)

```bash
railway init                       # link repo; Railway auto-detects the Dockerfile
railway variables --set DATABASE_URL="$SUPABASE_DB_URL" \
  --set OPENAI_API_KEY="sk-..." \
  --set EMBEDDING_PROVIDER=openai \
  --set AUTH_COOKIE_SECURE=true \
  --set API_HOST=0.0.0.0 --set API_PORT=8000 \
  --set CORS_ALLOW_ORIGINS_CSV="https://regwatch.vercel.app"
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
   - `API_PROXY_TARGET` = `https://regwatch-api.fly.dev` (or the Railway URL).
     Server-side only — no `NEXT_PUBLIC_` prefix.
4. Click **Deploy**. Note the production URL (e.g.
   `https://regwatch.vercel.app`).
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
6. **Analyst logs in** (provisioned user) → lands on the chat UI; reload keeps
   the session (cookie survives).
7. **Asks a question**: "What bioequivalence studies does FDA recommend for
   albuterol sulfate metered aerosol?" → cited answer with `[PSG_xxxxxx, p.N]`
   citations that open the FDA source; an off-corpus question still refuses.
8. **Populates a white paper**: White Paper tab → RLD name + NDA number (e.g.
   one of the seeded products) → cells fill with provenance
   (source/locator/fetched-at), manual cells say "Analyst input required".
9. **Downloads the docx** → file opens in Word, populated cells and the
   Provenance appendix are present.

If 6–9 pass, the deploy is good.

## 6. Operations

Day-2 runbook: rollback, uptime monitoring, and the monthly staging restore
drill. Everything here is operator-driven — agents and CI never touch
production data paths.

### 6.1 Rollback

Three independent levers, least to most drastic. Pick the smallest one that
covers the failure.

1. **App rollback (bad deploy, schema unchanged).** List releases, note the
   image ref of the last good one, pin it:

   ```bash
   fly releases --image                       # last good release's image ref
   fly deploy --image <previous-image-ref>    # e.g. registry.fly.io/regwatch-api:deployment-…
   ```

   (Railway: **Deployments → ⋮ → Redeploy** on the previous build.) The DB
   schema is alembic-stamped and verified on boot: an older app that expects
   an older head **refuses to start** rather than running against a newer
   schema. If the bad deploy also migrated the schema, an image rollback
   alone is not enough — restore data (lever 2) or roll forward with a fix.

2. **Data rollback (bad write / bad migration).** Supabase **Database →
   Backups** (daily on paid plans) → restore the last good backup, then
   restart the API (`fly apps restart regwatch-api`). A restore overwrites
   the whole database — stop the API first, and accept that sessions, audit
   rows, and any other writes since that backup are lost. Verify with the §5
   smoke checklist before calling it done.

3. **App-level fallback (Postgres unusable — last resort).** Unset
   `DATABASE_URL` (and set `EMBEDDING_PROVIDER` to match the local store) and
   the stack runs SQLite + Chroma from the pre-cutover `data/` snapshot
   exactly as before the migration. **Honest caveat — this is continuity,
   not rollback:** everything written to Postgres after the cutover (users,
   sessions, `query_log` audit rows, newly ingested PSGs) does NOT exist in
   the SQLite copy, and everything written during the fallback will not be
   in Postgres. The two stores diverge from the moment of cutover. Returning
   to Postgres later means a fresh SQLite/Chroma snapshot and a re-run of
   `scripts/migrate_to_supabase.py --truncate` (idempotent under
   `--truncate`; refuses a non-empty target otherwise).

### 6.2 Uptime

Point an external monitor at the one open endpoint:

- **URL:** `GET https://<api-host>/health` (no auth).
- **Expected:** HTTP `200` with compact JSON shaped like

  ```json
  {"status":"ok","components":{"db":{"ok":true},"chroma":{"ok":true,"corpus_count":1795},"llm":{"provider":"openai","key_present":true},"embedding":{"provider":"openai"}},"warnings":[]}
  ```

  (the `chroma` key reports the *active* vector store — pgvector when
  `DATABASE_URL` is set). When the DB or vector store is unreachable the API
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

### 6.3 Staging + restore drill (monthly, ~30 min)

A backup you have never restored is a hope, not a backup.

**One-time setup:** create a second free Supabase project,
`regwatch-staging`, same region as prod (§1 steps 1–3; the free tier holds
this corpus comfortably). Save its session-pooler URL next to the prod one —
they differ by project ref.

**Monthly drill:**

1. Get a restorable copy into staging. Either:
   - **Backup path (preferred — exercises the real recovery lever):**
     Supabase prod → **Database → Backups** → download the latest daily
     backup and restore it into `regwatch-staging` (dashboard restore, or
     `psql "<staging-url>" < backup.sql` run by the operator).
   - **Snapshot path:** re-run the migration from the SQLite/Chroma snapshot
     through the wrapper:

     ```bash
     DATABASE_URL='postgresql://postgres.<STAGING-ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres' \
       ./scripts/restore_drill.sh /tmp/regwatch-snapshot
     ```

     The wrapper runs `scripts/migrate_to_supabase.py --truncate` against the
     target and prints the migrate script's per-table verification table.
     This step passes only when every row is `OK` and the exit code is 0.
     `DRILL_SKIP_EMBED=1` rehearses the relational copy with no OpenAI
     embedding spend (`chunk` stays empty — fine for a schema/data drill,
     not for smoke step 7).

     A staging DB still stamped at an older revision (e.g. by last month's
     drill, run before a schema-advancing release) makes this step refuse
     with `stamped at alembic revision … but this build expects …` —
     `--truncate` clears rows, not schema, so it cannot self-heal. Advance
     staging first, then re-run the drill:

     ```bash
     DATABASE_URL='<staging-url>' uv run alembic upgrade head
     ```
2. Point a local API at staging and smoke it:
   `DATABASE_URL='<staging-url>' EMBEDDING_PROVIDER=openai uv run uvicorn
   regwatch.api.main:app --port 8099`, then run the §5 checklist against
   `localhost:8099` (step 1 directly; steps 6–9 via curl or a local
   frontend with `API_PROXY_TARGET=http://localhost:8099`).
3. Record date + result (a line in the team log is enough). Anything that
   fails here failed on staging — fix it now, not mid-incident.

**Hard guard:** `scripts/restore_drill.sh` refuses to run — before any
network call or subprocess, reserved exit code 4 — when the target URL
contains the production project ref `xvhbfmoynibkcghazzxc`. The ref appears
in the pooler username (`postgres.<ref>@…pooler.supabase.com`) and in the
direct-connection host (`db.<ref>.supabase.co`), so it is matched anywhere
in the URL, case-insensitively and after percent-decoding (an encoded ref
cannot slip past). Bare non-loopback IP hosts are refused too — they carry
no ref for the guard to vet (loopback stays allowed, so a local docker
Postgres still works as a rehearsal target). Residual limit: the guard is
textual; a production target reached through a hostname alias containing
neither the ref nor an IP is out of its scope. The drill can truncate
staging; it can never truncate production. Covered by
`tests/test_restore_drill.py`.
