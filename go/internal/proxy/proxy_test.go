package proxy_test

import (
	"context"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Hussain0327/amneal/go/internal/proxy"
)

// waitFor bounds every blocking read so a buffering regression fails the test
// instead of hanging the suite.
const waitFor = 5 * time.Second

// testClient bounds whole exchanges for the same reason: with FlushInterval
// broken even response HEADERS stay buffered, so an unbounded client would
// hang inside Get() until the suite deadline. Every test path completes in
// milliseconds when the proxy is correct.
var testClient = &http.Client{Timeout: waitFor}

func newProxyServer(t *testing.T, upstream string) *httptest.Server {
	t.Helper()
	u, err := url.Parse(upstream)
	if err != nil {
		t.Fatalf("parse upstream URL %q: %v", upstream, err)
	}
	srv := httptest.NewServer(proxy.NewHandler(u, log.New(io.Discard, "", 0)))
	t.Cleanup(srv.Close)
	return srv
}

// deadUpstreamURL returns a URL nothing listens on: bind an ephemeral port,
// note it, release it.
func deadUpstreamURL(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve dead port: %v", err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return "http://" + addr
}

// readFullWithin reads exactly n bytes or fails the test after waitFor.
func readFullWithin(t *testing.T, r io.Reader, n int, what string) []byte {
	t.Helper()
	type result struct {
		buf []byte
		err error
	}
	ch := make(chan result, 1)
	go func() {
		buf := make([]byte, n)
		_, err := io.ReadFull(r, buf)
		ch <- result{buf, err}
	}()
	select {
	case res := <-ch:
		if res.err != nil {
			t.Fatalf("reading %s: %v", what, res.err)
		}
		return res.buf
	case <-time.After(waitFor):
		t.Fatalf("%s not observable within %s (response buffered?)", what, waitFor)
	}
	return nil // unreachable; Fatalf does not return
}

type upstreamSeen struct {
	method string
	uri    string
	host   string
	body   string
	header http.Header
}

func TestProxiesVerbatim(t *testing.T) {
	// Channel (not shared struct fields) so the handler-goroutine writes are
	// race-detector-clean when read by the test goroutine.
	seen := make(chan upstreamSeen, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		seen <- upstreamSeen{
			method: r.Method,
			uri:    r.URL.RequestURI(),
			host:   r.Host,
			body:   string(body),
			header: r.Header.Clone(),
		}
		w.Header().Set("X-Upstream-Header", "upstream-value")
		http.SetCookie(w, &http.Cookie{Name: "session", Value: "abc123", Path: "/", HttpOnly: true})
		w.WriteHeader(http.StatusTeapot)
		_, _ = io.WriteString(w, "teapot-body")
	}))
	t.Cleanup(upstream.Close)

	p := newProxyServer(t, upstream.URL)
	req, err := http.NewRequest(http.MethodPost, p.URL+"/api/echo?q=1&form=Tablet+ER", strings.NewReader(`{"question":"?"}`))
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Custom-Header", "custom-value")
	req.Header.Set("Cookie", "sid=inbound-cookie")
	resp, err := testClient.Do(req)
	if err != nil {
		t.Fatalf("request through proxy: %v", err)
	}
	respBody, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		t.Fatalf("read response body: %v", err)
	}

	if resp.StatusCode != http.StatusTeapot {
		t.Errorf("status = %d, want %d", resp.StatusCode, http.StatusTeapot)
	}
	if got := resp.Header.Get("X-Upstream-Header"); got != "upstream-value" {
		t.Errorf("X-Upstream-Header = %q, want %q", got, "upstream-value")
	}
	cookies := resp.Cookies()
	if len(cookies) != 1 || cookies[0].Name != "session" || cookies[0].Value != "abc123" {
		t.Errorf("Set-Cookie = %v, want session=abc123", cookies)
	}
	if string(respBody) != "teapot-body" {
		t.Errorf("body = %q, want %q", respBody, "teapot-body")
	}

	got := <-seen
	if got.method != http.MethodPost {
		t.Errorf("upstream method = %q, want POST", got.method)
	}
	if got.uri != "/api/echo?q=1&form=Tablet+ER" {
		t.Errorf("upstream URI = %q, want path+query verbatim", got.uri)
	}
	// The app must keep seeing the PUBLIC host, not the upstream address the
	// proxy dialed (SetURL alone would rewrite it).
	if wantHost := strings.TrimPrefix(p.URL, "http://"); got.host != wantHost {
		t.Errorf("upstream Host = %q, want %q", got.host, wantHost)
	}
	if got.body != `{"question":"?"}` {
		t.Errorf("upstream body = %q", got.body)
	}
	if v := got.header.Get("X-Custom-Header"); v != "custom-value" {
		t.Errorf("X-Custom-Header = %q, want %q", v, "custom-value")
	}
	if v := got.header.Get("Cookie"); v != "sid=inbound-cookie" {
		t.Errorf("Cookie = %q, want %q", v, "sid=inbound-cookie")
	}
	if v := got.header.Get("Content-Type"); v != "application/json" {
		t.Errorf("Content-Type = %q, want %q", v, "application/json")
	}
}

func TestClientIPHeadersReachUpstreamUntouched(t *testing.T) {
	// Contract with src/regwatch/api/main.py::_client_ip under
	// TRUST_PROXY_HEADERS=true: the backend keys its login-spray limiter on
	// Fly-Client-IP verbatim, falling back to the RIGHTMOST X-Forwarded-For
	// hop ("appended by our trusted edge"). If the proxy appended its own hop
	// (Director mode or ProxyRequest.SetXForwarded would), the rightmost XFF
	// entry would become the proxy's peer address and every caller would
	// collapse into one rate-limit bucket. So both headers must pass through
	// byte-for-byte.
	seen := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(upstream.Close)

	p := newProxyServer(t, upstream.URL)
	req, err := http.NewRequest(http.MethodGet, p.URL+"/auth/login", nil)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	req.Header.Set("Fly-Client-IP", "203.0.113.9")
	req.Header.Set("X-Forwarded-For", "198.51.100.7, 172.16.11.22")
	resp, err := testClient.Do(req)
	if err != nil {
		t.Fatalf("request through proxy: %v", err)
	}
	_ = resp.Body.Close()

	h := <-seen
	if got := h.Get("Fly-Client-IP"); got != "203.0.113.9" {
		t.Errorf("Fly-Client-IP = %q, want %q", got, "203.0.113.9")
	}
	// Full-string compare catches a comma-appended hop; Values() length
	// catches a second header line.
	if got := h.Values("X-Forwarded-For"); len(got) != 1 || got[0] != "198.51.100.7, 172.16.11.22" {
		t.Errorf("X-Forwarded-For = %q, want exactly [%q]", got, "198.51.100.7, 172.16.11.22")
	}
}

func TestSSEEventsFlushIncrementally(t *testing.T) {
	// Guards the /query/stream user path end-to-end: the first event must be
	// observable while the upstream handler is still blocked mid-stream.
	// NOTE: ReverseProxy auto-flushes text/event-stream responses even with
	// FlushInterval unset, so this test alone cannot catch a dropped
	// FlushInterval -- TestFlushIntervalFlushesLengthDeclaredResponses does.
	first := "event: token\ndata: hello\n\n"
	second := "data: [DONE]\n\n"
	release := make(chan struct{})
	var once sync.Once
	releaseNow := func() { once.Do(func() { close(release) }) }

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, first)
		w.(http.Flusher).Flush()
		<-release // hold the stream open until the test saw the first event
		_, _ = io.WriteString(w, second)
	}))
	t.Cleanup(upstream.Close)

	p := newProxyServer(t, upstream.URL)
	// Registered AFTER the servers so it runs FIRST (cleanups are LIFO): both
	// Close calls block until the handler returns, and the handler is parked
	// on <-release whenever an assertion fails mid-stream.
	t.Cleanup(releaseNow)
	resp, err := testClient.Get(p.URL + "/query/stream")
	if err != nil {
		t.Fatalf("request through proxy: %v", err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("Content-Type = %q, want text/event-stream", ct)
	}

	got := readFullWithin(t, resp.Body, len(first), "first SSE event")
	if string(got) != first {
		t.Fatalf("first SSE event = %q, want %q", got, first)
	}
	releaseNow()
	rest, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read remainder: %v", err)
	}
	if string(rest) != second {
		t.Fatalf("remainder = %q, want %q", rest, second)
	}
}

func TestFlushIntervalFlushesLengthDeclaredResponses(t *testing.T) {
	// The regression guard for FlushInterval = -1. ReverseProxy auto-flushes
	// text/event-stream and unknown-length (chunked) responses no matter
	// what, so ONLY a Content-Length-declared response reveals whether
	// FlushInterval is actually set: without it the first chunk sits in the
	// proxy server's write buffer until the upstream handler completes.
	// Verified by mutation: deleting FlushInterval from NewHandler makes this
	// test fail with "first fixed-length chunk not observable".
	part1 := "first-chunk-"
	part2 := "second-chunk\n"
	release := make(chan struct{})
	var once sync.Once
	releaseNow := func() { once.Do(func() { close(release) }) }

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Content-Length", strconv.Itoa(len(part1)+len(part2)))
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, part1)
		w.(http.Flusher).Flush()
		<-release // hold the response open until the test saw part1
		_, _ = io.WriteString(w, part2)
	}))
	t.Cleanup(upstream.Close)

	p := newProxyServer(t, upstream.URL)
	// Registered AFTER the servers so it runs FIRST (cleanups are LIFO): both
	// Close calls block until the handler returns, and the handler is parked
	// on <-release exactly when this test fails (buffered first chunk).
	t.Cleanup(releaseNow)
	resp, err := testClient.Get(p.URL + "/download")
	if err != nil {
		// Reached when headers never flush: the buffered response trips the
		// client deadline inside Get itself.
		t.Fatalf("GET through proxy (FlushInterval regression? headers must flush immediately): %v", err)
	}
	defer resp.Body.Close()
	// Sanity: the length-declared path must actually be exercised, or the
	// auto-flush fallback would make this test pass vacuously.
	if want := int64(len(part1) + len(part2)); resp.ContentLength != want {
		t.Fatalf("ContentLength = %d, want %d", resp.ContentLength, want)
	}

	got := readFullWithin(t, resp.Body, len(part1), "first fixed-length chunk")
	if string(got) != part1 {
		t.Fatalf("first chunk = %q, want %q", got, part1)
	}
	releaseNow()
	rest, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read remainder: %v", err)
	}
	if string(rest) != part2 {
		t.Fatalf("remainder = %q, want %q", rest, part2)
	}
}

func TestHealthzIndependentOfUpstream(t *testing.T) {
	p := newProxyServer(t, deadUpstreamURL(t))
	resp, err := testClient.Get(p.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200 with upstream down", resp.StatusCode)
	}
	if string(body) != "ok\n" {
		t.Errorf("body = %q, want %q", body, "ok\n")
	}
}

func TestDeadUpstreamReturns502(t *testing.T) {
	p := newProxyServer(t, deadUpstreamURL(t))
	resp, err := testClient.Get(p.URL + "/query")
	if err != nil {
		t.Fatalf("GET through proxy: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status = %d, want 502", resp.StatusCode)
	}
	if string(body) != "upstream unavailable\n" {
		t.Errorf("body = %q, want %q", body, "upstream unavailable\n")
	}
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/plain") {
		t.Errorf("Content-Type = %q, want text/plain", ct)
	}
}

// syncLogBuffer captures proxy log output race-safely: the ErrorHandler
// writes from the proxy server's handler goroutine while the test polls.
type syncLogBuffer struct {
	mu sync.Mutex
	b  strings.Builder
}

func (s *syncLogBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncLogBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func TestClientDisconnectIsNotAnUpstreamError(t *testing.T) {
	// A client hanging up mid-request (closed tab on an SSE stream) must be
	// logged as a disconnect, not as "upstream error" -- otherwise routine
	// churn buries real upstream failures in the logs.
	arrived := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(arrived)
		<-r.Context().Done() // hold until the proxy cancels the outbound request
	}))
	t.Cleanup(upstream.Close)

	logBuf := &syncLogBuffer{}
	u, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatalf("parse upstream URL: %v", err)
	}
	p := httptest.NewServer(proxy.NewHandler(u, log.New(logBuf, "", 0)))
	t.Cleanup(p.Close)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.URL+"/query/stream", nil)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	done := make(chan error, 1)
	go func() {
		resp, doErr := testClient.Do(req)
		if doErr == nil {
			_ = resp.Body.Close()
		}
		done <- doErr
	}()

	select {
	case <-arrived:
	case <-time.After(waitFor):
		t.Fatal("request never reached upstream")
	}
	cancel() // the client hangs up while the upstream is still working

	select {
	case doErr := <-done:
		if doErr == nil {
			t.Fatal("expected the canceled request to fail client-side")
		}
	case <-time.After(waitFor):
		t.Fatal("canceled request did not return")
	}

	// The ErrorHandler runs on the proxy's handler goroutine after the client
	// is already gone: wait for its line instead of asserting on a snapshot.
	deadline := time.Now().Add(waitFor)
	for {
		logs := logBuf.String()
		if strings.Contains(logs, "client disconnected") {
			if strings.Contains(logs, "upstream error") {
				t.Fatalf("disconnect logged as upstream error:\n%s", logs)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("no client-disconnect log line within %s; logs:\n%s", waitFor, logs)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestConfigFromEnv(t *testing.T) {
	t.Setenv("UPSTREAM_URL", "")
	t.Setenv("PORT", "")
	cfg, err := proxy.ConfigFromEnv()
	if err != nil {
		t.Fatalf("defaults: %v", err)
	}
	if got := cfg.Upstream.String(); got != "http://127.0.0.1:8000" {
		t.Errorf("default upstream = %q", got)
	}
	if cfg.Addr != ":8080" {
		t.Errorf("default addr = %q", cfg.Addr)
	}

	t.Setenv("UPSTREAM_URL", "http://app.process.amneal.internal:8000")
	t.Setenv("PORT", "3000")
	cfg, err = proxy.ConfigFromEnv()
	if err != nil {
		t.Fatalf("overrides: %v", err)
	}
	if cfg.Upstream.Host != "app.process.amneal.internal:8000" {
		t.Errorf("upstream host = %q", cfg.Upstream.Host)
	}
	if cfg.Addr != ":3000" {
		t.Errorf("addr = %q", cfg.Addr)
	}

	// "127.0.0.1:8000" is the likeliest operator typo (missing scheme); it
	// must fail at boot, not dial garbage per-request.
	for _, bad := range []string{"http://", "ftp://somewhere", "127.0.0.1:8000"} {
		t.Setenv("UPSTREAM_URL", bad)
		if _, err := proxy.ConfigFromEnv(); err == nil {
			t.Errorf("ConfigFromEnv(%q) unexpectedly succeeded", bad)
		}
	}
}
