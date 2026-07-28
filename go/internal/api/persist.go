package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/Hussain0327/amneal/go/internal/store"
)

// Persistence for the step-5 CompleteQuery cutover. AUDIT-FIRST, three ISOLATED
// writes (not one collapsed transaction), reproducing grounded_qa.ask()/
// _persist_turn/_apply_session_patch exactly:
//
//	T1  user message         pre-RAG, best-effort   (persistUserTurn, in the handler)
//	T2  query_log            post-RAG, AUTHORITATIVE (persistTurn below)
//	T3  assistant message    post-audit, best-effort (persistTurn below)
//
// Each write is its own implicit transaction on a pooled connection, so a
// chat/connection fault after T2 can NEVER erase the committed audit row -- the
// property a single collapsed txn would lose. INV-6: exactly one query_log row
// per turn for every outcome (or -1, an out-of-band sentinel, when the audit
// store is fully down and the row genuinely could not be written).

// errSessionOwnershipLost is conversation.SessionOwnershipError's Go twin:
// the T1 upsert found the session already bound to a DIFFERENT user (a create
// race lost after authorizeSession's pre-check). Nothing has been written at
// that point, so the turn aborts with the same unaudited 404 the pre-check
// produces (main.py, the except-SessionOwnershipError branch of /query)
// instead of degrading to a detached turn.
var errSessionOwnershipLost = errors.New("chat session owned by another user")

// persistUserTurn is T1: upsert the session, then write the user message. Both
// best-effort EXCEPT the ownership guard: a session row bound to another user
// (the upsert's guarded conflict update returns no row) yields
// errSessionOwnershipLost with NOTHING written -- Python's ensure_session
// raises SessionOwnershipError in exactly this case. On any OTHER failure the
// session_id degrades to the fresh turn_id (never the requested id), exactly
// as ask() does -- so later writes never target a foreign session. Returns
// the (possibly degraded) session_id the turn proceeds with.
func (s *Server) persistUserTurn(
	ctx context.Context,
	sessionID, turnID, userID, question string,
	filtersObj []byte,
	t0 time.Time,
) (string, error) {
	if _, err := s.q.UpsertChatSession(ctx, store.UpsertChatSessionParams{
		ID:                sessionID,
		UserID:            text(userID),
		ActiveFiltersJson: []byte("{}"),
		CreatedAt:         ts(t0),
		UpdatedAt:         ts(t0),
	}); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", errSessionOwnershipLost
		}
		s.qaLog("qa_session_setup_failed", turnID, sessionID, err)
		return turnID, nil
	}
	msgID, err := newUUID4()
	if err != nil {
		s.qaLog("qa_user_uuid_failed", turnID, sessionID, err)
		return turnID, nil
	}
	if err := s.q.InsertChatMessage(ctx, store.InsertChatMessageParams{
		ID:            msgID,
		SessionID:     sessionID,
		TurnID:        turnID,
		Role:          "user",
		Content:       question,
		FiltersJson:   jsonbOr(filtersObj, "{}"),
		CitationsJson: []byte("[]"),
		ClarifyJson:   []byte("[]"),
		RelatedJson:   []byte("[]"),
		MetadataJson:  []byte("{}"),
		CreatedAt:     ts(t0),
		// status / model_name / audit_id / reason / interpretation stay NULL.
	}); err != nil {
		s.qaLog("qa_user_record_failed", turnID, sessionID, err)
		return turnID, nil
	}
	return sessionID, nil
}

// errAnswerUnaudited is _persist_turn's defensive re-raise ("if
// audit.failure_fallback is None: raise"): a strict validated answer whose
// authoritative audit write failed with NO core-supplied fallback to degrade
// to. INV-6 (no-audit-no-answer) forces the caller to WITHHOLD the answer and
// 500 -- returning it would leak a paid, citation-bearing answer with no
// audit row. Unreachable while Python always serializes a fallback for strict
// payloads, exactly like the Python branch it mirrors.
var errAnswerUnaudited = errors.New("strict answer audit failed with no fallback")

// persistTurn runs T2 (authoritative audit) then T3 (best-effort assistant) and
// returns the wire body with audit_id spliced in, plus the audit id. The only
// non-nil error is errAnswerUnaudited (nothing written, T3 skipped).
func (s *Server) persistTurn(ctx context.Context, payload *computePayload, t0 time.Time) (json.RawMessage, int32, error) {
	spec := payload.Persist
	response := payload.Response
	patch := spec.Patch
	var auditID int32
	turnID, sessionID := correlationIDs(spec)

	if spec.AllowSkip {
		// Skip-tolerant paths (refuse/clarify/meta/scope/summary + synthesized
		// errors): a failed audit degrades to -1, the answer still returns.
		auditID = s.auditSkipTolerant(ctx, spec.AuditLogKwargs, t0)
	} else {
		// Strict answer path (INV-6): a validated answer with no audit row is
		// never returned. On a failed write, degrade to the fixed-copy fallback
		// (a status="error" refusal, itself skip-audited) so the failure is
		// DEFINED, not a naked 500 the stream-fallback client would re-run.
		id, err := s.q.InsertQueryLog(ctx, auditParams(spec.AuditLogKwargs, s.now(), t0))
		if err != nil {
			s.qaCapture("qa_answer_audit_write_failed", turnID, sessionID, err)
			if spec.Fallback == nil {
				// INV-6 enforced the hard way: a validated answer is about to
				// be withheld and 500'd. Captured SEPARATELY from the write
				// failure above -- that one says "the audit store hiccuped",
				// this one says "a user lost a paid answer", and the runbook
				// treats a single confirmed miss as a rollback trigger.
				s.qaCapture("qa_answer_unaudited", turnID, sessionID,
					fmt.Errorf("%w: %v", errAnswerUnaudited, err))
				return nil, 0, errAnswerUnaudited
			}
			response = spec.Fallback.Response
			patch = spec.Fallback.Patch
			auditID = s.auditSkipTolerant(ctx, spec.Fallback.AuditLogKwargs, t0)
		} else {
			auditID = id
		}
	}

	// T3: assistant message + filter carry-over, best-effort. The audit row
	// (INV-6) is already committed, so a failure here must never erase it.
	s.insertAssistant(ctx, patch, auditID, t0)

	wire, err := spliceAuditID(response, auditID)
	if err != nil {
		// Unreachable for these shapes (response is always valid JSON). Never
		// 500 an already-persisted turn on a serialization glitch.
		s.qaLog("qa_wire_splice_failed", turnID, sessionID, err)
		return response, auditID, nil
	}
	return wire, auditID, nil
}

// correlationIDs picks the turn/session ids this turn's log lines and Sentry
// events are tagged with. The audit kwargs carry them for every payload the
// core produces (and for the Go-synthesized upstream_error turn); the session
// patch is the fallback, so a payload that somehow omits them still logs a
// correlatable line instead of a bare error.
func correlationIDs(spec persistSpec) (string, string) {
	turnID := derefStr(spec.AuditLogKwargs.TurnID)
	if turnID == "" {
		turnID = spec.Patch.TurnID
	}
	sessionID := derefStr(spec.AuditLogKwargs.SessionID)
	if sessionID == "" {
		sessionID = spec.Patch.SessionID
	}
	return turnID, sessionID
}

// auditSkipTolerant writes the audit row, returning -1 (a sentinel that never
// collides with a real id) on failure -- the defined-failure wrapper Python's
// _log_query_or_skip provides.
func (s *Server) auditSkipTolerant(ctx context.Context, k auditKwargs, t0 time.Time) int32 {
	id, err := s.q.InsertQueryLog(ctx, auditParams(k, s.now(), t0))
	if err != nil {
		// Captured, not just logged: a -1 audit_id IS a missing query_log row
		// for a turn that answered, which is the INV-6 miss the runbook calls
		// a rollback trigger.
		s.qaCapture("qa_audit_write_failed", derefStr(k.TurnID), derefStr(k.SessionID), err)
		return -1
	}
	return id
}

// insertAssistant is T3: the assistant chat_message + optional filter carry-
// over. Best-effort -- a degraded turn (no session context) writes nothing, and
// any DB failure is logged, never fatal.
func (s *Server) insertAssistant(ctx context.Context, patch sessionPatch, auditID int32, t0 time.Time) {
	if patch.SessionID == "" || patch.TurnID == "" {
		return
	}
	msgID, err := newUUID4()
	if err != nil {
		s.qaLog("qa_assistant_uuid_failed", patch.TurnID, patch.SessionID, err)
		return
	}
	// created_at strictly AFTER the user message: T1 and T3 straddle the RAG
	// round-trip so this is normally seconds later, but floor at t0+1us so
	// intra-turn order is deterministic even if the clock did not advance a
	// microsecond (ORDER BY created_at ASC, role DESC handles the exact tie).
	ta := s.now().Truncate(time.Microsecond)
	if !ta.After(t0) {
		ta = t0.Add(time.Microsecond)
	}
	if err := s.q.InsertChatMessage(ctx, store.InsertChatMessageParams{
		ID:             msgID,
		SessionID:      patch.SessionID,
		TurnID:         patch.TurnID,
		Role:           "assistant",
		Content:        patch.Content,
		Status:         text(patch.Status),
		ModelName:      text(patch.ModelName),
		AuditID:        pgtype.Int4{Int32: auditID, Valid: true},
		Reason:         textOrNull(patch.Reason),
		Interpretation: textOrNull(patch.Interpretation),
		FiltersJson:    jsonbOr(patch.Filters, "{}"),
		CitationsJson:  jsonbOr(patch.Citations, "[]"),
		ClarifyJson:    jsonbOr(patch.Clarify, "[]"),
		RelatedJson:    jsonbOr(patch.Related, "[]"),
		MetadataJson:   jsonbOr(patch.Metadata, "{}"),
		CreatedAt:      ts(ta),
	}); err != nil {
		s.qaLog("qa_assistant_record_failed", patch.TurnID, patch.SessionID, err)
		return
	}
	if patch.UpdateFilters {
		if err := s.q.UpdateChatSessionFilters(ctx, store.UpdateChatSessionFiltersParams{
			ID:                patch.SessionID,
			ActiveFiltersJson: jsonbOr(patch.Filters, "{}"),
			UpdatedAt:         ts(s.now()),
		}); err != nil {
			s.qaLog("qa_update_session_filters_failed", patch.TurnID, patch.SessionID, err)
		}
	}
}

// auditParams maps the endpoint's audit kwargs onto InsertQueryLogParams. jsonb
// columns are written VERBATIM; token/cost pointers preserve null vs 0.
//
// latency_ms is the one column NOT sourced from the kwargs: the stateless core
// cannot see transport time, so the control plane derives it from the turn
// clock (t0, stamped in the handler before the compute call) and the audit
// clock (at). Both come from s.now(), so an injected test clock controls it.
func auditParams(k auditKwargs, at, t0 time.Time) store.InsertQueryLogParams {
	return store.InsertQueryLogParams{
		Ts:            ts(at),
		SessionID:     textOrNull(k.SessionID),
		TurnID:        textOrNull(k.TurnID),
		UserID:        textOrNull(k.UserID),
		Mode:          k.Mode,
		QueryText:     k.QueryText,
		RetrievedJson: jsonbOr(k.Retrieved, "[]"),
		AnswerText:    k.AnswerText,
		CitationsJson: jsonbOr(k.Citations, "[]"),
		Refused:       k.Refused,
		Status:        textOrNull(k.Status),
		RouteJson:     jsonbOr(k.RouteJson, "{}"),
		ModelName:     k.ModelName,
		InputTokens:   int4OrNull(k.InputTokens),
		OutputTokens:  int4OrNull(k.OutputTokens),
		CostUsd:       float8OrNull(k.CostUsd),
		LatencyMs:     latencyMs(t0, at),
	}
}

// latencyMs is whole-millisecond turn wall time, or NULL. NULL -- never 0 --
// whenever the number would be a lie: no start stamp, or a clock that moved
// backwards between the two reads. A percentile computed over a column where
// "unknown" and "instantaneous" are both 0 understates every gate that reads
// it, which is exactly what the provider-cutover gates must not do. The int32
// clamp is a column-width guard, not an expected path: a turn cannot outlive
// the request timeout, let alone 24 days.
func latencyMs(t0, at time.Time) pgtype.Int4 {
	if t0.IsZero() {
		return pgtype.Int4{}
	}
	ms := at.Sub(t0).Milliseconds()
	if ms < 0 {
		return pgtype.Int4{}
	}
	if ms > math.MaxInt32 {
		ms = math.MaxInt32
	}
	return pgtype.Int4{Int32: int32(ms), Valid: true}
}

// spliceAuditID adds audit_id to the endpoint/synthesized wire body without
// touching any other key: decode to raw-valued map, set audit_id, re-marshal.
// Null-valued keys (interpretation/reason) survive as JSON null (json.RawMessage),
// which a typed struct with omitempty would silently drop.
func spliceAuditID(response json.RawMessage, auditID int32) (json.RawMessage, error) {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(response, &m); err != nil {
		return nil, err
	}
	idBytes, err := json.Marshal(auditID)
	if err != nil {
		return nil, err
	}
	m["audit_id"] = idBytes
	return json.Marshal(m)
}

func int4OrNull(p *int64) pgtype.Int4 {
	if p == nil {
		return pgtype.Int4{}
	}
	return pgtype.Int4{Int32: int32(*p), Valid: true}
}

func float8OrNull(p *float64) pgtype.Float8 {
	if p == nil {
		return pgtype.Float8{}
	}
	return pgtype.Float8{Float64: *p, Valid: true}
}

// jsonbOr returns raw jsonb bytes, or a default when the payload is absent, so a
// jsonb column is never written NULL where Python writes [] / {}.
func jsonbOr(raw []byte, def string) []byte {
	if len(raw) == 0 {
		return []byte(def)
	}
	return raw
}
