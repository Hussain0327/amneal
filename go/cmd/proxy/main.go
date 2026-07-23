// Command proxy is the public-edge binary: the strangler reverse proxy that
// fronts the Python API on Fly (docs/GO_PROXY_ROLLOUT.md) and -- strangler
// Step 4 (PR B) -- serves the auth + chat-session surface natively from
// go/internal/api instead of relaying it.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"

	"github.com/Hussain0327/amneal/go/internal/api"
	"github.com/Hussain0327/amneal/go/internal/proxy"
	"github.com/Hussain0327/amneal/go/internal/store"
)

func main() {
	logger := log.New(os.Stderr, "proxy: ", log.LstdFlags|log.LUTC)

	cfg, err := proxy.ConfigFromEnv()
	if err != nil {
		logger.Fatal(err)
	}
	native, preRelay, err := nativeRoutes(logger)
	if err != nil {
		logger.Fatal(err)
	}
	logger.Printf("listening on %s, upstream %s, native routes: %d", cfg.Addr, cfg.Upstream, len(native))
	if err := proxy.Serve(cfg.Addr, proxy.NewHandlerWithPreRelay(cfg.Upstream, logger, native, preRelay), logger); err != nil {
		logger.Fatal(err)
	}
}

// nativeRoutes builds the Go-owned route table. DATABASE_URL is a Fly secret
// (app-wide, so proxy machines already have it); the pgx pool is LAZY, so
// boot stays DB-independent -- a proxy machine must never crash-loop on a DB
// blip while holding the public edge (the 2026-06-18/07-07 incident class).
// Missing DATABASE_URL follows the REQUIRE_DATABASE_URL contract the Python
// app already honors: fail loudly when required (prod sets "true" app-wide,
// fly.toml [env]), otherwise WARN and serve relay-only so a local relay-only
// proxy still runs.
func nativeRoutes(logger *log.Logger) (map[string]http.Handler, func(http.ResponseWriter, *http.Request) bool, error) {
	dbURL := os.Getenv("DATABASE_URL")
	if dbURL == "" {
		required, err := api.EnvBool("REQUIRE_DATABASE_URL", false)
		if err != nil {
			return nil, nil, err
		}
		if required {
			return nil, nil, errors.New("REQUIRE_DATABASE_URL is set but DATABASE_URL is empty; refusing to serve auth without a session store")
		}
		logger.Printf("WARNING: DATABASE_URL unset -- native auth/session routes DISABLED, relaying everything upstream")
		return nil, nil, nil
	}
	apiCfg, err := api.ConfigFromEnv()
	if err != nil {
		return nil, nil, err
	}
	if apiCfg.Insecure {
		// Python parity: the app group WARNS and serves on this misconfig;
		// the edge process must be no stricter (a boot refusal here would
		// crash-loop the machine holding the public port).
		logger.Printf("WARNING: insecure_session_cookie_in_production -- SENTRY_ENVIRONMENT=production but AUTH_COOKIE_SECURE is false")
	}
	pool, err := store.NewPool(context.Background(), dbURL)
	if err != nil {
		return nil, nil, err
	}
	server := api.NewServer(pool, apiCfg, logger)
	// The step-5 StreamGate runs only when Go owns the query path: it gates
	// POST /query/stream (401/429) before relaying. With the flag off, both
	// /query and /query/stream relay to Python exactly as today.
	var preRelay func(http.ResponseWriter, *http.Request) bool
	if apiCfg.GONativeQuery {
		preRelay = server.StreamGate
	}
	return server.Routes(), preRelay, nil
}
