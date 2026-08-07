// Package obs is the Go edge's error-reporting seam: Sentry wiring for the
// binary that has served POST /query natively since the 2026-07-24
// GO_NATIVE_QUERY flip. Before this, the Go runtime had no error tracker at
// all, so persist.go's INV-6 audit-write failures -- which the Python path it
// replaced reported via capture_exception (generate/grounded_qa.py) -- were
// stderr Printfs only.
//
// Posture is a deliberate mirror of src/regwatch/common/observability.py so
// one ops variable configures both runtimes:
//
//   - OFF unless SENTRY_DSN is set; zero behavior change otherwise, and NEVER
//     a boot failure (this process holds the public port).
//   - No PII, no request bodies: the question text stays in query_log, which
//     is the system of record (INV-6).
//   - BeforeSend scrubbing as defense in depth against a driver error message
//     that embeds the row payload (see scrubEvent).
package obs

import (
	"log"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/getsentry/sentry-go"
)

// enabled makes the disabled state EXPLICIT. A hub with no bound client
// already no-ops, but neither boot nor the request path may depend on that
// staying true across SDK upgrades. Atomic because Init runs on the main
// goroutine at boot while Capture runs on request goroutines.
var enabled atomic.Bool

// InitFromEnv wires Sentry from SENTRY_DSN + SENTRY_ENVIRONMENT -- the same
// two variables the Python app reads, both already present on the prod
// machines (DSN a Fly secret, environment in fly.toml [env]). It reports
// whether error reporting is ON.
//
// A missing DSN is a silent no-op in dev/CI/tests and a LOUD warning in
// production: observability.py's B4 posture, where a production process whose
// errors vanish to stderr is a real gap worth naming, but an unconfigured
// error tracker must never take the public edge down.
func InitFromEnv(errLog *log.Logger) bool {
	if errLog == nil {
		errLog = log.Default()
	}
	dsn := strings.TrimSpace(os.Getenv("SENTRY_DSN"))
	env := strings.TrimSpace(os.Getenv("SENTRY_ENVIRONMENT"))
	if dsn == "" {
		if env == "production" {
			errLog.Printf("WARNING: sentry_disabled_in_production -- SENTRY_ENVIRONMENT=production but no SENTRY_DSN; query-path errors are not being reported")
		}
		return false
	}
	if err := sentry.Init(clientOptions(dsn, env)); err != nil {
		// A malformed DSN degrades to "no reporting". It must never crash-loop
		// the machine holding the public port (the 2026-06-18/07-07 incident
		// class), which is exactly what a fatal here would do.
		errLog.Printf("WARNING: sentry_init_failed: %v -- continuing without error reporting", err)
		return false
	}
	enabled.Store(true)
	return true
}

// clientOptions is the one place the SDK posture is defined, so the tests
// exercise the SAME options production runs (with only the transport swapped).
func clientOptions(dsn, env string) sentry.ClientOptions {
	return sentry.ClientOptions{
		Dsn:         dsn,
		Environment: env,
		// The capture points are explicit and few (persist.go's INV-6
		// surfaces); the frame list is what tells them apart in the Sentry UI
		// without parsing the message.
		AttachStacktrace: true,
		// No PII. This binary installs no HTTP integration, so an event
		// carries the error string plus the caller's tags -- never a request,
		// body, header, or cookie.
		SendDefaultPII: false,
		// Nothing here opens spans; a nonzero trace rate would only add cost.
		EnableTracing: false,
		BeforeSend:    scrubEvent,
	}
}

// Enabled reports whether Init bound a working client.
func Enabled() bool { return enabled.Load() }

// Capture reports err with tags attached. Tags are set on a CLONED hub so
// concurrent request goroutines never write each other's correlation ids onto
// a shared scope (the global hub's scope stack is not goroutine-safe). A
// no-op -- not a panic -- when reporting is off or err is nil.
func Capture(err error, tags map[string]string) {
	if err == nil || !enabled.Load() {
		return
	}
	hub := sentry.CurrentHub().Clone()
	scope := hub.Scope()
	for k, v := range tags {
		if v == "" {
			continue // an empty value is noise, not a correlation key
		}
		scope.SetTag(k, v)
	}
	hub.CaptureException(err)
}

// Flush ships buffered events, blocking for at most timeout, and reports
// whether the buffer drained. The default transport is asynchronous, so
// without this the errors raised during a bad deploy -- the ones worth having
// -- are precisely the ones lost at process exit. A no-op returning true when
// reporting is off.
func Flush(timeout time.Duration) bool {
	if !enabled.Load() {
		return true
	}
	return sentry.Flush(timeout)
}

// pgxEncodeMarker starts pgx's argument-encoding echo: "failed to encode
// args[3]: unable to encode <VALUE> into ..." (pgx v5
// extended_query_builder.go wrapping pgtype). That VALUE is the row being
// written -- for InsertQueryLog, query_text (the user's question) and
// answer_text.
const pgxEncodeMarker = "failed to encode args"

// scrubEvent is observability.py::_scrub_event's twin: defense in depth
// against a driver error message that embeds the row payload. Only the
// client-side encode path needs cutting -- server-side failures arrive as
// pgconn.PgError, whose Error() renders severity + message + SQLSTATE and
// never the DETAIL line or the parameter values.
func scrubEvent(event *sentry.Event, _ *sentry.EventHint) *sentry.Event {
	if event == nil {
		return nil
	}
	for i := range event.Exception {
		event.Exception[i].Value = scrubMessage(event.Exception[i].Value)
	}
	event.Message = scrubMessage(event.Message)
	return event
}

func scrubMessage(s string) string {
	if before, _, found := strings.Cut(s, pgxEncodeMarker); found {
		return before + pgxEncodeMarker + ": scrubbed"
	}
	return s
}
