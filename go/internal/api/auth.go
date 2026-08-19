package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"log"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"

	"github.com/Hussain0327/amneal/go/internal/store"
)

const (
	// SESSION_COOKIE in src/regwatch/auth/deps.py.
	sessionCookie = "regwatch_session"
	// _LAST_SEEN_COALESCE in src/regwatch/auth/sessions.py: last_seen_at is
	// informational; coalescing its write to once per 5 minutes per session
	// avoids a WAL-generating UPDATE on every authenticated read.
	lastSeenCoalesce = 5 * time.Minute
)

// dummyHash mirrors passwords.py::_DUMMY_HASH -- verified against on unknown
// email so login timing does not reveal whether an account exists. Cost 12 =
// Python's bcrypt.gensalt() default, keeping the dummy verify's duration in
// the same band as a real-user verify.
var dummyHash = func() []byte {
	h, err := bcrypt.GenerateFromPassword([]byte("regwatch-dummy-password-for-uniform-timing"), 12)
	if err != nil {
		panic(err) // deterministic input; cannot fail
	}
	return h
}()

// verifyPassword ports passwords.py::verify_password: bcrypt over the FIRST
// 72 BYTES of the password. Python's bcrypt lib truncates silently; Go's
// x/crypto/bcrypt ERRORS on >72-byte inputs instead, so the truncation here
// is load-bearing wire parity, not an optimization.
func verifyPassword(hash string, password string) bool {
	b := []byte(password)
	if len(b) > 72 {
		b = b[:72]
	}
	return bcrypt.CompareHashAndPassword([]byte(hash), b) == nil
}

// hashToken ports sessions.py::_hash_token: the cookie carries an opaque
// random token; only its sha256 hex ever touches the database. This MUST
// match Python exactly -- Python's require_user keeps resolving the sessions
// this binary mints (and vice versa) until step 4 completes.
func hashToken(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// newToken ports secrets.token_urlsafe(32): 32 CSPRNG bytes, URL-safe base64,
// no padding (43 chars).
func newToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// Server is the native auth/session surface. The seams (now, checkPassword,
// perIPLimit) mirror the exact monkeypatch surface the pytest suite used
// (main.authenticate / LOGIN_ATTEMPTS_PER_IP_PER_MINUTE), so the ported
// contract tests can exercise the same paths.
type Server struct {
	q       *store.Queries
	pool    *pgxpool.Pool
	cfg     Config
	limiter *RateLimiter
	errLog  *log.Logger

	// queryLimiter is the per-user window for POST /query and /query/stream,
	// distinct from the login limiter above (disjoint keys, same mechanics).
	// Since the step-5 cutover Go is the single rate-limit authority.
	queryLimiter *RateLimiter
	// rag calls the internal Python RAG compute endpoint for handleCompleteQuery.
	rag *ragClient

	now           func() time.Time
	checkPassword func(hash, password string) bool
	perIPLimit    int
}

// NewServer builds the native auth/session/query surface over pool with cfg,
// logging handler errors to errLog (log.Default() when nil).
func NewServer(pool *pgxpool.Pool, cfg Config, errLog *log.Logger) *Server {
	if errLog == nil {
		errLog = log.Default()
	}
	return &Server{
		q:             store.New(pool),
		pool:          pool,
		cfg:           cfg,
		limiter:       NewRateLimiter(),
		queryLimiter:  NewRateLimiter(),
		rag:           newRAGClient(cfg.InternalRAGURL, cfg.InternalRAGToken, cfg.RAGTimeout),
		errLog:        errLog,
		now:           func() time.Time { return time.Now().UTC() },
		checkPassword: verifyPassword,
		perIPLimit:    LoginAttemptsPerIPPerMinute,
	}
}

func ts(t time.Time) pgtype.Timestamp {
	return pgtype.Timestamp{Time: t, Valid: true}
}

type userOut struct {
	ID          int32  `json:"id"`
	Email       string `json:"email"`
	DisplayName string `json:"display_name"`
	Role        string `json:"role"`
}

type authUserResponse struct {
	User userOut `json:"user"`
}

// handleLogin ports main.py::login branch-for-branch. Response paths:
// 422 validation; 429 {"detail":"rate limit exceeded"} (no Retry-After --
// Python sends none); 401 {"detail":"invalid email or password"} (ONE message
// for unknown email / wrong password / inactive account); 200
// {"user":{id,email,display_name,role}} + the session cookie.
func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	// Pointer fields distinguish missing (422, like pydantic) from empty
	// (passes validation, fails authentication -- no min_length in Python).
	// Unknown body fields are ignored, matching pydantic's default.
	var body struct {
		Email    *string `json:"email"`
		Password *string `json:"password"`
	}
	if !decodeStrictJSON(w, r, &body) {
		return
	}
	// Max lengths count CODE POINTS (pydantic max_length semantics), not
	// bytes -- a multibyte email under 254 characters must not 422.
	var problems []validationItem
	if body.Email == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "email"}, Msg: "Field required"})
	} else if utf8.RuneCountInString(*body.Email) > 254 {
		// Bounded input: the limiter key embeds the client-supplied email, so
		// an unbounded string would pin arbitrary attacker memory per request.
		problems = append(problems, validationItem{Type: "string_too_long", Loc: []string{"body", "email"}, Msg: "String should have at most 254 characters"})
	}
	if body.Password == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "password"}, Msg: "Field required"})
	} else if utf8.RuneCountInString(*body.Password) > 256 {
		problems = append(problems, validationItem{Type: "string_too_long", Loc: []string{"body", "password"}, Msg: "String should have at most 256 characters"})
	}
	if len(problems) > 0 {
		writeValidationError(w, problems...)
		return
	}

	email := strings.ToLower(strings.TrimSpace(*body.Email))
	ip := clientIP(r, s.cfg.TrustProxy)

	// Two independent windows, per-email checked FIRST with short-circuit ||,
	// so a denied per-email attempt does NOT charge the per-IP bucket -- and
	// the limiter runs BEFORE the credential check: a brute force with the
	// CORRECT password still 429s (pinned by the Python suite).
	if !s.limiter.Allow("login:"+email, LoginAttemptsPerMinute) ||
		!s.limiter.Allow("login:ip:"+ip, s.perIPLimit) {
		writeDetail(w, http.StatusTooManyRequests, detailRateLimited)
		return
	}

	user, err := s.authenticate(r.Context(), email, *body.Password)
	if err != nil {
		s.internalError(w, "login", err)
		return
	}
	if user == nil {
		writeDetail(w, http.StatusUnauthorized, "invalid email or password")
		return
	}

	token, err := s.createSession(r.Context(), user.ID)
	if err != nil {
		s.internalError(w, "create session", err)
		return
	}

	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookie,
		Value:    token,
		MaxAge:   int(s.cfg.SessionTTL / time.Second),
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   s.cfg.CookieSecure,
		Path:     "/",
	})
	writeJSON(w, http.StatusOK, authUserResponse{User: userOut{
		ID: user.ID, Email: user.Email, DisplayName: user.DisplayName, Role: user.Role,
	}})
}

// authenticate ports sessions.py::authenticate: normalize, uniform bcrypt
// timing on unknown email (dummy hash), is_active checked AFTER the password
// verify, one nil for every failure mode.
func (s *Server) authenticate(ctx context.Context, normalizedEmail, password string) (*store.User, error) {
	u, err := s.q.GetUserByEmail(ctx, normalizedEmail)
	if errors.Is(err, pgx.ErrNoRows) {
		_ = s.checkPassword(string(dummyHash), password)
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !s.checkPassword(u.PasswordHash, password) {
		return nil, nil
	}
	if !u.IsActive {
		return nil, nil
	}
	return &u, nil
}

// createSession ports sessions.py::create_session: the opportunistic sweep of
// ALL expired rows and the fresh insert (no fixation -- login never reuses a
// session) run in ONE transaction, exactly like Python's session_scope.
func (s *Server) createSession(ctx context.Context, userID int32) (string, error) {
	token, err := newToken()
	if err != nil {
		return "", err
	}
	now := s.now()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	qtx := s.q.WithTx(tx)
	if _, err := qtx.DeleteExpiredAuthSessions(ctx, ts(now)); err != nil {
		return "", err
	}
	if _, err := qtx.CreateAuthSession(ctx, store.CreateAuthSessionParams{
		TokenHash: hashToken(token),
		UserID:    userID,
		CreatedAt: ts(now),
		ExpiresAt: ts(now.Add(s.cfg.SessionTTL)),
	}); err != nil {
		return "", err
	}
	return token, tx.Commit(ctx)
}

// handleLogout ports main.py::logout: revoke the presented session server-side
// (silent when absent), always clear the cookie, always 204. No auth required;
// this endpoint never errors.
func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	if c, err := r.Cookie(sessionCookie); err == nil && c.Value != "" {
		if _, err := s.q.DeleteAuthSessionByTokenHash(r.Context(), hashToken(c.Value)); err != nil {
			// Revocation is best-effort by contract (Python's revoke_token is
			// silent); losing it degrades to TTL expiry. Log, still 204.
			s.errLog.Printf("logout: revoke failed: %v", err)
		}
	}
	// The deletion cookie mirrors the mint attributes (handleLogin above):
	// browsers match deletion on (name, domain, path) alone, so this is
	// hygiene rather than correctness -- both Set-Cookie writes for
	// regwatch_session stay attribute-identical. The old bare shape existed
	// only for wire parity with Python's delete_cookie, retired with the
	// Python logout path.
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookie,
		Value:    "",
		MaxAge:   -1,
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   s.cfg.CookieSecure,
		Path:     "/",
	})
	w.WriteHeader(http.StatusNoContent)
}

// handleMe ports main.py::me.
func (s *Server) handleMe(w http.ResponseWriter, r *http.Request) {
	u, ok := s.currentUser(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, authUserResponse{User: userOut{
		ID: u.UserID, Email: u.Email, DisplayName: u.DisplayName, Role: u.Role,
	}})
}

// currentUser ports deps.py::require_user + sessions.py::resolve_token: no
// cookie, unknown token, EXPIRED token (row deleted on sight), and inactive
// user all yield the same 401 {"detail":"authentication required"} with no
// WWW-Authenticate header (Python sends none). Writes the 401 itself; the
// caller returns on !ok.
func (s *Server) currentUser(w http.ResponseWriter, r *http.Request) (store.GetAuthSessionWithUserRow, bool) {
	var zero store.GetAuthSessionWithUserRow
	c, err := r.Cookie(sessionCookie)
	if err != nil || c.Value == "" {
		writeDetail(w, http.StatusUnauthorized, "authentication required")
		return zero, false
	}
	tokenHash := hashToken(c.Value)
	row, err := s.q.GetAuthSessionWithUser(r.Context(), tokenHash)
	if errors.Is(err, pgx.ErrNoRows) {
		writeDetail(w, http.StatusUnauthorized, "authentication required")
		return zero, false
	}
	if err != nil {
		s.internalError(w, "resolve session", err)
		return zero, false
	}
	now := s.now()
	if !now.Before(row.ExpiresAt.Time) { // Python: now >= expires_at
		if _, err := s.q.DeleteAuthSessionByTokenHash(r.Context(), tokenHash); err != nil {
			s.errLog.Printf("purge expired session: %v", err)
		}
		writeDetail(w, http.StatusUnauthorized, "authentication required")
		return zero, false
	}
	if !row.IsActive {
		writeDetail(w, http.StatusUnauthorized, "authentication required")
		return zero, false
	}
	if !row.LastSeenAt.Valid || now.Sub(row.LastSeenAt.Time) >= lastSeenCoalesce {
		if err := s.q.TouchAuthSessionLastSeen(r.Context(), store.TouchAuthSessionLastSeenParams{
			TokenHash: tokenHash, LastSeenAt: ts(now),
		}); err != nil {
			// Informational column; a lost touch must never fail the request.
			s.errLog.Printf("touch last_seen: %v", err)
		}
	}
	return row, true
}

func (s *Server) internalError(w http.ResponseWriter, op string, err error) {
	// Error content (SQL, addresses) never reaches the client. Deliberate
	// shape divergence from FastAPI: its unhandled-exception 500 is
	// text/plain "Internal Server Error"; this emits the same words as the
	// JSON {"detail":...} shape every other error here uses. Nothing
	// observes 500 bodies (they only occur on DB failure).
	s.errLog.Printf("%s: %v", op, err)
	writeDetail(w, http.StatusInternalServerError, "Internal Server Error")
}
