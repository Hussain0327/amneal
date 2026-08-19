package api

// Route-table pins for the step-5 GO_NATIVE_QUERY flag. The load-bearing
// negative: POST /query/stream must NEVER gain a native route -- streaming
// persistence stays in Python (R3), and Go participates only through
// StreamGate (auth + rate limit) before the relay carries the SSE stream.
// Resolution is asserted through real ServeMux matching (the same mounting
// proxy.NewHandlerWithPreRelay performs), not string comparison on the route
// table, so an exact, subtree, or wildcard pattern claiming the path all fail
// here identically.

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// muxPattern resolves method+target through a mux built the way the proxy
// mounts Routes(): every native pattern plus the "/" relay catch-all. The
// returned pattern names the route that would serve the request; "/" means
// "falls through to the relay".
func muxPattern(t *testing.T, native bool, method, target string) string {
	t.Helper()
	srv := NewServer(nil, Config{GoNativeQuery: native, SessionTTL: time.Hour}, nil)
	mux := http.NewServeMux()
	for pattern, h := range srv.Routes() {
		mux.Handle(pattern, h)
	}
	mux.Handle("/", http.NotFoundHandler())
	_, pattern := mux.Handler(httptest.NewRequest(method, target, nil))
	return pattern
}

func TestStreamRouteStaysOnTheRelay(t *testing.T) {
	for _, native := range []bool{true, false} {
		if got := muxPattern(t, native, http.MethodPost, "/query/stream"); got != "/" {
			t.Errorf("GoNativeQuery=%v: POST /query/stream resolves to native pattern %q, want the relay catch-all", native, got)
		}
	}
}

// TestNativeQueryRouteIsFlagGated is the positive control for the pin above:
// the same resolution shows the flag doing its job on POST /query, so a
// regression that emptied the route table entirely could not fake a pass.
func TestNativeQueryRouteIsFlagGated(t *testing.T) {
	if got := muxPattern(t, true, http.MethodPost, "/query"); got != "POST /query" {
		t.Errorf("GoNativeQuery=true: POST /query resolves to %q, want the native route", got)
	}
	if got := muxPattern(t, false, http.MethodPost, "/query"); got != "/" {
		t.Errorf("GoNativeQuery=false: POST /query resolves to %q, want the relay catch-all", got)
	}
}
