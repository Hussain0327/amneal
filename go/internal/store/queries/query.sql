-- query_log store surface for the Go CompleteQuery cutover (polyglot step 5,
-- PR B). The AUTHORITATIVE audit write (INV-6): exactly one row per turn, for
-- every outcome. Mirrors src/regwatch/common/audit.py::log_query column-for-
-- column -- ts is supplied by the caller (the control plane stamps it, so the
-- three per-turn writes carry a deterministic clock) and id is RETURNED so the
-- assistant chat_message can reference it. jsonb payloads (retrieved/citations/
-- route) are written VERBATIM as opaque bytes -- Go never interprets what the
-- stateless RAG core computed, preserving byte-equivalence with the Python
-- writer. Token/cost columns stay NULL when the core reports none (never 0).
-- latency_ms is the ONE column the stateless core does not supply: it is turn
-- wall time, which only the control plane holding the turn clock can measure.
-- The handler stamps it; the core's auditKwargs contract is unchanged.

-- name: InsertQueryLog :one
INSERT INTO public.query_log (
    ts,
    session_id,
    turn_id,
    user_id,
    mode,
    query_text,
    retrieved_json,
    answer_text,
    citations_json,
    refused,
    status,
    route_json,
    model_name,
    input_tokens,
    output_tokens,
    cost_usd,
    latency_ms
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
)
RETURNING id;
