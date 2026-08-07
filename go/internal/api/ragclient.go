package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"
)

// ragClient calls the internal Python RAG compute endpoint
// (POST /internal/query/compute) -- the stateless-core half of the step-5
// CompleteQuery boundary. It reaches the SAME uvicorn the relay fronts, but
// DIRECTLY (never through this proxy's own mux, which 404s /internal/).
type ragClient struct {
	url    string
	token  string
	client *http.Client
}

func newRAGClient(baseURL, token string, timeout time.Duration) *ragClient {
	return &ragClient{
		url:   strings.TrimRight(baseURL, "/") + "/internal/query/compute",
		token: token,
		client: &http.Client{
			// A FINITE overall deadline: unlike the SSE relay (proxy.go, no
			// response timeout on purpose), this is a buffered JSON call, so an
			// accept-then-hang upstream must time out into a synthesized
			// upstream_error audit row rather than leak the request forever.
			Timeout: timeout,
			Transport: &http.Transport{
				// Nil Proxy: an inherited HTTP(S)_PROXY must never reroute the
				// internal hop to our own upstream (same rationale as proxy.go).
				Proxy: nil,
				DialContext: (&net.Dialer{
					Timeout:   5 * time.Second,
					KeepAlive: 30 * time.Second,
				}).DialContext,
				TLSHandshakeTimeout:   5 * time.Second,
				MaxIdleConnsPerHost:   8,
				IdleConnTimeout:       90 * time.Second,
				ExpectContinueTimeout: 1 * time.Second,
			},
		},
	}
}

// errUpstream marks every dead/slow/misbehaving-upstream outcome: dial refused,
// deadline exceeded, or a non-200/503 status. The handler converts it to the
// synthesized upstream_error audit row (INV-6 holds even when Python is down).
var errUpstream = errors.New("internal rag upstream error")

// errSaturated marks a compute-endpoint 503: the Python ask() pool shed load
// (main.py _shed_if_ask_pool_saturated). Distinct from errUpstream on purpose:
// a shed turn NEVER ran, so the handler must relay the defined 503 overload
// contract instead of auditing a synthesized upstream_error row. The hop is
// direct (no intermediary 503s), but the SAME app registers a provider-outage
// 503 handler with a DIFFERENT body (main.py _handle_upstream_error) -- today
// unreachable from compute_turn, yet classification must not depend on that
// staying true, so compute() also requires the byte-fixed shed body (pinned by
// contract S27) before classifying; any other 503 stays on the audited
// errUpstream path.
var errSaturated = errors.New("internal rag saturated")

// computeRequest is the compute endpoint's request body. Filters are the
// already-whitelisted request filters, forwarded verbatim (opaque to Go).
type computeRequest struct {
	Question  string          `json:"question"`
	Filters   json.RawMessage `json:"filters"`
	K         *int            `json:"k"`
	SessionID string          `json:"session_id"`
	TurnID    string          `json:"turn_id"`
	UserID    *string         `json:"user_id"`
}

// computePayload is {response, persist}: the wire body (minus audit_id) and the
// write instructions the control plane executes.
type computePayload struct {
	Response json.RawMessage `json:"response"`
	Persist  persistSpec     `json:"persist"`
}

type persistSpec struct {
	AuditLogKwargs auditKwargs      `json:"audit_log_kwargs"`
	AllowSkip      bool             `json:"allow_skip"`
	Patch          sessionPatch     `json:"patch"`
	Fallback       *persistFallback `json:"fallback"`
}

// persistFallback is the strict answer-path degrade: the fixed-copy error turn
// to write (skip-audited) if the authoritative audit write fails. Its own
// fallback is always nil (one level deep), so it carries none.
type persistFallback struct {
	Response       json.RawMessage `json:"response"`
	AuditLogKwargs auditKwargs     `json:"audit_log_kwargs"`
	AllowSkip      bool            `json:"allow_skip"`
	Patch          sessionPatch    `json:"patch"`
}

// auditKwargs mirrors audit.log_query's kwargs one-for-one. The jsonb payloads
// (retrieved/citations/route_json) are opaque RawMessage stored VERBATIM. The
// token/cost pointers preserve JSON null -> SQL NULL vs 0 -> SQL 0 (never
// coalesced): a refusal writes NULL, a real echo answer writes 0.
type auditKwargs struct {
	Mode         string          `json:"mode"`
	QueryText    string          `json:"query_text"`
	Retrieved    json.RawMessage `json:"retrieved"`
	AnswerText   string          `json:"answer_text"`
	Citations    json.RawMessage `json:"citations"`
	Refused      bool            `json:"refused"`
	ModelName    string          `json:"model_name"`
	SessionID    *string         `json:"session_id"`
	TurnID       *string         `json:"turn_id"`
	UserID       *string         `json:"user_id"`
	Status       *string         `json:"status"`
	RouteJSON    json.RawMessage `json:"route_json"`
	InputTokens  *int64          `json:"input_tokens"`
	OutputTokens *int64          `json:"output_tokens"`
	CostUSD      *float64        `json:"cost_usd"`
}

// sessionPatch mirrors rag_contract.SessionPatch: the chat-history mutations
// the control plane applies AFTER the audit write. jsonb payloads are opaque.
type sessionPatch struct {
	SessionID      string          `json:"session_id"`
	TurnID         string          `json:"turn_id"`
	Content        string          `json:"content"`
	Status         string          `json:"status"`
	ModelName      string          `json:"model_name"`
	Reason         *string         `json:"reason"`
	Interpretation *string         `json:"interpretation"`
	Filters        json.RawMessage `json:"filters"`
	Citations      json.RawMessage `json:"citations"`
	Clarify        json.RawMessage `json:"clarify"`
	Related        json.RawMessage `json:"related"`
	Metadata       json.RawMessage `json:"metadata"`
	UpdateFilters  bool            `json:"update_filters"`
}

// compute POSTs the turn to the internal endpoint and decodes {response,
// persist}. Any transport error, deadline, or non-200 is wrapped as errUpstream.
func (c *ragClient) compute(ctx context.Context, req computeRequest) (*computePayload, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("X-Internal-Token", c.token)

	resp, err := c.client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", errUpstream, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusServiceUnavailable {
		// Shed only if the body is the byte-fixed busy contract (see the
		// errSaturated comment); a non-shed 503 must be AUDITED, not relayed.
		b, readErr := io.ReadAll(io.LimitReader(resp.Body, 1024))
		if readErr == nil && string(b) == saturatedDetailJSON {
			return nil, fmt.Errorf("%w: status %d", errSaturated, resp.StatusCode)
		}
		return nil, fmt.Errorf("%w: status %d (non-shed 503 body)", errUpstream, resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%w: status %d", errUpstream, resp.StatusCode)
	}
	var payload computePayload
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("%w: decode: %v", errUpstream, err)
	}
	return &payload, nil
}
