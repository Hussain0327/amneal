-- Auth store surface: opaque-cookie sessions (auth_session) + "user".
-- Semantics mirror src/regwatch/auth/sessions.py. POLICY STAYS IN CODE: these
-- queries only move rows -- expiry rejection, is_active rejection, uniform
-- bcrypt timing, and the last_seen 5-minute coalesce all live in the handler
-- layer (PR B), exactly where Python keeps them today.

-- name: GetUserByEmail :one
SELECT * FROM public."user"
WHERE email = $1;

-- name: CreateAuthSession :one
INSERT INTO public.auth_session (token_hash, user_id, created_at, expires_at)
VALUES ($1, $2, $3, $4)
RETURNING id;

-- resolve_token's lookup: one round trip for session + user. The caller
-- rejects expired rows and inactive users; a miss here is simply "no session".
-- name: GetAuthSessionWithUser :one
SELECT s.id AS session_id,
       s.expires_at,
       s.last_seen_at,
       u.id AS user_id,
       u.email,
       u.display_name,
       u.role,
       u.is_active
FROM public.auth_session s
JOIN public."user" u ON u.id = s.user_id
WHERE s.token_hash = $1;

-- Logout / revoke: silent when the row is already gone (rows tells the caller).
-- name: DeleteAuthSessionByTokenHash :execrows
DELETE FROM public.auth_session
WHERE token_hash = $1;

-- Login-time sweep of expired rows (sessions.py:80-81 -- login is the only
-- periodic cleanup; the cutoff is a parameter so tests control the clock).
-- name: DeleteExpiredAuthSessions :execrows
DELETE FROM public.auth_session
WHERE expires_at < $1;

-- name: TouchAuthSessionLastSeen :exec
UPDATE public.auth_session
SET last_seen_at = $2
WHERE token_hash = $1;
