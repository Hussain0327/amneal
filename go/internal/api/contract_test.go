package api

// Contract tests for the Go-served auth + chat-session surface -- the ported
// pytest checklist (tests/test_auth.py, test_login_ratelimit_ip.py,
// test_api_contract_freeze.py::test_sessions_wire_keys_...). Assertions pin
// the SAME status codes, bodies, cookie attributes, orderings, and verbatim
// JSON passthrough the Python suite pinned, so PR B's deletion of the Python
// handlers cannot lose contract coverage.
//
// Opt-in Postgres discipline mirrors the pytest suite: TEST_DATABASE_URL
// unset => skip; the target database is DISPOSABLE (schema public is dropped
// and rebuilt from the PR-A snapshot). Requests run end-to-end through the
// COMPOSITE proxy handler (native routes + relay catch-all with a fake
// upstream), which also proves routing precedence and that everything else
// still relays untouched.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"

	"github.com/Hussain0327/amneal/go/internal/proxy"
	"github.com/Hussain0327/amneal/go/internal/store"
)

const (
	testEmail    = "analyst@example.com"
	testPassword = "correct-horse-battery-staple"
	waitFor      = 5 * time.Second
)

// bootstrap drops/recreates schema public from the PR-A snapshot (read from
// the store package dir -- go test's cwd is this package's dir).
func bootstrap(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx := t.Context()
	schema, err := os.ReadFile("../store/schema.sql")
	if err != nil {
		t.Fatalf("read schema snapshot: %v", err)
	}
	stmts := []string{"DROP SCHEMA public CASCADE", "CREATE SCHEMA public"}
	var b strings.Builder
	for _, line := range strings.Split(string(schema), "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "--") {
			continue
		}
		b.WriteString(line + "\n")
		if strings.HasSuffix(trimmed, ";") {
			stmts = append(stmts, b.String())
			b.Reset()
		}
	}
	for _, stmt := range stmts {
		if _, err := pool.Exec(ctx, stmt); err != nil {
			t.Fatalf("bootstrap: %v\n%s", err, stmt)
		}
	}
}

type harness struct {
	srv      *Server
	pool     *pgxpool.Pool
	ts       *httptest.Server
	upstream *httptest.Server
	client   *http.Client
}

// newHarness builds the full stack: disposable DB + Server + composite proxy
// handler over a recording fake upstream. Cookie jar OFF -- tests manage the
// session cookie explicitly so its attributes stay observable.
func newHarness(t *testing.T, cfg Config) *harness {
	t.Helper()
	dbURL := os.Getenv("TEST_DATABASE_URL")
	if dbURL == "" {
		t.Skip("TEST_DATABASE_URL not set (postgres contract tests are opt-in)")
	}
	ctx := t.Context()
	pool, err := store.NewPool(ctx, dbURL)
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	t.Cleanup(pool.Close)
	// Cross-package serialization with internal/store's tests: both suites
	// drop/recreate schema public on the same database while `go test ./...`
	// runs package binaries in parallel. Same advisory-lock key (721001),
	// held on a pinned conn for the whole test, released before pool.Close.
	lockConn, err := pool.Acquire(context.Background())
	if err != nil {
		t.Fatalf("acquire lock conn: %v", err)
	}
	if _, err := lockConn.Exec(context.Background(), "SELECT pg_advisory_lock(721001)"); err != nil {
		lockConn.Release()
		t.Fatalf("advisory lock: %v", err)
	}
	t.Cleanup(func() {
		_, _ = lockConn.Exec(context.Background(), "SELECT pg_advisory_unlock(721001)")
		lockConn.Release()
	})
	bootstrap(t, pool)

	logger := log.New(io.Discard, "", 0)
	srv := NewServer(pool, cfg, logger)

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Upstream", "python")
		_, _ = io.WriteString(w, "relayed:"+r.URL.Path)
	}))
	t.Cleanup(upstream.Close)
	up, _ := url.Parse(upstream.URL)

	ts := httptest.NewServer(proxy.NewHandlerWithNative(up, logger, srv.Routes()))
	t.Cleanup(ts.Close)

	return &harness{srv: srv, pool: pool, ts: ts, upstream: upstream,
		client: &http.Client{Timeout: waitFor}}
}

func (h *harness) seedUser(t *testing.T, email, password string, active bool) int32 {
	t.Helper()
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	var id int32
	err = h.pool.QueryRow(t.Context(),
		`INSERT INTO public."user" (email, password_hash, display_name, role, is_active, created_at)
		 VALUES ($1, $2, 'Test Analyst', 'analyst', $3, now()) RETURNING id`,
		strings.ToLower(email), string(hash), active).Scan(&id)
	if err != nil {
		t.Fatalf("seed user: %v", err)
	}
	return id
}

func (h *harness) do(t *testing.T, method, path, cookie string, body any) *http.Response {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		rdr = strings.NewReader(string(b))
	}
	req, err := http.NewRequest(method, h.ts.URL+path, rdr)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if cookie != "" {
		req.AddCookie(&http.Cookie{Name: sessionCookie, Value: cookie})
	}
	resp, err := h.client.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func decode(t *testing.T, resp *http.Response) map[string]any {
	t.Helper()
	var v map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	return v
}

// login performs a successful login and returns the session cookie value.
func (h *harness) login(t *testing.T, email, password string) string {
	t.Helper()
	resp := h.do(t, "POST", "/auth/login", "", map[string]string{"email": email, "password": password})
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("login: %d %s", resp.StatusCode, body)
	}
	for _, c := range resp.Cookies() {
		if c.Name == sessionCookie {
			return c.Value
		}
	}
	t.Fatal("no session cookie on successful login")
	return ""
}

func keySet(m map[string]any) map[string]bool {
	out := map[string]bool{}
	for k := range m {
		out[k] = true
	}
	return out
}

func wantKeys(t *testing.T, m map[string]any, keys ...string) {
	t.Helper()
	if len(m) != len(keys) {
		t.Fatalf("key set mismatch: got %v want %v", keySet(m), keys)
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			t.Fatalf("missing key %q in %v", k, keySet(m))
		}
	}
}

// --- login (test_login_success_returns_user_and_httponly_cookie etc.) ---

func TestLoginSuccessBodyAndCookie(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour, CORSOrigins: nil})
	uid := h.seedUser(t, testEmail, testPassword, true)

	// UPPERCASED email must normalize (Python: strip().lower()).
	resp := h.do(t, "POST", "/auth/login", "", map[string]string{
		"email": strings.ToUpper(testEmail), "password": testPassword,
	})
	if resp.StatusCode != 200 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	body := decode(t, resp)
	wantKeys(t, body, "user")
	user := body["user"].(map[string]any)
	wantKeys(t, user, "id", "email", "display_name", "role")
	if int32(user["id"].(float64)) != uid || user["email"] != testEmail ||
		user["display_name"] != "Test Analyst" || user["role"] != "analyst" {
		t.Fatalf("user body: %v", user)
	}

	var c *http.Cookie
	for _, cc := range resp.Cookies() {
		if cc.Name == sessionCookie {
			c = cc
		}
	}
	if c == nil {
		t.Fatal("no session cookie")
	}
	if !c.HttpOnly || c.Path != "/" || c.MaxAge != 72*3600 || c.SameSite != http.SameSiteLaxMode {
		t.Fatalf("cookie attributes: %+v", c)
	}
	if c.Secure {
		t.Fatal("Secure must be off when CookieSecure=false (localhost pilot parity)")
	}
	if len(c.Value) != 43 {
		t.Fatalf("token length %d, want 43 (token_urlsafe(32) parity)", len(c.Value))
	}
}

func TestLoginSecureCookieFlag(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour, CookieSecure: true})
	h.seedUser(t, testEmail, testPassword, true)
	resp := h.do(t, "POST", "/auth/login", "", map[string]string{"email": testEmail, "password": testPassword})
	for _, c := range resp.Cookies() {
		if c.Name == sessionCookie && !c.Secure {
			t.Fatal("Secure flag missing with CookieSecure=true")
		}
	}
}

// One message for unknown email / wrong password / inactive account
// (test_login_failures_share_one_message).
func TestLoginFailuresShareOneMessage(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	h.seedUser(t, testEmail, testPassword, true)
	h.seedUser(t, "inactive@example.com", testPassword, false)

	cases := []map[string]string{
		{"email": "nobody@example.com", "password": testPassword},
		{"email": testEmail, "password": "wrong"},
		{"email": "inactive@example.com", "password": testPassword},
	}
	for _, c := range cases {
		resp := h.do(t, "POST", "/auth/login", "", c)
		if resp.StatusCode != 401 {
			t.Fatalf("%v: status %d", c, resp.StatusCode)
		}
		if body := decode(t, resp); body["detail"] != "invalid email or password" {
			t.Fatalf("%v: body %v", c, body)
		}
		// A failed login must never set a cookie (test_auth.py:77).
		if n := len(resp.Cookies()); n != 0 {
			t.Fatalf("%v: %d cookies on a 401", c, n)
		}
	}
}

func TestLoginValidation(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	// Oversized email -> 422 (bounded input: the limiter key embeds it).
	resp := h.do(t, "POST", "/auth/login", "", map[string]string{
		"email": strings.Repeat("a", 300) + "@example.com", "password": "x",
	})
	if resp.StatusCode != 422 {
		t.Fatalf("oversized email: %d", resp.StatusCode)
	}
	// Missing field -> 422 (pydantic parity).
	resp = h.do(t, "POST", "/auth/login", "", map[string]string{"email": testEmail})
	if resp.StatusCode != 422 {
		t.Fatalf("missing password: %d", resp.StatusCode)
	}
}

// The limiter runs BEFORE the credential check: a brute force with the
// CORRECT password still 429s (test_login_brute_force_guard).
func TestLoginBruteForceGuard(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	h.seedUser(t, testEmail, testPassword, true)
	for i := 0; i < LoginAttemptsPerMinute; i++ {
		resp := h.do(t, "POST", "/auth/login", "", map[string]string{"email": testEmail, "password": "wrong"})
		if resp.StatusCode != 401 {
			t.Fatalf("attempt %d: %d", i+1, resp.StatusCode)
		}
	}
	resp := h.do(t, "POST", "/auth/login", "", map[string]string{"email": testEmail, "password": testPassword})
	if resp.StatusCode != 429 {
		t.Fatalf("11th attempt with CORRECT password: %d, want 429", resp.StatusCode)
	}
	if body := decode(t, resp); body["detail"] != "rate limit exceeded" {
		t.Fatalf("429 body: %v", body)
	}
	// Per-email isolation: a DIFFERENT account still logs in after the 429
	// (test_auth.py:401-404) -- the throttle is per-target, not global.
	h.seedUser(t, "other@example.com", testPassword, true)
	if resp := h.do(t, "POST", "/auth/login", "", map[string]string{
		"email": "other@example.com", "password": testPassword,
	}); resp.StatusCode != 200 {
		t.Fatalf("other account after 429: %d", resp.StatusCode)
	}
}

// TrustProxy=true keys the per-IP bucket on the Fly-edge-attested headers,
// end-to-end through the handler (the pytest suite covered this via
// test_login_ratelimit_ip.py; the clientIP unit table alone would not catch
// a mis-wiring of cfg.TrustProxy into handleLogin).
func TestLoginPerIPKeyingUnderTrustProxy(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour, TrustProxy: true})
	h.srv.perIPLimit = 2

	attempt := func(email string, hdr map[string]string) int {
		t.Helper()
		b, _ := json.Marshal(map[string]string{"email": email, "password": "x"})
		req, err := http.NewRequest("POST", h.ts.URL+"/auth/login", strings.NewReader(string(b)))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		for k, v := range hdr {
			req.Header.Set(k, v)
		}
		resp, err := h.client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = resp.Body.Close() }()
		_, _ = io.Copy(io.Discard, resp.Body)
		return resp.StatusCode
	}

	// Same attested Fly-Client-IP -> one bucket: third distinct email 429s.
	fly := map[string]string{"Fly-Client-IP": "198.51.100.9"}
	if attempt("a1@example.com", fly) != 401 || attempt("a2@example.com", fly) != 401 {
		t.Fatal("first two attempts should reach the credential check")
	}
	if got := attempt("a3@example.com", fly); got != 429 {
		t.Fatalf("third attempt from one attested IP: %d, want 429", got)
	}
	// A DIFFERENT attested IP has an independent budget.
	if got := attempt("b1@example.com", map[string]string{"Fly-Client-IP": "198.51.100.10"}); got != 401 {
		t.Fatalf("fresh attested IP must not share the bucket: %d", got)
	}
	// Rotating the LEFTMOST XFF hop must NOT mint fresh buckets: keying is
	// on the RIGHTMOST hop when Fly-Client-IP is absent.
	for i, want := range []int{401, 401, 429} {
		got := attempt(fmt.Sprintf("c%d@example.com", i), map[string]string{
			"X-Forwarded-For": fmt.Sprintf("attacker-%d.example, 203.0.113.50", i),
		})
		if got != want {
			t.Fatalf("XFF rotation attempt %d: %d, want %d", i, got, want)
		}
	}
}

// A live session whose user is deactivated afterwards stops resolving
// (test_cli_deactivate_user_blocks_login's HTTP half).
func TestDeactivatedUserWithLiveSession(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)
	if resp := h.do(t, "GET", "/auth/me", token, nil); resp.StatusCode != 200 {
		t.Fatalf("pre-deactivation me: %d", resp.StatusCode)
	}
	if _, err := h.pool.Exec(t.Context(), `UPDATE public."user" SET is_active = false`); err != nil {
		t.Fatal(err)
	}
	if resp := h.do(t, "GET", "/auth/me", token, nil); resp.StatusCode != 401 {
		t.Fatalf("deactivated user's live session must 401: %d", resp.StatusCode)
	}
}

// Per-IP cap: spraying DISTINCT emails from one host 429s at the IP window
// (test_login_ratelimit_ip.py, which monkeypatched the cap -- perIPLimit is
// the equivalent seam).
func TestLoginPerIPSprayGuard(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	h.srv.perIPLimit = 5
	for i := 0; i < 5; i++ {
		resp := h.do(t, "POST", "/auth/login", "", map[string]string{
			"email": fmt.Sprintf("spray-%d@example.com", i), "password": "x",
		})
		if resp.StatusCode != 401 {
			t.Fatalf("spray %d: %d", i, resp.StatusCode)
		}
	}
	resp := h.do(t, "POST", "/auth/login", "", map[string]string{
		"email": "spray-final@example.com", "password": "x",
	})
	if resp.StatusCode != 429 {
		t.Fatalf("6th distinct-email attempt from one IP: %d, want 429", resp.StatusCode)
	}
}

// --- me / logout / expiry (test_me_*, test_logout_*, test_expired_*) ---

func TestMeAndLogoutLifecycle(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)

	resp := h.do(t, "GET", "/auth/me", token, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("me: %d", resp.StatusCode)
	}
	// Full body pin: handleMe fills userOut from a DIFFERENT SQL row than
	// login does (the session join), so a field mix-up there would pass the
	// login-body test alone.
	user := decode(t, resp)["user"].(map[string]any)
	wantKeys(t, user, "id", "email", "display_name", "role")
	if int32(user["id"].(float64)) != uid || user["email"] != testEmail ||
		user["display_name"] != "Test Analyst" || user["role"] != "analyst" {
		t.Fatalf("me user: %v", user)
	}

	// Anonymous / garbage-token -> the single 401 message.
	for _, tok := range []string{"", "garbage-token"} {
		resp := h.do(t, "GET", "/auth/me", tok, nil)
		if resp.StatusCode != 401 {
			t.Fatalf("me(%q): %d", tok, resp.StatusCode)
		}
		if body := decode(t, resp); body["detail"] != "authentication required" {
			t.Fatalf("401 body: %v", body)
		}
	}

	// Logout: 204, cookie cleared, session revoked server-side; a second
	// logout (and one with no cookie at all) still 204s.
	resp = h.do(t, "POST", "/auth/logout", token, nil)
	if resp.StatusCode != 204 {
		t.Fatalf("logout: %d", resp.StatusCode)
	}
	cleared := false
	for _, c := range resp.Cookies() {
		if c.Name == sessionCookie && c.Value == "" && c.MaxAge < 0 {
			cleared = true
		}
	}
	if !cleared {
		t.Fatal("logout must clear the cookie (Max-Age=0)")
	}
	if resp := h.do(t, "GET", "/auth/me", token, nil); resp.StatusCode != 401 {
		t.Fatalf("me after logout: %d", resp.StatusCode)
	}
	if resp := h.do(t, "POST", "/auth/logout", token, nil); resp.StatusCode != 204 {
		t.Fatalf("repeat logout: %d", resp.StatusCode)
	}
	if resp := h.do(t, "POST", "/auth/logout", "", nil); resp.StatusCode != 204 {
		t.Fatalf("cookie-less logout: %d", resp.StatusCode)
	}
}

func TestExpiredSessionRejectedAndPurged(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)

	// A second, LIVE session must survive everything below -- the purge is
	// precise (only the presented expired row), not a broad delete.
	liveToken := h.login(t, testEmail, testPassword)

	// Move the clock past expiry for the FIRST session only -- the seam
	// Python got via direct row manipulation; expiring the row is equivalent.
	if _, err := h.pool.Exec(t.Context(),
		`UPDATE public.auth_session SET expires_at = now() - interval '1 hour' WHERE token_hash = $1`,
		hashToken(token)); err != nil {
		t.Fatalf("expire: %v", err)
	}
	if resp := h.do(t, "GET", "/auth/me", token, nil); resp.StatusCode != 401 {
		t.Fatalf("expired session: %d, want 401", resp.StatusCode)
	}
	// resolve_token deletes the presented expired row on sight, and ONLY it
	// (test_expired_session_rows_are_purged pins the remaining count).
	var n int
	if err := h.pool.QueryRow(t.Context(), `SELECT count(*) FROM public.auth_session`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("exactly the presented expired row must be purged; %d rows remain, want 1", n)
	}
	if resp := h.do(t, "GET", "/auth/me", liveToken, nil); resp.StatusCode != 200 {
		t.Fatalf("live session must survive the purge: %d", resp.StatusCode)
	}

	// Login-time sweep also clears expired rows (create_session's sweep).
	h.login(t, testEmail, testPassword)
	if _, err := h.pool.Exec(t.Context(),
		`UPDATE public.auth_session SET expires_at = now() - interval '1 hour'`); err != nil {
		t.Fatal(err)
	}
	h.login(t, testEmail, testPassword)
	if err := h.pool.QueryRow(t.Context(),
		`SELECT count(*) FROM public.auth_session WHERE expires_at < now()`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("login sweep left %d expired rows", n)
	}
}

// --- sessions (test_sessions_list_ordering_titles_and_counts etc.) ---

func (h *harness) seedChat(t *testing.T, sid string, userID any, title any, updatedOffset time.Duration) {
	t.Helper()
	if _, err := h.pool.Exec(t.Context(),
		`INSERT INTO public.chat_session (id, user_id, title, created_at, updated_at)
		 VALUES ($1, $2, $3, now(), now() + $4::interval)`,
		sid, userID, title, fmt.Sprintf("%f seconds", updatedOffset.Seconds())); err != nil {
		t.Fatalf("seed chat %s: %v", sid, err)
	}
}

func (h *harness) seedMsg(t *testing.T, id, sid, role, content, citations string, offset time.Duration) {
	t.Helper()
	if citations == "" {
		citations = "[]"
	}
	if _, err := h.pool.Exec(t.Context(),
		`INSERT INTO public.chat_message (id, session_id, turn_id, role, content, citations_json, clarify_json, created_at)
		 VALUES ($1, $2, 'turn-1', $3, $4, $5::jsonb, '[{"filters":{"source_url":"legacy"}}]'::jsonb, now() + $6::interval)`,
		id, sid, role, content, citations, fmt.Sprintf("%f seconds", offset.Seconds())); err != nil {
		t.Fatalf("seed msg %s: %v", id, err)
	}
}

func TestSessionsListOrderingTitlesAndCounts(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)
	uidStr := fmt.Sprint(uid)

	longQ := strings.Repeat("q", 80)
	h.seedChat(t, "sid1", uidStr, nil, -2*time.Hour)
	h.seedChat(t, "sid2", uidStr, nil, -3*time.Hour)
	h.seedChat(t, "empty-session", uidStr, nil, -1*time.Hour)
	h.seedMsg(t, "m1", "sid1", "user", longQ, "", -4*time.Hour)
	h.seedMsg(t, "m2", "sid1", "assistant", "a1", "", -3*time.Hour)
	h.seedMsg(t, "m3", "sid1", "user", "follow", "", -2*time.Hour)
	h.seedMsg(t, "m4", "sid1", "assistant", "a2", "", -1*time.Hour)
	h.seedMsg(t, "m5", "sid2", "user", "second session q", "", -3*time.Hour)
	h.seedMsg(t, "m6", "sid2", "assistant", "a", "", -2*time.Hour)

	resp := h.do(t, "GET", "/sessions", token, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("list: %d", resp.StatusCode)
	}
	body := decode(t, resp)
	wantKeys(t, body, "sessions")
	raw := body["sessions"].([]any)
	if len(raw) != 3 {
		t.Fatalf("want 3 sessions, got %d", len(raw))
	}
	var ids []string
	for _, s := range raw {
		m := s.(map[string]any)
		wantKeys(t, m, "id", "title", "created_at", "updated_at", "message_count")
		ids = append(ids, m["id"].(string))
	}
	// updated_at DESC: empty-session (-1h), sid1 (-2h), sid2 (-3h).
	if ids[0] != "empty-session" || ids[1] != "sid1" || ids[2] != "sid2" {
		t.Fatalf("ordering: %v", ids)
	}
	first := raw[0].(map[string]any)
	if first["title"] != "(untitled)" || first["message_count"].(float64) != 0 {
		t.Fatalf("empty session summary: %v", first)
	}
	second := raw[1].(map[string]any)
	if second["title"] != longQ[:60] {
		t.Fatalf("title must be first user message truncated to 60: %q", second["title"])
	}
	if second["message_count"].(float64) != 4 || raw[2].(map[string]any)["message_count"].(float64) != 2 {
		t.Fatal("message counts wrong")
	}
	// Timestamps parse as naive ISO (no timezone suffix).
	for _, key := range []string{"created_at", "updated_at"} {
		v := first[key].(string)
		if strings.ContainsAny(v, "Z+") {
			t.Fatalf("%s carries a timezone suffix: %q", key, v)
		}
		if _, err := time.Parse("2006-01-02T15:04:05.999999", v); err != nil {
			t.Fatalf("%s does not parse: %q (%v)", key, v, err)
		}
	}
}

func TestSessionsOwnershipInvisibility(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uidA := h.seedUser(t, testEmail, testPassword, true)
	h.seedUser(t, "b@example.com", testPassword, true)
	tokenB := h.login(t, "b@example.com", testPassword)

	h.seedChat(t, "owned-by-a", fmt.Sprint(uidA), nil, 0)
	h.seedChat(t, "legacy-null", nil, nil, 0)

	// Foreign and legacy NULL-user sessions: 404 with the exact body, never
	// confirmed to exist; absent from the list.
	for _, sid := range []string{"owned-by-a", "legacy-null", "never-existed"} {
		resp := h.do(t, "GET", "/sessions/"+sid, tokenB, nil)
		if resp.StatusCode != 404 {
			t.Fatalf("GET %s as B: %d", sid, resp.StatusCode)
		}
		if body := decode(t, resp); body["detail"] != "session not found" {
			t.Fatalf("404 body: %v", body)
		}
		if resp := h.do(t, "DELETE", "/sessions/"+sid, tokenB, nil); resp.StatusCode != 404 {
			t.Fatalf("DELETE %s as B: %d", sid, resp.StatusCode)
		}
	}
	resp := h.do(t, "GET", "/sessions", tokenB, nil)
	if got := decode(t, resp)["sessions"].([]any); len(got) != 0 {
		t.Fatalf("B's list must be empty, got %v", got)
	}
}

func TestSessionDetailShapeAndVerbatimPassthrough(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)
	h.seedChat(t, "s1", fmt.Sprint(uid), nil, 0)
	// Stored citation carries a key NO wire type declares -- it must survive
	// verbatim (api-contract-freeze passthrough contract).
	h.seedMsg(t, "m1", "s1", "user", "what changed?", `[{"doc_id":1,"legacy_extra":"kept","source_url":"https://x"}]`, -2*time.Hour)
	h.seedMsg(t, "m2", "s1", "assistant", "an answer", "", -1*time.Hour)

	resp := h.do(t, "GET", "/sessions/s1", token, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("detail: %d", resp.StatusCode)
	}
	body := decode(t, resp)
	wantKeys(t, body, "session", "messages")
	sess := body["session"].(map[string]any)
	wantKeys(t, sess, "id", "title", "created_at", "updated_at")
	if sess["title"] != "what changed?" {
		t.Fatalf("detail title fallback: %v", sess["title"])
	}
	msgs := body["messages"].([]any)
	if len(msgs) != 2 {
		t.Fatalf("want 2 messages, got %d", len(msgs))
	}
	m0 := msgs[0].(map[string]any)
	wantKeys(t, m0, "id", "turn_id", "role", "content", "status", "citations",
		"audit_id", "reason", "interpretation", "clarify", "related", "created_at")
	if m0["id"] != "m1" || m0["role"] != "user" || m0["content"] != "what changed?" {
		t.Fatalf("message order/content: %v", m0)
	}
	if m0["status"] != nil || m0["audit_id"] != nil {
		t.Fatalf("nullable fields must be JSON null: %v", m0)
	}
	cits := m0["citations"].([]any)
	c0 := cits[0].(map[string]any)
	if c0["legacy_extra"] != "kept" || c0["source_url"] != "https://x" {
		t.Fatalf("verbatim passthrough violated: %v", c0)
	}
	clarify := m0["clarify"].([]any)[0].(map[string]any)
	if clarify["filters"].(map[string]any)["source_url"] != "legacy" {
		t.Fatalf("clarify passthrough violated: %v", clarify)
	}
	// Assistant message with NULL/empty stored lists serializes them as [].
	m1 := msgs[1].(map[string]any)
	if _, ok := m1["related"].([]any); !ok {
		t.Fatalf("related must be a list: %v", m1["related"])
	}
}

func TestDeleteSessionRemovesMessages(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)
	h.seedChat(t, "s1", fmt.Sprint(uid), nil, 0)
	h.seedMsg(t, "m1", "s1", "user", "q", "", -1*time.Hour)
	h.seedMsg(t, "m2", "s1", "assistant", "a", "", 0)

	if resp := h.do(t, "DELETE", "/sessions/s1", token, nil); resp.StatusCode != 204 {
		t.Fatalf("delete: %d", resp.StatusCode)
	}
	if resp := h.do(t, "GET", "/sessions/s1", token, nil); resp.StatusCode != 404 {
		t.Fatalf("get after delete: %d", resp.StatusCode)
	}
	resp := h.do(t, "GET", "/sessions", token, nil)
	if got := decode(t, resp)["sessions"].([]any); len(got) != 0 {
		t.Fatalf("list after delete: %v", got)
	}
	var n int
	if err := h.pool.QueryRow(t.Context(), `SELECT count(*) FROM public.chat_message`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("messages must be hard-deleted with the session; %d remain", n)
	}
}

// --- 401 wall + routing precedence + relay untouched ---

func TestAuthWallAndRelayPrecedence(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})

	// Native routes behind the wall reject anonymous callers with the exact
	// Python body (test_every_protected_endpoint_requires_auth rows).
	for _, probe := range []struct{ method, path string }{
		{"GET", "/auth/me"}, {"GET", "/sessions"},
		{"GET", "/sessions/some-session-id"}, {"DELETE", "/sessions/some-session-id"},
		{"POST", "/feedback"}, {"GET", "/settings"},
		{"GET", "/products"}, {"POST", "/products"}, {"DELETE", "/products/1"},
	} {
		resp := h.do(t, probe.method, probe.path, "", nil)
		if resp.StatusCode != 401 {
			t.Fatalf("%s %s: %d", probe.method, probe.path, resp.StatusCode)
		}
		if body := decode(t, resp); body["detail"] != "authentication required" {
			t.Fatalf("401 body: %v", body)
		}
	}

	// Everything else still relays -- the strangler contract: /query, /health,
	// unknown paths. (/products left this list in PR C.)
	for _, path := range []string{"/query", "/health", "/anything/else"} {
		resp := h.do(t, "GET", path, "", nil)
		if resp.Header.Get("X-Upstream") != "python" {
			t.Fatalf("%s must relay to upstream (status %d)", path, resp.StatusCode)
		}
	}

	// Method mismatches on Go-owned paths must NOT relay (no Python handler
	// exists behind them since B2): FastAPI-shaped 405 with the first-match
	// Allow quirk preserved (GET for /sessions/{id}, never "GET, DELETE").
	for _, probe := range []struct{ method, path, allow string }{
		{"GET", "/auth/login", "POST"},
		{"DELETE", "/auth/logout", "POST"},
		{"PUT", "/auth/me", "GET"},
		{"POST", "/sessions", "GET"},
		{"PUT", "/sessions/some-id", "GET"},
	} {
		resp := h.do(t, probe.method, probe.path, "", nil)
		if resp.StatusCode != 405 {
			t.Fatalf("%s %s: %d, want 405", probe.method, probe.path, resp.StatusCode)
		}
		if got := resp.Header.Get("Allow"); got != probe.allow {
			t.Fatalf("%s %s Allow=%q, want %q", probe.method, probe.path, got, probe.allow)
		}
		if resp.Header.Get("X-Upstream") == "python" {
			t.Fatalf("%s %s leaked to the relay", probe.method, probe.path)
		}
		if body := decode(t, resp); body["detail"] != "Method Not Allowed" {
			t.Fatalf("405 body: %v", body)
		}
	}
}

func TestCORSParity(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour, CORSOrigins: []string{"https://amneal.vercel.app"}})
	h.seedUser(t, testEmail, testPassword, true)

	// Simple response: allowlisted Origin is echoed with credentials.
	req, _ := http.NewRequest("POST", h.ts.URL+"/auth/login", strings.NewReader(`{"email":"analyst@example.com","password":"correct-horse-battery-staple"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "https://amneal.vercel.app")
	resp, err := h.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.Header.Get("Access-Control-Allow-Origin") != "https://amneal.vercel.app" ||
		resp.Header.Get("Access-Control-Allow-Credentials") != "true" {
		t.Fatalf("CORS headers missing: %v", resp.Header)
	}

	// Disallowed origin: no CORS headers (the allowlist is the cookie guard).
	req2, _ := http.NewRequest("GET", h.ts.URL+"/auth/me", nil)
	req2.Header.Set("Origin", "https://evil.example.com")
	resp2, err := h.client.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp2.Body.Close() }()
	if resp2.Header.Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("disallowed origin must get no ACAO")
	}

	// Preflight.
	req3, _ := http.NewRequest("OPTIONS", h.ts.URL+"/auth/login", nil)
	req3.Header.Set("Origin", "https://amneal.vercel.app")
	req3.Header.Set("Access-Control-Request-Method", "POST")
	req3.Header.Set("Access-Control-Request-Headers", "content-type")
	resp3, err := h.client.Do(req3)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp3.Body.Close() }()
	if resp3.StatusCode != 200 ||
		resp3.Header.Get("Access-Control-Allow-Methods") != "GET, POST, DELETE" ||
		resp3.Header.Get("Access-Control-Allow-Headers") != "content-type" {
		t.Fatalf("preflight: %d %v", resp3.StatusCode, resp3.Header)
	}
}
