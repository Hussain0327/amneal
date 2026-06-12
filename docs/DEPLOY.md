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
     OPENFDA_API_KEY="..."        # optional
   ```

3. Deploy:

   ```bash
   fly deploy
   ```

   The entrypoint runs `regwatch init-db` on boot: on the already-migrated
   Postgres it verifies the alembic stamp matches head and starts; on a
   mismatch it refuses to start (that's the signal you deployed code without
   migrating, or vice versa).

4. Verify:

   ```bash
   curl -s https://regwatch-api.fly.dev/health | python -m json.tool
   ```

   Expect `"status": "ok"`, `db.ok true`, embedding provider `openai`,
   `llm.key_present true`, and a non-zero corpus count.

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

## 6. Rollback / recovery

- **App rollback:** `fly releases` → `fly deploy --image <previous image ref>`
  (or Railway → Deployments → Redeploy previous). The DB schema is stamped;
  an older app that expects an older head will refuse to boot rather than
  corrupt data.
- **Data rollback:** Supabase **Database → Backups** (daily on paid plans) —
  restore, then redeploy. The SQLite/Chroma snapshot in
  `/tmp/regwatch-snapshot` (and the untouched live `data/`) remain a full
  fallback: unset `DATABASE_URL` and the stack runs SQLite mode exactly as
  before the migration.
- **Re-migration:** rerun `scripts/migrate_to_supabase.py --truncate` from the
  snapshot at any time; it is idempotent under `--truncate` and refuses
  otherwise.
