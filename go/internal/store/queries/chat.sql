-- Chat session/message store surface for GET/DELETE /sessions*.
-- Mirrors src/regwatch/api/main.py list_sessions/get_session/delete_session:
-- the list page is DELIBERATELY two queries (sessions + one GROUP BY of
-- message counts), never N+1; ownership is enforced by scoping every query
-- to user_id and treating a miss as 404 (never 403 -- foreign rows must not
-- be confirmed to exist).

-- display_title falls back to the first user message's content when the
-- session has no explicit title (the correlated subquery the Python list
-- endpoint uses). NULL when a session has neither -- the wire mapping owns
-- what to render then. COALESCE vs Python truthiness: an EMPTY-STRING title
-- would coalesce here but fall through in Python; unreachable today (nothing
-- in src/ ever writes title), revisit if a rename/title feature lands.
-- name: ListChatSessionsForUser :many
SELECT cs.id,
       cs.user_id,
       cs.title,
       cs.active_filters_json,
       cs.created_at,
       cs.updated_at,
       COALESCE(cs.title, (
         SELECT cm.content
         FROM public.chat_message cm
         WHERE cm.session_id = cs.id AND cm.role = 'user'
         ORDER BY cm.created_at ASC
         LIMIT 1
       )) AS display_title
FROM public.chat_session cs
WHERE cs.user_id = $1
ORDER BY cs.updated_at DESC;

-- name: CountChatMessagesForUser :many
SELECT cm.session_id,
       COUNT(*)::bigint AS message_count
FROM public.chat_message cm
JOIN public.chat_session cs ON cs.id = cm.session_id
WHERE cs.user_id = $1
GROUP BY cm.session_id;

-- name: GetChatSessionOwned :one
SELECT * FROM public.chat_session
WHERE id = $1 AND user_id = $2;

-- name: ListChatMessages :many
SELECT * FROM public.chat_message
WHERE session_id = $1
ORDER BY created_at ASC;

-- Hard delete, messages first (chat_message.session_id FK has no ON DELETE
-- CASCADE -- the order here is load-bearing, same as the Python handler).
-- name: DeleteChatMessagesBySession :execrows
DELETE FROM public.chat_message
WHERE session_id = $1;

-- name: DeleteChatSession :execrows
DELETE FROM public.chat_session
WHERE id = $1;
