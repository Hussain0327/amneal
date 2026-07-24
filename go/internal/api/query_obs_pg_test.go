package api

// PG-backed test for the two /query branches that were completely silent
// before this change: the 429 rate-limit rejection and the 503 saturation
// shed. Neither writes a query_log row (nothing ran), neither emits a metric,
// and neither used to log -- so both were invisible in every surface the team
// has. Same opt-in Postgres discipline as contract_test.go: TEST_DATABASE_URL
// unset => skip.

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

// syncBuf is a race-free log sink: the handler writes on the server goroutine
// while the test reads after the response comes back.
type syncBuf struct {
	mu sync.Mutex
	b  []byte
}

func (s *syncBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.b = append(s.b, p...)
	return len(p), nil
}

func (s *syncBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return string(s.b)
}

// The turn ids are minted inside the handler, so the assertion is on the
// SHAPE: the event name followed by both correlation ids.
var shedLineRe = regexp.MustCompile(`qa_shed_saturated turn_id=[0-9a-f-]{36} session_id=[0-9a-f-]{36}`)

func TestPreviouslySilentQueryBranchesAreLogged(t *testing.T) {
	// The compute stub always sheds, so request 1 takes the 503 branch; with
	// the per-user limit at 1, request 2 takes the 429 branch.
	rag := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, saturatedDetailJSON)
	}))
	t.Cleanup(rag.Close)

	h := newHarness(t, Config{
		SessionTTL:         72 * time.Hour,
		GONativeQuery:      true,
		RAGTimeout:         5 * time.Second,
		RateLimitPerMinute: 1,
	})
	h.srv.rag = newRAGClient(rag.URL, "test-token", 5*time.Second)
	buf := &syncBuf{}
	h.srv.errLog = log.New(buf, "", 0)

	uid := h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)

	resp := h.do(t, "POST", "/query", token, map[string]any{"question": "What study design is recommended?"})
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("shed status = %d, want 503", resp.StatusCode)
	}
	if !shedLineRe.MatchString(buf.String()) {
		t.Errorf("a shed turn must leave a correlatable line; got:\n%s", buf.String())
	}

	resp2 := h.do(t, "POST", "/query", token, map[string]any{"question": "And a second question?"})
	if resp2.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("second request status = %d, want 429", resp2.StatusCode)
	}
	want := fmt.Sprintf("qa_rate_limited user_id=%d", uid)
	if !strings.Contains(buf.String(), want) {
		t.Errorf("a 429 must leave a line containing %q; got:\n%s", want, buf.String())
	}
}
