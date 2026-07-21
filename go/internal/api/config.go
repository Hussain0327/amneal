// Package api is strangler Step 4 (PR B) of docs/POLYGLOT_TARGET_2026-07-10.md:
// the auth + chat-session HTTP surface, served natively by the proxy binary
// instead of relayed to FastAPI. Wire behavior mirrors src/regwatch/api/main.py
// byte-for-byte where the frontend depends on it; every deliberate divergence
// is commented at the site.
package api

import (
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
}

// envBool mirrors pydantic's bool coercion for the subset of spellings that
// appear in this repo's env files ("true"/"false", "1"/"0", "yes"/"no",
// "on"/"off", any case). Unset or empty -> def.
func envBool(name string, def bool) (bool, error) {
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
	cookieSecure, err := envBool("AUTH_COOKIE_SECURE", false)
	if err != nil {
		return Config{}, err
	}
	trustProxy, err := envBool("TRUST_PROXY_HEADERS", false)
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

	cfg := Config{
		CookieSecure: cookieSecure,
		SessionTTL:   time.Duration(ttlHours) * time.Hour,
		TrustProxy:   trustProxy,
		CORSOrigins:  origins,
		Insecure:     insecure,
		// Pydantic-default mirrors (config/settings.py); prod fly.toml [env]
		// overrides EMBEDDING_PROVIDER to "openai", the rest ride defaults.
		EmbeddingProvider:     envOrDefault("EMBEDDING_PROVIDER", "local-bge-small"),
		LLMProvider:           envOrDefault("LLM_PROVIDER", "openai"),
		LLMModel:              envOrDefault("LLM_MODEL", "gpt-5.4-nano"),
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
	return cfg, nil
}

// envOrDefault: pydantic-settings semantics for plain string fields -- an
// UNSET variable takes the default; a set-but-empty value is kept as "".
func envOrDefault(name, def string) string {
	if v, ok := os.LookupEnv(name); ok {
		return v
	}
	return def
}

// EnvBool is the exported pydantic-parity bool parser for callers outside
// this package (cmd/proxy reads REQUIRE_DATABASE_URL with it, so both
// runtimes accept the same spellings).
func EnvBool(name string, def bool) (bool, error) {
	return envBool(name, def)
}
