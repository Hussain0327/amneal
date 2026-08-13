package api

// DB-free tests for the query-path observability wiring: every qa_* line
// carries the turn correlation id, and the INV-6 audit failures produce a log
// line at all. The store is a DBTX whose every call fails -- the only way to
// drive these branches deterministically without a database (the PG-backed
// happy paths live in query_pg_test.go / tests_contract).

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/Hussain0327/amneal/go/internal/store"
)

const (
	obsTurnID    = "11111111-1111-4111-8111-111111111111"
	obsSessionID = "22222222-2222-4222-8222-222222222222"
)

// errFakeDB reads like the real thing (a pool that cannot reach Postgres),
// which is the outage shape these branches exist for.
var errFakeDB = errors.New("dial tcp 10.0.0.1:5432: connect: connection refused")

// failingDB is a store.DBTX that fails every call.
type failingDB struct{}

func (failingDB) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, errFakeDB
}
func (failingDB) Query(context.Context, string, ...any) (pgx.Rows, error) { return nil, errFakeDB }
func (failingDB) QueryRow(context.Context, string, ...any) pgx.Row        { return failingRow{} }

type failingRow struct{}

func (failingRow) Scan(...any) error { return errFakeDB }

// newObsServer is the minimum Server the persistence path needs: a dead store,
// a captured logger, a frozen clock.
func newObsServer() (*Server, *bytes.Buffer) {
	buf := &bytes.Buffer{}
	s := &Server{
		q:      store.New(failingDB{}),
		errLog: log.New(buf, "", 0),
		now:    func() time.Time { return time.Unix(1700000000, 0).UTC() },
	}
	return s, buf
}

// obsPayload is a minimal compute payload carrying the correlation ids the log
// lines must reproduce. allowSkip=false is the STRICT answer path whose audit
// failure withholds the answer (INV-6).
func obsPayload(allowSkip bool) *computePayload {
	tid, sid, uid, status := obsTurnID, obsSessionID, "7", "answer"
	return &computePayload{
		Response: json.RawMessage(`{"answer":"a","refused":false}`),
		Persist: persistSpec{
			AuditLogKwargs: auditKwargs{
				Mode:       "qa",
				QueryText:  "q?",
				AnswerText: "a",
				ModelName:  "echo",
				SessionID:  &sid,
				TurnID:     &tid,
				UserID:     &uid,
				Status:     &status,
			},
			AllowSkip: allowSkip,
			Patch: sessionPatch{
				SessionID: sid,
				TurnID:    tid,
				Content:   "a",
				Status:    "answer",
				ModelName: "echo",
			},
		},
	}
}

func TestQueryPathLogLinesCarryTurnID(t *testing.T) {
	t0 := time.Unix(1700000000, 0).UTC()

	cases := []struct {
		name string
		run  func(t *testing.T, s *Server)
		want []string
	}{
		{
			// T1: the session upsert fails for a reason other than the
			// ownership guard, so the turn degrades instead of aborting.
			name: "t1_session_setup_failed",
			run: func(t *testing.T, s *Server) {
				sid, err := s.persistUserTurn(context.Background(), obsSessionID, obsTurnID, "7", "q?", originThread, []byte("{}"), t0)
				if err != nil {
					t.Fatalf("a non-ownership T1 failure must degrade, not error: %v", err)
				}
				if sid != obsTurnID {
					t.Fatalf("session must degrade to the turn id, got %q", sid)
				}
			},
			want: []string{"qa_session_setup_failed", "turn_id=" + obsTurnID, "session_id=" + obsSessionID},
		},
		{
			// Skip-tolerant audit failure: audit_id degrades to the -1
			// sentinel (a MISSING query_log row -- the INV-6 surface the
			// runbook greps for), and T3 still runs and fails too.
			name: "skip_tolerant_audit_failed",
			run: func(t *testing.T, s *Server) {
				wire, auditID, err := s.persistTurn(context.Background(), obsPayload(true), t0)
				if err != nil {
					t.Fatalf("the skip-tolerant path must never error: %v", err)
				}
				if auditID != -1 {
					t.Fatalf("audit id = %d, want the -1 sentinel", auditID)
				}
				if !strings.Contains(string(wire), `"audit_id":-1`) {
					t.Fatalf("wire body lost the sentinel: %s", wire)
				}
			},
			want: []string{
				"qa_audit_write_failed", "turn_id=" + obsTurnID, "session_id=" + obsSessionID,
				"qa_assistant_record_failed",
			},
		},
		{
			// Strict answer path with no fallback: the answer is WITHHELD
			// (INV-6). Both the write failure and the withholding get their
			// own line -- they are different incidents to an operator.
			name: "strict_answer_unaudited",
			run: func(t *testing.T, s *Server) {
				wire, auditID, err := s.persistTurn(context.Background(), obsPayload(false), t0)
				if !errors.Is(err, errAnswerUnaudited) {
					t.Fatalf("err = %v, want errAnswerUnaudited", err)
				}
				if wire != nil || auditID != 0 {
					t.Fatalf("a withheld answer must return nothing, got wire=%s id=%d", wire, auditID)
				}
			},
			want: []string{
				"qa_answer_audit_write_failed", "qa_answer_unaudited",
				"turn_id=" + obsTurnID, "session_id=" + obsSessionID,
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, buf := newObsServer()
			tc.run(t, s)
			out := buf.String()
			if out == "" {
				t.Fatal("this failure branch emitted no log line at all")
			}
			for _, want := range tc.want {
				if !strings.Contains(out, want) {
					t.Errorf("log is missing %q\ngot:\n%s", want, out)
				}
			}
			// Catch-all: a qa_* line with no correlation id is exactly the
			// pre-change state this workstream removed, and a newly added bare
			// Printf would slip past the per-case substring checks above.
			for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
				if strings.HasPrefix(line, "qa_") && !strings.Contains(line, "turn_id=") {
					t.Errorf("query-path log line without a turn id: %q", line)
				}
			}
		})
	}
}

func TestQALogOmitsIdsThatAreNotInScope(t *testing.T) {
	// The pre-turn branches have no ids yet: they must be OMITTED, not
	// emitted empty, so "grep turn_id=" only ever matches a real id.
	s, buf := newObsServer()
	s.qaLog("qa_test_event", "", "", errFakeDB)
	line := buf.String()
	if strings.Contains(line, "turn_id=") || strings.Contains(line, "session_id=") {
		t.Errorf("empty ids must be omitted, got %q", line)
	}
	if !strings.HasPrefix(line, "qa_test_event: ") || !strings.Contains(line, "connection refused") {
		t.Errorf("event name and cause must survive, got %q", line)
	}

	buf.Reset()
	s.qaLog("qa_test_event", obsTurnID, "", nil)
	if got := strings.TrimSpace(buf.String()); got != "qa_test_event turn_id="+obsTurnID {
		t.Errorf("line = %q", got)
	}
}

func TestCorrelationIDsFallBackToThePatch(t *testing.T) {
	// The audit kwargs carry the ids for every payload the core produces; the
	// patch is the fallback so a payload that omits them still logs a
	// correlatable line instead of a bare error.
	p := obsPayload(true)
	if turnID, sessionID := correlationIDs(p.Persist); turnID != obsTurnID || sessionID != obsSessionID {
		t.Errorf("kwargs ids = %q/%q", turnID, sessionID)
	}
	p.Persist.AuditLogKwargs.TurnID = nil
	p.Persist.AuditLogKwargs.SessionID = nil
	if turnID, sessionID := correlationIDs(p.Persist); turnID != obsTurnID || sessionID != obsSessionID {
		t.Errorf("patch fallback ids = %q/%q", turnID, sessionID)
	}
	if turnID, sessionID := correlationIDs(persistSpec{}); turnID != "" || sessionID != "" {
		t.Errorf("empty spec must yield empty ids, got %q/%q", turnID, sessionID)
	}
}
