package api

import (
	"errors"
	"fmt"
	"math"
	"net/http"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/Hussain0327/amneal/go/internal/store"
)

// feedbackResponse echoes the request (FeedbackResponse in main.py); the
// comment key is always present, null when the caller sent none.
type feedbackResponse struct {
	AuditID int     `json:"audit_id"`
	Rating  int     `json:"rating"`
	Comment *string `json:"comment"`
}

// handleFeedback ports main.py::feedback (H4): thumbs up/down on one of the
// caller's own answered qa turns. 404 for a missing, foreign, or non-qa audit
// row -- the response never confirms someone else's row exists. One row per
// (audit_id, user_id); re-rating replaces rating AND comment but keeps the
// original created_at (the ON CONFLICT upsert subsumes Python's
// IntegrityError-retry race handling atomically).
func (s *Server) handleFeedback(w http.ResponseWriter, r *http.Request) {
	// Auth precedes body validation: FastAPI solves the require_user
	// dependency before parsing parameters, so unauthenticated + bad body
	// is a 401, not a 422.
	u, ok := s.currentUser(w, r)
	if !ok {
		return
	}

	// Pointer fields distinguish missing from present; unknown keys are
	// ignored (pydantic default). Deliberate divergences from pydantic,
	// none reachable by the generated TS client (it types these fields as
	// required numbers): (a) lax coercion -- {"audit_id":"5"} or
	// {"rating":1.0} validates in Python but fails the decode here; (b) an
	// explicit null is indistinguishable from an absent key, so it reports
	// "missing"/"Field required" where pydantic reports int_type; (c) an
	// integer beyond int64 fails the decode (422) where Python's unbounded
	// int reaches the lookup and 404s.
	var body struct {
		AuditID *int    `json:"audit_id"`
		Rating  *int    `json:"rating"`
		Comment *string `json:"comment"`
	}
	if !decodeStrictJSON(w, r, &body) {
		return
	}
	// Field-declaration order (audit_id, rating, comment), errors
	// accumulated like pydantic's.
	var problems []validationItem
	if body.AuditID == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "audit_id"}, Msg: "Field required"})
	}
	if body.Rating == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "rating"}, Msg: "Field required"})
	} else if *body.Rating != -1 && *body.Rating != 1 {
		problems = append(problems, validationItem{Type: "value_error", Loc: []string{"body", "rating"}, Msg: "Value error, rating must be -1 (thumbs down) or 1 (thumbs up)"})
	}
	if body.Comment != nil && utf8.RuneCountInString(*body.Comment) > 2000 {
		problems = append(problems, validationItem{Type: "string_too_long", Loc: []string{"body", "comment"}, Msg: "String should have at most 2000 characters"})
	}
	if len(problems) > 0 {
		writeValidationError(w, problems...)
		return
	}

	// query_log.id is a 32-bit integer: an id outside that range cannot name
	// a row, and Python reaches the same 404 by finding nothing. Guard here
	// so the int32 conversion below cannot wrap into someone else's id.
	if *body.AuditID > math.MaxInt32 || *body.AuditID < math.MinInt32 {
		writeDetail(w, http.StatusNotFound, "answer not found")
		return
	}
	auditID := int32(*body.AuditID)
	userID := chatUserID(u) // query_log.user_id is text, same str(user.id) encoding as chat_session

	// Ownership probe and upsert are separate statements, exactly like the
	// Python handler's two session_scopes; the single-statement upsert is
	// atomic on its own, and losing the audit row between the two surfaces
	// as the same FK-violation 500 Python's exhausted retry produced.
	if _, err := s.q.GetOwnedQaAuditRow(r.Context(), store.GetOwnedQaAuditRowParams{
		ID: auditID, UserID: text(userID),
	}); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeDetail(w, http.StatusNotFound, "answer not found")
			return
		}
		s.internalError(w, "feedback ownership probe", err)
		return
	}
	if _, err := s.q.UpsertAnswerFeedback(r.Context(), store.UpsertAnswerFeedbackParams{
		AuditID:   auditID,
		UserID:    userID,
		Rating:    int32(*body.Rating),
		Comment:   textOrNull(body.Comment),
		CreatedAt: ts(s.now()),
	}); err != nil {
		s.internalError(w, fmt.Sprintf("feedback upsert audit_id=%d", auditID), err)
		return
	}
	writeJSON(w, http.StatusOK, feedbackResponse{
		AuditID: *body.AuditID, Rating: *body.Rating, Comment: body.Comment,
	})
}
