package api

// DB-free unit tests: limiter, client IP, wire helpers, config. These always
// run (no TEST_DATABASE_URL needed), mirroring the Python tests of the same
// units (test_rate_limiter_evicts_idle_keys, _client_ip docstring table,
// isoformat/truncation behavior).

import (
	"math"
	"net/http/httptest"
	"os"
	"strings"
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
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
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
	ascii := strings.Repeat("a", 70)
	if got := truncate60(ascii); len(got) != 60 {
		t.Fatalf("ascii truncation: len=%d", len(got))
	}
	multi := strings.Repeat("é", 70) // 2 bytes per rune
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
	long := strings.Repeat("x", 80)
	// Hash what Python would have hashed: the first 72 bytes. MinCost
	// keeps the test fast; verification cost comes from the stored hash.
	h, err := bcrypt.GenerateFromPassword([]byte(long[:72]), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	hashOfTruncated := string(h)
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

func TestConfigFromEnvPublicSettings(t *testing.T) {
	for _, name := range []string{"AUTH_COOKIE_SECURE", "TRUST_PROXY_HEADERS", "AUTH_SESSION_TTL_HOURS",
		"CORS_ALLOW_ORIGINS_CSV", "SENTRY_ENVIRONMENT", "INGEST_EMBEDDING_PROVIDER",
		"EMBEDDING_PROVIDER", "LLM_PROVIDER", "OPENAI_LLM_MODEL", "RETRIEVAL_TOP_K",
		"REFUSAL_SCORE_THRESHOLD", "REFUSAL_SCORE_THRESHOLD_BY_PROFILE",
		"RETRIEVAL_EMBEDDING_PROFILE", "ACTIVE_EMBEDDING_PROFILE", "COMPANY_NAME"} {
		// t.Setenv registers restoration of the original value; the Unsetenv
		// after it gives the UNSET state envOrDefault distinguishes from "".
		t.Setenv(name, "")
		_ = os.Unsetenv(name)
	}
	cfg, err := ConfigFromEnv()
	if err != nil {
		t.Fatalf("defaults: %v", err)
	}
	// Mirrors of config/settings.py: providers are required-explicit, so the
	// unset state is the EMPTY STRING here, never a guessed provider.
	if cfg.EmbeddingProvider != "" || cfg.LLMProvider != "" ||
		cfg.LLMModel != "gpt-5.6-luna" || cfg.RetrievalTopK != nil ||
		cfg.RefusalScoreThreshold != 0.30 || cfg.CompanyName != "Amneal" {
		t.Fatalf("settings defaults: %+v", cfg)
	}

	// Prod values (fly.toml [env] + app-wide secrets reach proxy machines).
	t.Setenv("INGEST_EMBEDDING_PROVIDER", "openai")
	t.Setenv("OPENAI_LLM_MODEL", "gpt-5.6-luna")
	t.Setenv("RETRIEVAL_TOP_K", "8")
	cfg, err = ConfigFromEnv()
	if err != nil {
		t.Fatalf("overrides: %v", err)
	}
	if cfg.EmbeddingProvider != "openai" || cfg.LLMModel != "gpt-5.6-luna" ||
		cfg.RetrievalTopK == nil || *cfg.RetrievalTopK != 8 {
		t.Fatalf("settings overrides: %+v", cfg)
	}

	// Fail-loud parsing, like the pydantic field validators.
	t.Setenv("RETRIEVAL_TOP_K", "eight")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("non-integer RETRIEVAL_TOP_K must fail loudly")
	}
	t.Setenv("RETRIEVAL_TOP_K", "8")
	t.Setenv("REFUSAL_SCORE_THRESHOLD", "1.5")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("out-of-range REFUSAL_SCORE_THRESHOLD must fail loudly")
	}
	// ParseFloat accepts "nan"; pydantic's 0<=v<=1 rejects it. A NaN slipping
	// through would break GET /settings (json.Encoder cannot marshal NaN,
	// failing after the 200 header on every request).
	t.Setenv("REFUSAL_SCORE_THRESHOLD", "nan")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("NaN REFUSAL_SCORE_THRESHOLD must fail loudly")
	}
	t.Setenv("REFUSAL_SCORE_THRESHOLD", "0.45")
	cfg, err = ConfigFromEnv()
	if err != nil || cfg.RefusalScoreThreshold != 0.45 {
		t.Fatalf("threshold override: %+v err=%v", cfg, err)
	}
}

// TestConfigFromEnvEffectiveRefusalThreshold pins GET /settings to the floor
// the synthesizer APPLIES (grounded_qa._refusal_threshold), not the global
// field. Prod sets only REFUSAL_SCORE_THRESHOLD_BY_PROFILE, so the global
// alone reported 0.30 while every answer was gated at the profile's 0.70 --
// the confidence band (issue #272) was derived from a floor nothing used.
func TestConfigFromEnvEffectiveRefusalThreshold(t *testing.T) {
	for _, name := range []string{"REFUSAL_SCORE_THRESHOLD", "REFUSAL_SCORE_THRESHOLD_BY_PROFILE",
		"RETRIEVAL_EMBEDDING_PROFILE", "ACTIVE_EMBEDDING_PROFILE"} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name)
	}
	// The prod shape: map only, global unset, profile named by the new env.
	t.Setenv("REFUSAL_SCORE_THRESHOLD_BY_PROFILE", `{"ep_live": 0.7, "legacy": 0.3}`)
	t.Setenv("RETRIEVAL_EMBEDDING_PROFILE", "ep_live")
	cfg, err := ConfigFromEnv()
	if err != nil || cfg.RefusalScoreThreshold != 0.7 {
		t.Fatalf("profile entry must win over the 0.30 default: %+v err=%v", cfg, err)
	}
	// The deprecated alias names the profile when the new env is unset.
	_ = os.Unsetenv("RETRIEVAL_EMBEDDING_PROFILE")
	t.Setenv("ACTIVE_EMBEDDING_PROFILE", "ep_live")
	if cfg, err = ConfigFromEnv(); err != nil || cfg.RefusalScoreThreshold != 0.7 {
		t.Fatalf("ACTIVE_EMBEDDING_PROFILE alias: %+v err=%v", cfg, err)
	}
	// Both unset resolves to "legacy", exactly as config/settings.py defaults.
	_ = os.Unsetenv("ACTIVE_EMBEDDING_PROFILE")
	if cfg, err = ConfigFromEnv(); err != nil || cfg.RefusalScoreThreshold != 0.3 {
		t.Fatalf("default profile must be legacy: %+v err=%v", cfg, err)
	}
	// A profile with no calibrated entry falls back to the global field.
	t.Setenv("RETRIEVAL_EMBEDDING_PROFILE", "ep_uncalibrated")
	t.Setenv("REFUSAL_SCORE_THRESHOLD", "0.45")
	if cfg, err = ConfigFromEnv(); err != nil || cfg.RefusalScoreThreshold != 0.45 {
		t.Fatalf("absent profile must fall back to the global: %+v err=%v", cfg, err)
	}
	// Malformed JSON refuses boot, as pydantic-settings does for the same env.
	t.Setenv("REFUSAL_SCORE_THRESHOLD_BY_PROFILE", `{"ep_live": 0.7`)
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("malformed REFUSAL_SCORE_THRESHOLD_BY_PROFILE must fail loudly")
	}
	t.Setenv("REFUSAL_SCORE_THRESHOLD_BY_PROFILE", `["ep_live", 0.7]`)
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("non-object REFUSAL_SCORE_THRESHOLD_BY_PROFILE must fail loudly")
	}
}

// TestLatencyMs pins query_log.latency_ms's NULL-not-zero rule. The column
// feeds the provider-cutover p95 gates (docs/DATABRICKS_ADOPTION_2026-07-28.md
// steps 2 and 7); a percentile where "unknown" and "instantaneous" both read 0
// understates exactly the regression those gates exist to catch.
func TestLatencyMs(t *testing.T) {
	t0 := time.Unix(1000, 0)

	if got := latencyMs(time.Time{}, t0.Add(time.Second)); got.Valid {
		t.Fatalf("no start stamp must be NULL, got %d", got.Int32)
	}
	// A clock that moved backwards is unknown, not negative and not zero.
	if got := latencyMs(t0, t0.Add(-time.Second)); got.Valid {
		t.Fatalf("backwards clock must be NULL, got %d", got.Int32)
	}
	got := latencyMs(t0, t0.Add(1500*time.Millisecond))
	if !got.Valid || got.Int32 != 1500 {
		t.Fatalf("want 1500ms, got %+v", got)
	}
	// A same-instant turn is a real measurement of 0, not an absent one.
	if got := latencyMs(t0, t0); !got.Valid || got.Int32 != 0 {
		t.Fatalf("zero-duration turn must be a VALID 0, got %+v", got)
	}
	// Column-width guard: int4, never a wrapped negative.
	if got := latencyMs(t0, t0.Add(400*24*time.Hour)); !got.Valid || got.Int32 != math.MaxInt32 {
		t.Fatalf("want int4 clamp, got %+v", got)
	}
}

// TestAuditParamsStampsLatencyFromTheTurnClock proves the column is derived
// from the control plane's clocks and NOT from the stateless core's kwargs --
// the core cannot see transport time, so a value arriving over the wire would
// be measuring the wrong thing.
func TestAuditParamsStampsLatencyFromTheTurnClock(t *testing.T) {
	t0 := time.Unix(2000, 0)
	p := auditParams(auditKwargs{Mode: "qa", QueryText: "q", AnswerText: "a"},
		t0.Add(750*time.Millisecond), t0)
	if !p.LatencyMs.Valid || p.LatencyMs.Int32 != 750 {
		t.Fatalf("want 750ms stamped, got %+v", p.LatencyMs)
	}
}
