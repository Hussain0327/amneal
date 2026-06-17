# Supabase + Auth Audit — 2026-06-17

**Project:** `xvhbfmoynibkcghazzxc` (RegWatch / amneal production DB)
**Method:** Live inspection via Supabase MCP (read-only queries against the running project) + an adversarially-verified static code audit of the connection, schema, RLS, and auth wiring.
**Auditor note:** Read-only. No data was changed.

---

## TL;DR (plain English)

- **Supabase is fully connected and set up correctly.** The live database is healthy, all your tables and data are present, and the app was actively reading/writing it the same day this audit ran. Vector search (pgvector) is correctly configured and populated. **Nothing is broken.**
- **The one real thing to decide is logins.** You have *two* login systems in the project; only one is actually used. The unused one (Supabase's built-in Auth) has leftover test accounts and a never-expiring session — harmless clutter, but worth cleaning up.
- A short list of **optional security tightenings** is at the bottom.

---

## 1. Connection — ✅ correct and live

| Check | Result |
|---|---|
| Project status | Postgres **17.6**, region **us-east-1**, **ACTIVE_HEALTHY** |
| Connection target | **session pooler** `…pooler.supabase.com:5432` — correct choice for SQLAlchemy's persistent pool + prepared statements (not the 6543 transaction pooler) |
| Driver | normalized to `postgresql+psycopg://` — psycopg **v3** (pinned 3.3.4) — `config/settings.py:179` |
| Engine | shared pool, `pool_pre_ping=True`, size 5 + overflow 5 (survives pooler recycling) — `src/regwatch/store/db.py:176` |
| Prod boot guard | `REQUIRE_DATABASE_URL=true` → app refuses to fall back to SQLite in prod — `db.py:190` |
| Live activity | last `query_log` write and last chat message both **same day as this audit**; ~7 live app connections via Supavisor — **the app is connected and working right now** |

**Architecture note:** This project uses **Supabase as managed Postgres + pgvector**, *not* as a typical `supabase-js` app. There is no Supabase Storage, no Edge Functions, and (intentionally) no Supabase Auth in the live path. The backend talks to Postgres directly as the `postgres` role. This is a valid, clean pattern.

---

## 2. Schema & pgvector — ✅ all verified on the live DB

- **All 16 tables present.** `alembic_version` is stamped at head **`0008_token_cost_feedback`** — schema is fully migrated.
- **Data is populated:** `psg_document` 1795, `psg_version` 1801, `be_requirement` 1795, `chat_message` 230, `query_log` 554, `chunk` **10,747**.
- **Vector store verified live:**
  - `vector` extension **0.8.0** installed in the **`extensions`** schema (not `public`) — matches the code's expectation.
  - `chunk.embedding` column is **`vector(1536)`** — matches `EMBEDDING_PROVIDER=openai` (text-embedding-3-small).
  - **100% of chunk rows (10,747/10,747) have non-null embeddings**, across 1,794 documents.
  - HNSW index is live: `ix_chunk_embedding_hnsw USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)` — **byte-for-byte matches the code DDL** in `db.py:90`. Plus the 4 btree filter indexes (`appl_no`, `doc_id`, `version_id`, `normalized_name`).

---

## 3. Security posture — ✅ strong (and proven, not assumed)

- **Row-Level Security (RLS) "deny-all" is real and load-bearing.** All 16 public tables have RLS **enabled with 0 policies**, owned by `postgres` (`force_rls=false`). The app connects as `postgres` (table owner → bypasses RLS and works normally), while `anon`/`authenticated` get **zero rows**.
- **Proven live** — using the project's real anon key against the running Data API:
  ```
  GET /rest/v1/user          → []
  GET /rest/v1/psg_document  → []
  GET /rest/v1/chunk         → []
  ```
  PostgREST (the Data API) **is running** (`authenticator/postgrest` connections are live), so this protection is actually doing work — and it holds. Even though `anon`/`authenticated` are granted broad table privileges by Supabase default, RLS blocks every row.
- The 16 `rls_enabled_no_policy` entries in Supabase's **security advisor are INFO-level and expected** — they *are* the intended deny-all design, not a defect.
- **Bonus defense found only in the live DB:** an event trigger `ensure_rls` → function `rls_auto_enable()` auto-enables RLS on **any new `public` table** at creation time. It's `SECURITY DEFINER` but correctly locked down (`search_path` pinned to `pg_catalog`; EXECUTE granted only to `postgres`/`service_role`, **not** `anon`). This closes the "someone creates a new table and forgets RLS" gap.
  - ⚠️ **Caveat:** this trigger exists **only in the live database — it is not in the repo or migrations.** If the DB were ever rebuilt from source, it would not exist. See P1 below.
- **No secrets leaked:** no `service_role`/secret key anywhere in the repo, frontend, or git history. The real `.env` (with the DB password) and the frontend `.env.local` are both git-ignored and were never committed. `uv.lock` is committed and DB deps are pinned.

---

## 4. Auth — ⚠️ the one real issue: two login systems, only one used

### A) Custom cookie-session login — ✅ this is the LIVE, real one
- `POST /auth/login` issues an **HttpOnly `regwatch_session` cookie**. Token is a **256-bit CSPRNG value, stored only as sha256** (never plaintext); passwords use **bcrypt**; sessions expire server-side and can be revoked; all protected routes funnel through one `Depends(require_user)` check.
- Live state: `public.user` = **2 active users**, `public.auth_session` = **3 active sessions**. This is what the app actually uses.
- The frontend confirms it: `regwatch/frontend/lib/api.ts` posts to `/auth/*` with `credentials:"include"`, and **`supabase-js` is not installed or imported anywhere** in the frontend.

### B) Supabase Auth — ⚠️ half-staged, NOT used by the app, leftover clutter
- `auth.users` = **2 leftover accounts**:
  - `r***@gmail.com` — self-signed-up **Jun 12**, signed in once.
  - `r***@amneal.com` — created **Jun 16**, **never logged in**.
- `auth.sessions` = **1 session with `not_after = NULL` → it never expires.**
- The frontend `.env.local` has `NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`. These correctly point at this project, the key is a **browser-safe `sb_publishable_` key (not a secret — no leak)**, and they appear in **zero** compiled `.next` bundle chunks (i.e., currently dead config that never ships to the browser).

### Why it matters
This is **not an active vulnerability** — it's a **trap for the future**. If anyone later wires `supabase-js` using that publishable key, it would authenticate as the `anon`/`authenticated` role → hit the deny-all RLS → return **zero rows**, and would create a *second identity* disconnected from the `regwatch_session` cookie the backend actually checks. Result: confusing "logged in but no data / split identity" bugs. Today it's just leftover scaffolding.

### Recommendation
Pick one and stick with it:
- **Recommended — remove the staging:** keep cookie-session as the single source of truth; delete the 2 dangling `auth.users` + the never-expiring `auth.sessions` row; strip the `NEXT_PUBLIC_SUPABASE_*` lines from `.env.local` (and Vercel env). Cleanest; removes the trap.
- **Or — formally defer:** keep it, but add a `docs/DECISIONS.md` note that cookie-session is the live system and that any future `supabase-js` integration must mint the `regwatch_session` via the backend rather than authenticate independently. (And enable Supabase's leaked-password protection, which is currently off — see below.)

---

## 5. What else to do — prioritized

### P1 — do soon
1. **Resolve the two-login-systems situation** (Section 4). Recommended: remove the Supabase Auth staging.
2. **Pin TLS on the DB connection.** Today the connection uses psycopg's default `sslmode=prefer` — almost certainly encrypted in transit (Supabase offers TLS) but **the certificate is not verified and the connection is downgradable**. Append `?sslmode=require` (ideally `verify-full` with the Supabase CA) to the prod `DATABASE_URL` (Fly secret) + local `.env`. One change fixes the runtime engine, Alembic, and the migrate script. — `config/settings.py:179`, `db.py:176`.
3. **Capture the `ensure_rls` event trigger in a migration** so the auto-RLS defense is reproducible from the repo, not just live-DB "shadow infrastructure."

### P2 — hardening / hygiene
4. **Shrink the Data API surface (defense-in-depth):** since PostgREST is unused by the app, consider `REVOKE`ing `anon`/`authenticated` privileges on schema `public`, or disabling the Data API entirely. RLS already blocks rows; this removes the surface altogether.
5. **Login rate-limit is per-email + in-memory per-process** — add a per-IP/global cap, or rely on the gateway/WAF in front of prod. — `src/regwatch/common/ratelimit.py`.
6. **No password-strength policy** on `create-user` — enforce a minimum length + breached-password check. — `src/regwatch/cli.py`.
7. **Two `chunk`-table bootstrap DDLs differ slightly** (`db.py` schema-qualifies `extensions.vector`; `pgvector_store.ensure_schema` doesn't) despite "must match" comments — benign in prod, but make one path authoritative. Also bring the `chunk` table under Alembic (it's currently created only by runtime DDL). — `db.py:72`, `pgvector_store.py`.
8. **"Leaked Password Protection" advisor (WARN)** — only relevant **if** you keep Supabase Auth; moot if you remove it.

---

## Appendix — evidence summary

| Item | Live value |
|---|---|
| Alembic stamp | `0008_token_cost_feedback` (head) |
| Tables (public) | 16, all RLS-enabled, 0 policies, owner `postgres` |
| chunk rows / embeddings | 10,747 / 10,747 non-null; `vector(1536)` |
| pgvector | extension `vector` 0.8.0 in `extensions` schema; HNSW cosine index present |
| Data API deny-all test | anon GET on `user`, `psg_document`, `chunk` → `[]` |
| Custom auth | `public.user` = 2, `public.auth_session` = 3 active |
| Supabase Auth (unused) | `auth.users` = 2 (1 never logged in), `auth.sessions` = 1 with `not_after = NULL` |
| Secrets in git | none (`.env` / `.env.local` git-ignored, never committed; no service_role key anywhere) |
| Security advisors | 16× `rls_enabled_no_policy` (INFO, expected) + 1× leaked-password-protection (WARN, only relevant if adopting Supabase Auth) |
