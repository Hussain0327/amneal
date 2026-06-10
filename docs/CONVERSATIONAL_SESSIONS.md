# Conversational Sessions

REGWATCH can support chat-style follow-up questions while staying FDA/CRA safe.

The key rule is:

> Conversation memory may help resolve context, but it is never treated as FDA evidence.

## What Changed

The API now supports conversational sessions for `POST /query`.

Request fields:

- `question`: the user's message.
- `session_id`: optional existing chat thread ID. If omitted, the backend creates one.
  The thread must belong to the caller; another user's `session_id` returns 404.
- `filters`: optional explicit evidence filters. Explicit filters win over session memory.
- `k`: optional retrieval width override.

Caller identity is not a request field: it comes from the `regwatch_session`
auth cookie (see the Auth section in the top-level README). Sessions are bound
to the authenticated user, and `GET /sessions` / `GET /sessions/{id}` /
`DELETE /sessions/{id}` expose only the caller's own threads.

Response fields now include:

- `session_id`: the durable chat thread.
- `turn_id`: the specific user/assistant exchange.
- `status`: one of `answer`, `summary`, `clarify`, `scope_warning`, or `refused`.
- `citations`: validated FDA evidence citations.
- `audit_id`: the query audit row.

## Response Modes

- `answer`: cited answer from FDA evidence.
- `summary`: cited summary from FDA evidence.
- `clarify`: the system needs the user to choose a product/source instead of guessing.
- `scope_warning`: the user asked for regulatory strategy, submission drafting, or judgment. REGWATCH explains the boundary and offers evidence lookup instead.
- `refused`: no reliable supporting evidence is available.

## Follow-Up Resolution

For a follow-up like:

```text
What about dissolution?
```

REGWATCH may reuse the session's active product filter only when the previous
turn established one unambiguously, for example:

```json
{"normalized_name": "albuterol sulfate"}
```

Then it reruns retrieval with that filter. It does not answer from prior chat
text alone.

## Audit Model

The database now stores:

- `chat_session`: one row per chat thread.
- `chat_message`: one row per user or assistant message.
- `query_log.session_id`: links the answer to the session.
- `query_log.turn_id`: links the answer to the exact turn.
- `query_log.status`: answer mode.
- `query_log.route_json`: route, filters, context usage, and response mode.

This makes every conversational answer traceable to:

- user/session/turn,
- model,
- route and filters,
- retrieved evidence IDs,
- citations,
- refusal or scope-warning reason.

## Compliance Boundary

REGWATCH should sound helpful, not abrupt, but it must keep these boundaries:

- It can summarize what FDA sources say.
- It can answer factual questions from retrieved FDA evidence.
- It can ask clarifying questions when product identity is unclear.
- It cannot author submission strategy.
- It cannot recommend what to file or how to persuade FDA.
- It cannot turn conversation history into evidence.

