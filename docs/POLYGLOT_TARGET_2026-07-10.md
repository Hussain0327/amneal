# Polyglot Target Architecture - 2026-07-10 (APPROVED direction)

Owner decision: all-Python is unacceptable; regwatch moves to a four-runtime
architecture. This supersedes the "no new language" default of
`POLYGLOT_ARCHITECTURE_REVIEW_2026-06-17.md` and the same-day conservative
verdict in `POLYGLOT_ASSESSMENT_2026-07-10.md` as a matter of owner-level
architecture policy (bus factor / control-plane rigor), not measured perf.
The two docs remain the record of what is and is not a perf-driven move.

Approved with two adjustments (owner's):
1. Rust owns PDF/document processing first - NOT production embeddings.
2. Go owns database writes through coarse transactional commands - NOT a
   chatty CRUD microservice.

## Ownership map (approved)

| Runtime    | Owns |
|---|---|
| TypeScript | UI, browser state, generated API client |
| Go         | Public API, auth/SSO, authorization, rate limiting, sessions, audit, idempotency, SSE gateway, database writes and migrations |
| Python     | Stateless RAG, product resolution, retrieval policy, refusal/citation validation, LLM synthesis/extraction, Assemble and White Paper logic |
| Rust       | Pure PDF bytes -> ordered pages/chunks; no database credentials, no business rules |

Coarse write commands (target: <= 4): CompleteQuery, CommitIngest,
CommitWhitepaperRun, CompleteWatchRun. Python keeps read-only DB access
(read-only role; stable views where they stabilize a contract). Postgres
outbox tables for delivery (Slack digest etc.); no Kafka/Redis/NATS.

## Verified facts the plan rests on (checked 2026-07-10)

- ask() coupling is real: `grounded_qa.py:27` imports `log_query` (INV-6
  audit), :34-42 imports ensure_session/get_session_filters/
  update_session_filters, :318-341 writes session filters + messages,
  :698-709 `_log_query_or_skip` implements audit-with-defined-failure.
  `def ask(` is :1025; 1,604-line file.
- Ingest dual-write gap is real but narrower than "no transaction":
  `pipeline.py:199-239` `_commit_version_and_doc` puts version+doc in ONE
  transaction; chunks (pgvector upsert), BE requirements, and alerts land
  OUTSIDE it, and `psg_version` lacks a unique (psg_document_id,
  content_hash) constraint (docstring calls the migration deferred).
  Everything is in one Supabase Postgres now, so CommitIngest can be one
  transaction.
- Empty-page preservation is a hard parser invariant (`pdf_parser.py:88-91`):
  pages append as "" rather than drop, so page indices stay 1:1 with the PDF
  and later citations never shift. The Rust CLI must honor this.
- LOC (verified): src Python 19,521; tests Python 20,036; frontend TS/TSX
  10,353. pdf_parser 212 + chunker 105 = 317; embedder 252 (=569 with it).
  main.py 1,845 + auth/ 283 = the core of the ~2.6k control-plane estimate.
- Dual-mode is live: chroma refs in cli/pipeline/retriever/threshold_sweep/
  main; sqlite branches in store/db.py. Dagster is dormant
  (`INSTALL_ORCHESTRATION=false` in prod; orchestration/ + extra exist).
  (Status as of Jul 10; R5, done: the SQLite/Chroma dual-mode described here
  has since been deleted — Postgres + pgvector is the only datastore. See
  `docs/DECISIONS.md`'s R5 entry.)
- Migration head is 0013 (alembic; Fly release_command is the authority).

## Corrections to the proposal (mechanics, not shape)

C1. "Bundle the Rust executable inside the Python worker image" - there is
    NO worker image in prod. Parsing runs on a GitHub Actions ubuntu-latest
    runner via `uv run regwatch watch` (watch-daily.yml:65,140); manual
    `ingest-all` runs on a dev laptop (macOS arm64). The Fly image never
    parses a PDF and should stay that way. Delivery instead: CI builds the
    Rust CLI as pinned release artifacts (linux x86_64 musl static + macOS
    arm64), sha256-pinned download in the watch workflow; cargo build cache
    for the Rust CI lane.

C2. Shadow parity "across the existing corpus" requires the raw bytes, and
    raw PDFs are NOT durably stored (parsed_text_path points at ephemeral
    runner disk; durable parsed text was explicitly deferred). Shadow plan:
    (a) run the Rust CLI side-by-side on every NEW download in the daily
    watch and log parity, and (b) a one-off polite re-crawl of the ~1,795
    catalog for full-corpus parity before cutover. Parity gates: page count,
    empty-page positions, extracted citation quotes, chunk page mappings.

C3. "Go owns migrations" - not during the strangler. Alembic (0013, run by
    Fly release_command) stays the single schema authority until the Python
    persistence layer is deleted (step 9); Go consumes the schema via sqlc.
    Moving migration ownership is the LAST move, not an early one.

## Risks to engineer around (accepted, with mitigations)

R1. INV-6 becomes cross-service. Audit-with-defined-failure (the 009cc41
    pattern: pending operation -> finalize, failure-safe row) moves to Go's
    transaction. The INV tests that prove "failure still leaves an audit
    row" must be reborn as cross-service contract tests (compose: Go +
    Python + Postgres in CI). This is the compliance surface - it gates the
    step-5 cutover.
R2. Three in-process ask() callers exist today: POST /query, the /assemble
    dossier (nested ask(), bind_session=False), and the whitepaper
    populator. The stateless core must serve all three; their audit
    persistence routes diverge after Python loses write creds (CompleteQuery
    for the API path; the WP/assemble paths ride CommitWhitepaperRun -
    which is why WP persistence moves last).
R3. SSE relay: /query/stream streams provisional tokens pre-validation with
    a sentinel guard (INV-1 lives in the post-validation path). The Go
    gateway must relay token frames + the terminal frame without reordering
    or buffering. Keep /query/stream a pass-through proxy until CompleteQuery
    is proven, then move the terminal-frame persistence only.
R4. Auth split-brain must be resolved at step 4: custom cookie auth is live;
    Supabase Auth is half-staged (2 dangling users). Decision folded in:
    remove the Supabase Auth remnants; if IT's IdP timeline is real,
    implement OIDC directly in Go (skip porting password auth twice) -
    otherwise port cookie sessions as-is and add OIDC later. Also ports the
    Fly-Client-IP (TRUST_PROXY_HEADERS) login-spray limiter semantics.
R5. Deleting SQLite/Chroma dual-mode re-platforms the test suite (20k test
    LOC currently runs largely in SQLite mode locally; CI already has a
    pgvector service). Worth it, but it is its own workstream - schedule it
    early since every later phase writes PG-only tests anyway.
    (DONE: R5 shipped — tests now run against `TEST_DATABASE_URL`, a
    disposable local Postgres, not SQLite/Chroma.)
R6. Deploy topology: run Go as a second process group in the SAME Fly app -
    Go takes the public :8000, uvicorn moves to an internal port. Vercel
    keeps pointing at the same hostname; cookies/CORS unchanged. Watch the
    Jun-18 incident class: release_command/boot-guard semantics stay with
    alembic (C3).
R7. D1 interplay: none of this closes D1. LLM/embedding calls stay in
    Python behind the provider seam; the per-provider base_url override
    (S4 of the assessment) remains the D1-readiness move and is unaffected
    by Go.

## Migration order (approved order, refined)

Gate for EVERY phase: the replaced Python path is deleted in the same phase
(measure handwritten behavior-bearing LOC, excluding generated OpenAPI/sqlc
types), full gate green (pytest + mypy + ruff + black + tsc + eslint +
vitest + new go/rust lanes), and no INV test lost - moved tests must land as
contract tests before the Python original is deleted.

0. Interim correctness (pure Python, ships independently, this week's PRs):
   a. Contract freeze prep = response_model on ALL routes + widen codegen
      (assessment S1) + commit an openapi.json snapshot + two-sided CI drift
      gate. This is step 1's substance and later generates BOTH the TS
      client and Go server interfaces from one contract.
   b. Widen `_commit_version_and_doc` to include chunks+BE in one
      transaction + add the deferred psg_version unique constraint
      (migration). Go's CommitIngest later ports PROVEN semantics instead
      of inventing them.
   c. PDF page-count bound in the parse child (assessment S3) - also
      becomes part of the Rust CLI contract.
   d. Populator bounded parallelism + request deadline (assessment S2) -
      Python keeps WP logic, so this is not throwaway.
1. Freeze the public API contract (0a lands it).
2. Refactor Python RAG to return RagOutcome + AuditPayload + SessionPatch;
   thin Python shell keeps persisting them for now (FastAPI still serves).
   Every INV test stays green. This is the old review's ask() decomposition
   with a Go-shaped return contract.
3. Go transparent proxy in front (same Fly app, second process group);
   /query/stream passes through untouched.
4. Move auth, sessions, feedback, settings, product CRUD to Go (R4 decision
   lands here). sqlc for queries; golangci-lint + sqlc vet CI lane.
5. Move query orchestration + atomic audit/message persistence to Go
   (CompleteQuery; R1 contract tests gate; R3 terminal-frame move).
6. Coarse write commands for ingest, Watch (CommitIngest, CompleteWatchRun;
   ports 0b semantics), outbox for digest delivery.
7. Python drops to a read-only DB role (this IS the open least-privilege
   creds item). WP/assemble persistence still Python until 8/9.
8. Rust PDF CLI: build + artifact pipeline (C1), shadow parity (C2),
   cutover in the watch workflow; delete _try_pdfplumber/_try_pypdf paths.
9. CommitWhitepaperRun last (whitepaper_runs.py:1 coupling); delete the
   public FastAPI surface + superseded Python persistence; THEN decide
   migration-tool ownership (C3).

Offsets adopted verbatim: PG/pgvector everywhere then delete dual-mode
(R5, DONE); remove Dagster if GH Actions stays the scheduler (it does; Dagster
was removed as part of R5's cutover); one OpenAPI contract -> Go server
interfaces + TS client; sqlc; Postgres outbox; OIDC directly in Go if SSO is
imminent.

## LOC accounting (baseline, verified 2026-07-10)

src Python 19,521 / tests Python 20,036 / frontend TS+TSX 10,353.
Expectation accepted: Python LOC down, total handwritten LOC roughly flat to
+10-25% while Go owns every table's writes; temporarily +30-50% during the
strangler. Metric: handwritten behavior-bearing LOC only.
