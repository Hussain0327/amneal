# Configuration reference

This page is the single owner of every environment variable, feature flag and
secret in RegWatch. If another document needs a configuration fact, it links
here instead of keeping its own copy. Nine documents used to keep their own env
lists and all nine rotted independently.

Everything below was verified against the files named in each row. Where a
value is a secret, this page names the mechanism and the command that reads it,
never the value.

## 1. How to read this page

There are four configuration layers. Later layers win.

1. **Code default.** Python: the field default in `config/settings.py`. Go: the
   default argument in `go/internal/api/config.go`, `go/internal/proxy/proxy.go`
   and `go/internal/store/pool.go`. This is what a bare local checkout runs.
2. **`fly.toml` `[env]` pin.** Committed, app-wide across both Fly process
   groups (`app` and `proxy`). Changing one requires a deploy.
3. **Fly secret.** Set with `fly secrets set`. A secret **beats** an `[env]`
   pin of the same name, and applies without a CI cycle. This is why `fly.toml`
   can pin a flag and an operator can still flip it during an incident.
4. **GitHub Actions secret.** A separate surface entirely. It configures CI and
   cron jobs, never the running Fly app. A Fly secret and an Actions secret with
   related names are two different stores that can drift apart.

A fifth source exists but only locally: pydantic-settings reads a `.env` file
(`config/settings.py:73`). It never ships. `.env` is listed in `.dockerignore`
(lines 29-31), so it is not in the image, and the Go binary does not read a
`.env` file at all (`go/internal/api/config.go:40-43` documents that
divergence).

### What a static checkout can and cannot prove

It **can** prove: which names exist, what the code default is, which process
reads each one, what happens when one is missing, and what `fly.toml` pins.
Every claim on this page is of that kind.

It **cannot** prove: the current value of any Fly secret or GitHub Actions
secret, whether a secret is set at all, which embedding profile production
serves today, or the live refusal floor. Reading those needs the commands in
section 8. Do not infer a live value from a committed default.

Related documents:

- `docs/PRODUCTION_TRUTH.md` owns what is actually live right now.
- `docs/BUILT_BUT_DORMANT.md` owns the analysis of code that exists but does
  not run.
- `docs/SECRETS_RUNBOOK.md` owns rotation procedure.
- `docs/DEPLOY.md` owns the deploy and incident runbooks.

## 2. Boot-required variables

Three variables have no default and refuse to boot when unset. This posture
came from the 2026-08-14 backfill outage: a worker booted without an embedding
provider fell back silently to a 384-dimension local model and failed the write
on all 295 documents after paying their fetch, parse and OCR cost
(`config/settings.py:80-85`).

| Variable | Consumer | Default | Where the live value lives |
| --- | --- | --- | --- |
| `INGEST_EMBEDDING_PROVIDER` | Python Settings, Go proxy (reported on `GET /settings`) | none (`config/settings.py:92-95`) | `fly.toml:67` `[env]` pin |
| `LLM_PROVIDER` | Python Settings, Go proxy (reported on `GET /settings`) | none (`config/settings.py:96`) | `fly.toml:68` `[env]` pin |
| `DATABASE_URL` | Python Settings, Go proxy | none (`config/settings.py:584`) | Fly secret |

### The exact failure for each

**`INGEST_EMBEDDING_PROVIDER` unset.** The API lifespan calls
`assert_embedding_runtime_available(s.embedding_provider)` at
`src/regwatch/api/main.py:214`. That resolves the name through
`_require_provider_name` (`src/regwatch/process/embedder.py:614-630`), which
raises `RuntimeError`:

```text
EMBEDDING_PROVIDER is not set and has no default. Set
EMBEDDING_PROVIDER=openai (text-embedding-3-large), or
EMBEDDING_PROVIDER=echo (tests only); this process refuses to ...
```

Set to `openai` but with `OPENAI_API_KEY` or `OPENAI_EMBEDDING_MODEL` missing,
the same assert raises instead
(`src/regwatch/process/embedder.py:641-653`). Set to anything other than `echo`
or an OpenAI name, `get_embedding_provider` raises
`ValueError: unknown embedding provider: <name>`
(`src/regwatch/process/embedder.py:686-699`). Only two provider classes exist,
`EchoEmbeddingProvider` and `OpenAIEmbeddingProvider`.

**`LLM_PROVIDER` unset.** The lifespan calls
`assert_llm_runtime_available(s.llm_provider)` at
`src/regwatch/api/main.py:221`, which delegates to `get_llm_provider`
(`src/regwatch/generate/llm.py:737-748`). Unset raises:

```text
LLM_PROVIDER is not set and has no default. Set LLM_PROVIDER=openai, or
LLM_PROVIDER=echo (tests only).
```

Set to `openai` with `OPENAI_API_KEY` or `OPENAI_LLM_MODEL` missing, it raises
`<name> not set; configure the OpenAI LLM provider`
(`src/regwatch/generate/llm.py:705-715`).

`LLM_PROVIDER=databricks` raises `ValueError: unknown LLM provider: databricks`
(`src/regwatch/generate/llm.py:732-733`). There is no Databricks rollback path
in the code. Any runbook that tells you to set it is wrong and would take Q&A
down.

The generation assert is scoped to the API lifespan on purpose. The corpus
worker (`dagster-daemon`) and the CLI commands never generate, so they do not
inherit the requirement (`src/regwatch/api/main.py:217-220`).

**`DATABASE_URL` unset.** Postgres plus pgvector is the only datastore. The
engine builder refuses at first use
(`src/regwatch/store/db.py:169-183`):

```text
DATABASE_URL is empty -- Postgres is the only datastore and there is no
fallback. Set DATABASE_URL to a Postgres URL (Databricks Lakebase in prod);
tests use TEST_DATABASE_URL (see tests/conftest.py).
```

The Go proxy handles the same gap differently, on purpose. With
`REQUIRE_DATABASE_URL=true` and no `DATABASE_URL`, `nativeRoutes` returns an
error, and `main()` treats it as fatal (`logger.Fatal`, then exit 1): the
process dies before it serves anything, native or relay, and the machine
holding the public port crash-loops until the variable is fixed
(`go/cmd/proxy/main.go:36-39,63-77`). Without `REQUIRE_DATABASE_URL`, it logs a
warning and serves relay-only, so a local relay-only proxy still runs. Once the
proxy is up the pgx pool is lazy, so a later database blip does not restart it.

### Two more boot guards worth knowing

- `REGWATCH_ALLOW_TEST_PROVIDERS`. With an `echo` provider configured and a
  non-empty corpus, boot fails unless this is set
  (`src/regwatch/api/main.py:149-170`). An empty corpus is fine.
- `REGWATCH_RETRIEVAL_CORPUS=authoritative_fda`. Boot additionally calls
  `assert_authoritative_corpus_ready_for_activation()`
  (`src/regwatch/api/main.py:208-211`).

## 3. The `fly.toml` `[env]` pins

Transcribed from `fly.toml:59-123`. These are app-wide: both the `app` and
`proxy` process groups see all of them, so some ride along inert on the group
that does not read them.

| Name | Pinned value | Line | What it does | How to revert |
| --- | --- | --- | --- | --- |
| `INGEST_EMBEDDING_PROVIDER` | `openai` | 67 | Satisfies the boot assert and steers the ingest/backfill write path plus the legacy retrieval arm | Edit the line and deploy. Unsetting it does not fall back, it refuses to boot |
| `LLM_PROVIDER` | `openai` | 68 | Satisfies the generation boot assert | Edit and deploy. `databricks` is not a valid value |
| `PROFILE_HNSW_INDEX_REQUIRED` | `false` | 71 | Retrieval is an exact pgvector scan, so a profile needs full coverage but not an HNSW index | `fly secrets set PROFILE_HNSW_INDEX_REQUIRED=true`, or edit and deploy |
| `REGWATCH_ROUTE_CALL` | `off` | 74 | Keeps the pre-retrieval router call off the Ask path. Modes are `off`, `shadow`, `live` | `fly secrets set REGWATCH_ROUTE_CALL=shadow` (no CI cycle), or edit and deploy |
| `AUTH_COOKIE_SECURE` | `true` | 75 | Session cookie carries the `Secure` flag. Read by the Go proxy, which mints the cookie | `fly secrets set AUTH_COOKIE_SECURE=false`. Both runtimes then log `insecure_session_cookie_in_production` and keep serving |
| `CORS_ALLOW_ORIGINS_CSV` | `https://amneal.vercel.app` | 76 | The allowlist that stops other origins riding the HttpOnly session cookie | Edit and deploy. Keep it tight |
| `SENTRY_ENVIRONMENT` | `production` | 77 | Tags Sentry events; also the trigger for the two production-only boot warnings | Edit and deploy |
| `TRUST_PROXY_HEADERS` | `true` | 85 | The Go login limiter keys its per-IP bucket on the platform-attested `Fly-Client-IP` instead of the TCP peer. `tests/test_trust_proxy_fly_toml.py` guards this line | Edit and deploy. Turning it off collapses every caller into one IP bucket |
| `REQUIRE_DATABASE_URL` | `true` | 90 | A proxy machine refuses to serve auth when `DATABASE_URL` is missing. Read only by Go | Edit and deploy |
| `GO_NATIVE_QUERY` | `true` | 98 | The Go proxy serves `POST /query` natively via buffered `CompleteQuery` instead of relaying to Python. The code default is `false` (`go/internal/api/config.go:209-213`), so a local proxy relays unless you set this | `fly secrets set GO_NATIVE_QUERY=false` for an instant rollback, or delete the line and deploy |
| `REGWATCH_PROSE_SYNTHESIS` | `true` | 110 | v6 prose synthesis. Code default is `false` (`config/settings.py:173-175`) | `fly secrets set REGWATCH_PROSE_SYNTHESIS=false` |
| `REGWATCH_SELECTIVE_CITATION` | `true` | 111 | v7 selective citation, the live answer policy. Only honored when prose is also on. Code default is `false` (`config/settings.py:212-214`) | `fly secrets set REGWATCH_SELECTIVE_CITATION=false` |
| `UPSTREAM_URL` | `http://app.process.amneal.internal:8000` | 123 | Where the Go proxy relays. Read only by `ConfigFromEnv` in `go/internal/proxy/proxy.go:43`; inert on app machines | Edit and deploy |

Two pins are policy-critical. `REGWATCH_PROSE_SYNTHESIS` and
`REGWATCH_SELECTIVE_CITATION` default to `false` in code, so before they were
pinned an unset or cleared secret silently reinstated the older v6 policy: a
policy rollback with no deploy and no diff (`fly.toml:99-111`). The pins make
`false` a deliberate act.

`fly.toml` also sets non-`[env]` operational values you will care about during
a deploy: `kill_timeout = 30` (line 14), `release_command = "regwatch release"`
(line 30), `min_machines_running = 2` (line 136), the proxy health check on
`/livez` (lines 160-165), and the app-group check on `/health` port 8000 (lines
174-183).

## 4. Values that exist only as Fly secrets

These have no `[env]` pin and no usable code default for production. They exist
only in Fly's secret store, and this repo cannot read them.

| Name | Consumer | Code default | Effect when unset |
| --- | --- | --- | --- |
| `DATABASE_URL` | Python Settings, Go proxy | none (`config/settings.py:584`) | Python refuses at first engine use; Go refuses to serve auth because `REQUIRE_DATABASE_URL` is pinned true |
| `OPENAI_API_KEY` | Python Settings | `None` (`config/settings.py:153`) | Both boot asserts fail: embeddings (`embedder.py:644-645`) and generation (`llm.py:705-715`) |
| `RETRIEVAL_EMBEDDING_PROFILE` | Python Settings, Go proxy (threshold resolution) | `"legacy"` (`config/settings.py:118-121`) | Retrieval silently falls back to the legacy `chunk.embedding` arm instead of the named profile |
| `REFUSAL_SCORE_THRESHOLD_BY_PROFILE` | Python Settings, Go proxy | `{}` (`config/settings.py:122-130`) | Every profile falls back to the global `REFUSAL_SCORE_THRESHOLD` default 0.30, which was validated against a retired vector space |
| `INTERNAL_RAG_TOKEN` | Python Settings, Go proxy | `""` (`config/settings.py:889`) | `POST /internal/query/compute` 404s unconditionally (fail-closed), so native `POST /query` synthesizes an upstream error |
| `SENTRY_DSN` | Python Settings, Go `obs` package | `None` (`config/settings.py:480`) | Sentry is off. In production the app logs `sentry_disabled_in_production` and keeps serving (`src/regwatch/api/main.py:183-187`) |
| `METRICS_TOKEN` | Python Settings | `None` (`config/settings.py:867`) | `GET /metrics` stays open with no bearer gate. Setting it arms a constant-time bearer check. It never gates `/health` or `/ready` |
| `WHITEPAPER_TEMPLATE_URL` | Python Settings | `None` (`config/settings.py:805`) | Every production white paper render falls back to a generated template and is stamped with the loud fallback marker, because prod machines have no persistent volume |

List them with:

```bash
fly secrets list -a amneal
```

That command prints names and digests, never values. Fly does not expose a
secret's value after it is set; the only way to change one is to set it again.

The set of names is not fixed. Any Settings field can be overridden by a Fly
secret of the same uppercase name, and a secret always beats the `fly.toml`
`[env]` pin. Treat the output of `fly secrets list` as authoritative over this
table.

## 5. Deprecated aliases and precedence

Two variables were renamed. Both old names still work.

| Canonical name | Deprecated alias | Resolution |
| --- | --- | --- |
| `RETRIEVAL_EMBEDDING_PROFILE` | `ACTIVE_EMBEDDING_PROFILE` | New name wins |
| `INGEST_EMBEDDING_PROVIDER` | `EMBEDDING_PROVIDER` | New name wins |

The new names say which path each knob steers.
`RETRIEVAL_EMBEDDING_PROFILE` picks the profile the query path serves with.
`INGEST_EMBEDDING_PROVIDER` picks the provider the ingest and backfill write
path embeds with (`config/settings.py:58-68`).

Resolution is pydantic `AliasChoices` on the field, new name first
(`config/settings.py:92-95` and `116-119`). The warning behavior is separate,
in `_warn_deprecated_env_names` (`config/settings.py:296-330`):

- Old name set, new name absent: the old value is used and one `FutureWarning`
  is emitted per process.
- Both set and disagreeing: the new name wins and the warning says so, naming
  both values.
- Both set and agreeing: silent. That is the safe state a deployment passes
  through while renaming a secret.
- Blank counts as unset, matching the field normalizers.

`FutureWarning`, not `DeprecationWarning`, because the audience is an operator
reading boot logs and Python's default filters hide `DeprecationWarning`
outside `__main__`. Python's default "print each unique warning once" filter
keeps it to a single line per process.

The Go proxy mirrors the same fallback order for both names
(`go/internal/api/config.go:135-138` for the provider,
`go/internal/api/config.go:236-242` for the profile).

`RETRIEVAL_TOP_K` is a third legacy name, handled differently. It is honored
only when `RERANK_TOP_K` is still at its default of 8, so a stale
`RETRIEVAL_TOP_K` lingering in the environment can no longer silently override
an explicit `RERANK_TOP_K` (`config/settings.py:555-568`). It emits no warning.

## 6. GitHub Actions secrets

Regenerate this list with:

```bash
grep -rhoE 'secrets\.[A-Z_]+' .github/workflows/*.yml | sort -u
```

Today that returns exactly seven names. Six workflow files exist: `ci.yml`,
`deploy.yml`, `machine-monitor.yml`, `openai-eval.yml`, `uptime-eval.yml`,
`watch-daily.yml`.

| Secret | Workflows that read it | Failure mode when absent |
| --- | --- | --- |
| `FLY_API_TOKEN` | `deploy.yml:161,184,201,208`; `machine-monitor.yml:68` | `deploy.yml` fails at the registry push. `machine-monitor.yml` skips cleanly with a log line and never checks machine state |
| `OPENAI_API_KEY` | `openai-eval.yml:68`; `watch-daily.yml:81` | Both preflight steps hard-fail with `::error::OPENAI_API_KEY is unset`. The eval job goes red rather than skipping (`openai-eval.yml:96-102`); the watch run aborts before any crawl or ingest (`watch-daily.yml:102-109`) |
| `PROD_HEALTH_URL` | `uptime-eval.yml:22` | The 30-minute health probe skips cleanly. No failure, no probe |
| `SLACK_WEBHOOK_URL` | `machine-monitor.yml:71`; `watch-daily.yml:92` | Optional. Both notify steps become graceful no-ops. A red run in the Actions tab is then the only signal |
| `WATCH_ACTIVE_EMBEDDING_PROFILE` | `watch-daily.yml:80` | The watch preflight hard-fails. It must also match `^ep_[0-9a-f]{32}$`, never `legacy` (`watch-daily.yml:115-128`) |
| `WATCH_DATABASE_URL` | `watch-daily.yml:73` | The whole job skips cleanly with a log line. No crawl, no alerts, no red run |
| `WATCH_HEALTHCHECK_URL` | `watch-daily.yml:93` | Optional dead-man switch. Both pings no-op |

`ci.yml` reads no secret directly. Its `openai-eval` job passes
`secrets: inherit` to the reusable workflow (`ci.yml:84-92`), which is how
`OPENAI_API_KEY` reaches the blocking eval.

Notes that matter when you touch this surface:

- `watch-daily.yml` hardcodes its own provider env block rather than inheriting
  anything: `INGEST_EMBEDDING_PROVIDER=openai`, `LLM_PROVIDER=openai`,
  `PROFILE_HNSW_INDEX_REQUIRED=false`, `REQUIRE_DATABASE_URL=true`, the four
  `OPENAI_*` model and shape values, and `SENTRY_ENVIRONMENT=production`
  (`watch-daily.yml:70-93`). Changing a model in `fly.toml` does not change it
  here.
- `openai-eval.yml` does the same in its own `env:` block
  (`openai-eval.yml:67-77`). It also derives the flags from workflow inputs, so
  `ci.yml` is what makes the blocking eval score the production arm.
- `machine-monitor.yml` runs on a 10-minute cron and reads `FLY_API_TOKEN` plus
  the optional `SLACK_WEBHOOK_URL`. Whether it is actually enabled at the
  repository level cannot be proven from a checkout. Committed YAML is not
  evidence that monitoring is running.
- `lint-type-test` sets `TEST_DATABASE_URL` inline, not from a secret
  (`ci.yml:96-104`), pointing at the `pgvector/pgvector:pg17` service
  container. `DATABASE_URL` is deliberately absent there so pytest cannot reach
  a real database.

Read the configured names with `gh secret list` (see section 8).

## 7. Dormant and off-by-default flags

Turning one of these on changes behavior. `docs/BUILT_BUT_DORMANT.md` owns the
analysis of what each dormant path is and why it does not run; this section
only names the switch and what flipping it does.

| Flag | Code default | Consumer | What turning it on does |
| --- | --- | --- | --- |
| `REGWATCH_MMR_DIVERSITY` | `false` (`config/settings.py:540`) | Python Settings | Stage 2 keeps the same number of passages, but a candidate that repeats an already-selected passage loses to a distinct one. Similarity is text-based (`src/regwatch/retrieve/diversity.py`), so the flip costs no extra embedding or query. Flip it only after an eval A/B |
| `RERANKER_ENABLED` | `false` (`config/settings.py:531`) | Python Settings | Enables the phase-2 cross-encoder rerank. Off, stage 2 is the identity: the first `RERANK_TOP_K` of the wide net (`src/regwatch/retrieve/reranker.py:22-24`) |
| `REGWATCH_ROUTE_CALL` | `off` (`config/settings.py:190-192`), pinned `off` in `fly.toml:74` | Python Settings | `shadow` adds one bounded pre-retrieval router call and records its advisory decision under `query_log.route_json.route_call`; nothing reads it. `live` additionally lets the no-product branch carry a session product over on the route's compiled scope instead of the word-list heuristic, guarded the same way and failing open to the heuristic. A live-classified corpus scope still only compiles and audits; it does not execute |
| `REGWATCH_LIVE_DRAFT` | `false` (`config/settings.py:178`) | Python Settings | Streams a live provisional draft over SSE. Effective only when prose synthesis is also on **and** the request opts in |
| `REGWATCH_RETRIEVAL_CORPUS` | `legacy` (`config/settings.py:507-509`) | Python Settings | `authoritative_fda` admits only chunks carrying one of the five policy-approved source families, and arms an extra boot gate that refuses activation unless the corpus is ready |
| `REGWATCH_SERVING_MANIFEST_SHA` | `None` (`config/settings.py:517-519`) | Python Settings | Activation counts against the durable manifest with this exact logical sha256 instead of requiring a complete-universe run. Unset keeps the original complete-universe-only behavior. This is the 2026-08-18 scoped-activation amendment |
| `EMBEDDING_SHADOW_PROFILE` | `None` (`config/settings.py:131`) | Python Settings | Names a second profile that is dual-written and backfilled but never serves user retrieval until it is explicitly promoted |
| `PROFILE_HNSW_INDEX_REQUIRED` | `false` (`config/settings.py:133`), pinned `false` in `fly.toml:71` | Python Settings | Requires a compatible index before a profile may serve. Retrieval is exact, so this is off |
| `REGWATCH_QUERY_EMBED_CACHE` | `true` (`config/settings.py:102-104`) | Python Settings | On by default, unusually for this group. Turning it **off** removes the process-wide LRU over query embeddings, so every repeated Ask query pays the serial pre-synthesis embedding round trip again |
| `REGWATCH_ALLOW_TEST_PROVIDERS` | `false` (`config/settings.py:256-258`) | Python Settings | Lets an `echo` provider boot against a non-empty corpus. Tests and CI only. It also arms `REGWATCH_FAULT_INJECT` |

Test-only knobs, inert without `REGWATCH_ALLOW_TEST_PROVIDERS`:

| Name | Consumer | Effect |
| --- | --- | --- |
| `REGWATCH_FAULT_INJECT` | Python | Names a stage to raise at. Checked against the same boot guard, so it cannot fire in production (`src/regwatch/generate/grounded_qa.py:275-279`) |
| `REGWATCH_ECHO_FORCE_REFUSAL` | Python | Makes the echo LLM provider always refuse (`src/regwatch/generate/llm.py:167-174`) |
| `REGWATCH_ECHO_FORCE_MALFORMED` | Python | Makes the echo LLM provider emit a malformed structure |

Not a flag, but often confused for one: `RetrievalMode.ANN_RERANKED` exists in
`src/regwatch/retrieve/mode.py` and `assert_mode_permitted` raises
unconditionally when it is requested (lines 108-112). No environment variable
turns it on. See `docs/BUILT_BUT_DORMANT.md`.

## 8. How to read the live values

Four commands, four different scopes. None of them is optional if you are
diagnosing a configuration problem.

**Resolved Python settings, no secrets.** Runs anywhere the app image runs:

```bash
regwatch status
```

It prints the embedding provider, active embedding profile, retrieval corpus,
shadow profile, OpenAI model and dimension, LLM provider and model, data
directory, whether a database URL is set, `retrieval_top_k`, the company name,
and both refusal thresholds: `refusal_score_threshold` is the **effective**
floor from `effective_refusal_threshold()`, and
`refusal_score_threshold_global` is the fallback beside it
(`src/regwatch/cli.py:194-218`). Read the effective one. The global 0.30 is not
what answers are gated on when a per-profile entry exists.

**What the running edge reports:**

```text
GET /settings
```

Six non-secret fields: `embedding_provider`, `llm_provider`, `llm_model`,
`retrieval_top_k`, `refusal_score_threshold`, `company_name`. It is served by
the Go proxy, not Python (`go/internal/api/settings.go:22-33`); there is no
Python `/settings` route any more. It requires a session, so an anonymous
`curl` gets nothing. The threshold it reports is resolved by
`effectiveRefusalThreshold` (`go/internal/api/config.go:227-247`), the same
rule as `Settings.effective_refusal_threshold()`, so the number the UI draws
its confidence band from is the number answers are gated on.

`GET /health` and `GET /ready` also report provider state and are
unauthenticated. `GET /metrics` is unauthenticated unless `METRICS_TOKEN` is
set. `GET /livez` is the DB-free liveness route Fly's proxy check uses.

**Which secrets the Fly app has:**

```bash
fly secrets list -a amneal
```

Names and digests only. If a name here matches a `fly.toml` `[env]` key, the
secret is what is in force.

**Which secrets the repository has:**

```bash
gh secret list
```

Compare that output against the seven names in section 6. A name in the
listing that is not in section 6 is dead configuration; a name in section 6
that is missing from the listing means the failure mode in that row is live
right now.

## 9. Full variable inventory

Every field on the `Settings` model, plus the variables read outside it. Rows
are grouped the way `config/settings.py` groups them. "Consumer" is which
process reads the value: Python Settings, Go proxy, or GitHub Actions.

Unless a row says otherwise, the live value lives in a Fly secret when it
differs from the code default, and nothing in this repo can tell you whether it
does.

### 9.1 Providers and models

| Name | Alias | Code default | Consumer | Notes |
| --- | --- | --- | --- | --- |
| `INGEST_EMBEDDING_PROVIDER` | `EMBEDDING_PROVIDER` | none | Python, Go | Boot-required. Pinned `openai` in `fly.toml:67` |
| `LLM_PROVIDER` | none | none | Python, Go | Boot-required. Pinned `openai` in `fly.toml:68` |
| `RETRIEVAL_EMBEDDING_PROFILE` | `ACTIVE_EMBEDDING_PROFILE` | `legacy` | Python, Go | Fly secret in prod |
| `EMBEDDING_SHADOW_PROFILE` | none | `None` | Python | Dual-written, never serves |
| `PROFILE_HNSW_INDEX_REQUIRED` | none | `false` | Python | Pinned `false` in `fly.toml:71` |
| `REGWATCH_QUERY_EMBED_CACHE` | none | `true` | Python | Bounded at 256 entries, successes only |
| `OPENAI_API_KEY` | none | `None` | Python | Fly secret. Shared by generation and embeddings |
| `OPENAI_BASE_URL` | none | `https://api.openai.com/v1` | Python | Must start with `https://` or boot fails (`config/settings.py:372-391`) |
| `OPENAI_LLM_MODEL` | none | `gpt-5.6-terra` | Python, Go | Go reads it for `GET /settings` (`config/settings.py:155`, `go/internal/api/config.go:153`) |
| `OPENAI_REASONING_EFFORT` | none | `medium` | Python | One of `none`, `low`, `medium`, `high`, `xhigh`, `max`; a typo fails boot (`config/settings.py:393-405`). Global, not per role |
| `OPENAI_EMBEDDING_MODEL` | none | `text-embedding-3-large` | Python | The model name is the embedding space; it is never guessed |
| `OPENAI_EMBEDDING_DIMENSION` | none | `1024` | Python | Matryoshka truncation of the native 3072. Must be in [32, 3072]. Changing it changes the profile fingerprint |
| `OPENAI_EMBEDDING_BATCH_SIZE` | none | `256` | Python | Must be in [1, 2048] |
| `OPENAI_TIMEOUT_S` | none | `60.0` | Python | |
| `OPENAI_MAX_RETRIES` | none | `3` | Python | |
| `LLM_TIMEOUT_S` | none | `60.0` | Python | Generic transport bound when the OpenAI-specific one is unset |
| `LLM_MAX_RETRIES` | none | `2` | Python | |
| `LLM_MODEL_PRICES` | none | `{}` | Python | JSON, USD per 1M tokens keyed by model. An unknown model logs `cost_usd` NULL, never a guess |
| `SYNTHESIZER_MAX_TOKENS` | none | `3000` | Python | Must be in [1, 5999]. At or above the 6000 ceiling the first call is silently clamped and the truncation retry stops working, so boot refuses instead (`config/settings.py:423-442`) |
| `REGWATCH_ALLOW_TEST_PROVIDERS` | none | `false` | Python | |

### 9.2 Answer policy flags

| Name | Alias | Code default | Consumer | Notes |
| --- | --- | --- | --- | --- |
| `REGWATCH_PROSE_SYNTHESIS` | none | `false` | Python | Pinned `true` in `fly.toml:110`. Blank reads as off, never a boot crash |
| `REGWATCH_SELECTIVE_CITATION` | none | `false` | Python | Pinned `true` in `fly.toml:111`. Only honored when prose is on |
| `REGWATCH_LIVE_DRAFT` | none | `false` | Python | Needs prose on and per-request opt-in |
| `REGWATCH_ROUTE_CALL` | none | `off` | Python | Pinned `off` in `fly.toml:74` |
| `REGWATCH_ROUTE_MAX_TOKENS` | none | `1200` | Python | Must be in [256, 6000] |
| `REFUSAL_TEXT` | none | see `config/settings.py:908-911` | Python | The app's own decline copy, served when a gate guard blocks the model's wording or the turn declines before any model text |

### 9.3 Retrieval and refusal

| Name | Alias | Code default | Consumer | Notes |
| --- | --- | --- | --- | --- |
| `VECTOR_TOP_K` | none | `50` | Python | Stage 1 wide net |
| `RERANK_TOP_K` | `RETRIEVAL_TOP_K` | `8` | Python, Go | The legacy name is honored only while this is at its default |
| `RETRIEVAL_TOP_K` | none | `None` | Python, Go | Go reports it on `GET /settings`; unset renders as a present null key |
| `RERANKER_ENABLED` | none | `false` | Python | |
| `REGWATCH_MMR_DIVERSITY` | none | `false` | Python | |
| `REFUSAL_SCORE_THRESHOLD` | none | `0.30` | Python, Go | Must be in [0, 1] on both sides. This is the fallback, not the live floor |
| `REFUSAL_SCORE_THRESHOLD_BY_PROFILE` | none | `{}` | Python, Go | JSON object, profile id to cosine floor. Malformed JSON refuses boot on both sides |
| `REGWATCH_RETRIEVAL_CORPUS` | none | `legacy` | Python | |
| `REGWATCH_SERVING_MANIFEST_SHA` | none | `None` | Python | |
| `METADATA_CACHE_TTL_S` | none | `60.0` | Python | Bounds how long the API serves a stale "which drugs exist" set after a separate ingest adds one. 0 disables the TTL |

### 9.4 Storage and connection behavior

Python reads all of these through Settings; the Go proxy's pgx pool reads the
four timeout names itself with matching defaults
(`go/internal/store/pool.go:37-107`).

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | none | Python, Go | Boot-required. `postgres://` and bare `postgresql://` are normalized to `postgresql+psycopg://` on the Python side |
| `DB_STATEMENT_TIMEOUT` | `30s` | Python, Go | GUC duration string. `0` or `''` disables |
| `DB_IDLE_IN_TX_TIMEOUT` | `60s` | Python, Go | Load-bearing: an idle-in-transaction read once blocked a boot-time `ALTER TABLE` and wedged production |
| `DB_LOCK_TIMEOUT` | `10s` | Python, Go | |
| `DB_CONNECT_TIMEOUT` | `10` | Python, Go | Integer seconds. Bounds the TCP and TLS handshake, which the GUCs above cannot |
| `DB_POOL_RECYCLE_S` | `1800` | Python | Bounds connection lifetime. Do not lower it to chase scale-to-zero |
| `DB_POOL_IDLE_PING_S` | `30` | Python | Pings a pooled connection at checkout only after this much idleness. `0` restores the old ping-every-checkout behavior with no deploy |
| `DB_KEEPALIVES_IDLE_S` | `30` | Python | `0` omits all four libpq keepalive keywords |
| `DATA_DIR` | `./data` | Python | Raw and processed PDFs only. Vectors live in Postgres |
| `RAW_PDF_DIR` | `./data/raw` | Python | |
| `PROCESSED_DIR` | `./data/processed` | Python | |

### 9.5 Crawler and FDA corpus worker

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `USER_AGENT` | see `config/settings.py:664` | Python | |
| `HTTP_TIMEOUT_S` | `30.0` | Python | |
| `CRAWL_CONCURRENCY` | `4` | Python | |
| `CRAWL_MIN_INTERVAL_MS` | `250` | Python | |
| `CRAWL_PACE_DIR` | `None` | Python | Unset keeps pacing in-process, correct for one worker. Set one shared path per host whenever more than one process crawls, or N processes each pace themselves and multiply request pressure by N |
| `DRUGSFDA_CACHE_TTL_S` | `86400.0` | Python | 0 disables |
| `ORANGE_BOOK_CACHE_TTL_S` | `86400.0` | Python | 0 disables |
| `DRUGSFDA_ZIP_MAX_BYTES` | `134217728` | Python | |
| `ORANGE_BOOK_ZIP_MAX_BYTES` | `33554432` | Python | |
| `FDA_CORPUS_PDF_MAX_BYTES` | `209715200` | Python | |
| `FDA_CORPUS_PDF_MAX_PAGES` | `3000` | Python | |
| `FDA_CORPUS_PDF_PARSE_TIMEOUT_S` | `180.0` | Python | |
| `FDA_CORPUS_CANARY_DOCUMENTS` | `5` | Python | Aborts a run whose first N processed documents all failed. 0 disables |
| `FDA_CORPUS_MAX_CONSECUTIVE_FAILURES` | `10` | Python | Catches a systemic failure that starts mid-run. 0 disables |
| `FDA_CORPUS_TERMINAL_ATTEMPTS` | `4` | Python | Must be in [2, 20]. Preserves the initial attempt plus three Dagster retries |
| `FDA_CORPUS_TEMP_DIR` | `None` | Python | `None` uses the OS temp directory. The full corpus must never accumulate under `DATA_DIR` |
| `FDA_ARTIFACT_STORE` | `discard` | Python | `discard`, `filesystem` or `s3` |
| `FDA_ARTIFACT_DIR` | `./data/fda_corpus/artifacts` | Python | |
| `FDA_ARTIFACT_S3_BUCKET` | `None` | Python | |
| `FDA_ARTIFACT_S3_PREFIX` | `regwatch/fda-corpus` | Python | |
| `FDA_ARTIFACT_S3_ENDPOINT_URL` | `None` | Python | |
| `FDA_ARTIFACT_S3_REGION` | `None` | Python | |
| `FDA_ARTIFACT_S3_ACCESS_KEY_ID` | `None` | Python | Prefer the platform's workload identity over explicit keys |
| `FDA_ARTIFACT_S3_SECRET_ACCESS_KEY` | `None` | Python | Secret |
| `FDA_ARTIFACT_S3_SESSION_TOKEN` | `None` | Python | Secret |
| `FDA_ARTIFACT_S3_SSE` | `AES256` | Python | `AES256` or `aws:kms` |
| `FDA_ARTIFACT_S3_KMS_KEY_ID` | `None` | Python | |
| `FDA_CORPUS_OCR_ENABLED` | `true` | Python | OCR runs only inside the killable PDF-parser child |
| `FDA_CORPUS_OCR_BINARY` | `tesseract` | Python | Invoked without a shell |
| `FDA_CORPUS_OCR_LANGUAGE` | `eng` | Python | |
| `FDA_CORPUS_OCR_DPI` | `200` | Python | Must be in [72, 400] |
| `FDA_CORPUS_OCR_PAGE_TIMEOUT_S` | `60.0` | Python | Must be in (0, 300] |
| `FDA_CORPUS_OCR_MAX_PAGES` | `500` | Python | Must be in [1, 3000] |
| `FDA_CORPUS_OCR_MAX_PIXELS` | `20000000` | Python | |
| `FDA_CORPUS_OCR_MAX_OUTPUT_BYTES` | `10485760` | Python | |
| `FDA_CORPUS_OCR_MEMORY_LIMIT_BYTES` | `2147483648` | Python | |

### 9.6 PDF ingest safety (cron and ingest worker only)

None of these is reachable from the API; parsing runs only in the CLI and cron
ingest path. Set any to 0 to disable that guard.

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `PDF_MAX_BYTES` | `52428800` | Python | Real PSG PDFs are under 2 MiB |
| `PDF_PARSE_TIMEOUT_S` | `60.0` | Python | Enforced by running the parse in a killable child. 0 parses in-process |
| `PDF_MAX_PAGES` | `500` | Python | Checked by both engines before any per-page extraction |

### 9.7 White paper populator

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `WHITEPAPER_TEMPLATE_PATH` | `./CRA White Paper Template May 2026 - Raja.docx` | Python | Gitignored, so absent in CI and in the image. The container entrypoint defaults it to `/app/data/templates/cra_white_paper_template.docx` (`docker/entrypoint.sh:7`) |
| `WHITEPAPER_TEMPLATE_URL` | `None` | Python | A long-lived signed URL, delivered as a Fly secret. Fetched and cached lazily on first render. Any fetch failure keeps the loud fallback marker |
| `WHITEPAPER_BUILD_TIMEOUT_S` | `90.0` | Python | Sits under the UI's 120s bound for `POST /whitepaper` so the client is still listening when the audited 504 arrives. 0 disables |

### 9.8 Deficiency analysis (DefPredict)

Upload-then-analyze runs execute as background tasks inside the API process, a
deliberate documented exception to "the Fly image never parses a PDF".

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `DEFICIENCY_ANALYZE_TIMEOUT_S` | `600.0` | Python | On breach the run is marked error and the worker thread is abandoned. 0 disables |
| `DEFICIENCY_ANALYZE_CONCURRENCY` | `2` | Python | Dedicated capacity limiter, never the default anyio pool. Excess runs queue |
| `DEFICIENCY_RUN_STALE_MINUTES` | `20` | Python | An unfinished run older than this flips to error on read |
| `DEFICIENCY_PRECEDENT_TOP_K` | `3` | Python | |

### 9.9 Auth, CORS and internal transport

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `AUTH_COOKIE_SECURE` | `false` | Python, Go | Pinned `true` in `fly.toml:75`. Both runtimes warn and keep serving on the production-plus-insecure combination; neither refuses boot |
| `AUTH_SESSION_TTL_HOURS` | `72` | Python, Go | Go rejects a non-positive integer at boot. The cookie `Max-Age` mirrors the server-side TTL |
| `RATE_LIMIT_PER_MINUTE` | `30` | Python, Go | Per user on `POST /query` and `POST /assemble`. Since the step-5 cutover Go is the single rate-limit authority across `/query` and `/query/stream`. 0 or less disables |
| `TRUST_PROXY_HEADERS` | `false` | Go | Pinned `true` in `fly.toml:85`. The field is still declared in Settings so the env contract stays one list, but nothing Python-side reads it (`config/settings.py:852-861`) |
| `CORS_ALLOW_ORIGINS_CSV` | `http://localhost:3000,http://127.0.0.1:3000` | Python, Go | Pinned to the Vercel origin in `fly.toml:76` |
| `METRICS_TOKEN` | `None` | Python | Blank means unset means open, so a blank secret cannot arm the gate with an empty password |
| `INTERNAL_RAG_TOKEN` | `""` | Python, Go | Fail-closed. Must be the same value on both runtimes |
| `INTERNAL_RAG_URL` | falls back to `UPSTREAM_URL`, then `http://127.0.0.1:8000` | Go | Where `handleCompleteQuery` calls the Python compute endpoint |
| `RAG_TIMEOUT_S` | `240` seconds | Go | Finite deadline for the Go-to-Python buffered hop. Sized as `LLM_TIMEOUT_S` 60 times `LLM_MAX_RETRIES` plus one, plus margin. Must be a positive number |
| `GO_NATIVE_QUERY` | `false` | Go | Pinned `true` in `fly.toml:98`. State both, or a local run surprises you |
| `REQUIRE_DATABASE_URL` | `false` | Go | Pinned `true` in `fly.toml:90`. Python no longer reads it |
| `UPSTREAM_URL` | `http://127.0.0.1:8000` | Go | Must parse as `http(s)://host[:port]` or the proxy refuses to boot |
| `PORT` | `8080` | Go | The proxy's listen port. Must match `internal_port` in `fly.toml:127` |

### 9.10 Observability

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `SENTRY_DSN` | `None` | Python, Go | Off unless set. No question text ever reaches Sentry: `query_text` lives only in the `query_log` audit table and request bodies are never attached to events |
| `SENTRY_ENVIRONMENT` | `dev` | Python, Go | Pinned `production` in `fly.toml:77`. Its value is what arms the two production-only boot warnings |

### 9.11 Company

| Name | Code default | Consumer | Notes |
| --- | --- | --- | --- |
| `COMPANY_NAME` | `Amneal` | Python, Go | Reported on `GET /settings` |
| `COMPANY_APPLICANT_ALIASES` | `AMNEAL PHARMS,AMNEAL PHARMACEUTICALS,AMNEAL PHARMS LLC` | Python | Comma-separated, upper-cased on read. Used for Drugs@FDA sponsor matching |

## 10. Variables that are not `Settings` fields

These never appear in `config/settings.py`. `tests/test_env_example_drift.py`
keeps `.env.example` to Settings knobs only, which is why some of these are
absent from that file.

### 10.1 Container and entrypoint

Read by `docker/entrypoint.sh`.

| Name | Default | Effect |
| --- | --- | --- |
| `REGWATCH_INIT_DB` | `true` | Set `false` to skip the boot-time `regwatch init-db` (stamp guard, idempotent ensures, RLS) |
| `RELEASE_COMMAND` | unset | Fly sets `1` on the release machine. The entrypoint skips its pre-command init so `regwatch release` can migrate before the guard runs |
| `REGWATCH_DB_INITIALIZED` | unset | Exported to `1` after a successful `init-db`. The API lifespan reads it and re-asserts the provider dimension instead of re-running init (`src/regwatch/api/main.py:198-207`) |

The entrypoint also defaults `DATA_DIR`, `RAW_PDF_DIR`, `PROCESSED_DIR` and
`WHITEPAPER_TEMPLATE_PATH` to container paths before creating them
(`docker/entrypoint.sh:4-9`). Four command classes skip the pre-command init:
`regwatch release`, `alembic`, `regwatch-proxy`, and the Dagster
daemon/webserver pair, which instead runs the maintenance-safe
`authoritative-corpus-init-db`.

### 10.2 Tests and CI

| Name | Consumer | Notes |
| --- | --- | --- |
| `TEST_DATABASE_URL` | pytest | Required. Points at a disposable Postgres with pgvector. Each xdist worker derives its own `<db>_gwN` database from it (`tests/conftest.py:18`). CI sets it inline at `ci.yml:104`; `DATABASE_URL` is deliberately absent from that job |
| `REGWATCH_EVAL_LATENCY_P95_CEILING_MS` | `regwatch.eval.run_eval` | Overrides the blocking end-to-end p95 ceiling, default 180000.0 ms (`src/regwatch/eval/run_eval.py:138,143,195-215`). Validated, never silently defaulted: a non-numeric, infinite or non-positive value exits |

The eval's blocking quality floors are not environment variables. They are the
`THRESHOLDS` dict in `src/regwatch/eval/run_eval.py:106-109`:
`recall_at_k >= 0.80` and `citation_precision >= 0.70`. The separate `TARGETS`
dict is aspirational and never blocking. `--assert-prod-mode` compares the
answer-policy flags against `config/prod_mode.json`.

### 10.3 Dagster corpus worker

| Name | Consumer | Notes |
| --- | --- | --- |
| `DAGSTER_POSTGRES_URL` | Dagster instance config | Read by `docker/dagster.yaml:6`, not by Settings. Point it at a dedicated Postgres database, schema or role. Deliberately not SQLite on an ephemeral worker disk |

### 10.4 Next.js frontend

Read by `regwatch/frontend`, deployed separately on Vercel. These are not Fly
secrets.

| Name | Default | Notes |
| --- | --- | --- |
| `API_PROXY_TARGET` | `http://127.0.0.1:8000` | Server-side. Where `/api/*` rewrites go. The build **fails loudly** on Vercel if it is still the local default (`regwatch/frontend/next.config.mjs:11-22`) |
| `NEXT_PUBLIC_API_BASE` | unset | Browser-side override for the API origin. Cross-origin, so the SameSite session cookie will not ride along (`regwatch/frontend/lib/api.ts:37,322,962`) |
| `NEXT_PUBLIC_SENTRY_DSN` | unset | Single switch for both browser and server Sentry. Unset is a no-op (`regwatch/frontend/sentry.server.config.ts:2-8`) |
| `NEXT_PUBLIC_FIXTURES` | unset | Must be `1` for the `/fixtures` routes to render in a production build (`regwatch/frontend/app/fixtures/page.tsx:359`) |
