package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/Hussain0327/amneal/go/internal/store"
)

// serviceUnavailableText is the fixed-copy answer for a Go-SYNTHESIZED
// upstream_error turn (the Python RAG core unreachable/timed out). Byte-
// identical to grounded_qa._SERVICE_UNAVAILABLE_TEXT and the harness constant.
// The em-dash is written as backslash-u-2014 (U+2014) so this SOURCE stays ASCII while the
// emitted bytes match Python's literal em-dash exactly.
const serviceUnavailableText = "The answer service is temporarily unavailable. " +
	"Your question was not answered \u2014 please try again in a moment."

// sessionFilterKeys mirrors conversation.SESSION_FILTER_KEYS: the only filter
// keys carried across a session. A caller-supplied version_id (which would
// switch off current-version scoping) is not here, so it can never be injected.
var sessionFilterKeys = map[string]bool{
	"normalized_name": true,
	"dosage_form":     true,
	"route":           true,
	"psg_type":        true,
	"doc_id":          true,
}

// handleCompleteQuery is the native POST /query (step-5 cutover): gates + T1,
// the internal RAG compute call, then the audit-first T2/T3 writes. Behavior-
// identical to the relayed Python /query; every decline/error is a 200 with
// refused=true, never a 4xx/5xx (except the pre-work auth/validation/ownership
// statuses, which mirror the Python route exactly).
func (s *Server) handleCompleteQuery(w http.ResponseWriter, r *http.Request) {
	u, ok := s.currentUser(w, r) // 401 (S3), unaudited
	if !ok {
		return
	}
	userID := chatUserID(u)

	// Decode + validate, reproducing pydantic QueryRequest.
	var body struct {
		Question  *string                    `json:"question"`
		Filters   map[string]json.RawMessage `json:"filters"`
		K         *int                       `json:"k"`
		SessionID *string                    `json:"session_id"`
	}
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&body); err != nil {
		writeValidationError(w, validationItem{Type: "json_invalid", Loc: []string{"body"}, Msg: "Input should be a valid JSON"})
		return
	}
	if dec.More() {
		writeValidationError(w, validationItem{Type: "json_invalid", Loc: []string{"body"}, Msg: "Input should be a valid JSON"})
		return
	}
	var problems []validationItem
	if body.Question == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "question"}, Msg: "Field required"})
	} else if n := utf8.RuneCountInString(*body.Question); n < 2 {
		problems = append(problems, validationItem{Type: "string_too_short", Loc: []string{"body", "question"}, Msg: "String should have at least 2 characters"})
	} else if n > 4000 {
		problems = append(problems, validationItem{Type: "string_too_long", Loc: []string{"body", "question"}, Msg: "String should have at most 4000 characters"})
	}
	if body.K != nil {
		if *body.K < 1 {
			problems = append(problems, validationItem{Type: "greater_than_equal", Loc: []string{"body", "k"}, Msg: "Input should be greater than or equal to 1"})
		} else if *body.K > 50 {
			problems = append(problems, validationItem{Type: "less_than_equal", Loc: []string{"body", "k"}, Msg: "Input should be less than or equal to 50"})
		}
	}
	if len(problems) > 0 {
		writeValidationError(w, problems...)
		return
	}

	// Rate limit (429): Go the single authority across /query + /query/stream.
	if !s.queryLimiter.Allow("user:"+userID, s.cfg.RateLimitPerMinute) {
		// Logged because this rejection is otherwise INVISIBLE: no query_log
		// row (nothing ran), no metric, and no log line -- a 429 storm left no
		// trace in any surface the team has. No turn exists yet (both ids are
		// minted below), so the user id is the only correlation key there is.
		s.errLog.Printf("qa_rate_limited user_id=%s", userID)
		writeDetail(w, http.StatusTooManyRequests, "rate limit exceeded")
		return
	}

	filters := whitelistFilters(body.Filters)
	filtersReq := filtersRequestJSON(filters) // null when absent, for the RAG request
	filtersObj := filtersObjectJSON(filters)  // {} when absent, for stored payloads

	// Ownership (S5) + session id.
	sessionID := ""
	if body.SessionID != nil {
		sessionID = *body.SessionID
	}
	if sessionID != "" {
		if !s.authorizeSession(r.Context(), w, sessionID, userID) {
			return // 404 written, nothing persisted
		}
	} else {
		id, err := newUUID4()
		if err != nil {
			s.internalError(w, "mint session id", err)
			return
		}
		sessionID = id
	}
	turnID, err := newUUID4()
	if err != nil {
		s.internalError(w, "mint turn id", err)
		return
	}

	// Detach from the request context: a client disconnect mid-turn must not
	// abort an in-flight turn that still needs auditing (INV-6).
	baseCtx := context.WithoutCancel(r.Context())

	// T1: user message, pre-RAG, best-effort (may degrade session_id) -- except
	// a lost create race on the session id, which aborts unaudited BEFORE the
	// RAG call with the exact 404 authorizeSession produces (the pre-check
	// cannot see a row created after it ran; ensure_session parity).
	t0 := s.now().Truncate(time.Microsecond)
	sessionID, t1err := s.persistUserTurn(baseCtx, sessionID, turnID, userID, *body.Question, filtersObj, t0)
	if errors.Is(t1err, errSessionOwnershipLost) {
		writeDetail(w, http.StatusNotFound, "session not found")
		return
	}

	// RAG compute over a FINITE deadline. Dead/slow upstream -> synthesized
	// upstream_error turn (still audited).
	ragCtx, cancel := context.WithTimeout(baseCtx, s.cfg.RAGTimeout)
	uid := userID
	payload, ragErr := s.rag.compute(ragCtx, computeRequest{
		Question:  *body.Question,
		Filters:   filtersReq,
		K:         body.K,
		SessionID: sessionID,
		TurnID:    turnID,
		UserID:    &uid,
	})
	cancel()
	if errors.Is(ragErr, errSaturated) {
		// The Python ask() pool shed load (503): relay the SAME defined
		// overload contract flag-on clients would see flag-off, with no audit
		// row and no T3 -- the turn never ran, so there is nothing to audit
		// (Python's shed is pre-dispatch for the same reason). ACCEPTED
		// divergence: T1 above already committed the user message, where
		// Python's shed writes nothing (its check precedes every write);
		// pinned in contract S27.
		//
		// Logged for the same reason as the 429 above: a shed turn writes no
		// audit row by design, so without this line load-shedding is invisible
		// -- and the ids ARE in scope here, which ties the shed 503 to the T1
		// user message it already committed.
		s.qaLog("qa_shed_saturated", turnID, sessionID, ragErr)
		writeSaturated(w)
		return
	}
	if ragErr != nil {
		s.qaLog("qa_upstream_error", turnID, sessionID, ragErr)
		payload = s.synthesizeUpstreamError(*body.Question, sessionID, turnID, userID, filtersObj)
	}

	// T2 (authoritative audit) + T3 (best-effort assistant) on a fresh,
	// bounded context independent of the RAG deadline.
	persistCtx, pcancel := context.WithTimeout(baseCtx, 30*time.Second)
	defer pcancel()
	wire, _, persistErr := s.persistTurn(persistCtx, payload, t0)
	if persistErr != nil {
		// errAnswerUnaudited: withhold the validated answer (INV-6). Python's
		// counterpart re-raise surfaces as Starlette's default text/plain
		// "Internal Server Error" 500; status matches exactly, the body keeps
		// this server's established JSON {"detail":...} shape for the same
		// words (the documented internalError divergence).
		writeDetail(w, http.StatusInternalServerError, "Internal Server Error")
		return
	}

	writeJSON(w, http.StatusOK, wire)
}

// saturatedDetailJSON is FastAPI's exact rendering of
// HTTPException(503, "server is busy, retry shortly") -- json.dumps with
// (",", ":") separators and no trailing newline -- so flag-on and flag-off
// clients see byte-identical overload bodies (writeDetail would append the
// json.Encoder newline).
const saturatedDetailJSON = `{"detail":"server is busy, retry shortly"}`

func writeSaturated(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusServiceUnavailable)
	_, _ = w.Write([]byte(saturatedDetailJSON))
}

// authorizeSession ports main.py::_authorize_session_access: missing -> proceed
// (created bound to caller in T1); NULL owner -> adopt via a conditional UPDATE
// (loser re-reads the committed owner); foreign -> 404 (never 403; existence is
// not confirmed). Returns false and has written the 404/500 when it denies.
func (s *Server) authorizeSession(ctx context.Context, w http.ResponseWriter, sessionID, userID string) bool {
	row, err := s.q.GetChatSessionByID(ctx, sessionID)
	if errors.Is(err, pgx.ErrNoRows) {
		return true
	}
	if err != nil {
		s.internalError(w, "authorize session", err)
		return false
	}
	if !row.UserID.Valid {
		if _, err := s.q.AdoptNullOwnerSession(ctx, store.AdoptNullOwnerSessionParams{
			ID: sessionID, UserID: text(userID),
		}); err != nil {
			s.internalError(w, "adopt session", err)
			return false
		}
		re, err := s.q.GetChatSessionByID(ctx, sessionID)
		if err != nil {
			s.internalError(w, "re-read adopted session", err)
			return false
		}
		row = re
	}
	if !row.UserID.Valid || row.UserID.String != userID {
		writeDetail(w, http.StatusNotFound, "session not found")
		return false
	}
	return true
}

// synthesizeUpstreamError builds the whole turn when the Python core is
// unreachable: a fixed-copy status="error"/reason="upstream_error" refusal,
// skip-audited, with empty retrieval and NULL tokens.
func (s *Server) synthesizeUpstreamError(question, sessionID, turnID, userID string, filtersObj []byte) *computePayload {
	routeJSON := errorRouteJSON("upstream_error", filtersObj)
	status := "error"
	reason := "upstream_error"
	uid, sid, tid := userID, sessionID, turnID

	response := mustJSON(map[string]any{
		"answer":         serviceUnavailableText,
		"citations":      []any{},
		"refused":        true,
		"model_name":     "",
		"session_id":     sessionID,
		"turn_id":        turnID,
		"status":         "error",
		"reason":         "upstream_error",
		"interpretation": nil,
		"clarify":        []any{},
		"related":        []any{},
	})
	return &computePayload{
		Response: response,
		Persist: persistSpec{
			AuditLogKwargs: auditKwargs{
				Mode:       "qa",
				QueryText:  question,
				Retrieved:  json.RawMessage("[]"),
				AnswerText: serviceUnavailableText,
				Citations:  json.RawMessage("[]"),
				Refused:    true,
				ModelName:  "",
				SessionID:  &sid,
				TurnID:     &tid,
				UserID:     &uid,
				Status:     &status,
				RouteJson:  routeJSON,
			},
			AllowSkip: true,
			Patch: sessionPatch{
				SessionID: sessionID,
				TurnID:    turnID,
				Content:   serviceUnavailableText,
				Status:    "error",
				ModelName: "",
				Reason:    &reason,
				Filters:   filtersObj,
				Citations: json.RawMessage("[]"),
				Clarify:   json.RawMessage("[]"),
				Related:   json.RawMessage("[]"),
				Metadata:  mustJSON(map[string]any{"retrieved": []any{}, "route": json.RawMessage(routeJSON)}),
			},
		},
	}
}

func errorRouteJSON(reason string, filtersObj []byte) json.RawMessage {
	return mustJSON(map[string]any{
		"route":           "psg_scoped_rag",
		"filters":         json.RawMessage(filtersObj),
		"reason":          reason,
		"context_applied": false,
		"response_mode":   "refused",
		// The Python service was never reached, so stage-1 provably did not
		// run: the empty ledger is the accurate value, and it keeps this row
		// the same SHAPE as every Python-authored route_json.
		"retrieval": map[string]any{},
	})
}

// whitelistFilters keeps only SESSION_FILTER_KEYS whose value is a JSON scalar,
// preserving the raw bytes per key so an int stays an int (byte-exact storage).
// nil input (absent or JSON null) stays nil, matching the pydantic validator.
func whitelistFilters(raw map[string]json.RawMessage) map[string]json.RawMessage {
	if raw == nil {
		return nil
	}
	kept := make(map[string]json.RawMessage)
	for k, v := range raw {
		if sessionFilterKeys[k] && isScalarJSON(v) {
			kept[k] = v
		}
	}
	return kept
}

// isScalarJSON reports whether v is a JSON string, number, or bool -- the
// Python `isinstance(val, str|int|float|bool)` gate. Objects, arrays, and null
// are dropped.
func isScalarJSON(v json.RawMessage) bool {
	t := bytes.TrimSpace(v)
	if len(t) == 0 {
		return false
	}
	switch t[0] {
	case '{', '[', 'n': // object, array, null
		return false
	default: // "..." string, number, true/false
		return true
	}
}

// filtersRequestJSON is the filters value for the RAG request: null when absent
// (mirrors InternalComputeRequest.filters=None), else the JSON object.
func filtersRequestJSON(kept map[string]json.RawMessage) json.RawMessage {
	if kept == nil {
		return json.RawMessage("null")
	}
	b, err := json.Marshal(kept)
	if err != nil {
		return json.RawMessage("null")
	}
	return b
}

// filtersObjectJSON is the filters value for STORED payloads: always an object
// ({} when absent), matching conversation._safe_filters.
func filtersObjectJSON(kept map[string]json.RawMessage) []byte {
	if kept == nil {
		return []byte("{}")
	}
	b, err := json.Marshal(kept)
	if err != nil {
		return []byte("{}")
	}
	return b
}

func mustJSON(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		return json.RawMessage("null") // unreachable for the fixed shapes here
	}
	return b
}
