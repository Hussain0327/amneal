package api

// DB-free unit tests: limiter, client IP, wire helpers, config. These always
// run (no TEST_DATABASE_URL needed), mirroring the Python tests of the same
// units (test_rate_limiter_evicts_idle_keys, _client_ip docstring table,
// isoformat/truncation behavior).

import (
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
	"golang.org/x/crypto/bcrypt"
)

func TestRateLimiterWindowAndEviction(t *testing.T) {
	clock := time.Unix(1000, 0)
	l := NewRateLimiter()
	l.now = func() time.Time { return clock }

	// 10 allowed, 11th denied -- the login per-email cap.
	for i := 0; i < LoginAttemptsPerMinute; i++ {
		if !l.Allow("login:a@example.com", LoginAttemptsPerMinute) {
			t.Fatalf("attempt %d should be allowed", i+1)
		}
	}
	if l.Allow("login:a@example.com", LoginAttemptsPerMinute) {
		t.Fatal("11th attempt within the window must be denied")
	}
	// Denied attempts do not extend the window: one minute later it reopens.
	clock = clock.Add(rateWindow)
	if !l.Allow("login:a@example.com", LoginAttemptsPerMinute) {
		t.Fatal("window should have slid open after 60s")
	}

	// limit <= 0 disables the check entirely.
	for i := 0; i < 100; i++ {
		if !l.Allow("anything", 0) {
			t.Fatal("limit<=0 must always allow")
		}
	}

	// Eviction: idle keys vanish after a full idle window (attacker-controlled
	// email keys must not pin memory for the process lifetime).
	l2 := NewRateLimiter()
	clock2 := time.Unix(2000, 0)
	l2.now = func() time.Time { return clock2 }
	for i := 0; i < 500; i++ {
		l2.Allow("login:spray-"+string(rune('a'+i%26))+string(rune('0'+i%10)), 10)
	}
	if len(l2.hits) == 0 {
		t.Fatal("expected live keys")
	}
	clock2 = clock2.Add(2 * rateWindow)
	l2.Allow("login:trigger-sweep", 10) // sweep runs at most once per window
	if got := len(l2.hits); got != 1 {
		t.Fatalf("idle keys must be evicted on sweep; %d keys remain", got)
	}
}

func TestClientIP(t *testing.T) {
	cases := []struct {
		name       string
		trust      bool
		remoteAddr string
		fly, xff   string
		want       string
	}{
		{"direct-tcp-peer", false, "203.0.113.7:5123", "198.51.100.9", "1.1.1.1", "203.0.113.7"},
		{"trust-fly-client-ip", true, "10.0.0.1:80", "198.51.100.9", "1.1.1.1, 2.2.2.2", "198.51.100.9"},
		{"trust-rightmost-xff", true, "10.0.0.1:80", "", "1.1.1.1, 2.2.2.2", "2.2.2.2"},
		{"trust-xff-trailing-empty", true, "10.0.0.1:80", "", "1.1.1.1, 2.2.2.2, ", "2.2.2.2"},
		{"trust-no-headers-falls-to-peer", true, "203.0.113.7:5123", "", "", "203.0.113.7"},
		{"no-peer", false, "", "", "", "unknown"},
	}
	for _, c := range cases {
		r := httptest.NewRequest("POST", "/auth/login", nil)
		r.RemoteAddr = c.remoteAddr
		if c.fly != "" {
			r.Header.Set("Fly-Client-IP", c.fly)
		}
		if c.xff != "" {
			r.Header.Set("X-Forwarded-For", c.xff)
		}
		if got := clientIP(r, c.trust); got != c.want {
			t.Fatalf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}

func TestIsoNaive(t *testing.T) {
	whole := pgtype.Timestamp{Time: time.Date(2026, 7, 20, 12, 34, 56, 0, time.UTC), Valid: true}
	if got := isoNaive(whole); got != "2026-07-20T12:34:56" {
		t.Fatalf("whole-second render: %q", got)
	}
	micro := pgtype.Timestamp{Time: time.Date(2026, 7, 20, 12, 34, 56, 789012000, time.UTC), Valid: true}
	if got := isoNaive(micro); got != "2026-07-20T12:34:56.789012" {
		t.Fatalf("microsecond render: %q", got)
	}
	small := pgtype.Timestamp{Time: time.Date(2026, 7, 20, 12, 34, 56, 123000, time.UTC), Valid: true}
	if got := isoNaive(small); got != "2026-07-20T12:34:56.000123" {
		t.Fatalf("zero-padded microsecond render: %q", got)
	}
}

func TestTruncate60IsRuneSafe(t *testing.T) {
	ascii := ""
	for i := 0; i < 70; i++ {
		ascii += "a"
	}
	if got := truncate60(ascii); len(got) != 60 {
		t.Fatalf("ascii truncation: len=%d", len(got))
	}
	multi := ""
	for i := 0; i < 70; i++ {
		multi += "é" // 2 bytes per rune
	}
	got := truncate60(multi)
	if r := []rune(got); len(r) != 60 {
		t.Fatalf("rune truncation: %d runes", len(r))
	}
	if truncate60("short") != "short" {
		t.Fatal("short strings pass through")
	}
}

func TestVerifyPasswordTruncatesAt72Bytes(t *testing.T) {
	// Python's bcrypt lib silently truncates at 72 bytes; x/crypto errors.
	// The wrapper must make a >72-byte password with a matching prefix verify,
	// exactly like Python.
	long := ""
	for i := 0; i < 80; i++ {
		long += "x"
	}
	hashOfTruncated := func() string {
		// Hash what Python would have hashed: the first 72 bytes. MinCost
		// keeps the test fast; verification cost comes from the stored hash.
		h, err := bcrypt.GenerateFromPassword([]byte(long[:72]), bcrypt.MinCost)
		if err != nil {
			t.Fatalf("hash: %v", err)
		}
		return string(h)
	}()
	if !verifyPassword(hashOfTruncated, long) {
		t.Fatal("80-byte password must verify against its 72-byte-prefix hash (Python parity)")
	}
	if verifyPassword(hashOfTruncated, "wrong") {
		t.Fatal("wrong password must not verify")
	}
}

func TestConfigFromEnvGuardsAndDefaults(t *testing.T) {
	t.Setenv("AUTH_COOKIE_SECURE", "")
	t.Setenv("TRUST_PROXY_HEADERS", "")
	t.Setenv("AUTH_SESSION_TTL_HOURS", "")
	t.Setenv("CORS_ALLOW_ORIGINS_CSV", "")
	t.Setenv("SENTRY_ENVIRONMENT", "")
	cfg, err := ConfigFromEnv()
	if err != nil {
		t.Fatalf("defaults: %v", err)
	}
	if cfg.CookieSecure || cfg.TrustProxy || cfg.SessionTTL != 72*time.Hour || len(cfg.CORSOrigins) != 2 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}

	// The production-with-insecure-cookie misconfig FLAGS (for a loud boot
	// warning) but never refuses -- Python parity: main.py's lifespan guard
	// is log.warning + keep serving, and the edge must be no stricter (a
	// boot refusal would crash-loop the machine holding the public port).
	t.Setenv("SENTRY_ENVIRONMENT", "production")
	cfg, err = ConfigFromEnv()
	if err != nil {
		t.Fatalf("production + insecure cookie must still boot (warn, not refuse): %v", err)
	}
	if !cfg.Insecure {
		t.Fatal("Insecure flag must be set for the boot warning")
	}
	t.Setenv("AUTH_COOKIE_SECURE", "true")
	cfg, err = ConfigFromEnv()
	if err != nil || !cfg.CookieSecure || cfg.Insecure {
		t.Fatalf("prod secure config: %+v err=%v", cfg, err)
	}

	t.Setenv("AUTH_SESSION_TTL_HOURS", "not-a-number")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("non-integer TTL is operator config rot and must fail loudly")
	}
}
