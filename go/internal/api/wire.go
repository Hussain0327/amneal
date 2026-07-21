package api

import (
	"encoding/json"
	"net/http"

	"github.com/jackc/pgx/v5/pgtype"
)

// Wire helpers pinning the FastAPI response conventions the frontend already
// depends on. Error bodies are {"detail": ...} exactly like FastAPI's
// HTTPException; success bodies are handler-specific structs.

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// writeDetail emits FastAPI's HTTPException shape: {"detail":"<msg>"}.
func writeDetail(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"detail": msg})
}

// validationItem is one entry of FastAPI's 422 detail array. Only the three
// fields the shape guarantees; the frontend treats 422 generically, and the
// one pytest assertion on this path checks the status code alone.
type validationItem struct {
	Type string   `json:"type"`
	Loc  []string `json:"loc"`
	Msg  string   `json:"msg"`
}

func writeValidationError(w http.ResponseWriter, items ...validationItem) {
	writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": items})
}

// isoNaive renders a naive-UTC DB timestamp the way FastAPI serializes the
// Python datetime: isoformat() with NO timezone suffix, and a 6-digit
// microsecond fraction ONLY when the microseconds are nonzero
// (e.g. "2026-07-20T12:34:56" / "2026-07-20T12:34:56.789012"). The frontend
// parses these with datetime-agnostic code and the pytest contract parses
// them via datetime.fromisoformat -- both shapes round-trip.
func isoNaive(t pgtype.Timestamp) string {
	tt := t.Time
	if tt.Nanosecond() == 0 {
		return tt.Format("2006-01-02T15:04:05")
	}
	return tt.Format("2006-01-02T15:04:05.000000")
}

// truncate60 is Python's s[:60] -- a CODE POINT slice, not bytes: multibyte
// content must not be cut mid-rune.
func truncate60(s string) string {
	r := []rune(s)
	if len(r) > 60 {
		return string(r[:60])
	}
	return s
}

// rawListOrEmpty passes a stored jsonb array through VERBATIM -- the
// api-contract-freeze tests pin that stored citation/clarify/related payloads
// survive including keys no wire type declares, so the Go port must NOT
// decode-validate-reencode them. A NULL column serializes as [] exactly like
// the Python handlers' `row.x_json or []`.
func rawListOrEmpty(b []byte) json.RawMessage {
	if len(b) == 0 || string(b) == "null" {
		return json.RawMessage("[]")
	}
	return json.RawMessage(b)
}

// textPtr converts a nullable pgtype.Text to the *string JSON null contract.
func textPtr(t pgtype.Text) *string {
	if !t.Valid {
		return nil
	}
	s := t.String
	return &s
}

// textOrNull is textPtr's inverse: a nil *string becomes SQL NULL.
func textOrNull(p *string) pgtype.Text {
	if p == nil {
		return pgtype.Text{}
	}
	return pgtype.Text{String: *p, Valid: true}
}
