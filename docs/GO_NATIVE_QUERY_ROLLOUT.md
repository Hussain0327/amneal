# GO_NATIVE_QUERY rollout (strangler Step 5): the query-cutover runbook

Status 2026-07-29: PROD IS FLAG-ON AND THE PIN IS DEPLOYED. The flip went live
on 2026-07-24 via `fly secrets set GO_NATIVE_QUERY=true -a amneal`. Prod serves
`POST /query` natively from the Go edge. The pin PR (#127) then put
`GO_NATIVE_QUERY = "true"` into `fly.toml` `[env]` and the same-named secret was
unset, so the versioned pin is now the only authority for this flag.
`INTERNAL_RAG_TOKEN` is still a Fly secret.

Last updated: 2026-08-11. Wording refresh only. Nothing about the flag changed.

Two things about the line above. `tests/test_go_native_query_pin.py` reads this
file and checks that status line against `fly.toml`, so keep exactly one of them
and keep it truthful. And the rollback below is state-dependent: today it is
state B, so `fly secrets unset` does NOT revert anything. Read the rollback
section before typing anything during an incident.

## What the flip changed

With `GO_NATIVE_QUERY=true` the Go edge serves `POST /query` itself
(`go/internal/api/query.go` `handleCompleteQuery`) instead of relaying it. Go:

1. runs the gates: auth 401, pydantic-parity validation 422, per-user rate limit
   429, session ownership 404
2. writes T1, the user chat message, best effort
3. calls Python's token-guarded `POST /internal/query/compute`
   (`src/regwatch/api/main.py` `internal_query_compute`), which computes and
   returns `{response, persist}` and writes nothing
4. writes T2, the `query_log` row. This one is authoritative, it is INV-6
5. writes T3, the assistant chat message, best effort

T1, T2 and T3 are three isolated writes (`go/internal/api/persist.go`).

`/query/stream` is still a relay behind StreamGate
(`go/internal/api/query_stream_gate.go`): Go does the pre-stream 401 and 429,
Python still owns streaming persistence. That is plan R3 in
`docs/POLYGLOT_TARGET_2026-07-10.md`.

## How the flag is read

It is boot-time and per-process. `go/internal/api/config.go` reads
`GO_NATIVE_QUERY` once in `ConfigFromEnv` (`envBool`, default false), and
`routes.go` registers the native `POST /query` handler and its 405 row only when
it is true. Changing it means restarting proxy machines. There is no runtime
toggle.

The same file resolves three related settings:

- `INTERNAL_RAG_TOKEN`. There is no boot-time coupling check. Flag-on with an
  empty or wrong token still boots, but Python's guard then 404s every compute
  call and every native `/query` degrades into a synthesized `upstream_error`
  turn. Stage the secret before any flip.
- `INTERNAL_RAG_URL`, which falls back to `UPSTREAM_URL`. Prod needs no new URL
  variable: `fly.toml` `[env]` already carries
  `UPSTREAM_URL=http://app.process.amneal.internal:8000`.
- `RAG_TIMEOUT_S`, default 240s. That is the finite Go to Python compute
  deadline.

## Behavior worth knowing (the three pre-flip parity fixes)

These shipped before the flip and describe how prod behaves today.

- **Lost-create session race.** A T1 upsert that loses the create race to
  another user's session aborts the turn as an unaudited 404 (ownership-guarded
  upsert in `go/internal/api/persist.go` `persistUserTurn`). This matches
  Python's `ensure_session`. There is no silent degrade onto a turn-id session
  for this case.
- **Audit failure on the strict path.** If the authoritative T2 write fails and
  `persist.fallback` is nil, Go withholds the answer with a 500
  (`persistTurn`). That is INV-6: no audit, no answer. With a fallback present
  the normal degrade is unchanged, a fixed-copy error turn, skip-audited.
- **Python-side saturation.** When the compute endpoint sheds under
  `_ASK_LIMITER` saturation, Python returns the same
  `503 {"detail": "server is busy, retry shortly"}` as the public route and Go
  passes it straight through with no audit row. A shed is not a turn. Go relays
  only that byte-fixed busy body. Any other 503 on the compute hop lands on the
  audited `upstream_error` path instead (`go/internal/api/ragclient.go`).

## Live smoke checklist

Run every row, in order, after any deploy or restart that could touch this path.

| # | check | how | pass looks like |
| - | ----- | --- | --------------- |
| 1 | fleet | `fly status` | 2 proxy + 2 app, all checks passing, same VERSION |
| 2 | native buffered query | authed POST /query (frontend, or curl with a session cookie) | 200; body has the full QueryResponse key set; `audit_id > 0`. A -1 means the audit store is down, stop and investigate |
| 3 | audit row (INV-6) | SQL against the prod database (Databricks Lakebase): `SELECT id, mode, refused, status, route_json, model_name, input_tokens, output_tokens FROM query_log WHERE id = <audit_id from row 2>` | exactly one row, id == audit_id; `route_json` a sane object (route/filters/reason/context_applied/response_mode); tokens NULL on refusals, populated on answers |
| 4 | chat trail | `SELECT role, audit_id, status FROM chat_message WHERE turn_id = '<turn_id from row 2>' ORDER BY created_at` | exactly [user, assistant]; the assistant row's audit_id equals row 2's audit_id |
| 5 | 405 wiring | `curl -s -o /dev/null -w '%{http_code}' https://amneal.fly.dev/query` (GET) | 405. Go owns the method table for /query |
| 6 | internal stays internal | `curl -s -o /dev/null -w '%{http_code}' -X POST https://amneal.fly.dev/internal/query/compute` | 404, before and after the flip |
| 7 | streaming untouched | authed `curl -N -X POST https://amneal.fly.dev/query/stream ...` | `event: token` frames arrive incrementally, terminal result frame last. Still the relay + StreamGate path |
| 8 | auth gate | unauthenticated `curl -s -o /dev/null -w '%{http_code}' -X POST https://amneal.fly.dev/query -H 'content-type: application/json' -d '{"question":"ping?"}'` | 401, unaudited |

## What to watch

- **Missing audit rows.** Any `/query` 200 whose `audit_id` has no `query_log`
  row, or an `audit_id == -1` response. Grep `fly logs` for
  `qa_audit_write_failed` and `qa_answer_audit_write_failed`. This is the INV-6
  surface. One confirmed miss is a rollback trigger, not a ticket.
- **5xx spike on /query** against the pre-flip baseline. Remember that
  strict-path audit failure without a fallback 500s on purpose. A trickle during
  a DB blip is the design working. A sustained rate is the trigger.
- **`upstream_error` rows.**
  `SELECT count(*) FROM query_log WHERE route_json->>'reason' = 'upstream_error' AND ts > now() - interval '1 hour'`.
  Each one means the Go to Python compute hop failed: dial, 240s deadline, or a
  non-200. A handful during deploy churn is expected. A steady stream means the
  internal hop is misconfigured, and a token mismatch shows up exactly here as
  compute 404s.
- **Sentry**, for new error groups on the query path in either runtime.
- **`qa_session_setup_failed` / `qa_user_record_failed` /
  `qa_assistant_record_failed`** log lines. Best-effort write failures. A
  degradation signal, not by itself a trigger.

## The exact revert: it depends on the current state, so check first

There is no single revert command. Fly secrets take precedence over `fly.toml`
`[env]`, so `fly secrets unset` reverts the flag only while a secret is what
holds it on. Once the `fly.toml` pin is the authority, an unset is a no-op and
the machines come back still flag-on. Run this first and let the answer pick
your command:

    fly secrets list -a amneal | grep GO_NATIVE_QUERY

**State A, the secret IS listed.** Historical: this was true from the
2026-07-24 flip until the pin PR #127 deployed and the secret was unset.

    fly secrets unset GO_NATIVE_QUERY -a amneal

That removes the only authority, and a deployed `fly.toml` that pins nothing
lets the machines boot flag-off. If the pin has also deployed by then, this
unset just drops back to the pinned "true" and reverts nothing. Use state B.

**State B, NO secret listed and the `fly.toml` pin is the only authority.**
This is the live state today.

    fly secrets set GO_NATIVE_QUERY=false -a amneal

A secret overrides the `[env]` pin, and the Go loader parses "false" to boolean
false (`envBool`, `go/internal/api/config.go`), so this is a genuine one-command
revert with no git round trip. `fly secrets unset` here does nothing except roll
the machines back onto the same pin.

**Permanent revert, either state.** Delete the `GO_NATIVE_QUERY = "true"` line
from `fly.toml` `[env]` and deploy. That is a merge plus CI plus deploy cycle,
roughly 15 to 20 minutes, so it is the follow-up to a secret revert, not the
incident action. If a secret is set it still overrides the absent pin, so clear
the secret too or the deploy changes nothing.

Every form above is a rolling restart back to the relay path. The token can stay
set, it just goes back to being inert. No schema or data change is involved in
any direction. The relay path does not rot while flag-on is live: the contract
suite's `base_relay_stack` rows (`tests_contract/conftest.py`,
`tests_contract/test_query_relay_parity.py`) run flag-off on every CI run, so
the rollback target stays continuously proven.

## Known divergences (accepted, with file refs)

Each row was checked against the code and is covered by the contract suite where
it is visible on the wire. These are accepted, not bugs. Do not re-report them as
flip regressions.

| # | divergence | where | why accepted |
| - | ---------- | ----- | ------------ |
| 1 | A 503 shed leaves an orphaned user turn. Go writes T1 before the compute call, so a Python saturation shed (503 passthrough, no audit row) leaves the user chat message with no assistant sibling. Python's own shed wrote nothing at all | `go/internal/api/query.go` (T1 before the rag call); `src/regwatch/api/main.py` (`_shed_if_ask_pool_saturated` / `_dispatch_ask` 503) | audit-first ordering is the INV-6-preserving design; an orphaned user turn is display-benign and the session survives a retry |
| 2 | Strict-path audit failure with a nil fallback 500s | `go/internal/api/persist.go` `persistTurn` | both runtimes withhold an unaudited validated answer, which is parity with Python's raise. Listed because the pre-fix Go build degraded to -1 instead |
| 3 | 422 validation detail granularity. Go reproduces status, top-level shape, and per-field loc/type for the checks it implements, but not every pydantic detail nuance | `go/internal/api/query.go` (validation block) | contract pins are status-level and shape-level; no client parses detail items |
| 4 | JSON key order differs. Go re-marshals through a map in `spliceAuditID`; Python preserves model field order | `go/internal/api/persist.go` `spliceAuditID` | shape parity, not byte parity, is the contract; every consumer parses |
| 5 | StreamGate 401/429 responses carry no CORS headers | `go/internal/api/query_stream_gate.go` | dev-only exposure. Prod traffic rides the Vercel same-origin `/api` rewrite, which never needs CORS |
| 6 | Rate-limit buckets are per-process across 2 proxy machines, so the effective fleet ceiling is about 2x `RATE_LIMIT_PER_MINUTE` | `go/internal/api/ratelimit.go`; `fly.toml` `min_machines_running = 2` | pre-existing. Python's limiter split across 2 app machines the same way, and Go is at least the single authority across `/query` and `/query/stream` now |
| 7 | Fly edge idle-timeout exposure on a zero-byte buffered response. Native `/query` sends nothing until compute returns, up to `RAG_TIMEOUT_S` = 240s | `go/internal/api/ragclient.go` (finite client timeout) | UNMEASURED whether Fly's edge cuts an idle buffered response before 240s. The relay path had identical exposure, so it is not a flip regression. Observation item: if long-tail turns start dying at a fixed wall clock, measure this first |
| 8 | The NULL-owner adopt race is stricter in Go. Two callers racing to adopt a legacy NULL-owner session both succeed in Python (`ensure_session`'s unconditional `row.user_id = user_id` lets the last commit clobber the owner), while Go's guarded upsert re-checks ownership against the winner's committed row and 404s the loser with zero writes | `go/internal/store/queries/chat.sql` (guarded ON CONFLICT update); `src/regwatch/common/conversation.py` `ensure_session` | the divergence is in the safe direction. Go closes a real Python clobber, and a turn is never attributed to a session another user owns |

## What is left

1. **The deletion PR.** Delete Python's buffered `POST /query` route and its
   now-dead persistence branches. De-flag Go, so `GO_NATIVE_QUERY` and its
   `routes.go` conditionals go away and native becomes the only path. Regenerate
   the OpenAPI schema and the frontend api-types, and move the Query wire types
   to the hand-maintained pattern the step-4 surfaces use. The INV-mapping audit
   that gates this is done (the step-5 INV test mapping, in git history): no
   INV test may be lost, and a test with no mapped contract successor blocks the
   deletion.
2. **R3, the stream terminal-frame move.** Keep relaying token frames, move only
   the terminal-frame persistence to Go. See
   `docs/POLYGLOT_TARGET_2026-07-10.md` R3. The flag-gated live draft channel
   shipped on 2026-08-10, but streaming persistence is still Python. R3 gets its
   own runbook slice.
3. **Plan steps 6 to 9**: CommitIngest, CompleteWatchRun, CommitWhitepaperRun,
   and Python's read-only DB role. Python still holds write credentials during
   this flag-gated phase; dropping them is gated on the deletion PR landing.

## History (executed 2026-07-24, kept as the record)

- **Phase 0, stage the secret.** `openssl rand -hex 32`, then
  `fly secrets set INTERNAL_RAG_TOKEN=<value> -a amneal`. Fly secrets are
  app-wide, so both process groups get it, which is what the design needs: the
  Python guard (`main.py` `_require_internal_token`) and the Go ragclient
  (`X-Internal-Token` header) must carry the same value. Setting a secret rolls
  all four machines one at a time, so treat it as a deploy and watch it. The
  token is inert while the flag is off. Do not rotate it mid-incident: that 404s
  every compute call.
- **Phase 1, the flip.** `fly secrets set GO_NATIVE_QUERY=true -a amneal`,
  proven live, then pinned in `fly.toml` by PR #127 and the secret unset. Flipping
  by secret rather than by PR was deliberate: step 5 changes one boot-time
  boolean whose off state is the continuously proven relay path, so speed of
  revert mattered more than versioning. The pin PR restored the versioned end
  state right after.
- During the roll, one proxy machine served natively while the other still
  relayed. Both paths produce the same wire bytes and the same DB rows, which is
  the whole parity surface `tests_contract` pins, so the mixed window was
  harmless.
