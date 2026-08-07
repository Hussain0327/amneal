package obs

// DB-free unit tests for the error-reporting seam. The load-bearing property
// is the DISABLED one: dev, CI, and every other test binary run with no
// SENTRY_DSN, so Init/Capture/Flush must be silent no-ops that never panic and
// never fail a boot.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/getsentry/sentry-go"
)

func TestInitFromEnvNoDSNIsSilentNoOp(t *testing.T) {
	t.Setenv("SENTRY_DSN", "")
	t.Setenv("SENTRY_ENVIRONMENT", "development")

	var buf bytes.Buffer
	logger := log.New(&buf, "", 0)
	if InitFromEnv(logger) {
		t.Fatal("InitFromEnv must report OFF with no DSN")
	}
	if Enabled() {
		t.Fatal("Enabled() must stay false with no DSN")
	}
	if buf.Len() != 0 {
		t.Errorf("non-production boot must log nothing, got %q", buf.String())
	}
	// The whole disabled surface, called exactly as the query path calls it.
	Capture(errors.New("boom"), map[string]string{"turn_id": "t"})
	Capture(nil, nil)
	if !Flush(time.Second) {
		t.Error("Flush must succeed trivially when reporting is off")
	}
}

func TestInitFromEnvNoDSNInProductionWarns(t *testing.T) {
	// observability.py's B4 posture: a production process whose errors vanish
	// to stderr is a gap worth naming -- loudly, but never fatally.
	t.Setenv("SENTRY_DSN", "")
	t.Setenv("SENTRY_ENVIRONMENT", "production")

	var buf bytes.Buffer
	if InitFromEnv(log.New(&buf, "", 0)) {
		t.Fatal("InitFromEnv must report OFF with no DSN, production or not")
	}
	if !strings.Contains(buf.String(), "sentry_disabled_in_production") {
		t.Errorf("production boot without a DSN must warn, got %q", buf.String())
	}
}

func TestInitFromEnvBadDSNDegradesInsteadOfFailingBoot(t *testing.T) {
	// A typo in the Fly secret must not crash-loop the machine holding the
	// public port -- it must degrade to "no reporting" and say so.
	t.Setenv("SENTRY_DSN", "not-a-dsn")
	t.Setenv("SENTRY_ENVIRONMENT", "production")

	var buf bytes.Buffer
	if InitFromEnv(log.New(&buf, "", 0)) {
		t.Fatal("a malformed DSN must report OFF")
	}
	if Enabled() {
		t.Fatal("a malformed DSN must leave reporting disabled")
	}
	if !strings.Contains(buf.String(), "sentry_init_failed") {
		t.Errorf("a malformed DSN must warn, got %q", buf.String())
	}
	Capture(errors.New("boom"), nil) // still a no-op, still no panic
}

// recordingTransport captures events instead of sending them, so the tests
// exercise the real client with the real clientOptions (BeforeSend included).
type recordingTransport struct {
	mu     sync.Mutex
	events []*sentry.Event
}

func (rt *recordingTransport) Configure(sentry.ClientOptions) {}
func (rt *recordingTransport) SendEvent(e *sentry.Event) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	rt.events = append(rt.events, e)
}
func (rt *recordingTransport) Flush(time.Duration) bool              { return true }
func (rt *recordingTransport) FlushWithContext(context.Context) bool { return true }
func (rt *recordingTransport) Close()                                {}

func (rt *recordingTransport) captured() []*sentry.Event {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	out := make([]*sentry.Event, len(rt.events))
	copy(out, rt.events)
	return out
}

// initRecording turns reporting ON against a stub transport and restores the
// disabled state (the package default every other test relies on) afterwards.
func initRecording(t *testing.T) *recordingTransport {
	t.Helper()
	rt := &recordingTransport{}
	opts := clientOptions("https://publickey@o0.ingest.example.invalid/1", "test")
	opts.Transport = rt
	if err := sentry.Init(opts); err != nil {
		t.Fatalf("sentry.Init with the production options failed: %v", err)
	}
	enabled.Store(true)
	t.Cleanup(func() {
		enabled.Store(false)
		sentry.CurrentHub().BindClient(nil)
	})
	return rt
}

func TestCaptureAttachesCorrelationTags(t *testing.T) {
	rt := initRecording(t)

	Capture(errors.New("insert query_log failed"), map[string]string{
		"event":      "qa_answer_audit_write_failed",
		"turn_id":    "11111111-1111-4111-8111-111111111111",
		"session_id": "", // omitted, never sent as an empty tag
	})

	events := rt.captured()
	if len(events) != 1 {
		t.Fatalf("expected exactly 1 event, got %d", len(events))
	}
	tags := events[0].Tags
	if tags["turn_id"] != "11111111-1111-4111-8111-111111111111" {
		t.Errorf("turn_id tag = %q, want %q", tags["turn_id"], "11111111-1111-4111-8111-111111111111")
	}
	if tags["event"] != "qa_answer_audit_write_failed" {
		t.Errorf("event tag = %q, want %q", tags["event"], "qa_answer_audit_write_failed")
	}
	if _, ok := tags["session_id"]; ok {
		t.Errorf("empty tag values must be dropped, got %q", tags["session_id"])
	}
}

func TestCaptureScrubsPgxEncodedRowPayload(t *testing.T) {
	rt := initRecording(t)

	// pgx wraps a client-side encode failure with the VALUE inline -- for
	// InsertQueryLog that value is the user's question / the answer text.
	secret := "what is the dissolution method for MY-UNRELEASED-PRODUCT"
	err := fmt.Errorf("failed to encode args[5]: unable to encode %q into text format for text (OID 25)", secret)
	Capture(err, nil)

	events := rt.captured()
	if len(events) != 1 {
		t.Fatalf("expected exactly 1 event, got %d", len(events))
	}
	if len(events[0].Exception) == 0 {
		t.Fatal("event carried no exception -- the scrub assertions below would be vacuous")
	}
	for _, exc := range events[0].Exception {
		if strings.Contains(exc.Value, secret) {
			t.Fatalf("row payload leaked into the event: %q", exc.Value)
		}
		if !strings.Contains(exc.Value, "scrubbed") {
			t.Errorf("scrub marker missing from %q", exc.Value)
		}
	}
}

func TestScrubMessageLeavesServerErrorsIntact(t *testing.T) {
	// pgconn.PgError.Error() is severity + message + SQLSTATE, with no DETAIL
	// line and no parameters -- it must survive verbatim or every DB incident
	// loses its diagnosis.
	cases := map[string]string{
		"ERROR: relation \"query_log\" does not exist (SQLSTATE 42P01)": "ERROR: relation \"query_log\" does not exist (SQLSTATE 42P01)",
		"timeout: context deadline exceeded":                            "timeout: context deadline exceeded",
		"failed to encode args[0]: unable to encode secret into text":   "failed to encode args: scrubbed",
		"conn busy: failed to encode args[2]: unable to encode secret":  "conn busy: failed to encode args: scrubbed",
	}
	for in, want := range cases {
		if got := scrubMessage(in); got != want {
			t.Errorf("scrubMessage(%q) = %q, want %q", in, got, want)
		}
	}
}
