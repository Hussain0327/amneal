// Package store is the Go data layer for the step-4 surface (auth, chat
// sessions, feedback, products) of docs/POLYGLOT_TARGET_2026-07-10.md.
// Queries are sqlc-generated from internal/store/queries/*.sql against the
// drift-gated schema snapshot internal/store/schema.sql; this file owns the
// one thing sqlc does not: HOW connections are made.
package store

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// The Python engine's connection discipline, mirrored exactly
// (src/regwatch/store/db.py:_pg_connect_args / _enforce_sslmode). The app
// connects as the postgres role, which has NO server-side statement/lock/idle
// timeouts; on 2026-06-18 an idle-in-transaction connection held its locks
// and wedged prod. Per-connection GUCs make such a stall self-heal, and a Go
// pool WITHOUT them would quietly reintroduce that incident class. Same env
// names, same defaults, same "empty or 0 disables" semantics as
// config/settings.py, so one ops override reaches both runtimes.
//
// Pool sizing and liveness are deliberately NOT set below, so pgx's defaults
// apply: MaxConns max(4, NumCPU), MaxConnLifetime 1h, MaxConnIdleTime 30m,
// HealthCheckPeriod 1m, and a checkout ping only when IdleDuration > 1s.
// Python now uses that same idle-gated ping policy in place of SQLAlchemy's
// pool_pre_ping, but with a 30s gate (DB_POOL_IDLE_PING_S) rather than pgx's
// 1s: Python's is a deliberate latency choice sized against Lakebase's 60s
// minimum scale-to-zero suspend, while pgx's 1s is simply its default. The two
// numbers differ on purpose -- do not "fix" one to match the other.
const (
	defaultStatementTimeout = "30s" // DB_STATEMENT_TIMEOUT
	defaultIdleInTxTimeout  = "60s" // DB_IDLE_IN_TX_TIMEOUT
	defaultLockTimeout      = "10s" // DB_LOCK_TIMEOUT
	defaultConnectTimeout   = "10"  // DB_CONNECT_TIMEOUT, integer seconds
)

// localHosts mirrors db.py:_LOCAL_HOSTS -- hosts that skip the sslmode
// enforcement (CI service containers, local scratch clusters).
var localHosts = map[string]bool{
	"localhost": true,
	"127.0.0.1": true,
	"::1":       true,
	"0.0.0.0":   true,
	"":          true,
}

// envOr returns the env value for name, or def when the variable is UNSET.
// A variable that is set-but-empty stays empty: like the Python settings
// layer, "" (and "0") means "this timeout is deliberately disabled", which
// is different from "not configured".
func envOr(name, def string) string {
	if v, ok := os.LookupEnv(name); ok {
		return strings.TrimSpace(v)
	}
	return def
}

func timeoutDisabled(v string) bool {
	return v == "" || v == "0"
}

// NewPool builds the store's pgxpool with Python-parity connection behavior:
// forced TLS for non-local hosts, per-connection timeout GUCs, and a bounded
// connect handshake. databaseURL is the same postgres:// or postgresql:// URL
// the Python app receives (the +psycopg driver suffix, a SQLAlchemy-ism, is
// not expected here).
func NewPool(ctx context.Context, databaseURL string) (*pgxpool.Pool, error) {
	withSSL, err := enforceSSLMode(databaseURL)
	if err != nil {
		return nil, err
	}

	cfg, err := pgxpool.ParseConfig(withSSL)
	if err != nil {
		return nil, fmt.Errorf("store: parse pool config: %w", err)
	}

	if cfg.ConnConfig.RuntimeParams == nil {
		cfg.ConnConfig.RuntimeParams = map[string]string{}
	}
	for _, guc := range []struct{ name, env, def string }{
		{"statement_timeout", "DB_STATEMENT_TIMEOUT", defaultStatementTimeout},
		{"idle_in_transaction_session_timeout", "DB_IDLE_IN_TX_TIMEOUT", defaultIdleInTxTimeout},
		{"lock_timeout", "DB_LOCK_TIMEOUT", defaultLockTimeout},
	} {
		if v := envOr(guc.env, guc.def); !timeoutDisabled(v) {
			cfg.ConnConfig.RuntimeParams[guc.name] = v
		}
	}

	// Bound the TCP/TLS handshake itself (the GUCs above only exist once a
	// session does). A non-numeric override is operator config rot: fail
	// loudly at pool construction, exactly like Python's int() does.
	if v := envOr("DB_CONNECT_TIMEOUT", defaultConnectTimeout); !timeoutDisabled(v) {
		seconds, err := strconv.Atoi(v)
		if err != nil {
			return nil, fmt.Errorf("store: DB_CONNECT_TIMEOUT must be integer seconds, got %q", v)
		}
		cfg.ConnConfig.ConnectTimeout = time.Duration(seconds) * time.Second
	}

	// pgx's default QueryExecModeCacheStatement (server-side prepared
	// statements) is safe ONLY because prod's DATABASE_URL is the Lakebase
	// DIRECT endpoint, where each conn keeps a dedicated backend. Verified
	// against the live branch 2026-07-28. If that URL is ever repointed at
	// the "-pooler" host (PgBouncer TRANSACTION mode), prepared statements
	// break ("prepared statement does not exist") and this default must
	// change to QueryExecModeExec. Nothing in this file detects that -- the
	// failure surfaces as a total outage of the service holding the public
	// edge, so treat the host suffix as a release-gate check.
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("store: create pool: %w", err)
	}
	return pool, nil
}

// enforceSSLMode appends sslmode=require for non-local Postgres hosts unless
// the operator already chose an sslmode -- db.py:_enforce_sslmode verbatim.
// The Lakebase endpoint is reached over the public internet; an unencrypted
// fallback there is not an acceptable failure mode.
func enforceSSLMode(databaseURL string) (string, error) {
	u, err := url.Parse(databaseURL)
	if err != nil {
		return "", fmt.Errorf("store: parse database url: %w", err)
	}
	if !strings.HasPrefix(u.Scheme, "postgres") {
		return databaseURL, nil
	}
	// SQLAlchemy-form scheme tolerance: Python's settings normalizer ACCEPTS
	// "postgresql+psycopg://" (its own internal driver form), and pgx would
	// silently mis-parse that scheme via its keyword/value fallback -- the
	// pool would construct cleanly and every query would then fail, a silent
	// native-auth outage. Both runtimes must honor the same env contract, so
	// strip any "+driver" suffix here. Untouched URLs return VERBATIM (no
	// parse/re-encode round trip), like Python.
	changed := false
	if base, _, found := strings.Cut(u.Scheme, "+"); found {
		u.Scheme = base
		changed = true
	}
	q := u.Query()
	host := strings.ToLower(strings.Trim(u.Hostname(), "[]"))
	if !q.Has("sslmode") && !localHosts[host] {
		q.Set("sslmode", "require")
		u.RawQuery = q.Encode()
		changed = true
	}
	if !changed {
		return databaseURL, nil
	}
	return u.String(), nil
}
