// Package proxy is strangler Step 3 of docs/POLYGLOT_TARGET_2026-07-10.md: a
// transparent reverse proxy that will front the Python API as a second Fly
// process group. This slice is deliberately inert -- nothing in fly.toml,
// the Dockerfile, or deploy.yml runs it yet (see docs/GO_PROXY_ROLLOUT.md).
package proxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const (
	defaultUpstream = "http://127.0.0.1:8000"
	defaultPort     = "8080"

	// Fly sends SIGTERM and escalates to SIGKILL after kill_timeout (default
	// 5s). The fly.toml wiring slice must raise kill_timeout above this drain
	// window or in-flight SSE streams die at the hard kill, not here.
	shutdownGrace = 20 * time.Second
)

// Config is resolved once at boot so a bad UPSTREAM_URL fails startup loudly
// instead of surfacing as a 502 on every request.
type Config struct {
	Upstream *url.URL
	Addr     string
}

// ConfigFromEnv reads UPSTREAM_URL (default http://127.0.0.1:8000) and PORT
// (default 8080).
func ConfigFromEnv() (Config, error) {
	raw := os.Getenv("UPSTREAM_URL")
	if raw == "" {
		raw = defaultUpstream
	}
	u, err := url.Parse(raw)
	if err != nil {
		return Config{}, fmt.Errorf("invalid UPSTREAM_URL %q: %w", raw, err)
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return Config{}, fmt.Errorf("UPSTREAM_URL %q must be http(s)://host[:port]", raw)
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = defaultPort
	}
	return Config{Upstream: u, Addr: ":" + port}, nil
}

// NewHandler returns the proxy handler: /healthz is answered locally (Fly
// liveness for THIS process must not depend on the upstream being up; the
// Python app keeps its own /health), everything else forwards verbatim.
// A nil errLog falls back to log.Default(), which writes to stderr.
func NewHandler(upstream *url.URL, errLog *log.Logger) http.Handler {
	if errLog == nil {
		errLog = log.Default()
	}

	transport := &http.Transport{
		// Nil Proxy on purpose (DefaultTransport would use
		// ProxyFromEnvironment): this is an internal hop to our own upstream,
		// and an inherited HTTP_PROXY/HTTPS_PROXY must never reroute it
		// through an egress proxy.
		Proxy: nil,
		// Connection setup is bounded; response reads deliberately are not.
		// The upstream serves long-lived SSE (/query/stream) where minutes of
		// silence between tokens is legal, so any read/response-header timeout
		// would sever live streams. A hung upstream is still bounded by the
		// client: when the inbound request context ends (disconnect), the
		// ReverseProxy cancels the outbound request.
		DialContext: (&net.Dialer{
			Timeout:   5 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		TLSHandshakeTimeout:   5 * time.Second,
		ForceAttemptHTTP2:     true,
		MaxIdleConnsPerHost:   32,
		IdleConnTimeout:       90 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}

	rp := &httputil.ReverseProxy{
		// Rewrite (not Director) is load-bearing: Director mode auto-appends
		// the peer address to X-Forwarded-For, which would corrupt the
		// contract below. Rewrite mode instead STRIPS Forwarded/X-Forwarded-*
		// before this func runs (stdlib anti-spoofing default), so the trusted
		// values must be restored explicitly.
		//
		// Header contract with src/regwatch/api/main.py::_client_ip under
		// TRUST_PROXY_HEADERS=true (prod): the backend keys its login-spray
		// limiter on Fly-Client-IP (Fly-edge-attested, never stripped) and
		// falls back to the RIGHTMOST X-Forwarded-For hop. Both headers must
		// reach uvicorn byte-for-byte: an appended hop would make the
		// rightmost XFF entry Fly's internal proxy address and collapse every
		// caller into one rate-limit bucket; a stripped XFF would break the
		// fallback entirely.
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(upstream)
			// SetURL rewrites the Host header to the upstream's host; keep the
			// public Host the client sent so the app sees the same value it
			// does today when Fly's edge hits it directly.
			pr.Out.Host = pr.In.Host
			// Fly's edge upstream of us is the trust boundary here, and the
			// app NEEDS its attested forwarding values: restore them exactly
			// as they arrived (Del first stays idempotent if the stdlib strip
			// list ever changes).
			for _, h := range []string{"Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"} {
				pr.Out.Header.Del(h)
				for _, v := range pr.In.Header.Values(h) {
					pr.Out.Header.Add(h, v)
				}
			}
		},
		Transport: transport,
		// -1 flushes every write downstream immediately. ReverseProxy does
		// auto-flush text/event-stream and unknown-length responses, but SSE
		// delivery must be guaranteed by explicit config, not content-type
		// sniffing -- and length-declared responses would otherwise sit in the
		// server's write buffer until complete.
		FlushInterval: -1,
		ErrorLog:      errLog,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// Runs only before any downstream bytes are written (dial /
			// RoundTrip failure, i.e. the client vanished before TTFB).
			// Once headers or body have started streaming, a write error
			// takes the stdlib's copyResponse panic(http.ErrAbortHandler)
			// path instead: net/http's server suppresses that silently and
			// this handler never runs, so it does not cover mid-stream SSE
			// aborts -- those were already silent pre-flush and stay that
			// way, just not logged here.
			if r.Context().Err() != nil {
				// The inbound client hung up (closed tab, aborted fetch)
				// before any response was sent, and the canceled outbound
				// request surfaced here. Checking the inbound context
				// rather than errors.Is(err, context.Canceled) stays
				// robust to transport error wrapping. Label it truthfully
				// -- pre-TTFB disconnects are routine churn, and fake
				// "upstream error" lines would bury real ones during an
				// incident -- and skip the 502: its connection is gone.
				errLog.Printf("client disconnected: %s %s: %v", r.Method, r.URL.Path, err)
				return
			}
			// Body stays generic: err content (internal addresses) never
			// reaches the client.
			errLog.Printf("upstream error: %s %s: %v", r.Method, r.URL.Path, err)
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.WriteHeader(http.StatusBadGateway)
			_, _ = io.WriteString(w, "upstream unavailable\n")
		},
	}

	mux := http.NewServeMux()
	// Exact-path pattern: /healthz/anything still proxies through.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.Handle("/", rp)
	return mux
}

// Run serves cfg.Addr until SIGTERM/SIGINT, then drains in-flight requests
// for up to shutdownGrace before force-closing what remains. A nil errLog
// falls back to log.Default() (stderr).
func Run(cfg Config, errLog *log.Logger) error {
	if errLog == nil {
		errLog = log.Default()
	}
	srv := &http.Server{
		Addr:    cfg.Addr,
		Handler: NewHandler(cfg.Upstream, errLog),
		// Slowloris guard on request headers only. ReadTimeout/WriteTimeout
		// stay 0 on purpose: either one severs long uploads or SSE streams.
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       75 * time.Second,
		ErrorLog:          errLog,
	}

	serveErr := make(chan error, 1)
	go func() {
		err := srv.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		serveErr <- err
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, os.Interrupt)
	defer signal.Stop(stop)

	select {
	case err := <-serveErr:
		// Listener failed on its own (bad PORT, address in use).
		return err
	case sig := <-stop:
		errLog.Printf("received %s: draining for up to %s", sig, shutdownGrace)
		ctx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			// Drain deadline hit with streams still open: close them hard so
			// we exit on our own terms before Fly escalates to SIGKILL.
			_ = srv.Close()
			return fmt.Errorf("drain deadline exceeded: %w", err)
		}
		return nil
	}
}
