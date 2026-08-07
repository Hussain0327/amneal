package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"

	"github.com/Hussain0327/amneal/go/internal/store"
)

// Chat-session wire types. Key sets are contract-frozen by the pytest suite
// being ported (test_api_contract_freeze.py): list summaries carry EXACTLY
// {id,title,created_at,updated_at,message_count}; detail is {session,messages}
// with message keys {id,turn_id,role,content,status,citations,audit_id,
// reason,interpretation,clarify,related,created_at}. Stored citation/clarify/
// related payloads pass through VERBATIM (json.RawMessage) -- older sessions
// carry legacy keys no wire type declares, and stripping them is a contract
// violation, not a cleanup.

type sessionSummary struct {
	ID           string `json:"id"`
	Title        string `json:"title"`
	CreatedAt    string `json:"created_at"`
	UpdatedAt    string `json:"updated_at"`
	MessageCount int64  `json:"message_count"`
}

type sessionOut struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

type messageOut struct {
	ID             string          `json:"id"`
	TurnID         string          `json:"turn_id"`
	Role           string          `json:"role"`
	Content        string          `json:"content"`
	Status         *string         `json:"status"`
	Citations      json.RawMessage `json:"citations"`
	AuditID        *int32          `json:"audit_id"`
	Reason         *string         `json:"reason"`
	Interpretation *string         `json:"interpretation"`
	Clarify        json.RawMessage `json:"clarify"`
	Related        json.RawMessage `json:"related"`
	CreatedAt      string          `json:"created_at"`
}

// chatUserID: chat_session.user_id stores str(user.id) -- a STRING column
// holding the integer id, a Python-era artifact both runtimes must agree on.
func chatUserID(u store.GetAuthSessionWithUserRow) string {
	return strconv.Itoa(int(u.UserID))
}

// handleListSessions ports main.py::list_sessions: the caller's sessions
// newest-updated first, title falling back to the first user message
// (truncated to 60 code points) then the literal "(untitled)", counts from
// one GROUP BY. Deliberately two queries, never N+1 -- same as Python.
func (s *Server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	u, ok := s.currentUser(w, r)
	if !ok {
		return
	}
	uid := text(chatUserID(u))
	rows, err := s.q.ListChatSessionsForUser(r.Context(), uid)
	if err != nil {
		s.internalError(w, "list sessions", err)
		return
	}
	counts, err := s.q.CountChatMessagesForUser(r.Context(), uid)
	if err != nil {
		s.internalError(w, "count session messages", err)
		return
	}
	byID := make(map[string]int64, len(counts))
	for _, c := range counts {
		byID[c.SessionID] = c.MessageCount
	}
	out := make([]sessionSummary, 0, len(rows))
	for _, row := range rows {
		out = append(out, sessionSummary{
			ID:           row.ID,
			Title:        summaryTitle(row),
			CreatedAt:    isoNaive(row.CreatedAt),
			UpdatedAt:    isoNaive(row.UpdatedAt),
			MessageCount: byID[row.ID],
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": out})
}

// summaryTitle mirrors Python's `row.title or (first_msg[:60] if first_msg
// else "(untitled)")`: truthiness, not NULL-ness -- an empty-string title
// falls through (unreachable today; nothing writes title -- see chat.sql).
// DisplayTitle carries the UNTRUNCATED first user message from the SQL
// subquery; the 60-code-point cut happens here, exactly where Python cuts.
func summaryTitle(row store.ListChatSessionsForUserRow) string {
	if row.Title.Valid && row.Title.String != "" {
		return row.Title.String
	}
	if row.DisplayTitle.Valid && row.DisplayTitle.String != "" {
		return truncate60(row.DisplayTitle.String)
	}
	return "(untitled)"
}

// handleGetSession ports main.py::get_session + _owned_session_or_404 +
// _session_title: missing, foreign, and legacy NULL-user sessions are all the
// same 404 {"detail":"session not found"} -- existence is never confirmed
// (404, never 403). Messages come back created_at ASC with stored JSON
// payloads passed through verbatim.
func (s *Server) handleGetSession(w http.ResponseWriter, r *http.Request) {
	u, ok := s.currentUser(w, r)
	if !ok {
		return
	}
	row, err := s.q.GetChatSessionOwned(r.Context(), store.GetChatSessionOwnedParams{
		ID: r.PathValue("id"), UserID: text(chatUserID(u)),
	})
	if errors.Is(err, pgx.ErrNoRows) {
		writeDetail(w, http.StatusNotFound, detailSessionNotFound)
		return
	}
	if err != nil {
		s.internalError(w, "get session", err)
		return
	}
	msgs, err := s.q.ListChatMessages(r.Context(), row.ID)
	if err != nil {
		s.internalError(w, "list session messages", err)
		return
	}

	// _session_title: explicit title, else STRICTLY THE FIRST user message
	// (already in hand -- messages are ordered created_at ASC, so the first
	// role=="user" row IS the subquery's answer; no extra query needed).
	// Python takes that one row and falls to "(untitled)" if its content is
	// empty -- it does NOT scan later user messages, so neither do we.
	title := "(untitled)"
	if row.Title.Valid && row.Title.String != "" {
		title = row.Title.String
	} else {
		for _, m := range msgs {
			if m.Role != "user" {
				continue
			}
			if m.Content != "" {
				title = truncate60(m.Content)
			}
			break
		}
	}

	out := make([]messageOut, 0, len(msgs))
	for _, m := range msgs {
		var auditID *int32
		if m.AuditID.Valid {
			v := m.AuditID.Int32
			auditID = &v
		}
		out = append(out, messageOut{
			ID:             m.ID,
			TurnID:         m.TurnID,
			Role:           m.Role,
			Content:        m.Content,
			Status:         textPtr(m.Status),
			Citations:      rawListOrEmpty(m.CitationsJson),
			AuditID:        auditID,
			Reason:         textPtr(m.Reason),
			Interpretation: textPtr(m.Interpretation),
			Clarify:        rawListOrEmpty(m.ClarifyJson),
			Related:        rawListOrEmpty(m.RelatedJson),
			CreatedAt:      isoNaive(m.CreatedAt),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"session": sessionOut{
			ID:        row.ID,
			Title:     title,
			CreatedAt: isoNaive(row.CreatedAt),
			UpdatedAt: isoNaive(row.UpdatedAt),
		},
		"messages": out,
	})
}

// handleDeleteSession ports main.py::delete_session: ownership-checked hard
// delete, messages first then the session row (the FK has no ON DELETE
// CASCADE), in ONE transaction like Python's session_scope. 204 on success.
func (s *Server) handleDeleteSession(w http.ResponseWriter, r *http.Request) {
	u, ok := s.currentUser(w, r)
	if !ok {
		return
	}
	row, err := s.q.GetChatSessionOwned(r.Context(), store.GetChatSessionOwnedParams{
		ID: r.PathValue("id"), UserID: text(chatUserID(u)),
	})
	if errors.Is(err, pgx.ErrNoRows) {
		writeDetail(w, http.StatusNotFound, detailSessionNotFound)
		return
	}
	if err != nil {
		s.internalError(w, "get session for delete", err)
		return
	}
	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		s.internalError(w, "begin delete session", err)
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()
	qtx := s.q.WithTx(tx)
	if _, err := qtx.DeleteChatMessagesBySession(r.Context(), row.ID); err != nil {
		s.internalError(w, "delete session messages", err)
		return
	}
	if _, err := qtx.DeleteChatSession(r.Context(), row.ID); err != nil {
		s.internalError(w, "delete session", err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		s.internalError(w, "commit delete session", err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
