package api

// PG-backed tests for the step-5 CompleteQuery parity fixes (PR C):
//
//   1. T1 session-ownership lost-create race -> the unaudited 404 Python's
//      ensure_session/SessionOwnershipError path produces (main.py, the
//      except-SessionOwnershipError branch of /query), with ZERO writes.
//   2. Strict answer path, audit write failed, Fallback nil -> the answer is
//      WITHHELD as a 500 (Python _persist_turn's re-raise), never returned
//      with audit_id=-1.
//   3. Compute-endpoint 503 (ask() pool shed) -> relayed as the byte-identical
//      FastAPI busy body, no audit row, no T3.
//
// Same opt-in Postgres discipline as contract_test.go: TEST_DATABASE_URL unset
// => skip. The RAG core is a canned httptest stub (the real cross-runtime
// behavior is pinned end-to-end by tests_contract; these force the branches a
// live core cannot reach deterministically).

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func (h *harness) chatMessageRoles(t *testing.T) []string {
	t.Helper()
	rows, err := h.pool.Query(t.Context(),
		`SELECT role FROM public.chat_message ORDER BY created_at`)
	if err != nil {
		t.Fatalf("read chat_message roles: %v", err)
	}
	defer rows.Close()
	var roles []string
	for rows.Next() {
		var role string
		if err := rows.Scan(&role); err != nil {
			t.Fatalf("scan role: %v", err)
		}
		roles = append(roles, role)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate roles: %v", err)
	}
	return roles
}

func (h *harness) queryLogCount(t *testing.T) int {
	t.Helper()
	var n int
	if err := h.pool.QueryRow(t.Context(), `SELECT count(*) FROM public.query_log`).Scan(&n); err != nil {
		t.Fatalf("count query_log: %v", err)
	}
	return n
}

func (h *harness) sessionOwner(t *testing.T, sid string) any {
	t.Helper()
	var owner any
	if err := h.pool.QueryRow(t.Context(),
		`SELECT user_id FROM public.chat_session WHERE id = $1`, sid).Scan(&owner); err != nil {
		t.Fatalf("read session owner: %v", err)
	}
	return owner
}

// --- fix 1: persistUserTurn's write-time ownership contract ---

func TestPersistUserTurnForeignSessionAbortsWithZeroWrites(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	t0 := time.Now().UTC().Truncate(time.Microsecond)

	h.seedChat(t, "raced", "999", nil, 0)
	sid, err := h.srv.persistUserTurn(t.Context(), "raced", "turn-a", "1", "q?", []byte("{}"), t0)
	if err != errSessionOwnershipLost {
		t.Fatalf("err = %v, want errSessionOwnershipLost", err)
	}
	if sid != "" {
		t.Fatalf("sid = %q, want empty on ownership loss", sid)
	}
	if roles := h.chatMessageRoles(t); len(roles) != 0 {
		t.Fatalf("ownership loss must write nothing; got messages %v", roles)
	}
	if owner := h.sessionOwner(t, "raced"); owner != "999" {
		t.Fatalf("foreign owner clobbered: %v", owner)
	}
}

func TestPersistUserTurnAdoptsNullOwner(t *testing.T) {
	// ensure_session parity: a NULL-owner (legacy) row at write time is ADOPTED
	// (row.user_id = user_id), never treated as foreign.
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	t0 := time.Now().UTC().Truncate(time.Microsecond)

	h.seedChat(t, "legacy", nil, nil, 0)
	sid, err := h.srv.persistUserTurn(t.Context(), "legacy", "turn-b", "1", "q?", []byte("{}"), t0)
	if err != nil || sid != "legacy" {
		t.Fatalf("adopt: sid=%q err=%v", sid, err)
	}
	if owner := h.sessionOwner(t, "legacy"); owner != "1" {
		t.Fatalf("NULL owner not adopted: %v", owner)
	}
	if roles := h.chatMessageRoles(t); len(roles) != 1 || roles[0] != "user" {
		t.Fatalf("user message missing after adopt: %v", roles)
	}
}

func TestPersistUserTurnFreshAndSelfOwnedSucceed(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	t0 := time.Now().UTC().Truncate(time.Microsecond)

	// Fresh create binds the session to the caller.
	sid, err := h.srv.persistUserTurn(t.Context(), "fresh", "turn-c", "1", "q?", []byte("{}"), t0)
	if err != nil || sid != "fresh" {
		t.Fatalf("fresh create: sid=%q err=%v", sid, err)
	}
	if owner := h.sessionOwner(t, "fresh"); owner != "1" {
		t.Fatalf("fresh session owner: %v", owner)
	}
	// A second turn on the caller's OWN session still succeeds.
	sid, err = h.srv.persistUserTurn(t.Context(), "fresh", "turn-d", "1", "again?", []byte("{}"), t0.Add(time.Second))
	if err != nil || sid != "fresh" {
		t.Fatalf("self-owned upsert: sid=%q err=%v", sid, err)
	}
	if roles := h.chatMessageRoles(t); len(roles) != 2 {
		t.Fatalf("want 2 user messages, got %v", roles)
	}
}

// --- shared stub RAG plumbing for the handler-level tests ---

// nativeQueryHarness is a full native-/query stack whose RAG core is the given
// stub handler: harness with GO_NATIVE_QUERY on, rag pointed at the stub.
func nativeQueryHarness(t *testing.T, stub http.HandlerFunc) *harness {
	t.Helper()
	rag := httptest.NewServer(stub)
	t.Cleanup(rag.Close)
	// RAGTimeout is load-bearing: the handler wraps the compute call in
	// context.WithTimeout(cfg.RAGTimeout), and zero means already-expired.
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour, GONativeQuery: true, RAGTimeout: 5 * time.Second})
	h.srv.rag = newRAGClient(rag.URL, "test-token", 5*time.Second)
	return h
}

// strictComputeBody is a canned compute payload for the STRICT answer path
// (allow_skip=false) with fallback null -- the shape Python never emits today
// (it always serializes a fallback for strict payloads), which is exactly the
// defensive branch under test.
const strictComputeBody = `{
  "response": {
    "answer": "ECHO: a paid validated answer [PSG_1, p.3]",
    "citations": [{"short_name": "PSG_1", "page": 3}],
    "refused": false,
    "model_name": "echo",
    "session_id": "sid-strict",
    "turn_id": "turn-strict",
    "status": "answer",
    "reason": null,
    "interpretation": null,
    "clarify": [],
    "related": []
  },
  "persist": {
    "audit_log_kwargs": {
      "mode": "qa",
      "query_text": "q?",
      "retrieved": [],
      "answer_text": "ECHO: a paid validated answer [PSG_1, p.3]",
      "citations": [],
      "refused": false,
      "model_name": "echo",
      "session_id": "sid-strict",
      "turn_id": "turn-strict",
      "user_id": "1",
      "status": "answer",
      "route_json": {},
      "input_tokens": 0,
      "output_tokens": 0,
      "cost_usd": 0
    },
    "allow_skip": false,
    "patch": {
      "session_id": "sid-strict",
      "turn_id": "turn-strict",
      "content": "ECHO: a paid validated answer [PSG_1, p.3]",
      "status": "answer",
      "model_name": "echo",
      "reason": null,
      "interpretation": null,
      "filters": {},
      "citations": [],
      "clarify": [],
      "related": [],
      "metadata": {},
      "update_filters": false
    },
    "fallback": null
  }
}`

// --- fix 1: the handler maps the lost race to the unaudited 404 ---

func TestCompleteQueryOwnershipRaceMapsToUnaudited404(t *testing.T) {
	ragCalled := false
	h := nativeQueryHarness(t, func(w http.ResponseWriter, r *http.Request) {
		ragCalled = true
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, strictComputeBody)
	})
	h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)

	// Reproduce the race through the s.now seam: within one handleCompleteQuery
	// request, s.now call #1 is currentUser's expiry check and call #2 is the
	// t0 stamp taken AFTER authorizeSession (which saw no row and proceeded)
	// and BEFORE persistUserTurn -- landing the foreign row there is exactly
	// the lost-create window the pre-check cannot see.
	calls := 0
	h.srv.now = func() time.Time {
		calls++
		if calls == 2 {
			if _, err := h.pool.Exec(t.Context(),
				`INSERT INTO public.chat_session (id, user_id, created_at, updated_at)
				 VALUES ('raced-session', '999', now(), now())`); err != nil {
				t.Errorf("inject foreign session: %v", err)
			}
		}
		return time.Now().UTC()
	}

	resp := h.do(t, "POST", "/query", token, map[string]any{
		"question": "What study design is recommended?", "session_id": "raced-session",
	})
	if resp.StatusCode != 404 {
		t.Fatalf("status %d, want the unaudited 404", resp.StatusCode)
	}
	if body := decode(t, resp); body["detail"] != "session not found" {
		t.Fatalf("404 body: %v", body)
	}
	if ragCalled {
		t.Fatal("the RAG call must never run after an ownership loss")
	}
	if n := h.queryLogCount(t); n != 0 {
		t.Fatalf("unaudited by contract; %d query_log rows", n)
	}
	if roles := h.chatMessageRoles(t); len(roles) != 0 {
		t.Fatalf("no chat writes on ownership loss; got %v", roles)
	}
	if owner := h.sessionOwner(t, "raced-session"); owner != "999" {
		t.Fatalf("winner's ownership clobbered: %v", owner)
	}
}

// --- fix 2: nil-fallback strict audit failure withholds the answer ---

func TestCompleteQueryNilFallbackAuditOutageWithholdsAnswer(t *testing.T) {
	h := nativeQueryHarness(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, strictComputeBody)
	})
	h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)

	// Break the audit store (same shape as tests_contract's audit_boom_trigger;
	// no drop needed -- bootstrap rebuilds schema public per test).
	for _, stmt := range []string{
		`CREATE FUNCTION test_audit_boom() RETURNS trigger AS $$
		 BEGIN RAISE EXCEPTION 'simulated audit outage'; END $$ LANGUAGE plpgsql`,
		`CREATE TRIGGER test_audit_boom_trg BEFORE INSERT ON public.query_log
		 FOR EACH ROW EXECUTE FUNCTION test_audit_boom()`,
	} {
		if _, err := h.pool.Exec(t.Context(), stmt); err != nil {
			t.Fatalf("break query_log: %v", err)
		}
	}

	resp := h.do(t, "POST", "/query", token, map[string]any{"question": "What study design is recommended?"})
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	body := string(raw)
	if resp.StatusCode != 500 {
		t.Fatalf("status %d, want 500 (no-audit-no-answer); body %s", resp.StatusCode, body)
	}
	// The paid answer and its citation markers must be WITHHELD -- the old
	// behavior (return the answer with audit_id=-1) fails here.
	if strings.Contains(body, "ECHO") || strings.Contains(body, "PSG_1") || strings.Contains(body, "audit_id") {
		t.Fatalf("unaudited answer content leaked: %s", body)
	}
	var detail map[string]any
	if err := json.Unmarshal(raw, &detail); err != nil || detail["detail"] != "Internal Server Error" {
		t.Fatalf("500 body: %s (err %v)", body, err)
	}
	if n := h.queryLogCount(t); n != 0 {
		t.Fatalf("audit store is down; %d query_log rows", n)
	}
	// T1 committed before the outage mattered; T3 must have been SKIPPED.
	if roles := h.chatMessageRoles(t); len(roles) != 1 || roles[0] != "user" {
		t.Fatalf("want only the T1 user row, got %v", roles)
	}
}

// --- fix 3: compute-endpoint 503 relays the byte-identical busy contract ---

func TestCompleteQuerySaturatedComputeRelays503(t *testing.T) {
	h := nativeQueryHarness(t, func(w http.ResponseWriter, r *http.Request) {
		// FastAPI's exact rendering of the _shed_if_ask_pool_saturated 503.
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"detail":"server is busy, retry shortly"}`)
	})
	h.seedUser(t, testEmail, testPassword, true)
	token := h.login(t, testEmail, testPassword)

	resp := h.do(t, "POST", "/query", token, map[string]any{"question": "What study design is recommended?"})
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	if resp.StatusCode != 503 {
		t.Fatalf("status %d, want 503; body %s", resp.StatusCode, raw)
	}
	// Byte-identical to FastAPI's flag-off body (no json.Encoder newline).
	if string(raw) != saturatedDetailJSON {
		t.Fatalf("body %q, want %q", raw, saturatedDetailJSON)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type %q", ct)
	}
	if n := h.queryLogCount(t); n != 0 {
		t.Fatalf("a shed turn never ran; %d query_log rows", n)
	}
	// Pinned ACCEPTED divergence from Python's zero-write shed: T1 committed
	// before the shed was known; no assistant row, no audit row.
	if roles := h.chatMessageRoles(t); len(roles) != 1 || roles[0] != "user" {
		t.Fatalf("want exactly the orphaned T1 user row, got %v", roles)
	}
}

// --- non-shed compute failures: every OTHER non-200 status must land on the
// AUDITED synthesized-upstream_error path, never the shed relay (the
// errUpstream->errSaturated mutant survived the whole suite before these) ---

// assertSingleErrorAudit pins INV-6 for a synthesized upstream_error turn:
// exactly ONE refused error query_log row plus the T1/T3 chat pair.
func assertSingleErrorAudit(t *testing.T, h *harness) {
	t.Helper()
	if n := h.queryLogCount(t); n != 1 {
		t.Fatalf("INV-6: want exactly one query_log row, got %d", n)
	}
	var refused bool
	var status string
	var latencyMs *int32
	if err := h.pool.QueryRow(t.Context(),
		`SELECT refused, status, latency_ms FROM public.query_log`,
	).Scan(&refused, &status, &latencyMs); err != nil {
		t.Fatalf("read query_log row: %v", err)
	}
	if !refused || status != "error" {
		t.Fatalf("audit row refused=%v status=%q, want a refused error row", refused, status)
	}
	// The skip-tolerant audit path must stamp the turn clock too. A NULL here
	// means the p95 gates the provider cutover depends on would silently see
	// only the strict-answer path, i.e. exactly the failing turns are missing.
	if latencyMs == nil {
		t.Fatal("latency_ms is NULL on a synthesized error turn; the turn clock was not threaded")
	}
	if roles := h.chatMessageRoles(t); len(roles) != 2 || roles[0] != "user" || roles[1] != "assistant" {
		t.Fatalf("want the T1 user + T3 assistant pair, got %v", roles)
	}
}

func TestCompleteQueryComputeErrorStatusIsAuditedUpstreamError(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   string
	}{
		// A compute-route crash leaking through as a plain 500.
		{"compute_500", http.StatusInternalServerError, `{"detail":"Internal Server Error"}`},
		// The token-mismatch guard (main.py _require_internal_token): the
		// documented prod failure signature for a missing/mismatched
		// INTERNAL_RAG_TOKEN, which must degrade to audited turns, not 503s.
		{"compute_404_token_mismatch", http.StatusNotFound, `{"detail":"Not Found"}`},
		// A 503 whose body is NOT the byte-fixed busy contract (the app-wide
		// provider-outage handler, main.py _handle_upstream_error): the body
		// discriminator must keep it off the unaudited shed relay.
		{"non_shed_503", http.StatusServiceUnavailable,
			`{"detail":"the answer service is temporarily unavailable; please try again"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := nativeQueryHarness(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			})
			h.seedUser(t, testEmail, testPassword, true)
			token := h.login(t, testEmail, testPassword)

			resp := h.do(t, "POST", "/query", token, map[string]any{"question": "What study design is recommended?"})
			if resp.StatusCode != 200 {
				t.Fatalf("status %d, want the audited 200 upstream_error turn", resp.StatusCode)
			}
			body := decode(t, resp)
			if body["reason"] != "upstream_error" || body["refused"] != true || body["status"] != "error" {
				t.Fatalf("want a synthesized upstream_error refusal, got %v", body)
			}
			if body["answer"] != serviceUnavailableText {
				t.Fatalf("answer %q, want the fixed-copy unavailable text", body["answer"])
			}
			assertSingleErrorAudit(t, h)
		})
	}
}
