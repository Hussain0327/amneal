-- Feedback store surface for POST /feedback.
-- Mirrors src/regwatch/api/main.py feedback(): the caller may only rate their
-- OWN qa-mode query_log rows, and a foreign/missing/non-qa audit_id is a 404
-- that never confirms the row exists -- which is why the ownership probe and
-- the upsert are separate queries, exactly like the Python handler.

-- name: GetOwnedQaAuditRow :one
SELECT id FROM public.query_log
WHERE id = $1 AND user_id = $2 AND mode = 'qa';

-- Re-rating replaces the RATING and COMMENT but PRESERVES the original
-- created_at -- verified against the Python handler (_upsert_feedback updates
-- only rating and comment on the existing row), whose IntegrityError-retry
-- upsert this ON CONFLICT subsumes atomically. $5 feeds the INSERT path only.
-- name: UpsertAnswerFeedback :one
INSERT INTO public.answer_feedback (audit_id, user_id, rating, comment, created_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT ON CONSTRAINT uq_answer_feedback_audit_user
DO UPDATE SET rating = EXCLUDED.rating,
              comment = EXCLUDED.comment
RETURNING id;
