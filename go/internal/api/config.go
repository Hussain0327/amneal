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

	return Config{
		CookieSecure: cookieSecure,
		SessionTTL:   time.Duration(ttlHours) * time.Hour,
		TrustProxy:   trustProxy,
		CORSOrigins:  origins,
		Insecure:     insecure,
	}, nil
}

// EnvBool is the exported pydantic-parity bool parser for callers outside
// this package (cmd/proxy reads REQUIRE_DATABASE_URL with it, so both
// runtimes accept the same spellings).
func EnvBool(name string, def bool) (bool, error) {
	return envBool(name, def)
}
