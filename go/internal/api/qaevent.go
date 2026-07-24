package api

import (
	"strings"

	"github.com/Hussain0327/amneal/go/internal/obs"
)

// Query-path event logging. Every /query log line goes through here so it
// carries the turn correlation id in one greppable shape:
//
//	qa_<event> turn_id=<uuid4> session_id=<uuid4>: <error>
//
// The qa_* NAMES are load-bearing and unchanged: the flip runbook's monitoring
// window (docs/GO_NATIVE_QUERY_ROLLOUT.md) greps `fly logs` for them literally,
// so renaming one would silently break the documented procedure. Only the
// key=value context is new -- before this, every line was a bare
// "qa_x: <err>" with no way to tie it to a turn, a query_log row, or the
// turn_id the client was handed on the wire.

// qaLog writes one query-path line. Ids that are not in scope yet (the
// pre-turn branches) are omitted rather than emitted empty, so
// "grep turn_id=" only ever matches a real id.
func (s *Server) qaLog(event, turnID, sessionID string, err error) {
	var b strings.Builder
	b.WriteString(event)
	if turnID != "" {
		b.WriteString(" turn_id=")
		b.WriteString(turnID)
	}
	if sessionID != "" {
		b.WriteString(" session_id=")
		b.WriteString(sessionID)
	}
	if err != nil {
		b.WriteString(": ")
		b.WriteString(err.Error())
	}
	s.errLog.Print(b.String())
}

// qaCapture logs the line AND reports it as a Sentry event. Reserved for the
// INV-6 audit surfaces (no-audit-no-answer), where the runbook says a single
// confirmed miss is a rollback trigger -- and where "grep fly logs" was the
// only detector until now.
//
// Best-effort chat writes deliberately stay log-only: that mirrors the Python
// side's LoggingIntegration(event_level=None) posture, where logged errors are
// breadcrumbs and only explicit capture points become events.
func (s *Server) qaCapture(event, turnID, sessionID string, err error) {
	s.qaLog(event, turnID, sessionID, err)
	obs.Capture(err, map[string]string{
		"event":      event,
		"turn_id":    turnID,
		"session_id": sessionID,
	})
}

// derefStr is the nil-safe read of an optional id (auditKwargs carries
// *string session/turn ids).
func derefStr(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}
