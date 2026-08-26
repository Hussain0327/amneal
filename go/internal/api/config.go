// Package api is strangler Step 4 (PR B) of docs/POLYGLOT_TARGET_2026-07-10.md:
// the auth + chat-session HTTP surface, served natively by the proxy binary
// instead of relayed to FastAPI. Wire behavior mirrors src/regwatch/api/main.py
// byte-for-byte where the frontend depends on it; every deliberate divergence
// is commented at the site.
package api

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Env names, defaults, and parsing mirror config/settings.py exactly so one
// ops override reaches both runtimes. On prod proxy machines today (fly.toml
// [env] is app-wide): AUTH_COOKIE_SECURE="true", TRUST_PROXY_HEADERS="true",
// CORS_ALLOW_ORIGINS_CSV="https://amneal.vercel.app",
// SENTRY_ENVIRONMENT="production"; AUTH_SESSION_TTL_HOURS is UNSET, so the
// code default below is what preserves the 72h wire contract (cookie Max-Age
// mirrors the server-side TTL).
const (
	defaultSessionTTLHours = 72
	defaultCORSOrigins     = "http://localhost:3000,http://127.0.0.1:3000"
)

// Config is resolved once at boot so config rot fails startup loudly.
type Config struct {
	CookieSecure bool
	SessionTTL   time.Duration
	TrustProxy   bool
	CORSOrigins  []string
	// Insecure flags the production-with-insecure-cookie misconfig; the
	// caller LOGS it loudly (Python parity: a warning, never a boot refusal
	// -- see ConfigFromEnv).
	Insecure bool
	// The GET /settings values, resolved from the same env names with the
	// same pydantic defaults as config/settings.py -- the six-field
	// PublicSettings allowlist. Parity note: pydantic-settings also loads a
	// .env FILE, which this does not; inert in prod (.env is .dockerignored
	// and never in the image), dev-only divergence for a bare local proxy.
	EmbeddingProvider     string
	LLMProvider           string
	LLMModel              string
	RetrievalTopK         *int // env unset => null on the wire, key present
	RefusalScoreThreshold float64
	CompanyName           string

	// -- step-5 CompleteQuery (PR B) --
	// Per-user requests/minute on POST /query and /query/stream, keyed
	// "user:{id}". Mirrors config/settings.py::rate_limit_per_minute (default
	// 30); <=0 disables (RateLimiter.Allow contract). Since the cutover Go is
	// the SINGLE rate-limit authority across both routes.
	RateLimitPerMinute int
	// The internal Python RAG compute endpoint (POST /internal/query/compute)
	// the CompleteQuery handler calls. Defaults to INTERNAL_RAG_URL, then the
	// proxy's UPSTREAM_URL, then loopback -- it is the SAME uvicorn the relay
	// fronts, reached directly (not through this proxy's own mux).
	InternalRAGURL string
	// Shared secret for that endpoint (X-Internal-Token). Empty is only valid
	// with native query OFF; the endpoint fail-closes (404) without it.
	InternalRAGToken string
	// FINITE overall deadline for the Go->Python compute call. NOT the SSE
	// "no timeout" -- this is a buffered JSON hop, so an accept-then-hang
	// upstream (or an embedding/retrieval stall llm_timeout_s does not cover)
	// must convert to a synthesized upstream_error audit row, never a leaked
	// request. Default 240s = llm_timeout_s(60) x (max_retries(2)+1) + margin.
	RAGTimeout time.Duration
	// Flag-gated cutover: false (default) relays POST /query to Python exactly
	// as today; true serves it natively via handleCompleteQuery. Flip is an
	// env change + restart, instantly reversible.
	GoNativeQuery bool
}

// EnvBool mirrors pydantic's bool coercion for the subset of spellings that
// appear in this repo's env files ("true"/"false", "1"/"0", "yes"/"no",
// "on"/"off", any case). Unset or empty -> def. Exported because cmd/proxy
// reads REQUIRE_DATABASE_URL with it, so both runtimes accept the same
// spellings.
func EnvBool(name string, def bool) (bool, error) {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(name)))
	switch v {
	case "":
		return def, nil
	case "1", "true", "yes", "on", "y", "t":
		return true, nil
	case "0", "false", "no", "off", "n", "f":
		return false, nil
	}
	return false, fmt.Errorf("env %s: cannot parse %q as bool", name, os.Getenv(name))
}

// ConfigFromEnv resolves the auth config and enforces the same fail-loud
// boot guard as the Python app (main.py lifespan): refusing to run a
// production environment with an insecure session cookie.
func ConfigFromEnv() (Config, error) {
	cookieSecure, err := EnvBool("AUTH_COOKIE_SECURE", false)
	if err != nil {
		return Config{}, err
	}
	trustProxy, err := EnvBool("TRUST_PROXY_HEADERS", false)
	if err != nil {
		return Config{}, err
	}

	ttlHours := defaultSessionTTLHours
	if v := strings.TrimSpace(os.Getenv("AUTH_SESSION_TTL_HOURS")); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return Config{}, fmt.Errorf("env AUTH_SESSION_TTL_HOURS: %q is not a positive integer", v)
		}
		ttlHours = n
	}

	csv := os.Getenv("CORS_ALLOW_ORIGINS_CSV")
	if strings.TrimSpace(csv) == "" {
		csv = defaultCORSOrigins
	}
	var origins []string
	for _, o := range strings.Split(csv, ",") {
		if o = strings.TrimSpace(o); o != "" {
			origins = append(origins, o)
		}
	}

	// Insecure-cookie-in-production check: Python's lifespan guard
	// (main.py) is a WARNING that keeps serving, and the edge process must
	// be no stricter -- a boot refusal here would crash-loop the machine
	// holding the public port on a config state the app group tolerates
	// (the exact failure mode the lazy DB pool exists to avoid). So: warn
	// via the Insecure flag (main.go logs it), never refuse.
	insecure := os.Getenv("SENTRY_ENVIRONMENT") == "production" && !cookieSecure
	embeddingProvider := envOrDefault("INGEST_EMBEDDING_PROVIDER", "")
	if embeddingProvider == "" {
		embeddingProvider = envOrDefault("EMBEDDING_PROVIDER", "")
	}

	cfg := Config{
		CookieSecure: cookieSecure,
		SessionTTL:   time.Duration(ttlHours) * time.Hour,
		TrustProxy:   trustProxy,
		CORSOrigins:  origins,
		Insecure:     insecure,
		// Mirrors of config/settings.py's required-explicit providers
		// (2026-08-14 postmortem: no implicit provider defaults anywhere).
		// Empty means unset -- the Python app refuses to boot in that state,
		// and /settings reports "" rather than a guessed provider. LLMModel
		// mirrors the OpenAI Responses model used by every role.
		EmbeddingProvider:     embeddingProvider,
		LLMProvider:           envOrDefault("LLM_PROVIDER", ""),
		LLMModel:              envOrDefault("OPENAI_LLM_MODEL", "gpt-5.6-luna"),
		RefusalScoreThreshold: 0.30,
		CompanyName:           envOrDefault("COMPANY_NAME", "Amneal"),
	}
	if v := strings.TrimSpace(os.Getenv("RETRIEVAL_TOP_K")); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return Config{}, fmt.Errorf("env RETRIEVAL_TOP_K: %q is not an integer", v)
		}
		cfg.RetrievalTopK = &n
	}
	if v := strings.TrimSpace(os.Getenv("REFUSAL_SCORE_THRESHOLD")); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		// Same range validator, same fail-loud posture as the pydantic field.
		// The negated >=/<= form rejects NaN too (ParseFloat accepts "nan",
		// every comparison with which is false -- and a NaN here would make
		// json.Encoder fail AFTER the 200 header on every GET /settings).
		if err != nil || !(f >= 0.0 && f <= 1.0) {
			return Config{}, fmt.Errorf("env REFUSAL_SCORE_THRESHOLD must be in [0, 1], got %q", v)
		}
		cfg.RefusalScoreThreshold = f
	}
	// The floor the synthesizer actually applies is per embedding profile
	// (grounded_qa._refusal_threshold): REFUSAL_SCORE_THRESHOLD_BY_PROFILE
	// keyed by the active profile, falling back to the global above. Prod
	// sets ONLY the map (the live profile's measured 0.70), so reporting the
	// global here told the UI the floor was 0.30 -- the confidence band and
	// its tooltip were keyed to a number no answer is ever gated on.
	effective, err := effectiveRefusalThreshold(cfg.RefusalScoreThreshold)
	if err != nil {
		return Config{}, err
	}
	cfg.RefusalScoreThreshold = effective

	// -- step-5 CompleteQuery (PR B) --
	cfg.RateLimitPerMinute = 30
	if v := strings.TrimSpace(os.Getenv("RATE_LIMIT_PER_MINUTE")); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return Config{}, fmt.Errorf("env RATE_LIMIT_PER_MINUTE: %q is not an integer", v)
		}
		cfg.RateLimitPerMinute = n
	}
	cfg.InternalRAGURL = envOrDefault("INTERNAL_RAG_URL", "")
	if cfg.InternalRAGURL == "" {
		cfg.InternalRAGURL = envOrDefault("UPSTREAM_URL", "http://127.0.0.1:8000")
	}
	cfg.InternalRAGToken = os.Getenv("INTERNAL_RAG_TOKEN")
	cfg.RAGTimeout = 240 * time.Second
	if v := strings.TrimSpace(os.Getenv("RAG_TIMEOUT_S")); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil || !(f > 0.0) {
			return Config{}, fmt.Errorf("env RAG_TIMEOUT_S must be a positive number of seconds, got %q", v)
		}
		cfg.RAGTimeout = time.Duration(f * float64(time.Second))
	}
	nativeQuery, err := EnvBool("GO_NATIVE_QUERY", false)
	if err != nil {
		return Config{}, err
	}
	cfg.GoNativeQuery = nativeQuery

	return cfg, nil
}

// envOrDefault: pydantic-settings semantics for plain string fields -- an
// UNSET variable takes the default; a set-but-empty value is kept as "".
// effectiveRefusalThreshold mirrors grounded_qa._refusal_threshold: the
// per-profile entry of REFUSAL_SCORE_THRESHOLD_BY_PROFILE for the active
// embedding profile (RETRIEVAL_EMBEDDING_PROFILE, legacy alias
// ACTIVE_EMBEDDING_PROFILE, default "legacy"), else the global fallback.
// The map is the same JSON object pydantic-settings parses; malformed JSON
// refuses boot on both sides. Map values are taken as-is, as pydantic does
// (only the global field carries the [0, 1] validator there).
func effectiveRefusalThreshold(global float64) (float64, error) {
	raw := strings.TrimSpace(os.Getenv("REFUSAL_SCORE_THRESHOLD_BY_PROFILE"))
	if raw == "" {
		return global, nil
	}
	byProfile := map[string]float64{}
	if err := json.Unmarshal([]byte(raw), &byProfile); err != nil {
		return 0, fmt.Errorf("env REFUSAL_SCORE_THRESHOLD_BY_PROFILE must be a JSON object of profile -> float: %w", err)
	}
	profile := strings.TrimSpace(envOrDefault("RETRIEVAL_EMBEDDING_PROFILE", ""))
	if profile == "" {
		profile = strings.TrimSpace(envOrDefault("ACTIVE_EMBEDDING_PROFILE", ""))
	}
	if profile == "" {
		profile = "legacy"
	}
	if f, ok := byProfile[profile]; ok {
		return f, nil
	}
	return global, nil
}

func envOrDefault(name, def string) string {
	if v, ok := os.LookupEnv(name); ok {
		return v
	}
	return def
}
