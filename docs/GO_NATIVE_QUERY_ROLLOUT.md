# GO_NATIVE_QUERY rollout (strangler Step 5): the query-cutover runbook

Status 2026-07-24: PROD IS FLAG-OFF. PR B (the Go CompleteQuery cutover,
merged #124) and the open-model work (#125) are both deployed since
2026-07-23, but GO_NATIVE_QUERY and INTERNAL_RAG_TOKEN are BOTH unset in
prod -- fly.toml sets neither, and there is no Fly secret for either. Every
POST /query still relays through the Go proxy to Python's buffered route,
exactly the pre-step-5 topology. This runbook is the flip.

What the flip changes (and only this): with GO_NATIVE_QUERY=true, the Go
edge serves POST /query natively (go/internal/api/query.go
handleCompleteQuery) instead of relaying it. Go runs the gates (auth 401,
pydantic-parity validation 422, per-user rate limit 429, session-ownership
404), writes T1 (user chat message, best-effort), calls Python's
token-guarded POST /internal/query/compute (src/regwatch/api/main.py
internal_query_compute) which computes and returns {response, persist} and
writes NOTHING, then Go writes T2 (query_log, AUTHORITATIVE -- INV-6) and
T3 (assistant chat message, best-effort) as three isolated writes
(go/internal/api/persist.go). /query/stream stays a relay behind StreamGate
(go/internal/api/query_stream_gate.go: pre-stream 401/429 in Go, streaming
persistence still Python -- plan R3, docs/POLYGLOT_TARGET_2026-07-10.md).

The flag is BOOT-TIME and PER-PROCESS: go/internal/api/config.go reads
GO_NATIVE_QUERY (envBool, default false) once in ConfigFromEnv, and
routes.go registers the native POST /query handler (and its 405 row) only
when it is true. Changing it means restarting proxy machines; there is no
runtime toggle. The same file resolves INTERNAL_RAG_TOKEN (NO boot-time
coupling check: flag-on with an empty or mismatched token still boots, but
Python's guard then 404s every compute call and every native /query
degrades to a synthesized upstream_error turn -- stage the secret BEFORE
the flip), INTERNAL_RAG_URL (falls back to UPSTREAM_URL, so
prod needs no new URL var -- fly.toml [env] already carries
UPSTREAM_URL=http://app.process.amneal.internal:8000), and RAG_TIMEOUT_S
(default 240s, the finite Go->Python compute deadline).

## Preconditions (hard gates, in order)

1. The three step-5-C parity fixes (branch go/step5-c-preflip-fixes) are
   MERGED AND DEPLOYED. They are prerequisites, not hygiene -- each closes a
   Go-vs-Python behavior divergence on a path the flip exposes. Target
   post-fix state, which this runbook assumes everywhere below:
   - Lost-create session-ownership race: a T1 upsert that loses the
     create race to another user's session now ABORTS the turn as an
     unaudited 404 (ownership-guarded upsert in
     go/internal/api/persist.go persistUserTurn), parity with Python's
     ensure_session. No more silent degrade onto a turn-id session for
     this case.
   - Strict-path audit failure with no serialized fallback: when the
     authoritative T2 write fails AND persist.fallback is nil, Go now
     WITHHOLDS the answer with a 500 (go/internal/api/persist.go
     persistTurn) -- INV-6 no-audit-no-answer, parity with Python's raise.
     (The normal strict-path degrade is unchanged: fallback present ->
     fixed-copy error turn, skip-audited.)
   - Python-side saturation: when the compute endpoint sheds under
     _ASK_LIMITER saturation, Python returns the SAME
     503 {"detail": "server is busy, retry shortly"} as the public route
     (src/regwatch/api/main.py) and Go passes it through as a 503 with NO
     audit row -- a shed is not a turn. Go relays ONLY that byte-fixed busy
     body; any other 503 on the compute hop (none exists today) lands on
     the audited upstream_error path instead of the unaudited relay
     (go/internal/api/ragclient.go). KNOWN ACCEPTED DIVERGENCE: T1 has
     already been written by then, so the shed leaves an orphaned user
     chat message, unlike Python's zero-write shed. See the divergence
     table below.
2. CI fully green on main at the deployed commit, INCLUDING the
   cross-service contract lane: tests_contract/ (28+ scenarios over the
   REAL compiled Go proxy + uvicorn + disposable Postgres). Native mode is
   the harness DEFAULT (tests_contract/conftest.py stack(native=True)), so
   a green contract lane is direct evidence for the post-flip topology; the
   base_relay_stack rows are what keep the flag-off rollback path proven.
3. The standing pre-merge checklist from docs/GO_PROXY_ROLLOUT.md (green
   docker-build, go lane, full python gate, fly config validate) plus a
   low-traffic window with `fly logs`, `fly status`, and this runbook open.

## Phase 0 -- stage the secret (safe while the flag is off)

Generate and set the shared internal token:

    openssl rand -hex 32
    fly secrets set INTERNAL_RAG_TOKEN=<the-64-hex-value> -a amneal

Facts to hold in mind while running that:

- Fly secrets are APP-WIDE: both process groups (proxy AND app machines)
  receive it, which is exactly what the design needs -- the Python compute
  endpoint's guard (main.py _require_internal_token) and the Go ragclient
  (go/internal/api/ragclient.go, X-Internal-Token header) must carry the
  SAME value, and one `fly secrets set` reaches both.
- Setting a secret triggers a ROLLING RESTART of all 4 machines (2 proxy +
  2 app), one at a time, each gated by its health check ([[http_service.checks]]
  for the proxy group, [checks.app_health] for the app group). Treat it as
  a deploy: watch it.
- The token is INERT while GO_NATIVE_QUERY is off. Nothing calls the
  compute endpoint (the native handler is not even registered,
  go/internal/api/routes.go), the endpoint 404s any mismatched or missing
  token without confirming it exists, and the Go edge 404s the whole
  /internal/ subtree UNCONDITIONALLY -- so staging the secret first is a
  zero-behavior-change deploy, which is why it is its own phase.

Phase 0 exit criteria (all read-only):

| # | command | pass looks like |
| - | ------- | --------------- |
| 1 | `fly status` | 2 proxy + 2 app machines, all started, checks passing, all on the same VERSION (per-machine column, not the header) |
| 2 | `fly secrets list -a amneal` | INTERNAL_RAG_TOKEN listed with a fresh created-at; GO_NATIVE_QUERY absent |
| 3 | `curl -fsS https://amneal.fly.dev/health` | 200, db ok -- the restart converged |
| 4 | `curl -s -o /dev/null -w '%{http_code}' -X POST https://amneal.fly.dev/internal/query/compute` | 404 -- the edge still walls off /internal/ |
| 5 | an authed POST /query via the frontend | normal answer/refusal -- still the relay path, proving the restart changed nothing |

## Phase 1 -- the flip

Recommended mechanism: flip by SECRET, then pin by PR once proven.

    fly secrets set GO_NATIVE_QUERY=true -a amneal

- On Fly, secrets take precedence over fly.toml [env], so this wins even
  after the pin lands; instant revert is
  `fly secrets unset GO_NATIVE_QUERY -a amneal` (another rolling restart,
  no git round-trip, no CI wait).
- Once the smoke checklist and monitoring window below are clean, land a
  follow-up PR that pins `GO_NATIVE_QUERY = "true"` in fly.toml [env] so
  the state is versioned (the step-3 precedent), and after THAT deploy is
  green, `fly secrets unset GO_NATIVE_QUERY -a amneal` so the versioned
  value is the only authority. Leaving the secret set forever would make
  fly.toml lie about who controls the flag.

Alternative considered: fly.toml-first (flip in a PR, the step-3
GO_PROXY_ROLLOUT.md precedent -- versioned, reviewed, CI-gated). Rejected
for the FIRST flip this time because revert speed dominates: a fly.toml
revert is merge + CI + deploy (~15-20 min under the workflow_run chain,
longer if CI queues), while a secrets unset is one command and one rolling
restart. Step 3 changed topology (machine groups, ports), where versioning
was the safety property; step 5 changes ONE boot-time boolean whose off
state is the continuously-proven relay path -- speed-of-revert is the
safety property here. The pin PR restores the versioned end state either
way.

Restart mechanics to expect:

- `fly secrets set` rolls ALL FOUR machines (secrets are app-wide), one at
  a time. The app machines pick up nothing new (Python never reads
  GO_NATIVE_QUERY); only the proxy machines change behavior.
- The flag is boot-time and per-process, so during the roll one proxy
  machine serves /query natively while the other still relays. Both paths
  produce the same wire bytes and the same DB rows (that is the whole PR B
  parity surface, pinned by tests_contract), so the mixed window is
  harmless. It ends when the second proxy machine restarts.

## Live smoke checklist (run every row, in order, right after the roll)

| # | check | how | pass looks like |
| - | ----- | --- | --------------- |
| 1 | fleet | `fly status` | 2 proxy + 2 app, all checks passing, same VERSION |
| 2 | native buffered query | authed POST /query (frontend, or curl with a session cookie) | 200; body has the full QueryResponse key set; `audit_id > 0` (a -1 here means the audit store is down -- stop and investigate) |
| 3 | audit row (INV-6) | SQL against prod Supabase: `SELECT id, mode, refused, status, route_json, model_name, input_tokens, output_tokens FROM query_log WHERE id = <audit_id from row 2>` | EXACTLY ONE row, id == audit_id; route_json a sane object (route/filters/reason/context_applied/response_mode); tokens NULL on refusals, populated on answers |
| 4 | chat trail | `SELECT role, audit_id, status FROM chat_message WHERE turn_id = '<turn_id from row 2>' ORDER BY created_at` | exactly [user, assistant]; the assistant row's audit_id equals row 2's audit_id |
| 5 | 405 wiring | `curl -s -o /dev/null -w '%{http_code}' https://amneal.fly.dev/query` (GET) | 405 -- Go now owns the method table for /query |
| 6 | internal stays internal | `curl -s -o /dev/null -w '%{http_code}' -X POST https://amneal.fly.dev/internal/query/compute` | 404, before and after the flip |
| 7 | streaming untouched | authed `curl -N -X POST https://amneal.fly.dev/query/stream ...` | `event: token` frames arrive incrementally, terminal result frame last -- still the relay + StreamGate path |
| 8 | auth gate | unauthenticated `curl -s -o /dev/null -w '%{http_code}' -X POST https://amneal.fly.dev/query -H 'content-type: application/json' -d '{"question":"ping?"}'` | 401, unaudited |

## Monitoring window and rollback

Watch for AT LEAST an hour of real traffic, then again at the next
watch-daily run:

- Missing audit rows: any /query 200 whose audit_id has no query_log row,
  or audit_id == -1 responses (grep `fly logs` for
  `qa_audit_write_failed` / `qa_answer_audit_write_failed`). This is the
  INV-6 surface -- a single confirmed miss is a rollback trigger, not a
  ticket.
- 5xx spike on /query relative to the pre-flip baseline (Fly metrics /
  `fly logs`). Remember the post-fix semantics: strict-path
  audit-failure-without-fallback 500s ON PURPOSE -- a trickle of those
  during a DB blip is the design working; a sustained rate is the trigger.
- status="error" / reason="upstream_error" query_log rows
  (`SELECT count(*) FROM query_log WHERE route_json->>'reason' = 'upstream_error' AND ts > now() - interval '1 hour'`):
  each one means the Go->Python compute hop failed (dial, 240s deadline,
  or non-200). A handful under deploy churn is expected; a steady stream
  means the internal hop is misconfigured (token mismatch shows up
  EXACTLY here, as compute 404s).
- Sentry: new error groups from the query path in either runtime.
- `qa_session_setup_failed` / `qa_user_record_failed` /
  `qa_assistant_record_failed` log lines (best-effort write failures --
  degradation signal, not by themselves a trigger).

The exact revert:

    fly secrets unset GO_NATIVE_QUERY -a amneal

Rolling restart back to the relay path; the token can stay set (it goes
back to being inert). No schema, data, or fly.toml change is involved in
either direction. The relay path does not rot while flag-on is live: the
contract suite's base_relay_stack rows (tests_contract/conftest.py,
tests_contract/test_query_relay_parity.py) run flag-off on every CI run,
so the rollback target stays continuously proven.

## Known divergences (accepted, with file refs)

Each row was verified against the code, is covered by the contract suite
where wire-visible, and is accepted rather than fixed. Do not re-report
them as flip regressions.

| # | divergence | where | why accepted |
| - | ---------- | ----- | ------------ |
| 1 | 503 shed leaves an ORPHANED USER TURN: Go writes T1 before the compute call, so a Python-side saturation shed (503 passthrough, no audit row) leaves the user chat message with no assistant sibling; Python's own shed wrote nothing at all | go/internal/api/query.go (T1 before the rag call); src/regwatch/api/main.py (_shed_if_ask_pool_saturated / _dispatch_ask 503) | audit-first ordering (T1 pre-RAG) is the INV-6-preserving design; an orphaned user turn is display-benign and the session survives a retry |
| 2 | strict-path audit failure with a NIL fallback now 500s (post-fix state) | go/internal/api/persist.go persistTurn | BOTH runtimes withhold an unaudited validated answer -- this is parity with Python's raise, listed here because the pre-fix Go build degraded to -1 instead |
| 3 | 422 validation detail granularity: Go reproduces status, top-level shape, and per-field loc/type for the checks it implements, but not every pydantic detail nuance | go/internal/api/query.go (validation block) | contract pins are status-level + shape-level; no client parses detail items |
| 4 | JSON key ORDER differs (Go re-marshals through a map in spliceAuditID; Python preserves model field order) | go/internal/api/persist.go spliceAuditID | shape parity, not byte parity, is the contract; every consumer parses |
| 5 | StreamGate 401/429 responses lack CORS headers | go/internal/api/query_stream_gate.go | dev-only exposure: prod traffic rides the Vercel same-origin /api rewrite, which never needs CORS |
| 6 | rate-limit buckets are PER-PROCESS across 2 proxy machines, so the effective fleet ceiling is ~2x RATE_LIMIT_PER_MINUTE | go/internal/api/ratelimit.go; fly.toml min_machines_running = 2 | pre-existing (Python's limiter split across 2 app machines the same way); Go is at least now the SINGLE authority across /query + /query/stream |
| 7 | Fly edge idle-timeout exposure on a ZERO-BYTE buffered response: the native /query sends nothing until compute returns, up to RAG_TIMEOUT_S = 240s (go/internal/api/config.go) | go/internal/api/ragclient.go (finite client timeout) | UNMEASURED whether Fly's edge would cut an idle buffered response before 240s -- but the relay path had byte-identical exposure (Python's buffered /query also sends nothing until done), so it is NOT a flip regression. OBSERVATION ITEM, not a blocker: if long-tail turns start dying at a fixed wall-clock, measure this first |
| 8 | NULL-owner adopt race is STRICTER in Go: two callers racing to adopt a legacy NULL-owner session between the ownership pre-check and T1 both succeed in Python (ensure_session's unconditional row.user_id = user_id lets the last commit clobber the owner, so the earlier adopter's turns land in a session finally owned by the other user), while Go's guarded upsert re-evaluates ownership against the winner's committed row and 404s the loser with zero writes | go/internal/store/queries/chat.sql (guarded ON CONFLICT update); src/regwatch/common/conversation.py ensure_session | divergence is in the SAFE direction: Go closes a genuine Python clobber and matches SessionOwnershipError's "last line of defense" intent; a turn is never attributed to a session another user owns |

## After the flip is proven

In order, each its own PR:

1. The pin PR (fly.toml [env] GO_NATIVE_QUERY = "true"), then unset the
   secret -- see Phase 1.
2. REQUIRED FIRST, before any deletion: the tests/ -> tests_contract
   INV-mapping audit. Walk every INV-tagged and query-path test in tests/
   that exercises Python's buffered query() route and record, per test,
   the tests_contract scenario that now carries its invariant. The gate is
   "no INV test lost": a test with no mapped successor blocks the deletion
   PR until a contract scenario is written for it.
3. The phase-gated deletion PR (the step-4 B2/C2 pattern): delete
   Python's buffered POST /query route and its now-dead persistence
   branches; de-flag Go (GO_NATIVE_QUERY and its routes.go conditionals
   go away, native becomes the only path); regenerate the OpenAPI schema
   and frontend api-types; move the Query wire types to the
   hand-maintained pattern the step-4 surfaces use.
4. Then, and only then, the R3 stream terminal-frame move: keep relaying
   token frames, move ONLY the terminal-frame persistence to Go
   (docs/POLYGLOT_TARGET_2026-07-10.md R3 -- "keep /query/stream a
   pass-through proxy until CompleteQuery is proven"). That work gets its
   own runbook slice; it is out of scope here.

## Out of scope for this slice

- /query/stream persistence and the SSE terminal frame (R3, above).
- CommitIngest / CompleteWatchRun / CommitWhitepaperRun (plan steps 6-9).
- Python's read-only DB role (plan step 7) -- Python still holds write
  creds during the flag-gated phase; dropping them is gated on the
  deletion PR landing.
