# Production Gaps + Orchestration Cost — 2026-06-17

Three production bugs surfaced during the polyglot architecture review (see `POLYGLOT_ARCHITECTURE_REVIEW_2026-06-17.md`), each confirmed against the actual code, plus a costed answer to "what should run our daily pipeline, and how much does Dagster cost?"

---

## The 3 bugs (confirmed with evidence)

### 1. Alerts are wiped on every redeploy ⚠️
- `write_digest()` writes the daily digest to `processed_dir/alerts/digest-*.jsonl` (`src/regwatch/watch/alerts.py:113`); in the container that resolves to `/app/data/processed` (`docker/entrypoint.sh`).
- `fly.toml` has **no `[[mounts]]`**, so `/app/data` is the Fly machine's ephemeral rootfs. Every `fly deploy` ships a new image and wipes it.
- `/watch/latest` reads the digest back from that same local disk (`latest_digest_records()`, `alerts.py:126`), so the UI only ever shows what *this* machine wrote since its last deploy.
- **Impact:** the product's core output (change alerts) is not durable.

### 2. The daily watch pipeline does not run in prod ⚠️
- The schedule exists and is even `default_status=RUNNING` (`src/regwatch/orchestration/definitions.py:73-79`, cron `0 6 * * *`) — **but a Dagster schedule only fires if the `dagster-daemon` process is alive to evaluate it.**
- The Fly app runs **only** the API: `ENTRYPOINT regwatch-entrypoint` → `CMD uvicorn …` (`Dockerfile:77-78`). No daemon. `dagster-daemon`/`dagster-webserver` exist **only in `compose.yaml`** (local dev), never in `fly.toml`.
- **Impact:** in prod the corpus only updates when someone runs `regwatch watch` by hand. (Same root cause as #1: neither the schedule nor the alert state has a durable home in prod.)

### 3. `lib/api.ts` declares fields the backend never sends
- Backend `QueryResponse` (`src/regwatch/api/main.py:388-399`) emits: `answer, citations, refused, model_name, audit_id, session_id, turn_id, status, interpretation, clarify`. Status values: `answer | summary | clarify | scope_warning | refused`.
- Frontend `QueryResponse` (`regwatch/frontend/lib/api.ts:47-72`) additionally declares **`suggestions`**, **`unanswered`**, and a **`status: "conversational"`** — none of which appear anywhere in `main.py`. `normalizeQuery()` defaults them to `[]`.
- **Impact:** no crash, but the TypeScript types lie — the compiler thinks `suggestions` is always populated (it's always empty) and that `"conversational"` is reachable (it never is). Silent contract drift.

---

## How to fix each

### Fix #1 — Make alerts durable in Postgres (P0)
Kills bug #1 *and* decouples bug #2 from "where the job runs."
- Add an `alert` SQLModel table (the `Alert` dataclass at `alerts.py:34` maps 1:1: `product_id` FK, `psg_document_id`/`psg_version_id` FK, `captured_at`, `diff_summary`, `confidence`, `rationale`, `source_url`, `+ created_at`), with a unique constraint on `(product_id, psg_version_id)` for de-dup.
- New Alembic migration `0009_alerts`.
- Rewrite `write_digest()` → upsert rows (`ON CONFLICT DO NOTHING`); rewrite `latest_digest_records()` → `SELECT … ORDER BY captured_at DESC LIMIT n`.
- While there: collapse the N+1 in `build_alerts` (`_fetch_version_for_listing` runs one query *per match*) into one `DISTINCT ON (psg_document_id) … ORDER BY captured_at DESC` query.
- **Effort:** ~half a day, self-contained, with tests. Survives redeploys; works regardless of which machine runs the job.

### Fix #2 — Actually run the daily job in prod
With alerts in Postgres, the job just needs to run `regwatch watch` once a day against the prod DB. See the cost comparison below for the recommended trigger.

### Fix #3 — Kill the contract drift
- *Now (free):* delete `suggestions`/`unanswered`/`"conversational"` from `lib/api.ts` (unused dead type-surface), **or** if the conversational layer is genuinely coming, add them to the backend `QueryResponse`. Convert `status: str` → a Pydantic `Literal[...]` so the union is enforced.
- *Soon (gated):* generate `lib/api.ts` types from the FastAPI OpenAPI schema (`openapi-typescript`) + a CI drift check so this can't recur.
- **Effort:** ~1 hour cleanup; ~half a day for codegen + CI.

---

## Orchestration / "how much does Dagster cost?"

**Key point:** Dagster the software is free (open-source, Apache-2.0). The cost is the **always-on compute** to run its daemon + webserver + code-server 24/7 — the only way the `0 6 * * *` schedule fires. For **one daily job** (`regwatch watch`, ~3–15 min), that's poor value.

All prices verified live (June 2026) from official pricing pages (sources at bottom). Model case: one ~10-min run/day ≈ 300 min/month.

| Option | ~$/month for this workload | Setup effort | Key caveat |
|---|---|---|---|
| **GitHub Actions cron** | **$0** (300 min « 2,000–3,000 free private-repo min; overage only $0.002/min Linux) | Low — one `.yml` with `schedule:` + run CLI | Scheduled runs are **best-effort, no SLA** — can be delayed 20–40 min or dropped under load (worst at the top of the hour). Schedule at an odd off-peak minute (e.g. `17 4 * * *`). |
| **Fly ephemeral Machine** (daily ~10-min run) | **~$0.01–0.02** (256MB: 18,000 s/mo × $0.00000078) | Medium — needs an external trigger to `fly machine run` | A *stopped* machine won't self-wake; needs a scheduler (GH Actions / `pg_cron`) to start it. Per-second billing only while running. |
| **Fly 24/7 shared-cpu-1x 256MB + internal cron** | **$2.02** (512MB = $3.32) | Medium — supercronic/cron in a tiny always-on Machine | Pay 24/7 for a job that runs 10 min/day; isolated from the web app (good). |
| **Dagster OSS daemon+webserver 24/7 on Fly** | **~$5.94** (needs ~1GB; 512MB=$3.32 is tight) | High — daemon + webserver + gRPC code server, kept up 24/7 | Heaviest RAM + most moving parts, just to fire one daily schedule. |
| **Dagster+ Serverless (Solo)** | **~$10–11** ($10 base + $0.010/min compute + $0.040/credit; no free credits post-May-2026) | Medium — push code to Dagster Cloud | $10/mo floor; overkill for one CLI job. |
| **Dagster+ Hybrid (Solo)** | **$10 base + credits** *and* you still host the agent 24/7 on Fly (≈ +$3–6) | High | Worst of both: SaaS floor **plus** self-hosted compute. |
| **Supabase pg_cron** (already have it) | **$0** (free, all plans incl. Free tier) | Low–Medium — `cron.schedule()` SQL | Runs **SQL only inside Postgres** — cannot run a Python CLI. Must call out via `pg_net` HTTP or an Edge Function to trigger the real job. ≤8 concurrent jobs, ≤10 min each. |
| **Cron inside existing Fly app machine** | **$0 marginal** | Low — supercronic/cron in the app container | **Couples batch to the web process** (crawl/PDF parse competes with the API on the single `min_machines_running=1` box); job lost if the machine restarts at the scheduled minute. |

### Verified per-unit prices
- **GitHub Actions** — free private-repo minutes: Free/Org-Free 2,000/mo, Pro/Team 3,000/mo, Enterprise 50,000/mo. Overage: Linux 1-core $0.002/min. → 300 min/mo is free on every plan.
- **Fly Machines** (per-second while running): shared-cpu-1x 256MB = $0.00000078/s ≈ **$2.02/mo 24/7**; 512MB ≈ **$3.32/mo**; ~$5/GB-RAM/30 days. Stopped machines bill only rootfs at $0.15/GB/30 days. New accounts have **no free tier**.
- **Dagster+** (pay-as-you-go, eff. May 1 2026): Solo **$10/mo + $0.040/credit**; Starter $100/mo + $0.035/credit. Serverless compute **+$0.010/min**; Hybrid has no compute charge but you host the agent. Credits = each asset materialization + each op execution. No included free credits (30-day trial only).
- **Supabase pg_cron / Cron** — free, included on all plans; SQL/HTTP/Edge-Function only (cannot run a Python CLI directly); ≤8 concurrent, ≤10 min/job recommended.

### Recommendation
For one once-daily job on this stack, the cheapest *robust* option is a **GitHub Actions scheduled workflow ($0)** running `regwatch watch` on a hosted Linux runner — isolated from the web app, free logs/retries/alerts, the CLI already lives in the repo. Mitigate its one weakness (best-effort cron) by scheduling at an odd off-peak minute (e.g. `17 4 * * *`, never `0 * * * *`). It needs the prod `DATABASE_URL` + `OPENAI_API_KEY` (+ `OPENFDA_API_KEY`) as GitHub secrets.

If you ever need wall-clock-precise firing + full isolation, the cheap upgrade is **Supabase `pg_cron` (free) → `pg_net`/Edge Function → `fly machine run` on an ephemeral Fly Machine (~$0.01–0.02/mo)**.

**Avoid for this workload:** Dagster+ ($10/mo floor + per-credit) and self-hosting the Dagster OSS daemon 24/7 (~$3–6/mo + most operational overhead). Keep Dagster for *local dev* only; revisit Dagster-in-prod when you have several interdependent pipelines that actually benefit from its asset graph / UI / retries / backfills.

### Sources
- GitHub Actions billing: https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions
- GitHub Actions schedule best-effort caveat: https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows
- Fly.io pricing: https://fly.io/docs/about/pricing/
- Dagster+ pricing: https://dagster.io/pricing · May 2026 Solo/Starter update: https://support.dagster.io/articles/3171123463-dagster-solo-and-starter-pricing-updates-may-2026 · billing model: https://docs.dagster.io/deployment/dagster-plus/management/billing
- Supabase Cron / pg_cron: https://supabase.com/docs/guides/cron · https://supabase.com/docs/guides/database/extensions/pg_cron

---

## Suggested order of work
1. **P0 — Fix #1** (alerts → Postgres `alert` table + migration). Makes the product's core output durable.
2. **P0 — Fix #2** (GitHub Actions cron running `regwatch watch`). Gets the pipeline actually running in prod.
3. **P1 — Fix #3 cleanup** (reconcile `lib/api.ts` ↔ backend; `status` → `Literal`).
4. **P1 — gated** — `openapi-typescript` codegen + CI drift check.
