package api

import "net/http"

// StreamGate is the pre-relay gate for POST /query/stream in the step-5 cutover.
// It enforces currentUser (401, S4) and the per-user query rate limit (429,
// S18) as real PRE-stream HTTP statuses, then hands the request to the relay
// (returns false) which carries the SSE stream to Python -- streaming
// persistence stays in Python (R3). Every other request is an immediate no-op,
// so native routes, /healthz, and the catch-all relay are untouched. Returns
// true only when it has fully written a gate response (401/429).
//
// Rate-limit authority: Go's per-user bucket counts BOTH /query (native) and
// /query/stream, so its count is always >= Python's stream-only bucket and Go
// 429s first -- Python never spuriously rejects a Go-admitted stream. The
// double-count is therefore inert (no trusted-header skip needed).
func (s *Server) StreamGate(w http.ResponseWriter, r *http.Request) bool {
	if r.Method != http.MethodPost || r.URL.Path != "/query/stream" {
		return false
	}
	u, ok := s.currentUser(w, r)
	if !ok {
		return true // 401 already written
	}
	if !s.queryLimiter.Allow(queryRateKey(chatUserID(u)), s.cfg.RateLimitPerMinute) {
		writeDetail(w, http.StatusTooManyRequests, detailRateLimited)
		return true
	}
	return false // authed + under limit -> let the relay carry the stream
}
