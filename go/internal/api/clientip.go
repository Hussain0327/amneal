package api

import (
	"net"
	"net/http"
	"strings"
)

// clientIP ports src/regwatch/api/main.py::_client_ip -- the IP the per-IP
// login limiter keys on. Any client-supplied forwarding header is spoofable
// (the LEFTMOST X-Forwarded-For hop is whatever the browser sent), so:
//   - trustProxy OFF (direct exposure): key on the TCP-level peer address.
//   - trustProxy ON (behind Fly's edge): prefer Fly-Client-IP, which Fly's
//     edge sets to the platform-attested real client; only if absent fall
//     back to the RIGHTMOST XFF hop (appended by our trusted edge), never
//     split(",")[0].
//
// Falls back to "unknown" only when no source is available, so the limiter
// never breaks the login path. Note the proxy's own relay preserves these
// headers byte-for-byte for Python (proxy.go Rewrite contract); this native
// path reads the SAME attested values, so both runtimes bucket identically.
func clientIP(r *http.Request, trustProxy bool) string {
	if trustProxy {
		if v := strings.TrimSpace(r.Header.Get("Fly-Client-IP")); v != "" {
			return v
		}
		if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
			hops := strings.Split(fwd, ",")
			for i := len(hops) - 1; i >= 0; i-- {
				if hop := strings.TrimSpace(hops[i]); hop != "" {
					return hop
				}
			}
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || host == "" {
		if r.RemoteAddr != "" {
			return r.RemoteAddr
		}
		return "unknown"
	}
	return host
}
