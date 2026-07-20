package api

import (
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5/pgtype"
)

// text builds a non-null pgtype.Text (query param helper).
func text(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: true}
}

// Routes returns the native route table the proxy mounts ahead of its relay
// catch-all. Go 1.22 method patterns route the exact method+path pairs below;
// everything else -- other paths AND other METHODS on these paths -- falls
// through to the relay catch-all ("/" matches any method), where Python
// still answers 405 for method mismatches today. The follow-up deletion PR
// must decide those 405s' fate explicitly (add method-mismatch patterns or
// accept upstream 404s); this table must not silently own them.
func (s *Server) Routes() map[string]http.Handler {
	routes := map[string]http.Handler{
		"POST /auth/login":      http.HandlerFunc(s.handleLogin),
		"POST /auth/logout":     http.HandlerFunc(s.handleLogout),
		"GET /auth/me":          http.HandlerFunc(s.handleMe),
		"GET /sessions":         http.HandlerFunc(s.handleListSessions),
		"GET /sessions/{id}":    http.HandlerFunc(s.handleGetSession),
		"DELETE /sessions/{id}": http.HandlerFunc(s.handleDeleteSession),
	}
	out := make(map[string]http.Handler, len(routes)*2)
	seenPaths := map[string]bool{}
	for pattern, h := range routes {
		out[pattern] = s.corsSimple(h)
		// One preflight handler per PATH, mirroring Starlette's CORSMiddleware,
		// which answers OPTIONS itself on every route (the browser never
		// preflights the prod same-origin /api rewrite path, but local direct-
		// API dev and the Python parity contract both expect this to exist).
		path := pattern[strings.Index(pattern, " ")+1:]
		if !seenPaths[path] {
			seenPaths[path] = true
			out["OPTIONS "+path] = http.HandlerFunc(s.handlePreflight)
		}
	}
	return out
}

// allowedOrigin reports whether the request Origin is in the configured
// allowlist (exact match, like Starlette's non-wildcard allow_origins).
func (s *Server) allowedOrigin(origin string) bool {
	if origin == "" {
		return false
	}
	for _, o := range s.cfg.CORSOrigins {
		if o == origin {
			return true
		}
	}
	return false
}

// corsSimple mirrors CORSMiddleware's simple-response behavior for the routes
// Go owns: when the Origin is allowlisted, echo it with credentials allowed
// (that combination is what lets the browser send the HttpOnly session
// cookie cross-origin; the EXPLICIT allowlist is what keeps it safe).
func (s *Server) corsSimple(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if origin := r.Header.Get("Origin"); s.allowedOrigin(origin) {
			h := w.Header()
			h.Set("Access-Control-Allow-Origin", origin)
			h.Set("Access-Control-Allow-Credentials", "true")
			h.Add("Vary", "Origin")
		}
		next.ServeHTTP(w, r)
	})
}

// handlePreflight mirrors Starlette's preflight response for
// allow_methods=["GET","POST","DELETE"], allow_headers=["*"],
// allow_credentials=True: with credentials, "*" cannot be sent literally, so
// the requested headers are echoed back. Disallowed origins get the
// middleware's plain 400.
func (s *Server) handlePreflight(w http.ResponseWriter, r *http.Request) {
	origin := r.Header.Get("Origin")
	reqMethod := r.Header.Get("Access-Control-Request-Method")
	allowedMethod := reqMethod == "GET" || reqMethod == "POST" || reqMethod == "DELETE"
	if !s.allowedOrigin(origin) || !allowedMethod {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte("Disallowed CORS origin, method, or headers"))
		return
	}
	h := w.Header()
	h.Set("Access-Control-Allow-Origin", origin)
	h.Set("Access-Control-Allow-Credentials", "true")
	h.Set("Access-Control-Allow-Methods", "GET, POST, DELETE")
	if reqHeaders := r.Header.Get("Access-Control-Request-Headers"); reqHeaders != "" {
		h.Set("Access-Control-Allow-Headers", reqHeaders)
	}
	h.Set("Access-Control-Max-Age", "600")
	h.Add("Vary", "Origin")
	w.WriteHeader(http.StatusOK)
}
