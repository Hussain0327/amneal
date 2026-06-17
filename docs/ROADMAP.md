# REGWATCH Roadmap — Open / Not-Yet-Done Work

The single consolidated list of everything **not yet done**, aggregated from
every doc in the repo. If a doc and this file disagree on what remains, this
file plus [`PROD_READINESS.md`](PROD_READINESS.md) win.

- **Already shipped** (and therefore *not* listed here) lives in
  [`../README.md`](../README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), and the
  🟢 sections of [`PROD_READINESS.md`](PROD_READINESS.md). In short, on `main`:
  Router→Handlers→Synthesizer with INV-1..9 as tests; the seven FDA source
  handlers; cited Q&A + conversational sessions; cookie-session auth; the White
  Paper populator; the dual-mode SQLite/Postgres+pgvector storage *path*; and
  the unified Next.js four-surface shell (Ask rebuilt as a cited chat, the
  URL-scoped "Under review" product bar, and the resolve-backed `POST /resolve`
  picker). Streamlit is fully retired.
- `PROD_READINESS.md` holds the prod-launch detail; items below map to its gates
  (#1–#11) where applicable. This file additionally captures product/quality and
  future items that aren't strictly launch gates.

_Status: 2026-06-17, against `main` — after the CI-scan hardening / Next.js 16
upgrade (#11) that turned the newly-added supply-chain + Trivy gates green._

Legend: 🔴 blocks external exposure · 🟡 should-have before launch · ⚪ decision needed · 🔵 future / optional

---

## 🔴 Launch blockers — must land before any external exposure

### ⚪ D1 — LLM / data-handling decision  (PROD_READINESS #5)
Every Q&A and populate sends FDA-related queries to OpenAI. Before launch this
needs a deliberate data-handling choice: a BAA / zero-retention vendor agreement,
or an in-house OpenAI-compatible model. **Business/compliance call, not code.**
- Where: `config/settings.py` (`llm_provider`), the `LLMProvider` interface.
- Done when: provider chosen, data-processing terms reviewed, decision logged in
  [`DECISIONS.md`](DECISIONS.md). *This is the longest pole — start it first.*

### Gateway / TLS / SSO + distributed rate limiting  (PROD_READINESS #1)
App-layer auth is done (cookie sessions, per-user ownership, login brute-force
cap). What remains is environment work: an IT gateway terminating TLS (then set
`AUTH_COOKIE_SECURE=true`), OIDC/SSO against the corporate IdP, and the app never
directly reachable. The rate limiter is **in-memory/per-process**, so multi-replica
deploys need gateway-level limiting.
- Where: `src/regwatch/api/main.py`, `src/regwatch/auth/`, `common/ratelimit.py`.
- Done when: gateway terminates TLS + enterprise auth in front of the app (or the
  cookie-session layer is formally accepted as the pilot boundary), and distributed
  rate limiting is owned by that gateway.

### Provision managed Postgres / pgvector  (PROD_READINESS #2)
The dual-mode code + cutover runbook are ready (`DATABASE_URL` switch,
`REQUIRE_DATABASE_URL` refusing the SQLite fallback, pgvector dimension fail-fast,
Postgres boot verifying the Alembic stamp == head). Not yet *done* in the world.
- Where: `config/settings.py`, `store/db.py`, `store/pgvector_store.py`, [`DEPLOY.md`](DEPLOY.md).
- Done when: a managed Postgres/pgvector is provisioned, migrated from a clean
  snapshot, smoke-tested, and a restore drill (`scripts/restore_drill.sh`) has
  passed against staging — with least-privilege app DB credentials.

### Migration release gate  (PROD_READINESS #3)
Postgres boot *verifies* the Alembic stamp, but schema-advancing releases should
run `alembic upgrade head` as an explicit, gated deploy step (app boot = verify
only), with rehearsed rollback / roll-forward.
- Where: `store/db.py`, `docker/entrypoint.sh`, [`DEPLOY.md`](DEPLOY.md).
- Done when: schema releases run Alembic as a gated deploy step and rollback is rehearsed.

### UI production smoke + load behind the approved gateway  (PROD_READINESS #4)
The UI is feature-complete (unified shell + chat + scope picker). Remaining is
deploy-time proof, not code.
- Where: `regwatch/frontend/`, [`DEPLOY.md`](DEPLOY.md) §5 smoke checklist.
- Done when: deployed behind the approved auth/gateway path; API origin/proxy
  verified for that environment; the analyst smoke flows pass; a load test is run.

---

## 🟡 Operability hardening — should-have before launch

### Observability  (PROD_READINESS #6)
Structured logging, audit rows, privacy-scrubbed Sentry wiring, and a component
`/health` exist. Missing: exported request/latency/**cost** metrics, a real
readiness probe (DB + vector store + **LLM reachability**, distinct from `/health`
liveness), and a Sentry DSN actually configured in prod.
- Where: `common/logging.py`, `common/observability.py`, `common/audit.py`, `api/main.py`.

### Production Watch worker / scheduler  (PROD_READINESS #7)
Dagster defines `watch_digest_job` + a daily 06:00 UTC schedule for the local/Compose
path, but the Fly/Vercel deploy keeps Watch out of scope (ad-hoc runs). Needs a
supported scheduled worker with monitored run history, **partial-ingest recovery**
(the `alerted_at` / durable-diff residual that can silently skip an alert), and
pluggable alert delivery (email/Slack) beyond the JSONL digest.
- Where: `watch/run.py`, `orchestration/definitions.py`, `compose.yaml`.

### Secrets management  (PROD_READINESS #10)
`.env`/`.env.local`/data/stores/logs are gitignored and the runbook uses platform
secrets. Needs an approved secret manager/platform policy and documented, tested
key rotation.
- Where: `.env`, `config/settings.py`, [`DEPLOY.md`](DEPLOY.md).

### CI supply-chain & container security  (PROD_READINESS #11)
_Landed 2026-06-17 (CI green):_ CI gates on `pip-audit` (Python deps, via
`uv export`), `npm audit` (frontend prod deps), and Trivy scans of the API + web
images. The scans were added in #10 but failed `main` on first run; #11 cleared
all of them:
- **`npm audit`** — `next@14.2.35` carried HIGH advisories with no patched 14.x,
  so the frontend was upgraded **Next.js 14.2 → 16.2.9** (React stays 18.3.1; Next
  16 peer-supports React ^18.2). This forced the ESLint-9 flat-config migration
  (`next lint` was removed in 16; `eslint.config.mjs` replaces `.eslintrc.json`).
- **Trivy (web image)** — two layers: the `node:20-slim` Debian packages
  (`libgnutls30`, `libcap2`) are cleared by `apt-get upgrade`, and the npm-bundled
  deps (`tar`/`glob`/`minimatch`/`cross-spawn`, which ship inside npm 10.8.2 — not
  the app) are cleared by pinning **npm 11.17.0** in the web `Dockerfile`.
- **`npm ci`** on the linux runners also needed the frontend lockfile regenerated
  cross-platform: the macOS-generated lock omitted the linux/wasm `@emnapi` branch
  (`@img/sharp-wasm32`), so strict `npm ci` rejected it.

**Still open:** container resource limits (`compose.yaml`, `fly.toml` have none),
and the one accepted `pip-audit` ignore — chromadb `CVE-2026-45829` (ChromaToast,
server-only RCE; we use the embedded client + pgvector in prod) — should be
dropped the moment an upstream fix ships.
- Where: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), [`regwatch/frontend/`](../regwatch/frontend/), `compose.yaml`, `fly.toml`.

### Operations runbook hardening
Incident-response + rollback procedures and an external uptime monitor (a CI
uptime backstop already exists in `uptime-eval.yml`). The CRA White Paper template
is now wired via a documented mount convention (`WHITEPAPER_TEMPLATE_PATH` defaults
to `/app/data/templates/cra_white_paper_template.docx`; absent it the docx writer
falls back loudly) — _resolved 2026-06-17_; the remaining piece is operationally
*placing* the file on the chosen platform (Compose volume vs. Fly private overlay).
- Where: [`DEPLOY.md`](DEPLOY.md) (Operations), `docker/`.

---

## 🟡 Product & quality

### Real token-by-token streaming for Ask
The Ask client targets `/query/stream` (SSE) but **the backend has no such
endpoint**, so every send falls back to a blocking `POST /query` — nothing streams
today (the "thinking" ticker is honest motion, not faked tokens). Building it must
respect INV-1 (no answer text before a validated citation): stream the
retrieval/thinking phase, then stream answer deltas only once grounding is attached,
still writing exactly one audit row.
- Where: `api/main.py` (new endpoint), `regwatch/frontend/lib/api.ts` (`askQueryStream` already consumes SSE).

### Eval expansion  (PROD_READINESS #8)
The deterministic offline eval gate fires in CI and thresholds hold
(recall@k≥0.90, citation_precision≥0.95, refusal_accuracy≥0.95), but gold sets are
small (12 Q&A + 16 white-paper rows; spec wants 30–50) and scoring is mechanical
`(short_name, page)` + `expected_facts`. Add LLM-as-judge alongside, and expand the
gold set paired to what the seed actually ingests.
- Where: `src/regwatch/eval/`, `gold_set.jsonl`, `whitepaper_gold.jsonl`, `tests/test_eval_gate.py`.

### Persist-and-cite beyond the White Paper  (PROD_READINESS #9)
The persist-and-cite + freshness pattern (OB/SPL provenance with `last_fetched_at`,
multi-source synthesis) is wired for the White Paper but **not** the Ask/Assemble
read paths, which still query live HTTP without persisting source rows/freshness.
- Where: `src/regwatch/sources/`, the Q&A/assemble handlers.

### Non-technical product / watchlist management UX
Watchlist products and watched products are managed via API/CLI; there is no
in-app non-technical UX to add/manage them.
- Where: `regwatch/frontend/` (Watch surface), `watch/watchlist.py`.

---

## 🔵 Future / optional

- **Cross-encoder reranker** — exists as a hook, off by default (`RERANKER_ENABLED`);
  enable + tune `VECTOR_TOP_K` if retrieval precision needs it. (`retrieve/`)
- **Corpus expansion beyond PSGs** — broaden the retrievable corpus past
  product-specific guidances. (`ingest/`, `sources/`)
- **Ingest hardening at scale** — multi-worker Alembic init race + large-ingest
  resilience for `regwatch ingest-all`. (`store/db.py`, `ingest/`)
- **Kubernetes / Helm** — manifests or a chart if the deploy outgrows Fly/Compose.
- **Tunable refusal threshold** — `REFUSAL_SCORE_THRESHOLD` is calibrated on the
  gold set; revisit as the gold set grows.

---

## Suggested order
1. **D1 data-handling decision** (longest lead time — kick off immediately).
2. **Gateway/TLS/SSO** + distributed rate limiting (the exposure boundary).
3. **Provision Postgres/pgvector** + **migration release gate** + restore drill.
4. **Observability** + **production Watch worker** (operability).
5. **Eval expansion** + **persist-and-cite beyond White Paper** + **Ask streaming**.
6. **Secrets policy** + **CI security scans** + **ops runbook hardening**.
7. **UI production smoke/load** + product/watchlist management UX.
