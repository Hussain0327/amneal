# Step 5: INV test mapping for the Python buffered /query deletion

Purpose: satisfy the phase gate in docs/POLYGLOT_TARGET_2026-07-10.md:128-131 --
"no INV test lost - moved tests must land as contract tests before the Python
original is deleted" -- for the PR that deletes the Python buffered `query()`
route (src/regwatch/api/main.py:762, handler body ~740-761ff) and de-flags
GO_NATIVE_QUERY in Go. Retained on the Python side: /query/stream (until R3),
ask()/_persist_turn, QueryRequest, _dispatch_ask, _shed_if_ask_pool_saturated,
_build_query_response, _wire_citations, /internal/query/compute.

Produced by a mapper fleet and consolidated with adversarial spot-checks
(both sides of every WEAKER/GAP row and all session-ownership / rate-limit /
INV-6 twins were read; see "Spot-check overturns" below).

Legend for disposition:
- unaffected: no dependency on the buffered route; keep as-is.
- needs-contract-twin: property must be pinned in tests_contract/ (or Go)
  before deletion; twin column names the pin.
- rewire-in-place: test survives but must be edited in the deletion PR
  (sentinel, seeding path, or dead comparison leg).
- dies-with-route: test's entire subject is the flag-off/Python path; delete
  it in the same PR.
- Parity: CONFIRMED = twin read and asserts equal-or-stronger; WEAKER = twin
  read, misses a named assert; GAP = no contract-level twin exists.

## Mapping table

| File | Test (line) | Property | Category | Disposition | Twin | Parity |
|---|---|---|---|---|---|---|
| tests/test_api.py | test_cors_allowlist (120) | CORS preflight on /query path string; CORSMiddleware answers before routing | other | unaffected | Go TestCORSParity (go/internal/api/contract_test.go:791) | N/A |
| tests/test_api.py | test_query_refuses_on_empty_corpus (156) | INV-1 refuse-not-fabricate on empty corpus, no LLM call, session continuity + distinct turn ids | inv1 | needs-contract-twin | S8 tests_contract/test_query_outcomes.py:131 + S17 test_sessions_cross_runtime.py:32 | CONFIRMED |
| tests/test_api.py | test_query_rejects_zero_k (184) | 422 on k=0 before any work | wire-shape | needs-contract-twin | S28 tests_contract/test_query_auth.py:96 | CONFIRMED |
| tests/test_auth.py | test_every_protected_endpoint_requires_auth (56; /query row at 48) | 401 + exact detail body, unauthenticated | auth | needs-contract-twin (+ rewire: drop the _PROTECTED /query row) | S3 tests_contract/test_query_auth.py:30 | CONFIRMED |
| tests/test_auth.py | test_query_rejects_foreign_session_with_404 (130) | Hijack is 404 not 403, exact body, AND owner/messages preserved | session-ownership | needs-contract-twin | S5 tests_contract/test_query_auth.py:58 | CONFIRMED |
| tests/test_auth.py | test_legacy_null_user_session_adopted_via_query (156) | NULL-owner legacy session ADOPTED via /query (200, sid echo, owner column flips) | session-ownership | needs-contract-twin | S29 tests_contract/test_sessions_cross_runtime.py:123 (+ persist-layer: go query_pg_test.go:92) | CONFIRMED |
| tests/test_auth.py | test_lost_ownership_race_aborts_instead_of_writing (177) | Below-API race defense in ensure_session/ask() | session-ownership | unaffected (Go layer twin: query_pg_test.go:72) | N/A | N/A |
| tests/test_auth.py | test_query_log_records_user_id_for_query (194) | INV-6 attribution: one query_log row, user_id = caller | inv6 | needs-contract-twin | _one_new_row tests_contract/test_query_outcomes.py:41-52, applied on S6-S13 | CONFIRMED |
| tests/test_auth.py | test_query_and_assemble_rate_limited_per_user (259) | Per-user 429; shared /query+/assemble budget; second user gets fresh budget | rate-limit | needs-contract-twin + rewire-in-place | S18 test_sessions_cross_runtime.py:85 | CONFIRMED |
| tests/test_ask_tier2_wire.py | test_answer_round_trips_audit_id (266) | Wire audit_id == assistant chat_message.audit_id | inv6 | needs-contract-twin (then delete with route) | S6 test_query_outcomes.py:69 (link assert at :61) | CONFIRMED |
| tests/test_ask_tier2_wire.py | 6 other tests (100-206) | _wire_citations / recency / refusal persistence; no HTTP to /query | wire-shape/other | unaffected (serializers + ask() survive) | N/A | N/A |
| tests/test_invariants.py | test_inv6_authenticated_query_records_user_attribution (190) | INV-6 attribution via HTTP | inv6 | needs-contract-twin (then delete with route) | S8 via _one_new_row (branch-exact) | CONFIRMED |
| tests/test_invariants.py | all other INV tests | drive ask() in-process or non-query layers | inv1/5/6 | unaffected | N/A | N/A |
| tests/test_ready_metrics.py | test_metrics_counts_query_log_rows (97) | /metrics counters count query_log rows; /query used only to seed | seeding-only | rewire-in-place | N/A | N/A |
| tests/test_ready_metrics.py | test_metrics_groups_by_mode_without_n_plus_one (108) | /metrics per-mode grouping; two /query seeds | seeding-only | rewire-in-place | N/A | N/A |
| tests/test_openapi_contract.py | test_openapi_snapshot_matches_live_schema (67) | Snapshot == live schema | wire-shape | rewire-in-place (regen snapshot; see checklist item 4-6) | key-set pins: QUERY_RESPONSE_KEYS/CITATION_KEYS tests_contract/conftest.py:93-124 | N/A |
| tests/test_openapi_contract.py | test_every_json_route_declares_a_response_model (76) | response_model discipline; ("POST","/query") anti-vacuity sentinel at :99 | wire-shape | rewire-in-place (drop the sentinel tuple) | N/A | N/A |
| tests/test_whitepaper_api.py | test_no_draft_or_submit_endpoints (185) | INV-3 route-surface grep; '/query' sentinel at :192 | other | rewire-in-place (sentinel -> '/query/stream') | N/A | N/A |
| tests/test_query_stream.py | test_query_stream_matches_blocking_query (118) | Stream terminal frame == blocking response | wire-shape | needs-contract-twin (delete the blocking leg) | S19 tests_contract/test_query_stream.py:53-89 | CONFIRMED |
| tests/test_query_stream.py | test_query_stream_foreign_session_is_404_before_stream (194) | Pre-stream 404; /query used only to mint the seed session | seeding-only | rewire-in-place | S5 (already pins both routes) | CONFIRMED |
| tests/test_query_stream.py | test_ask_pool_saturation_sheds_with_defined_failures (334) | 503 shed on buffered /query + stream closes with no result frame + /health live | other | split: 503 leg -> S27; stream/health legs stay, rewire saturation seeding | S27 tests_contract/test_query_failure_audit.py:275 | CONFIRMED (503 leg only) |
| tests/test_query_stream.py | test_query_filters_are_whitelisted_at_the_boundary (376) | INV-5 edge whitelist: version_id/unknown/legacy/non-scalar dropped before compute | inv5 | needs-contract-twin | S30 tests_contract/test_query_outcomes.py:286 (+ Go unit twin: query_unit_test.go:54) | CONFIRMED |
| tests/test_query_stream.py | test_query_request_filters_validator_unit (412) | Whitelist on the shared QueryRequest model | inv5 | unaffected (update stale "cannot drift" docstring) | N/A | N/A |
| tests/test_internal_compute.py | test_public_query_shed_uses_the_same_helper_detail (89) | Flag-off /query shed parity with /internal/query/compute | wire-shape | dies-with-route | S27 (byte-identical busy body, native leg) | CONFIRMED |
| tests/test_internal_compute.py | compute shed + fault-seam fence tests (52) | /internal/query/compute semantics (survives) | other | unaffected | N/A | N/A |
| tests_contract/test_query_relay_parity.py | both tests (33, 59) | GO_NATIVE_QUERY=false relay behavior | wire-shape/auth | dies-with-route (delete module) | S6 / S3 pin the native path stronger | CONFIRMED |
| tests_contract/test_query_failure_audit.py | S27 relay comparison leg (299-305) | Flag-off shed parity | other | rewire-in-place (keep native leg; move T1-divergence rationale onto it) | N/A | N/A |
| tests_contract/conftest.py | Harness.stack(native=False) + base_relay_stack (708/750/975) | Relay-flavored stacks | other | rewire-in-place (remove with the flag) | N/A | N/A |
| src/regwatch/api/main.py | @protected.post("/query") (762) | The deletion target; helpers retained | other | dies-with-route | N/A | N/A |
| scripts/export_openapi.py + regwatch/frontend/openapi.json | codegen chain (CI gate ci.yml:337-345) | Committed schema snapshot | wire-shape | rewire-in-place (regen; QueryResponse + QueryCitation + ClarifyOptionOut all drop, see checklist) | remaining pin = QUERY_RESPONSE_KEYS/CITATION_KEYS | N/A |
| regwatch/frontend/lib/api.ts | type anchors (36, 37, 40, 42) + postJSON('/query') (368, 509, 553) | Citation/ClarifyOption/QueryResponse/QueryStatus derive from schemas the regen deletes; runtime consumer unaffected (Go serves the edge) | wire-shape | rewire-in-place (hand-maintained types) | S6-S13 pin the wire | CONFIRMED |
| regwatch/frontend/lib/api-types.ts | paths['/query'] (75) | Generated types | wire-shape | rewire-in-place (regen in same PR) | N/A | N/A |
| regwatch/frontend/test/sse.test.ts, apiTimeout.test.ts | fallback /query client tests | Mocked-fetch client behavior; edge URL stays valid | other | unaffected | N/A | N/A |
| go: config.go:73/188, routes.go:36/77/80, cmd/proxy/main.go:76, internal/proxy relay branch | GO_NATIVE_QUERY flag sites | De-flag must land in the SAME PR (flag-off boot would 502 against a deleted upstream) | other | rewire-in-place | N/A | N/A |
| comments only: config/settings.py:367, embedder.py:26/121, observability.py, grounded_qa.py:1807, main.py route table | stale wording after cutover | other | unaffected (optional wording pass) | N/A | N/A |

## Gap list (all five CLOSED Jul 24 2026 -- S28/S29/S30 + S5/S18 hardenings landed; phase gate satisfied, deletion PR unblocked)

All new scenarios follow the S24-S27 style in
tests_contract/test_query_failure_audit.py: real Go edge + uvicorn + Postgres,
edge_login for auth, direct pg_conn/query_log_count assertions.

### GAP-1: CLOSED (landed as S28, tests_contract/test_query_auth.py:96) -- 422 validation on native /query was entirely unpinned
Go implements the pydantic-shaped validation (go/internal/api/query.go:56-81:
json_invalid, missing question, string_too_short/too_long, k
greater_than_equal/less_than_equal) but NO test anywhere exercises it -- zero
422s in tests_contract/, and no Go test references writeValidationError or
validationItem at all (wider than just the k bounds).
Remediation, S28 (base_stack flavor):
1. client = edge_login(base_stack)
2. POST /query {"question": "q?", "k": 0} -> 422; body["detail"][0] has
   type "greater_than_equal", loc ["body","k"] (pydantic shape).
3. POST /query {"question": "q?", "k": 51} -> 422, type "less_than_equal".
4. POST /query {"k": 3} (no question) -> 422, type "missing".
5. After all three: query_log_count() == 0 and no chat_message rows --
   422 is pre-work and unaudited (parallel to the feedback pin at
   go/internal/api/contract_c_test.go:217).

### GAP-2: CLOSED (landed as S29, tests_contract/test_sessions_cross_runtime.py:123) -- NULL-owner legacy session adoption through the edge
Original: tests/test_auth.py:156 (dies with the route). Only pin left is the
persist-layer Go test TestPersistUserTurnAdoptsNullOwner
(go/internal/api/query_pg_test.go:92) -- not a cross-service contract test, and
it covers only the persistUserTurn write-time site; the authorizeSession
pre-check adoption site (go/internal/api/query.go:205-218, conditional-UPDATE
adopt + re-read) has no test at any level. Phase gate not satisfied.
Remediation, S29 (base_stack flavor):
1. INSERT INTO chat_session (id, user_id, ...) VALUES (uuid4, NULL, ...) via
   pg_conn (the legacy demo-row shape).
2. client = edge_login(base_stack); POST /query {"question": ..., "session_id": sid}
   (empty corpus is fine; refusal turns adopt too).
3. Assert 200, payload["session_id"] == sid.
4. pg_conn: SELECT user_id FROM chat_session WHERE id = sid ->
   str(client.user_id) (the owner column flipped; this drives the
   authorizeSession pre-check path, closing both adoption sites).
5. _one_new_row-style attribution check, and GET /sessions/{sid} -> 200
   (adoption makes it visible cross-runtime).

### GAP-3: CLOSED (landed as S30, tests_contract/test_query_outcomes.py:286) -- INV-5 filters whitelist at the edge
Original: tests/test_query_stream.py:376 (dies with the route: it pins the
buffered path via monkeypatched ask()). Go has a faithful unit twin
(TestWhitelistFilters, go/internal/api/query_unit_test.go:54: version_id,
legacy source_url, unknown keys, non-scalars all dropped; scalar byte
preservation) but no edge-level scenario proves the handler actually applies
it before compute. /internal/query/compute TRUSTS filters (main.py:917-920),
so a Go regression here would silently disable current-version scoping.
Remediation, S30 (base_stack flavor):
1. seed_answerable_corpus(); client = edge_login(base_stack).
2. POST /query with filters {"normalized_name": <seeded>, "version_id": 17,
   "source_url": "http://x", "page": 3, "dosage_form": ["Gel","Cream"]}.
3. Assert 200 (dropped, never 422'd -- old clarify echoes must keep working).
4. latest_query_log_row(): route_json["filters"] == {"normalized_name": ...}
   only -- the audit trail proves the whitelist ran before compute.

### GAP-4: CLOSED (landed in S5, tests_contract/test_query_auth.py:86-93) -- hijack must not flip the owner (one-assert parity fix to S5)
S5 (tests_contract/test_query_auth.py:55) pins 404 + exact body on both routes
+ zero new query_log/chat_message rows, but never re-reads
chat_session.user_id; a flip-owner-then-404 bug escapes it (message counts
would not change). The dying Python original (tests/test_auth.py:147-149)
asserted owner-still-A explicitly. Go's persist-layer test preserves the owner
only on the write-time path, not the authorizeSession pre-check.
Remediation: add to S5 after the two blocked probes:
  with pg_conn() as conn: owner = SELECT user_id FROM chat_session WHERE id=sid
  assert owner == str(user_a.user_id)

### GAP-5: CLOSED (landed in S18, tests_contract/test_sessions_cross_runtime.py:113-120) -- second-user fresh budget on the Go query limiter (S18 addition)
S18 is single-user; the dying Python original (tests/test_auth.py:272-274)
pinned per-user isolation. Go's RateLimiter class has key-isolation unit
coverage (TestRateLimiterWindowAndEviction, go/internal/api/unit_test.go:18)
but the wire-level "user:"+id keying of queryLimiter is untested with two
users.
Remediation: append to S18 (rate_limited_stack, RATE_LIMIT_PER_MINUTE=1):
  other = edge_login(rate_limited_stack)  # distinct user
  assert other.http.post("/query", json={...}).status_code == 200
  assert query_log_count() == 2  # the fresh-budget turn was audited

Non-blocking but noted: after deletion no machine-checked schema (OpenAPI or
otherwise) describes the Go-owned /query wire; the remaining pins are the
exact key-set assertions QUERY_RESPONSE_KEYS/CITATION_KEYS in
tests_contract/conftest.py:93-124. Acceptable for this PR; revisit if Go grows
an OpenAPI export.

## Rewire list (edits inside the deletion PR, no new coverage needed)

1. tests/test_auth.py:48 -- drop the ("post", "/query", ...) row from
   _PROTECTED, or the 401-wall test fails 404 != 401.
2. tests/test_auth.py:259 -- rewire test_query_and_assemble_rate_limited_per_user
   to spend Python's surviving budget via two remaining Python routes
   (e.g. /assemble + /sources/search; both call _enforce_query_rate_limit,
   main.py:1062/1110). The shared query+assemble budget claim in its comment is
   dissolved: Go's bucket now covers /query + /query/stream (query.go:85 +
   query_stream_gate.go:25), Python's covers its own routes. The second-user
   probe moves to S18 (GAP-5).
3. tests/test_ready_metrics.py:97,108 -- seed query_log rows directly
   (log_query(mode="qa", refused=True, ...) via the store, or plain INSERT)
   instead of POST /query; the /assemble seed at :115 stays.
4. tests/test_openapi_contract.py:99 -- drop ("POST", "/query") from the
   anti-vacuity sentinel set (keep /watch/latest + /health).
5. tests/test_whitepaper_api.py:192 -- swap the '/query' sentinel to
   '/query/stream' (Python-persisted until R3).
6. tests/test_query_stream.py:118 -- delete the blocking-/query comparison leg
   (S19 owns stream/blocking parity); :194 -- mint the seed session via a
   /query/stream turn or a direct chat_session insert; :334 -- strip the two
   client.post("/query") calls (503 pin lives in S27), saturate the pool via
   /query/stream or direct dispatch, KEEP the stream-closes-without-result-frame
   and /health-liveness legs (no contract twin exists for those); :412 --
   update the stale "so /query and /query/stream cannot drift" docstring.
7. tests/test_internal_compute.py:89 -- delete
   test_public_query_shed_uses_the_same_helper_detail (flag-off parity is its
   whole subject).
8. tests_contract/ -- delete test_query_relay_parity.py; drop the S27 relay
   comparison leg (:299-305) and move the pinned T1-divergence rationale onto
   the native leg; remove Harness.stack(native=...), base_relay_stack, and the
   GO_NATIVE_QUERY env plumbing from conftest.py.
9. Comment/wording staleness (optional, same PR): main.py route table + :685/:729
   comments, config/settings.py:367, src/regwatch/process/embedder.py:26/121,
   tests_contract docstrings that say "flag-on".

## Deletion-PR checklist

1. Delete the Python route: the @protected.post("/query") handler at
   src/regwatch/api/main.py:762 (body ~740-761ff) ONLY. Retain QueryRequest,
   _dispatch_ask, _shed_if_ask_pool_saturated, _build_query_response,
   _wire_citations, _authorize_session_access, /query/stream,
   /internal/query/compute, ask()/_persist_turn.
2. Land GAP-1..GAP-5 contract scenarios FIRST (or in the same PR, ordered
   before the deletion commit) -- the phase gate condition.
3. Apply the rewire list above; delete tests marked dies-with-route
   (test_ask_tier2_wire.py:266, test_invariants.py:190,
   test_internal_compute.py:89, tests_contract/test_query_relay_parity.py).
4. Regenerate regwatch/frontend/openapi.json (scripts/export_openapi.py) --
   CI diff-gates it (ci.yml:337-345). The regen drops paths['/query'] AND
   components.schemas QueryResponse, QueryCitation, ClarifyOptionOut (verified:
   QueryResponse's only $ref is /query; the '/query/stream' mention is
   docstring prose; QueryCitation/ClarifyOptionOut are reachable only through
   QueryResponse).
5. Regenerate regwatch/frontend/lib/api-types.ts (npm run gen:types) in the
   same PR.
6. Move the Query wire types to the hand-maintained lib/auth-types.ts pattern:
   a new hand-declared module (e.g. lib/query-types.ts) exporting QueryResponse,
   Citation (QueryCitation), ClarifyOption/Suggestion (ClarifyOptionOut), and
   QueryStatus; update the anchors at lib/api.ts:36-42 and every import.
   Without this, tsc/next-build breaks on the regen. Their source of truth is
   now the Go-served wire pinned by QUERY_RESPONSE_KEYS/CITATION_KEYS.
7. De-flag Go in the SAME PR: remove GO_NATIVE_QUERY from
   go/internal/api/config.go:73/:188-192, routes.go:36/:77 (+ allow405 entry
   :80 stays, /query still 405s non-POST), cmd/proxy/main.go:76, and the
   flag-off relay branch in go/internal/proxy/proxy.go. A flag-off boot after
   deletion would relay /query to a 404 upstream.
8. Remove the relay-parity smoke: tests_contract/test_query_relay_parity.py,
   the S27 relay leg, and the conftest native=False machinery (rewire item 8).
9. Full gate: pytest (default + contract), go test ./..., sqlc diff, mypy/ruff/
   black over src tests migrations, npm run gen:types + tsc + eslint + vitest +
   next build. Grep broadly for '/query' literals in tests before pushing
   (the smoke-test-prefix lesson).
