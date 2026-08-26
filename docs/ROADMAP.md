# RegWatch roadmap

Everything that is not done yet, plus the numbered production gates. This file
is the single owner of open work. If another document says an item is open or
closed and this file disagrees, this file wins.

What this file does not own:

- What actually serves a request today, and which flag is set in production:
  [`PRODUCTION_TRUTH.md`](PRODUCTION_TRUTH.md).
- Environment variables, flags and secrets:
  [`CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md).
- Stable design, pipeline stages, the answer gate, the data model and the
  INV-1..9 invariants: [`ARCHITECTURE.md`](ARCHITECTURE.md).
- Code that exists but does not run:
  [`BUILT_BUT_DORMANT.md`](BUILT_BUT_DORMANT.md).
- Why a past decision was made: [`DECISIONS.md`](DECISIONS.md).

Numbers like `#221` are GitHub issues on this repository.

Labels: **BLOCKER** stops external exposure. **SHOULD-HAVE** before launch.
**DECISION** needs a person to choose. **LATER** is optional.

Every item says what it is, why it matters, where the code is, and what done
means. Claims here were checked against the repository on 2026-08-26. Live
values that only a deployed system can answer are named as commands, not
copied.

---

## What blocks production

### Corporate SSO, and a rate limiter that is not per process

Gate 1. Application auth is finished: the Go edge owns cookie sessions,
ownership checks, and the login brute-force cap, and Fly terminates HTTPS. What
is missing is the corporate front door, OIDC or SSO against the company IdP
behind the IT TLS gateway. [`DECISIONS.md`](DECISIONS.md) defers that to IT with
cookie sessions as the pilot boundary, so the open work is either the gateway or
a signed acceptance of the pilot boundary.

Separately, the rate limiter is an in-memory sliding window per process
(`go/internal/api/ratelimit.go`, ported from `common/ratelimit.py`). With two
proxy machines running, the real fleet ceiling is about twice the configured
rate. The file says so itself.

- Why it matters: this is the exposure decision. Nothing else on this list
  unblocks external users.
- Where: `go/internal/api/` (`auth.go`, `sessions.go`, `ratelimit.go`),
  `fly.toml`.
- Done when: enterprise auth fronts the app, or the cookie-session layer is
  formally accepted as the pilot boundary, and rate limiting no longer scales
  with machine count.

### Restore drill, and a least-privilege database role

Gate 2. Nobody has ever rehearsed a restore. The scripted `restore_drill.sh` was
deleted in R5 and `scripts/` has no replacement. The application also still
connects with a full-privilege role.

- Why it matters: Lakebase is the only datastore. Every chunk, vector, session
  and audit row is in it. An unrehearsed restore is an untested backup.
- Where: [`DEPLOY.md`](DEPLOY.md) staging and restore-drill section,
  `src/regwatch/store/db.py`. The least-privilege half is polyglot step 7, so it
  can be folded into the Go strangler work below.
- Done when: a restore drill against staging has passed and the application
  connects with a role that cannot drop or alter schema, or both are formally
  waived in writing.

### UI smoke and load behind the approved gateway

Gate 4. The UI is feature complete and deployed on Vercel. What is left is
deploy-time proof for the exposure decision, not code.

- Why it matters: the smoke checklist is the only end-to-end evidence that the
  approved auth path, the same-origin `/api` proxy and the analyst flows work
  together.
- Where: `regwatch/frontend/`, the smoke checklist in [`DEPLOY.md`](DEPLOY.md).
- Done when: the smoke flows pass behind the approved auth path and a load test
  has run against it.

### Decide whether a data-residency control is wanted (DECISION)

Gate 5. This item used to claim a shipped, tested residency guard. That guard
does not exist. `D1_ENFORCED` and `D1_ALLOWED_LLM_MODELS` appear nowhere in
`src/`, `config/` or `go/`. The only related code is `class D1ResidencyError` at
`src/regwatch/generate/llm.py:429`, which several call sites catch and re-raise
deliberately (`generate/grounded_qa.py`, `deficiency/structured.py`,
`deficiency/detection/`) but which no provider ever raises. Only tests construct
it. There is no `tests/test_d1_guards.py`.

The factual position today: generation and embeddings both call OpenAI, an
external vendor, on every normal question. Only the database stays in the
company tenant. That is the intended result of the 2026-08-20 owner decision,
not a bypass.

So the open question is a product and compliance decision, not a bug: does
RegWatch want a runtime residency control at all now that the model calls are
deliberately external? If yes, someone has to define what it fences (a served
model allowlist, a per-tenant provider pin, or an egress policy) and give
`D1ResidencyError` a real raise site. If no, delete the exception class and the
catch sites with it.

- Why it matters: the exception type shapes real error handling. The SSE
  fallback in `llm.py` re-raises it instead of retrying, on the theory that a
  residency violation must never be re-sent. That machinery is currently
  unreachable, and a reader can easily mistake it for an armed control.
- Where: `src/regwatch/generate/llm.py`, and every module that imports
  `D1ResidencyError`.
- Done when: the decision is recorded in [`DECISIONS.md`](DECISIONS.md) and the
  code matches it in either direction.

---

## What is next

### Corpus: review the curated inventory and decide the serving flip (#257)

Gate 9. The complete-universe target is dead. The 140,438 source records in the
frozen manifest can never all be indexed: the Lakebase branch is capped at
512 MiB, and `config/settings.py:508-516` records the 2026-08-18 amendment that
made the full universe permanently unreachable. Activation now counts against a
curated manifest whose sha256 an operator names explicitly in
`REGWATCH_SERVING_MANIFEST_SHA`. Unset keeps the old complete-universe behavior,
which now means never activating.

Open work: review what the curated manifest actually contains, decide whether it
is enough to serve, then run acceptance, retrieval and citation evaluation,
serving smoke, and a rollback rehearsal before moving
`REGWATCH_RETRIEVAL_CORPUS` off `legacy`. Never freeze a second manifest
under a running backfill.

- Why it matters: retrieval still serves the legacy PSG index. The FDA corpus
  work is built and not serving anyone.
- Where: `src/regwatch/corpus/`, `src/regwatch/sources/policy.py`,
  `Dockerfile.corpus-worker`, and
  [`AUTHORITATIVE_FDA_CORPUS.md`](AUTHORITATIVE_FDA_CORPUS.md). Status comes
  from `regwatch authoritative-corpus-status`.
- Done when: every record in the named manifest resolves as indexed or as an
  evidence-backed terminal outcome, indexed chunks have full selected-profile
  coverage, status reports `activation_ready=true`, evaluation passes, and the
  cutover and its rollback have both been rehearsed.

### Post-deploy smoke against the live stack

On 2026-08-03 an answer-path regression reached real users and the whole CI
suite was blind to it. `deploy.yml` verifies that machines are running
(`scripts/fly-verify-machines.sh`) but never asks the deployed stack a real
question.

- Why it matters: machine health is not answer health. The failure class that
  actually hurt users is invisible to every current gate.
- Where: `.github/workflows/deploy.yml`, `src/regwatch/eval/`.
- Done when: a post-deploy job asks one real question end to end and fails the
  deploy, or pages, on anything other than an answered, cited turn.

### Observability export

Gate 6. Structured logging, audit rows, privacy-scrubbed Sentry wiring,
`/health` component diagnostics, `/ready` and `/metrics` counters all exist.
`/metrics` is hand-rolled Prometheus text built by aggregating `query_log`
(`src/regwatch/api/main.py:598-660`), so it emits counters only.

Missing: latency histograms (per-turn latency is stored in
`query_log.latency_ms` by both runtimes but never exported), cost gauges,
tracing, a configured production Sentry DSN, and a decision on whether a paid
LLM reachability probe is worth the noise.

- Why it matters: you cannot see a latency regression until a user reports it.
- Where: `src/regwatch/common/observability.py`, `common/audit.py`,
  `api/main.py`, `go/internal/api/`.
- Done when: latency and cost are exported and scraped, tracing is on, and
  production error tracking is configured.

### Watch operations

Gate 7. The cron is live. `.github/workflows/watch-daily.yml` crawls, matches,
ingests, writes durable alerts and a digest, notifies Slack on failure, and
pings a healthcheck.

It needs three repository secrets: `WATCH_DATABASE_URL`, `OPENAI_API_KEY`, and
`WATCH_ACTIVE_EMBEDDING_PROFILE`, the last validated against `^ep_[0-9a-f]{32}$`
before checkout. Two more are optional and only affect notification:
`SLACK_WEBHOOK_URL` and `WATCH_HEALTHCHECK_URL`. The workflow hardcodes the
OpenAI provider and model in its own env block. Earlier versions of this file
demanded six secrets named for a Qwen embedding endpoint; the workflow no longer
references any of them, and that item is dead.

Open work is operational, not code: keep the three secrets provisioned and
matching what the application serves, watch real run history, and decide whether
alerts should move beyond `/watch/latest` plus Slack into product-facing email
or digests. Deferred from the July watch wave: alert acknowledgement state and
durable parsed text.

- Why it matters: this cron is the only production driver of the watch pipeline.
  A silent failure leaves analysts on stale FDA guidance.
- Where: `src/regwatch/watch/run.py`, `watch/alerts.py`,
  `.github/workflows/watch-daily.yml`, and
  [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md).
- Done when: the cron runs on the same embedding profile the application serves,
  run history is monitored, and an outbound alert channel is chosen.

### Eval hardening

Gate 8. The merge gate now scores the arm production serves, so the largest gap
in this section is closed (see Recently closed). What is still open:

- The blocking floors are a ratchet, not a quality bar. They live in
  `THRESHOLDS` in `src/regwatch/eval/run_eval.py` and were each set just below
  the first real measurement. The aspirational `TARGETS` in the same file have
  never been shown reachable on real geometry.
- `refusal_accuracy` is measured but not gated, by owner decision, because the
  product is moving away from refusing. Its 16 `must_refuse` rows in
  `gold_set.jsonl` come out when that direction lands.
- Scoring is mechanical: `(short_name, page)` plus `expected_facts` substrings.
  A correct answer worded differently undercounts. LLM-as-judge is not wired.
- The CI eval runs against a small seeded corpus, not the production corpus.

- Why it matters: a gate that can only ratchet cannot tell you the product got
  better, only that it did not get much worse.
- Where: `src/regwatch/eval/`, `gold_set.jsonl`, `tests/test_eval_gate.py`,
  [`EVAL_STATUS.md`](EVAL_STATUS.md).
- Done when: LLM-as-judge sits alongside the mechanical metrics and the
  aspirational targets are met rather than aspired to.

### Calibrate the refusal floor and the confidence band (#218)

The refusal floor withholds any passage scoring below it before the synthesizer
runs. `Settings.effective_refusal_threshold()` resolves a per-profile floor from
`REFUSAL_SCORE_THRESHOLD_BY_PROFILE` and falls back to the global default
(`config/settings.py:120-147`). The global default was validated against a
1536-dimension OpenAI space that no longer exists.

The live space is OpenAI `text-embedding-3-large` truncated to 1024 dimensions,
a different cosine distribution, so that validation does not carry over.
Earlier versions of this item blamed Qwen3 embeddings; that is doubly stale,
since Qwen3 was itself replaced on 2026-08-20.

`REFUSAL_SCORE_THRESHOLD_BY_PROFILE` defaults to an empty dict in the
repository, so the live per-profile floor is a platform secret this checkout
cannot read. [`CLAUDE.md`](CLAUDE.md) records a per-profile floor measured on
2026-08-20 against 40 gold questions and 8 off-corpus controls. Treat that as
unverified from code, and confirm it against the deployment before you decide
whether #218 still has a calibration gap or only a documentation one.

The UI compounds this. `confidenceBand()` in `regwatch/frontend/lib/turns.ts`
cuts High from Moderate at a fixed fraction of the headroom above the live
floor. An uncalibrated floor makes the band an uncalibrated label on every
answer.

The advisory sweep in the watch cron cannot settle it alone: its must-refuse
cases stop before retrieval and produce no cosine scores. Add scored hard
negatives and rerun.

- Why it matters: the floor decides what an analyst never sees, and the band
  tells them how much to trust what they do see.
- Where: `config/settings.py`, `src/regwatch/eval/threshold_sweep.py`,
  `regwatch/frontend/lib/turns.ts`. Read the deployed value with
  `GET /settings` or `regwatch status`; it is a platform secret, not a
  repository value.
- Done when: a per-profile floor is calibrated on scored hard negatives for the
  profile in service, recorded in `REFUSAL_SCORE_THRESHOLD_BY_PROFILE`, and the
  band cut is rechecked against it.

### Structured-claims follow-ups (#272)

Block-tagged claims shipped, along with the fixes that followed. The remaining
watch items from that merge are tracked on the issue.

- Where: `src/regwatch/generate/prose_turn.py`,
  `src/regwatch/generate/turn_gate.py`.
- Done when: #272 is closed with its watch items either fixed or re-filed.

### Finish the Go strangler (#243)

Steps 0 to 5 of [`POLYGLOT_TARGET_2026-07-10.md`](POLYGLOT_TARGET_2026-07-10.md)
are done. Step 8, the Rust PDF CLI, was cancelled on 2026-08-19; PDF parsing
stays in Python permanently and the target is Python, TypeScript and Go.
Remaining:

- **The step-5 deletion PR.** Python still registers its own `POST /query` at
  `src/regwatch/api/main.py:1010` even though the Go edge orchestrates the live
  path. Two rules gate the deletion, and they still bind:
  - No INV test may be lost. A test that dies with the Python route must have a
    named contract-test successor in `tests_contract/` before the route goes.
  - A test with no mapped successor blocks the deletion.

  The five coverage gaps that mapping found are all closed, each by a contract
  test that already exists: 422 validation on native `/query` (S28,
  `tests_contract/test_query_auth.py`), NULL-owner session adoption through the
  edge (S29, `test_sessions_cross_runtime.py`), the INV-5 filter whitelist at
  the edge (S30, `test_query_outcomes.py`), owner-preservation on a hijack (S5,
  `test_query_auth.py`), and the second-user fresh rate-limit budget (S18,
  `test_sessions_cross_runtime.py`). The gate condition is therefore met; what
  remains is the rewire-and-delete pass itself. The full per-test mapping is in
  git history at `docs/archive/STEP5_INV_TEST_MAPPING.md`.
- **R3**, the stream terminal-frame move. It has to preserve the SSE fallback's
  special handling of `D1ResidencyError`, which the residency decision above may
  change.
- **Steps 6, 7 and 9**: coarse write commands for ingest and Watch (6), Python
  down to a read-only database role (7, the least-privilege item above), and
  `CommitWhitepaperRun` plus deletion of the Python persistence layer (9).
- CI enforcement of the language boundary is part of the issue and is not built.

- Why it matters: two implementations of `POST /query` is two places for the
  answer contract to drift.
- Done when: #243 is closed, with the deletion PR merged and the boundary
  enforced in CI.

### Compliance Studio backend, steps 3 to 7 (#216)

Studio is a three-way split today, not a mockup and not a finished product:

- Real and wired: the PSG reference rail and the chat assistant call live
  endpoints (`regwatch/frontend/app/studio/page.tsx:25` imports `askQuery`,
  `fetchPsgLibrary`, `fetchPsgContent` and `fetchPsgRequirements`).
- Real but not wired: `POST /studio/check` and `GET /studio/check/{run_id}`
  exist and persist runs (`src/regwatch/api/main.py:2839` and `:2876`), and the
  frontend says so at `page.tsx:62`. Nothing in the UI calls them.
- Fixtures: the working-document set and its findings, in
  `regwatch/frontend/lib/studio-fixtures.ts`. A refresh destroys every recorded
  disposition.

Steps 3 to 7 on #216 are the remaining backend work. The two prerequisites that
are not about the check endpoint: Studio needs persistence for dispositions
(`localStorage` is not acceptable for GMP records), and it sits outside
`app/(shell)/` so it never sees `CurrentProductProvider` and its tree cannot yet
be "the documents for the product under review".

- Where: `regwatch/frontend/app/studio/`, `src/regwatch/api/main.py`,
  `src/regwatch/deficiency/`, [`COMPLIANCE_STUDIO.md`](COMPLIANCE_STUDIO.md).
- Done when: the UI calls the real check endpoint, dispositions survive a
  refresh, and Studio reads the product scope.

### Retire v5 and make the live answer policy the code default

These are the items from the prompt-layer execution plan that never shipped.
That plan is no longer a living document; it is recoverable from git history.

- `prose_synthesis_enabled` still defaults to `False` in
  `config/settings.py:171` and `selective_citation_enabled` still defaults to
  `False` at `:210`, while `fly.toml` pins both to `"true"`. A cleared platform
  secret would silently reinstate the retired prompt with no deploy and no
  diff.
- The v5 claims-JSON path is still in the tree: `GROUNDED_QA_PROMPT`,
  `TURN_SCHEMA_MESSAGE`, the echo JSON synthesizer branch and `synth_turn_json`.
  Delete it as one change once the defaults flip.
- Route promotion. `REGWATCH_ROUTE_CALL` has three modes, off, shadow and live.
  Live is implemented (`grounded_qa.py:2477-2498`, PRODUCT scope only) and the
  route prompt is at v2 (`generate/route.py:104`). The default is off and the
  promotion decision needs shadow data.
- Clarify from the catalog with candidates named from retrieval, and the decline
  copy rewrite that removes `refusal_accuracy` and its gold rows.
- Converse mode behind `REGWATCH_CONVERSE_MODE`. The flag does not exist in
  `config/settings.py`; nothing is built.

- Why it matters: the deployed answer policy is currently one unset secret away
  from a silent rollback.
- Where: `config/settings.py`, `src/regwatch/generate/prompts.py`,
  `generate/grounded_qa.py`, `generate/route.py`.
- Done when: each default flip lands as its own change proved by a green
  blocking eval, and the v5 path is deleted.

### Bounded corpus-scoped Ask (#163)

The scope compiler produces PRODUCT, CORPUS, CLARIFY or CONVERSE, but a
CORPUS decision only compiles and audits; it never executes. The separate
corpus-scope flag the plan called for does not exist in `config/settings.py`.
Until it does, a question about a guidance family with no named product
clarifies instead of searching.

- Why it matters: "what do the PSGs say about X" is a question analysts ask and
  the product cannot answer.
- Where: `src/regwatch/retrieve/scope.py`, `retrieve/scope_catalog.py`,
  `generate/grounded_qa.py`.
- Done when: a corpus turn retrieves only from an application-compiled, bounded,
  allowlisted set of `version_id`s, with zero leakage outside that set, behind a
  flag that defaults off.

### Give Ask real tools (#268)

`src/regwatch/generate/llm.py` sends no `tools` and parses no tool calls. Every
answer comes from one retrieval pass and one synthesis call.

- Why it matters: questions that need a lookup the retriever cannot express
  (a count, a date comparison, a structured field) currently fail as low-score
  refusals.
- Where: `src/regwatch/generate/llm.py`, `generate/grounded_qa.py`.
- Done when: #268 defines the tool set and at least one tool executes under the
  same gate and audit rules as retrieval.

### Query preparation latency (#221)

Query embedding sits on the post-submit critical path. A proposed design
prepares the canonical `retrieval_query` digest and its vector while the user is
still typing, so a hit removes the embedding call from the measured turn. The
design write-up is no longer a living document. Nothing was ever built (there
is no `prepare` seam in `src/` or `go/`), and the idea is recorded on #221.

- Why it matters: it is the largest identified saving on Ask latency that does
  not depend on a vendor.
- Done when: #221 is closed, either by building it or by recording why not.

### The deficiency precedent KB cannot be populated

The analyzer is live behind no flag (`POST /deficiency/analyze`,
`GET /deficiency/runs[/{id}]`). Precedents are always absent because the KB is
always empty: `add_entries` in `src/regwatch/store/deficiency_kb.py:120` has no
caller anywhere in `src/`, `tests/` or `scripts/`, and its own docstring calls
itself the loader seam. `deficiency/precedents.py` short-circuits at
`kb_count() == 0`.

When something does load it, the dimension gate applies:
`precedents.py:35` builds `get_embedding_provider("openai")` and raises unless
the provider width equals `KB_EMBEDDING_DIM`, which is 1024. The OpenAI provider
already defaults to 1024, so the configuration hazard the old text described is
gone.

Also open: decide whether PDF parsing inside the API process survives past MVP.
A durable queue is the documented upgrade path.

- Where: `src/regwatch/deficiency/`, `src/regwatch/store/deficiency_kb.py`.
- Done when: a loader writes real precedent entries, or the precedent feature is
  removed.

### Persist and cite beyond the White Paper

Gate 9 adjacent. The persist-and-cite plus freshness pattern (source provenance
with `last_fetched_at`, multi-source synthesis) is wired for the White Paper
only. The Ask and Assemble read paths still query live HTTP without persisting
source rows or freshness.

- Where: `src/regwatch/sources/`, the Q&A and assemble handlers.
- Done when: every cited non-corpus source is persisted with a fetch timestamp
  on the path that cites it.

### Answer-gate follow-ups

- `MATERIALITY_WORDS` in `src/regwatch/generate/turn_gate.py:114` is a
  hand-written literal and could be narrowed from logged traffic.
- `filter_citations` in `src/regwatch/common/citations.py:77` has no production
  caller left, only `tests/test_citations.py`. Retire the function and its tests
  together, or keep it deliberately and write down why.

### Secrets policy

Gate 10. `.env` and friends are gitignored, the Actions secret surface is
documented in [`SECRETS_RUNBOOK.md`](SECRETS_RUNBOOK.md), and production uses
Fly and Vercel platform secrets. Still needed: an approved secret manager or
platform policy, and key rotation that is documented and rehearsed.

- Done when: secrets come from approved injection and a rotation has been run,
  not just written down.

### Container resource limits

Gate 11 residual. Neither `compose.yaml` nor `fly.toml` sets any CPU or memory
limit. The supply-chain gates around them are green and enforced.

- Done when: both files carry explicit limits sized from observed usage.

### Watchlist management for non-technical users

Watchlist products and watched products are managed through the API and the CLI.
`regwatch/frontend/lib/api.ts` has no watchlist call, so there is no in-app way
for an analyst to add or manage them.

- Where: `regwatch/frontend/` (Watch surface),
  `src/regwatch/watch/watchlist.py`.
- Done when: an analyst can add, remove and review watched products in the UI.

### Evidence drawer accessibility

The drawer sets `aria-modal="true"`
(`regwatch/frontend/components/EvidenceDrawer.tsx:84`) and ships a scrim, but
nothing in the frontend sets `inert` on the shell behind it, so a screen reader
or a Tab key can still reach the page underneath.

- Done when: the shell behind an open drawer or panel is `inert`.

### White Paper phase 3

Batch populate, watch-driven staleness flagging, run-to-run section diff, and
overlay edit history. The design document was archived; it is recoverable from
git history.

- Where: `src/regwatch/whitepaper/`.

---

## Production gates

The numbered checklist that used to live in a separate readiness document. The
numbers are kept because other documents and past discussions cite them by
number.

| # | Gate | State |
|---|---|---|
| 1 | API auth and authorization | SSO and shared-store rate limiting open |
| 2 | Production datastore | Live; restore drill and least-privilege role open |
| 3 | Migration discipline | Done |
| 4 | Production UI | Deployed; smoke and load open |
| 5 | LLM provider and data handling | No residency control exists |
| 6 | Observability | Counters and logs done; metric and trace export open |
| 7 | Ingest and watch scheduling | Cron live; operational follow-up open |
| 8 | Eval hardening | Gate scores the production arm; judge and targets open |
| 9 | Structured-source layer | Built; activation decision open |
| 10 | Secrets management | Platform secrets in use; policy and rotation open |
| 11 | Supply chain and security in CI | Done; container resource limits open |

Detail for each open gate is in the sections above. Notes on the gates that are
done or that need a fact restated in one place:

**Gate 1, what is already in place.** Every product endpoint sits behind
cookie-session auth. Four operational probes do not: `GET /health`,
`GET /ready`, `GET /metrics` (bearer-gated when `METRICS_TOKEN` is set) and
`GET /livez`. Session tokens are opaque and stored as sha256, passwords are
bcrypt, and users are CLI-provisioned. Chat
history is per user with ownership checks, so a foreign `session_id` returns
404. Audit rows carry the caller (INV-6). `POST /query` and `POST /assemble` are
rate limited per user, and login is capped per email and per IP. One value has
to match on both sides of the internal seam: `INTERNAL_RAG_TOKEN` gates
`/internal/query/compute`, which the Go proxy calls when native query
orchestration is on. If the two do not match, native `POST /query` fails.

**Gate 2, what is already in place.** Production Postgres is Databricks Lakebase
with pgvector in the same database. It is the only datastore. `DATABASE_URL` is
required and the application refuses to boot without it; pgvector dimension
checks fail fast. The branch is capped at 512 MiB, which is what killed the
complete-universe corpus target. Check headroom with `pg_database_size` before
any bulk write. The current migration head is whatever `alembic heads` reports;
what is deployed is a live fact, so read it from the deployment rather than from
a document.

**Gate 3 is done.** `release_command = "regwatch release"` runs in a one-off
machine before machines roll. It advances Alembic to head and then runs the same
serving-readiness guard as a cold boot, so a bad migration or embedding-profile
drift fails before any long-lived machine is replaced. Rollback and roll-forward
rehearsal is folded into the restore drill in gate 2. Migrations still have to
be backward compatible and reversible.

**Gate 4, what is already in place.** The scoped surfaces render inside one App
Router `(shell)` group with one sidebar and one set of tokens. A URL-scoped
current product (`?rp=&appl=`) is shareable and survives reload. Streaming is
done: `POST /query/stream` emits provisional `token` and `draft` frames and then
one validated terminal `result` frame, which is the only authoritative one, so
INV-1 holds. That endpoint is served by Python; the Go edge only rate-limits in
front of it.

**Gate 5, the residency position.** See the decision item above. State it
plainly to anyone who asks: generation and embeddings go to OpenAI on every
normal question, and only the database stays in the company tenant.

**Gate 11 is done.** CI gates on `pip-audit`, `npm audit` for frontend
production dependencies, and Trivy scans of the API, corpus-worker and web
images. The only residual is the container limits item above.

---

## Deferred

- **Cross-encoder reranker.** Exists as a hook, off by default
  (`reranker_enabled`, `config/settings.py:531`). Turn it on and tune
  `VECTOR_TOP_K` if retrieval precision needs it. Retrieve and rerank failures
  on the ask path are audited under INV-6.
- **MMR diversity.** Implemented in `src/regwatch/retrieve/diversity.py` behind
  `REGWATCH_MMR_DIVERSITY`, default off (`config/settings.py:540`). Flipping it
  needs an eval A/B first.
- **ANN retrieval.** `RetrievalMode.ANN_RERANKED` exists but
  `assert_mode_permitted()` in `src/regwatch/retrieve/mode.py` raises
  unconditionally, and there is no HNSW index on the live arm because of the
  512 MiB cap. Retrieval is an exact pgvector scan and will stay one until the
  storage decision changes.
- **Graph-assisted retrieval.** The Tier-1 graph tables are written by ingest
  and the CLI, and nothing reads them at runtime. The proposed consumer and its
  gates are in
  [`GRAPH_ASSISTED_RETRIEVAL.md`](GRAPH_ASSISTED_RETRIEVAL.md). It stays
  default-off until the eval set is bigger and the graph path shows fewer false
  refusals with no citation, scope, latency or context-budget regression.
- **Product shape: collapse to two surfaces.** Owner direction from 2026-08-05
  was to end with Ask and Studio, folding Assemble, Watch, White Paper and
  Deficiency into Studio. Nothing has moved and nothing should: the four
  surfaces being folded in have working backends and Studio mostly does not, so
  folding now would trade shipped functionality for a mockup. The Studio backend
  work (#216) is the prerequisite. One open question either way: whether
  Deficiency folds in as a check on an open document or stays a whole-submission
  upload, since it takes a PDF today rather than a document from a tree.
- **Further corpus families.** None are approved. Any expansion needs an
  explicit policy and product decision.
- **Kubernetes or Helm.** Only if the deployment outgrows Fly and Compose.
- **Old Supabase project cleanup.** Two dangling `auth.users` rows, an
  `auth.sessions` row with `not_after = NULL`, and a `REVOKE` on the public
  schema that was never applied. Production moved to Lakebase, so this only
  matters if that Supabase project still exists. Unverified: it cannot be
  checked from this repository.

---

## Recently closed

Kept short, and only for items this file used to list as open. History lives in
[`DECISIONS.md`](DECISIONS.md).

- **The blocking eval now scores what production serves.** Closed 2026-08-25.
  `.github/workflows/ci.yml:84-92` calls the `openai-eval` workflow with
  `prose: true`, `selective: true` and `assert_prod_mode: true`, asserted
  against `config/prod_mode.json`, so a drift between the checked-in target and
  the scored arm fails the run. The old risk was that the gate scored the
  retired v5 arm while production served v7.
- **The Qwen watch-secret blocker.** Obsolete. `watch-daily.yml` needs three
  secrets and none of them names a Qwen endpoint.
- **The residency guard item.** Withdrawn, not completed. It described code that
  does not exist. Replaced by the decision item above.
- **The complete-universe corpus target.** Dead under the 512 MiB cap since
  2026-08-18. Replaced by the curated-manifest item above.
- **Rust in the polyglot target.** Step 8 cancelled 2026-08-19. PDF parsing
  stays in Python permanently.

---

## Suggested order

1. Review the curated corpus manifest and decide the serving flip (#257). It is
   the largest built-but-unserved asset in the tree.
2. Corporate SSO and a shared-store rate limiter. That is the exposure boundary.
3. Restore drill and least-privilege credentials. Fold least privilege into
   polyglot step 7 where you can.
4. Post-deploy smoke, then observability export.
5. Calibrate the refusal floor and the confidence band (#218), then revisit the
   eval targets.
6. Flip the answer-policy defaults and delete the v5 path.
7. Secrets policy and container limits.
8. UI smoke and load, watchlist UX, and the Studio backend (#216). The Go
   strangler work (#243) runs in parallel as capacity allows.
