# Conversational sessions

**Last updated:** 2026-08-11

RegWatch answers follow-up questions like a chat, and stays inside the FDA/CRA
boundary while doing it. One rule holds the whole design together:

> Conversation memory helps work out what the user means. It is never treated as
> FDA evidence.

## The endpoint

`POST /query` (blocking) and `POST /query/stream` (Server-Sent Events).

Since 2026-07-24 the Go edge holds the public port:

- **`POST /query`** is served natively by Go when `GO_NATIVE_QUERY` is on, which
  it is in production. Go enforces auth (401), request validation (422), the
  per-user rate limit (429) and session ownership (a foreign `session_id` gets
  404), writes the chat rows, and calls the Python RAG core through the
  token-gated internal endpoint `POST /internal/query/compute`.
- **`POST /query/stream`** is gated by Go for auth and rate limit, then relayed
  to Python, which runs the whole turn including its own writes. The rate-limit
  bucket is shared with `/query`, so Go is the single authority and Python never
  rejects a stream Go already admitted.

The semantics below are the same on both paths.

### Request

| Field | Meaning |
|---|---|
| `question` | the user's message, 2 to 4000 characters |
| `session_id` | optional existing thread. Omitted, the backend creates one. Another user's thread returns 404 |
| `filters` | optional explicit evidence filters. Explicit filters beat session memory |
| `k` | optional retrieval width, 1 to 50 |
| `live_draft` | opt in to the live draft channel. Stream route only |

`filters` is narrowed to the five session keys (`normalized_name`,
`dosage_form`, `route`, `psg_type`, `doc_id`). Anything else is dropped, not
rejected, so options saved by older sessions keep working. Dropping rather than
trusting matters: a caller-supplied `version_id` would switch off current-version
scoping and let superseded chunks be cited as current.

Caller identity is not a request field. It comes from the `regwatch_session`
cookie. Threads are bound to the authenticated user, and `GET /sessions`,
`GET /sessions/{id}` and `DELETE /sessions/{id}` only ever show the caller their
own.

### Response

`answer`, `citations`, `refused`, `model_name`, `audit_id`, `session_id`,
`turn_id`, `status`, `reason`, `interpretation`, `clarify`, `related`,
`draft_withdrawn`.

The Ask surface in the Next.js frontend (`regwatch/frontend`,
`components/Turns.tsx`) is the client for this: user bubbles, citation chips
linking to FDA sources, and clarify pills, all driven by those fields.

## Response modes

`status` is one of seven values:

- `answer`: a cited answer from FDA evidence.
- `summary`: a cited summary.
- `clarify`: we need the user to pick a product or source instead of guessing.
- `scope_warning`: the user asked for strategy, drafting or judgment. RegWatch
  explains the boundary and offers evidence lookup instead.
- `meta`: a "what does this system do" question, answered from system state
  (corpus, watchlist, digest). Zero citations, `refused` is false, no
  model-written prose, so it cannot carry a regulatory claim.
- `refused`: the passages did not support an answer.
- `error`: the turn did not complete.

## What v7 changed

v7 selective citation is live. The headline rule is no longer "cite or refuse",
it is **cite the facts, talk like a person**:

- A sentence stating what FDA guidance says must carry the passage numbers it
  came from. An uncited one is still dropped by the gate. That is INV-1 and it
  is still enforced in code.
- Our own reading carries no numbers and must open with one of four pinned
  phrases.
- Greetings, offers and questions back to the user are plain text.

**There is no sentinel and no code word for "not found".** When the passages do
not answer the question, the model says so in ordinary words, names what it does
have nearby, and offers a next step, with zero passage numbers.

The wire shape of that turn did not change: it still leaves as
`status="refused"`, `reason="model_refusal"`, `citations=[]`. Only the text is
now the model's own, and only after every sentence of it is re-scanned against
the materiality and source-assertion lexicons.

## Follow-ups and memory

For a follow-up like:

```text
What about dissolution?
```

two separate mechanisms do the work.

**Carried-forward filters.** The session stores the active product filter, and
only when a previous turn established one unambiguously, for example
`{"normalized_name": "albuterol sulfate"}`. Retrieval then reruns with that
filter. Nothing is answered from prior chat text.

**Recent turns.** `get_recent_turns` returns up to 3 completed `answer` or
`summary` turns, oldest first. A turn is the user/assistant `chat_message` pair
sharing a `turn_id`. Refused, clarify and meta turns are skipped, because they
carry no fact worth threading. Citations are stripped from both sides and each
side is capped (400 characters for the question, 600 for the answer) so the
current passages stay dominant in the prompt window.

Those turns are used in exactly two places:

1. The "Recent conversation" block in the synthesizer prompt. The prompt states
   plainly that it is not a source, and `admit_turn` validates every citation
   against this turn's passages regardless.
2. Re-anchoring the retrieval query. A drill-down follow-up ("why?", "tell me
   more") carries no topical signal, so embedded literally it scores near zero
   and the turn dies on the low-score refusal. The retrieval text is re-anchored
   on the most recent prior question. Retrieval only, and INV-1 is untouched:
   how a passage was found cannot make an unsupported claim citable.

Memory is best effort. No session, no limit, or any database error returns an
empty list and logs it, so a memory hiccup can never fail or wrongly refuse an
answerable question.

Each remembered turn also carries a scope label for the route prompt.
`scope_kind` is only `product` when the stored assistant message has both a real
audit id and a `normalized_name`. Corpus scope is never inferred from shadow
output.

## Route observation (shadow only)

`REGWATCH_ROUTE_CALL` defaults to `off`. Both `shadow` and the reserved value
`live` currently execute as shadow: one bounded router call, strict parsing, and
a deterministic scope-compilation record written to
`query_log.route_json["route_call"]`. It never picks a retrieval query, mutates
a session, or writes anything the user sees. PR12 is the first change allowed to
make `live` mean something.

## Audit model

The database stores:

- `chat_session`: one row per thread.
- `chat_message`: one row per user or assistant message.
- `query_log.session_id`, `.turn_id`, `.status`: which thread, which exchange,
  which mode.
- `query_log.route_json`: route, filters, context usage, response mode, and the
  route-shadow record when it ran.
- `query_log.latency_ms`: wall time from turn start to the audit write
  (migration `0016`).

On `POST /query` the Go runtime (`go/internal/api/persist.go`) writes in three
steps: T1 the user `chat_message` (pre-RAG, best effort), T2 the `query_log`
audit row (post-RAG, authoritative), T3 the assistant `chat_message` (post-audit,
best effort). A chat or connection fault after T2 can never erase the committed
audit row.

Every completed turn writes exactly one audit row, answered or not. That makes
each answer traceable to the user, session and turn, the model, the route and
filters, the retrieved evidence ids, the citations, and the reason it declined
if it did.

One accepted edge: if the Python compute side sheds the request under load (a
503), the best-effort T1 user message may already exist, leaving an orphaned
user message with no audit row. Nothing ran, so there is nothing to audit.

`POST /resolve`, which backs the product-scope picker, is **not** a
conversational turn. It is deterministic entity resolution, mapping an RLD name
plus application number to the canonical spine, with no LLM call. It writes no
audit row on success or failure, creates no session or turn, and returns no
answer text. A mismatch is a 422 (refuse over guess), not a refused turn. So
setting the product scope never shows up in the conversational audit trail, only
the `/query` turns that use it do.

## Streaming

`POST /query/stream` emits five event names:

| Event | Payload | What it is |
|---|---|---|
| `status` | `{"text": ...}` | pipeline progress line |
| `token` | `{"delta": ...}` | a slice of the final answer, replayed |
| `draft` | `{"delta": ...}` | live un-gated prose from the model |
| `draft_reset` | `{}` | discard every draft delta received so far |
| `result` | full `QueryResponse` | exactly one, and the only authoritative frame |

`token` is not a live model stream. `ask()` replays the rendered answer only
after the turn cleared the claim gate (INV-1) and its audit row was committed
(INV-6), and only for an `answer` or `summary` turn. A decline replays zero
tokens. So a token delta is always a slice of the exact bytes the `result` frame
will carry.

`draft` is the live one, and it is triple gated: `REGWATCH_LIVE_DRAFT` on,
prose synthesis on, and the request setting `live_draft: true`. The Ask page
sets it. Draft text has not been through citation validation, so the UI renders
it as a clearly provisional draft with no citation surface and replaces it with
the result. `draft_reset` fires when a truncation retry restarts the completion,
and the client throws the partial draft away.

When drafts painted but the turn did not end as an answer, the server sets
`draft_withdrawn` on the result to the final status, or to `"partial"` when the
answer disclosed a dropped claim. The client keys its withdrawal note on that
value, never on diffing text.

If the stream fails before a `result` frame, it closes with no result and the
client falls back to blocking `POST /query` exactly once, discarding any draft.
Either way `ask()` writes exactly one audit row. Once `ask()` is dispatched onto
its worker thread it runs to completion even if the client disconnects, so that
turn is still audited.

## Compliance boundary

RegWatch should sound helpful, not abrupt, and it still holds these lines:

- It can summarize what FDA sources say.
- It can answer factual questions from retrieved FDA evidence.
- It can ask a clarifying question when the product is unclear.
- It cannot author submission strategy.
- It cannot recommend what to file or how to persuade FDA.
- It cannot turn conversation history into evidence.
