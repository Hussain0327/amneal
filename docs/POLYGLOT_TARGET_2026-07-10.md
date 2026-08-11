# Polyglot Target Architecture - 2026-07-10 (APPROVED direction)

Last updated: 2026-08-11. **Steps 0 through 5 are done and live.** What is left
is the legacy-path deletion, hardening step R3, and steps 6 to 9.

Owner decision: all-Python is unacceptable, so regwatch moves to a four-runtime
architecture. This supersedes the "no new language" default of
`archive/POLYGLOT_ARCHITECTURE_REVIEW_2026-06-17.md` and the same-day
conservative verdict in `archive/POLYGLOT_ASSESSMENT_2026-07-10.md`. That was an
owner-level architecture call about bus factor and control-plane rigor, not a
measured performance result. The two archived docs remain the record of what is
and is not a perf-driven move.

Approved with two adjustments (the owner's):

1. Rust owns PDF and document processing first, NOT production embeddings.
2. Go owns database writes through coarse transactional commands, NOT a chatty
   CRUD microservice.

## Ownership map

| Runtime    | Owns |
|---|---|
| TypeScript | UI, browser state, generated API client |
| Go         | Public API, auth/SSO, authorization, rate limiting, sessions, audit, idempotency, SSE gateway, database writes and migrations |
| Python     | Stateless RAG, product resolution, retrieval policy, citation validation, LLM synthesis and extraction, Assemble and White Paper logic |
| Rust       | Pure PDF bytes into ordered pages and chunks. No database credentials, no business rules |

Coarse write commands, target four or fewer: CompleteQuery, CommitIngest,
CommitWhitepaperRun, CompleteWatchRun. Python keeps read-only DB access
(read-only role, and stable views where they stabilize a contract). Postgres
outbox tables handle delivery such as the Slack digest. No Kafka, Redis or NATS.

## Where the migration stands

Done, one line each:

- **Step 0a. Contract freeze.** `response_model` on every route, wider codegen, a
  committed `regwatch/frontend/openapi.json` snapshot, and a two-sided CI drift
  gate.
- **Step 0b. Ingest transaction.** A revision now lands atomically: the version
  row, the document's content fields, the version's pgvector chunk rows and its
  `be_requirement` row commit together, plus the deferred `psg_version` unique
  constraint (migration 0014).
- **Step 0c/0d.** PDF page-count bound in the parse child, and bounded
  parallelism plus a request deadline in the White Paper populator.
- **Step 1.** The public API contract is frozen (0a landed it).
- **Step 2.** Python RAG returns `RagOutcome` + `AuditPayload` + `SessionPatch`;
  a thin Python shell kept persisting them at the time.
- **Step 3.** Go transparent proxy in front, second process group in the same Fly
  app. See `docs/GO_PROXY_ROLLOUT.md`.
- **Step 4.** Auth, sessions, feedback, settings and product CRUD moved to Go,
  with sqlc for queries and a golangci-lint plus sqlc vet CI lane.
- **Step 5.** Query orchestration and atomic audit persistence moved to Go
  (CompleteQuery). Flipped live 2026-07-24. See
  `docs/GO_NATIVE_QUERY_ROLLOUT.md`.
- **R5, out of order but done.** SQLite and Chroma dual-mode deleted. Postgres
  plus pgvector is the only datastore, and the suite runs against a real
  Postgres via `TEST_DATABASE_URL`. Dagster went with it. See the R5 entry in
  `docs/DECISIONS.md`.

Left to do:

6. **Coarse write commands for ingest and Watch**: CommitIngest and
   CompleteWatchRun, porting the proven 0b semantics, plus an outbox for digest
   delivery.
7. **Python drops to a read-only DB role.** This is the open least-privilege
   credentials item. White Paper and Assemble persistence stays Python until
   steps 8 and 9.
8. **Rust PDF CLI**: build and artifact pipeline (C1 below), shadow parity (C2),
   cutover in the watch workflow, then delete the `_try_pdfplumber` and
   `_try_pypdf` paths. Nothing has been built yet; there is no `rust/` directory.
9. **CommitWhitepaperRun last** (the `whitepaper_runs.py` coupling). Delete the
   public FastAPI surface and the superseded Python persistence, and only then
   decide who owns the migration tool (C3).

Two items sit between step 5 and step 6:

- **The step-5 deletion PR.** Delete Python's buffered `POST /query` route and
  its dead persistence branches, and de-flag Go. Details in
  `docs/GO_NATIVE_QUERY_ROLLOUT.md`.
- **R3, the stream terminal-frame move.** See R3 below.

Gate for every remaining phase: the replaced Python path is deleted in the same
phase (measured in handwritten behavior-bearing LOC, excluding generated OpenAPI
and sqlc types), the full gate is green (pytest, mypy, ruff, black, tsc, eslint,
vitest, and the go/rust lanes), and no INV test is lost. A moved test must land
as a contract test before the Python original is deleted.

## Facts the remaining work still rests on

- **Empty-page preservation is a hard parser invariant**
  (`pdf_parser.py`): pages append as `""` rather than being dropped, so page
  indices stay 1:1 with the PDF and citations never shift. The Rust CLI must
  honor this.
- **Three in-process `ask()` callers exist**: `POST /query`, the `/assemble`
  dossier (a nested `ask()` with `bind_session=False`), and the White Paper
  populator. The stateless core has to serve all three, and their audit
  persistence routes diverge once Python loses write credentials: CompleteQuery
  for the API path, CommitWhitepaperRun for the WP and assemble paths. That is
  why WP persistence moves last.
- **Everything is in one Postgres** (Databricks Lakebase in prod since
  2026-07-28), so CommitIngest can be a single transaction.

## Corrections to the original proposal (mechanics, not shape)

**C1. There is no worker image in prod, so nothing can bundle the Rust
executable "inside the Python worker image".** Parsing runs on a GitHub Actions
ubuntu-latest runner via `uv run regwatch watch`; manual `ingest-all` runs on a
dev laptop (macOS arm64). The Fly image never parses a PDF and should stay that
way. Delivery instead: CI builds the Rust CLI as pinned release artifacts (Linux
x86_64 musl static, macOS arm64), sha256-pinned download in the watch workflow,
and a cargo build cache for the Rust CI lane.

**C2. Shadow parity "across the existing corpus" needs the raw bytes, and raw
PDFs are not durably stored.** `parsed_text_path` points at ephemeral runner
disk, and durable parsed text was deferred. Shadow plan: run the Rust CLI side
by side on every new download in the daily watch and log parity, then do a
one-off polite re-crawl of the catalog for full-corpus parity before cutover.
Parity gates: page count, empty-page positions, extracted citation quotes, chunk
page mappings.

**C3. Go does not own migrations during the strangler.** Alembic, run by Fly's
`release_command`, stays the single schema authority until the Python
persistence layer is deleted at step 9. Go consumes the schema via sqlc. Moving
migration ownership is the last move, not an early one. The live Alembic head is
`0020_eval_run`.

## Risks

Still live:

- **R2. Three `ask()` callers.** See the facts section above. Open until steps 8
  and 9.
- **R3. SSE.** Pre-validation provisional token streaming was deliberately
  reversed at commit 0a96f7e, because the per-sentence gate that guarded it
  could not survive the structured-turn contract. It came back on 2026-08-10 as
  the flag-gated `draft` channel under the owner-amended INV-1, and that is live
  in prod. See
  [`superpowers/specs/2026-08-10-ask-sse-live-draft-design.md`](superpowers/specs/2026-08-10-ask-sse-live-draft-design.md).
  The Go gateway must relay `status`, `token`, `draft`, `draft_reset` and
  `result` frames without reordering or buffering. `/query/stream` is still a
  pass-through proxy; the remaining R3 work is moving only the terminal-frame
  persistence to Go.

Closed:

- **R1. INV-6 became cross-service, and the contract harness landed**:
  `tests_contract/` (the S1-S23 matrix) plus the cross-service contract CI lane,
  as step-5 PR A.
- **R4. Auth split-brain resolved at step 4.** Go owns auth and sessions. Prod
  has since moved off Supabase entirely, so the half-staged Supabase Auth
  question is moot. The `Fly-Client-IP` / `TRUST_PROXY_HEADERS` login-spray
  limiter semantics were ported with it. OIDC in Go is still not built, and SSO
  remains an open prerequisite for exposing the app externally.
- **R5. Dual-mode deleted.** See the step list above.
- **R6. Deploy topology.** Go runs as a second process group in the same Fly
  app, holding the public port, with uvicorn on an internal port. Vercel still
  points at the same hostname, cookies and CORS unchanged. Alembic kept
  `release_command` (C3).
- **R7. D1.** This plan closed none of it, as predicted, and it did not have to:
  D1 was closed on the provider seam instead. Generation, embeddings and the
  database all sit inside the company's Databricks tenant as of 2026-08-11. See
  `docs/DATABRICKS_ADOPTION_2026-07-28.md`.

## LOC baseline (verified 2026-07-10, not re-measured since)

src Python 19,521; tests Python 20,036; frontend TS+TSX 10,353. Expectation
accepted at the time: Python LOC goes down, total handwritten LOC stays roughly
flat to +10-25% while Go owns every table's writes, with a temporary +30-50%
during the strangler. The metric is handwritten behavior-bearing LOC only.
