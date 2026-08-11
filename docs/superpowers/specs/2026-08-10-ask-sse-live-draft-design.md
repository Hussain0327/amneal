# Ask live SSE draft streaming - design

Date: 2026-08-10
Status: approved by owner (this doc replaces the LOST Aug 7 design; the
surviving memory record plus a 4-agent code exploration over main @ f3e4aa4
reconstructed and re-verified it). All file:line anchors below refer to
main @ f3e4aa4.

> **Read as history, not current state (noted 2026-08-11).** This is a dated
> design doc and its "today" statements were true on 2026-08-10 only. Two of them
> are now wrong: the prod model is `gpt-oss-120b` (served id `gpt-oss-120b-080525`),
> repointed on 2026-08-05, not `gpt-oss-20b`; and `REGWATCH_PROSE_SYNTHESIS` is no
> longer dark, it is ON in prod along with `REGWATCH_LIVE_DRAFT` and
> `REGWATCH_SELECTIVE_CITATION`. The design itself shipped in PR #179. For current
> state see `docs/DEPLOY.md` and `docs/ARCHITECTURE.md`.

## 1. Context and premise

The Ask page (surface 01) already streams over SSE end-to-end, but the
`token` frames are a post-audit REPLAY of a fully buffered synthesis call,
never live model tokens. The backend states this in its own docstring
(src/regwatch/api/main.py:918-940); the replay is gated on the audit write
(src/regwatch/generate/grounded_qa.py:1000-1072). Live streaming existed
once and was deliberately deleted at commit 0a96f7e;
tests/test_streaming_synthesis.py:1-21 pins the reversal.

This feature adds a live, un-audited, provisional draft channel while the
audited pipeline runs, under the amended INV-1 (owner, Aug 7): nothing
un-audited may be PRESENTED AS VALIDATED; live un-gated prose MAY stream as
an explicitly provisional draft; the terminal `result` frame remains the
only validated artifact.

Key code facts the design is fitted to:

- DatabricksProvider.stream() is stream-shaped but atomic: _complete_stream
  buffers every wire event before returning (llm.py:886-953), then stream()
  yields one full-text delta plus done=True (llm.py:955-989). True
  incremental delivery requires a new provider path.
- The reasoning scrubber _visible_gemma_text matches only the Gemma
  thought-channel delimiters and generic think-tags (llm.py:518-519,
  541-557). gpt-oss emits the Harmony response format, which wraps
  reasoning in its own channel/message delimiter tokens that the scrubber
  does NOT match; a plain-string content passes unfiltered
  (llm.py:560-562). Where
  reasoning actually surfaces on the prod endpoint is unknowable from the
  repo (G1 probe, section 4).
- Prod model naming conflict: repo docs say gpt-oss-20b behind alias
  workspace.default.regwatch, served id gpt-oss-20b-080525 (docs/DEPLOY.md:20,
  docs/OPEN_MODEL_ROLLOUT.md:8-9, docs/DECISIONS.md:273-286); gpt-oss-120b
  appears only in the eval lane (docs/EVAL_STATUS.md:209). G1 records the
  actually-served id.
- The gate rewrites every citation marker and appends a Sources trailer
  (turn_gate.py:689-721), so draft text and the rendered answer differ on
  essentially every turn. Divergence is the norm, not an edge case.
- ProvisionalDraft already exists and is live-wired to the token replay
  (app/(shell)/page.tsx:185, 456-458, 935-936; components/Turns.tsx:56-74).
- REGWATCH_PROSE_SYNTHESIS is dark in prod (no Fly secret; prod runs v5
  claims-JSON). This feature is downstream of the prose path and ships
  fully dark regardless.

## 2. Decisions of record (owner)

- Aug 7: INV-1 amended. Provisional drafts are allowed; the `result` frame
  stays the only validated artifact. This amendment gets WRITTEN into
  docs/PROJECT_SPEC.md section 4 and the turn_gate.py / ask_core docstrings
  as part of this work (today PROJECT_SPEC.md:75 still reads "No ungrounded
  claims, ever" and turn_gate.py:1-37 claims to be the only place model
  bytes become user-visible).
- Aug 10: scope = full G1 -> L3, one branch, one PR by this session.
- Aug 10: withdrawal UX = explicit withdrawal note, keyed on a
  server-declared signal (never text-diffing).
- Aug 10: truncation retry under streaming = `draft_reset` frame; client
  clears the draft and attempt 2 streams fresh.
- Aug 10: opt-in scope = any authed analyst; dual gate only (server flag +
  per-request opt-in), no per-user machinery.
- Aug 10 (revision): true low latency wins. Deltas are NOT held waiting
  for late model metadata and NOT coalesced server-side; the server sends
  clean user-facing deltas as soon as they are available and the CLIENT
  buffers and renders them at a typewriter cadence. Harmony/control
  markup is parsed out at the model adapter boundary, never treated as
  user-facing content. Rationale: the slm layer is moving conversational
  (refusal philosophy being removed), so draft liveness is the product,
  not a nice-to-have.

## 3. Contract (L0)

SSE event names grow from three to five (src/regwatch/api/main.py:904-910):

| event         | semantics                                                       | status    |
|---------------|-----------------------------------------------------------------|-----------|
| `status`      | cosmetic phase prose, never answer bytes                        | unchanged |
| `token`       | post-gate, post-audit replay, byte-identical to result.answer   | unchanged |
| `result`      | terminal; the ONLY validated artifact                           | extended  |
| `draft`       | NEW: live, un-gated, provisional prose delta (adapter-cleaned)  | new       |
| `draft_reset` | NEW: client must discard all draft text received so far         | new       |

Draft frames carry plain text only: no citations array, no audit id, no
confidence, no evidence-drawer affordance, nothing clickable. `result`
gains an optional `draft_withdrawn` field (section 7). Old clients are
safe: the frontend SSE dispatcher silently drops unknown event names
(lib/api.ts:508-510). Old servers are safe: QueryRequest is extra='ignore'
(pydantic 2 default, main.py:635-644), so a new client sending
`live_draft` to an old server degrades to today's behavior.

Effective gate, evaluated once per request as a single conjunction:

    settings.live_draft_enabled            # REGWATCH_LIVE_DRAFT, new
    AND settings.prose_synthesis_enabled   # REGWATCH_PROSE_SYNTHESIS
    AND request.live_draft                 # per-request opt-in, default False

Anything else constructs no on_draft callback and the turn is
byte-identical to today. Both server flags are boot-time (get_settings is
lru_cached, config/settings.py:720-724).

## 4. G1 hard gate: live probe (before any L1 code)

Probe the endpoint the prod secret actually points at (alias
workspace.default.regwatch; profile chosen by the owner at probe time,
never auto-selected). Record in a short docs note, in writing:

  (a) Does `choices[0].delta.content` arrive incrementally on the wire, or
      as one blob? (If one blob, L1 collapses to the atomic early-reveal
      and the owner is consulted before proceeding.)
  (b) Where does reasoning surface: typed field (delta.reasoning_content,
      already ignored at llm.py:940-942 - cheap path), typed content parts,
      or raw Harmony markup inside a content string? If Harmony-in-content:
      the scrubber must become Harmony-aware in this PR, AND this is a P0
      finding for the prose flip itself (the buffered path leaks reasoning
      today, llm.py:879 -> 541-557) - flagged to the owner immediately.
  (c) Does the FIRST stream event carry `model`? Determines the D1 bind
      point (section 5).
  (d) The actually-served model id, for the D1 allowlist and the docs.

No L1 code is written until (a)-(d) are answered in writing.

## 5. L1: provider incremental streaming (src/regwatch/generate/llm.py)

New incremental path (`_stream_events`) feeding DatabricksProvider.stream(),
with four bindings:

1. Adapter-boundary stream parser. Reasoning/control markup (the Gemma
   thought-channel and think-tag forms, plus the G1-confirmed Harmony
   channel structure) is PARSED at the model adapter, not forwarded: the
   parser tracks channel state across wire chunks, drops reasoning-channel
   content, and emits only clean user-facing text. Because a delimiter can
   split across wire chunks, the parser withholds only the minimal
   ambiguous tail (a possible partial delimiter) - never whole-response
   buffering. One narrow hold stays: while the accumulated visible text is
   still a prefix of PROSE_NO_EVIDENCE_SENTINEL (prose_turn.py:39),
   nothing is emitted - a refusal must never paint as a draft (cheap, and
   moot on any turn where the model actually answers). Modeled on the
   deleted prefix-hold (git show
   0a96f7e^:src/regwatch/generate/grounded_qa.py, lines 637-660).
2. D1 bind, opportunistic and non-blocking. _check_served_model
   (llm.py:789-822) fires on the FIRST event that reports `model`,
   rejecting before anything is yielded when the wire cooperates. Owner
   decision: deltas are NOT held waiting for late metadata - if `model`
   arrives late the check fires at arrival; if it never arrives, the
   end-of-stream check raises exactly as today (llm.py:797-802). Residual
   risk accepted and recorded in section 13.
3. No re-send after first yield. The except-branch fallback to
   _buffered_stream (llm.py:980-987) re-issues a full complete(); after the
   first yielded delta it must raise instead (the existing D1 branch at
   llm.py:971-977 already models this).
4. Prose-only by construction. stream() carries no response_format
   (llm.py:123-134); only the prose arm (response_format=None,
   grounded_qa.py:1927-1933) is shape-compatible. The v5 JSON arm is
   structurally unable to reach the streaming call.

EchoLLMProvider.stream() already yields a deterministic two-chunk stream
(llm.py:272-287), so L1/L2 are wire-testable without Databricks.

## 6. L2: pipeline + API

- New `_stream_structured` sibling to _complete_structured
  (grounded_qa.py:798-873): same signature plus on_delta; forwards deltas;
  returns the terminal LLMResponse so prose_turn.parse and admit_turn keep
  operating on the COMPLETE text, unchanged (grounded_qa.py:1972, 2015,
  2026). Selection point is the single call at grounded_qa.py:1927-1933,
  inside the try whose except already degrades provider faults to an
  audited status="error" refusal (grounded_qa.py:1934-1950).
- on_draft threads exactly like on_progress: param on ask() beside
  on_progress/on_token (grounded_qa.py:2468-2469), into ask_core
  (2541-2551), wrapped in a best-effort _emit_draft closure beside _emit
  (2244-2250), passed only into _synthesize_and_admit (1836-1847).
  on_token stays out of ask_core (2568 -> _persist_turn only).
- ask_core's docstring prohibition (2237-2241) is AMENDED, not silently
  violated: the core may emit un-gated bytes ONLY on the dual-gated draft
  channel. The amendment text cites the owner's INV-1 change.
- Settings: `live_draft_enabled: bool = Field(default=False,
  validation_alias="REGWATCH_LIVE_DRAFT")` beside prose_synthesis_enabled
  (config/settings.py:140-148), inheriting the blank-string validator
  (settings.py:222-250). Request: `live_draft: bool = False` on
  QueryRequest (main.py:635-644).
- SSE wiring: third closure on_draft -> ("draft", delta) via
  loop.call_soon_threadsafe beside main.py:944-951; "draft" and
  "draft_reset" branches in the drain loop beside main.py:996-998;
  _sse_event docstring extended to five names. Owner decision: no
  server-side coalescing - each adapter-emitted delta is forwarded as one
  frame as soon as it is available; pacing is a client concern. Frame
  count is bounded by the wire's own chunking (G1(a) records the observed
  granularity).
- Truncation retry (grounded_qa.py:855-873): when a retry fires after any
  draft frame was emitted, emit `draft_reset` before attempt 2's first
  delta.
- Go edge: ZERO changes. /query/stream is never a native route
  (go/internal/api/routes.go:30-34); the relay is byte-transparent with
  FlushInterval -1 (go/internal/proxy/proxy.go:112-151); StreamGate
  inspects only the request line/auth/rate limit
  (go/internal/api/query_stream_gate.go).

## 7. Withdrawal semantics

Server-declared, never text-diffed (the gate rewrites citations and appends
a Sources trailer on every turn, turn_gate.py:689-721, so diffing would
fire always).

The terminal `result` payload gains `draft_withdrawn: str | None`, set when
at least one draft frame was emitted this turn AND the turn ended
refused / clarify / error / partial (claims dropped: VERDICT_PARTIAL or
MATERIAL_DROP, turn_gate.py:106-118). Values name the reason
(e.g. "refused", "clarify", "error", "partial"). A normal answer turn whose
draft merely differs cosmetically from the rendered answer sets nothing -
the swap is the routine finalize, already built (page.tsx:481-486).

## 8. L3: frontend (regwatch/frontend)

- Client: `onDraft` and `onDraftReset` on StreamCallbacks
  (lib/api.ts:437-443); `draft`/`draft_reset` branches in the SSE dispatch
  beside `token` (lib/api.ts:499-507); `live_draft` in the request body and
  an askQueryStream param (lib/api.ts:570-606).
- Page: incoming deltas append to a pacing buffer; a client-side
  typewriter drain (interval-based, modeled on the existing replay typing
  feel) feeds the SAME draft state ProvisionalDraft already renders
  (page.tsx:185, 456-458, 935-936; Turns.tsx:56-74 - Markdown, plainLinks,
  no citations). The buffer absorbs bursty frames so the render cadence is
  smooth regardless of wire chunking. onDraftReset clears both buffer and
  rendered draft. The existing finalize-swap (page.tsx:481-486) and the
  existing STREAM_FALLBACK_STATUS discard (page.tsx:450-453) are
  unchanged; both also flush the pacing buffer.
- Withdrawal note: modeled on FallbackNote (Turns.tsx:76-89) at its four
  fixed slots (Turns.tsx:218, 243, 315, 419), keyed on
  result.draft_withdrawn, with a trace field beside streamFellBack
  (lib/turns.ts:47-50, 229-255).
- Copy fixes in the same PR: the SR milestone announcement currently
  promises "citations will be verified before it is shown"
  (page.tsx:459-470) - revised to name the draft as provisional. The
  FallbackNote copy ("answer re-verified over a fresh request",
  Turns.tsx:76-89) is revised since draft and answer are no longer
  byte-identical by construction. Draft [n] markers render as plain text
  (already the ProvisionalDraft behavior); the visible reflow at finalize
  is accepted and softened by the provisional label.
- Docs truth-up rides along: regwatch/frontend/README.md:98-103 falsely
  claims no /query/stream endpoint exists.

## 9. Failure paths

- Mid-stream provider error after draft frames: the existing except at
  grounded_qa.py:1934-1950 still produces an audited error refusal; the
  result carries draft_withdrawn="error" and the client shows the
  withdrawal note. Never a silent swap to "service unavailable".
- Provider error before first yield: indistinguishable from today.
- D1 violation: when the wire reports the served model on the first event
  (the expected case, recorded by G1(c)), rejection happens before any
  yield and an off-perimeter answer never reaches the screen. On a
  late-reporting wire the check fires at arrival or at end-of-stream, the
  turn is withdrawn, and the residual paint window is the owner-accepted
  risk in section 13.
- SSE disconnect: the client already falls back to a plain POST /query
  re-run and discards the draft (lib/api.ts:630-651, page.tsx:450-453).
  Note the second paid synthesis; the revised FallbackNote copy covers a
  possibly-different answer. The 75s client watchdog (lib/api.ts:463) and
  15s server keep-alive (main.py:915) are re-checked under the new traffic
  pattern in review.
- Flag half-states: single conjunction, evaluated once. The dangerous cell
  (live_draft on, prose off) is structurally unreachable (section 5.4).
- Frame volume: with no server coalescing, worst case is one frame per
  wire chunk (up to ~3000/turn at the 3000-token budget,
  config/settings.py:186). SSE frames are small, the per-request queue is
  short-lived, and the 16-thread ask pool (main.py:844-877) bounds
  concurrent streams. Accepted by owner; revisit only if G1 shows
  pathological chunking.

## 10. Test plan

- Unit (tests/): the adapter parser drops reasoning-channel content and
  never emits a partial delimiter even when one splits across wire chunks;
  the NO_EVIDENCE sentinel prefix never paints; D1: model-on-first-event
  rejects before any yield, model-arriving-late binds at arrival, and
  completion-without-model raises as today; no re-send after first yield
  (the fallback raises); draft_reset emitted when the truncation retry
  fires after a draft frame.
- tests/test_streaming_synthesis.py: premise amended honestly - re-scoped
  to the flag-off arm (nothing streams from the model BY DEFAULT), plus a
  flag-on twin asserting drafts stream and the gate still operates on the
  complete text. Same for the replayed==result.answer assertion at
  tests_contract/test_query_stream.py:81-88 (flag-off arm only).
- Contract (tests_contract/): new scenario S31 (S24-S30 are allocated;
  conftest.py:13-17). New flavor `live_draft` =
  {REGWATCH_PROSE_SYNTHESIS:1, REGWATCH_LIVE_DRAFT:1} in _FLAVOR_OVERRIDES
  (conftest.py:559-590), echo provider ONLY - no Databricks in CI (known
  QPS-collision failure mode, docs/EVAL_STATUS.md:207-214). Asserts: draft
  frames appear only with both flags AND the request opt-in; exactly one
  terminal result frame, still last; exactly one audit row per streamed
  turn; a refusal turn emits zero token frames and zero un-withdrawn draft
  frames (a fluent draft followed by a bare refusal without
  draft_withdrawn is a failure); the event grammar over the real Go edge
  is unchanged for flag-off flavors (test_s19's event-name set widens only
  under the new flavor).
- Frontend (test/): existing draft-swap tests (test/askPage.test.tsx) and
  SSE tests (test/sse.test.ts) pass UNCHANGED with the flag off; new tests
  for the draft branch, draft_reset clearing, and the withdrawal note.
- Gates before any push: full pytest, mypy over src tests tests_contract,
  ruff, black (LAST edit before push), frontend vitest + eslint + tsc, go
  test ./..., contract lane with GO_NATIVE_QUERY both true and false.

## 11. Delivery

One branch (worktree-ask-sse-live-draft), one PR by this session. Commit
slices, in order:

1. Docs truth-up + INV-1 amendment of record (PROJECT_SPEC section 4,
   turn_gate/ask_core docstrings, frontend README, POLYGLOT R3 rewritten to
   record the 0a96f7e reversal and this flag-gated partial re-instatement).
2. G1 probe results note (docs/), recording (a)-(d) verbatim.
3. L1 provider incremental path + unit tests (dark: nothing calls it).
4. L2 pipeline + API + settings + draft/draft_reset frames (flag-dark).
5. S31 contract scenario + re-scoped legacy streaming tests.
6. Withdrawal signal (backend) + its contract assertion.
7. L3 frontend + copy fixes + frontend tests.

Commits and the PR happen only on explicit owner go-ahead, after gates are
green and the diff is shown.

## 12. Rollout and rollback

Order: prose flip first (own decision, own scorecard - out of scope here),
then REGWATCH_LIVE_DRAFT on a canary; watch qa_provider_error rate,
malformed_structure rate, and draft-withdrawn count for a week before any
wider flip. Rollback = unset REGWATCH_LIVE_DRAFT: every draft path is
behind the single conjunction, so the wire contract reverts exactly to
today's. If G1(b) finds Harmony-in-content, the scrubber fix ships in this
PR but the prose-flip P0 is escalated to the owner separately.

## 13. Risks accepted

- Visible reflow at finalize on every answer turn (draft [n] vs rendered
  citations + Sources trailer), softened by the provisional label.
- A second paid synthesis on stream-fallback turns (pre-existing, but live
  drafts make divergence visible).
- Analysts may read and act on prose that is later withdrawn; bounded by
  the explicit withdrawal note and the no-validated-affordances rule.
- D1 residual (owner-accepted): if a misconfigured endpoint reports its
  served model late, draft text can paint before the detective check
  raises and withdraws the turn. The check itself, the audit row, and the
  answer-blocking behavior of the validated path are unchanged.
- The 16-thread ask pool (main.py:844-877) is unchanged; live drafts do
  not add synthesis calls, only frames.

## 14. Engineering calls made without owner input (flagged, reversible)

- S31 runs echo-only in CI; live-model probing stays manual (G1).
- draft_withdrawn as a field on the terminal result payload rather than a
  dedicated terminal frame (fewer event types; the client keys one place).
- Client typewriter cadence and pacing buffer live in the page layer (no
  new dependency); the exact pacing constant is tuned in review.
