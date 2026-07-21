package api

// PR C contract tests: POST /feedback, GET /settings, GET/POST /products,
// DELETE /products/{product_id} -- the ported pytest checklist
// (tests/test_feedback_api.py, test_api.py products/settings tests,
// test_api_contract_freeze.py::test_products_create_delete_wire_keys...,
// ::test_settings_wire_keys_exact) plus the upsert trust-gate semantics from
// watchlist.py that only unit-level Python tests covered. Same opt-in
// Postgres discipline as contract_test.go (TEST_DATABASE_URL, disposable DB,
// composite proxy handler end-to-end).

import (
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
	"testing"
	"time"
)

func (h *harness) seedAudit(t *testing.T, userID int32, mode string) int32 {
	t.Helper()
	var id int32
	err := h.pool.QueryRow(t.Context(),
		`INSERT INTO public.query_log (ts, user_id, mode, query_text, answer_text, refused, model_name)
		 VALUES (now(), $1, $2, 'What study design is recommended?', 'A fasting study [PSG_020503, p.3].', false, 'echo')
		 RETURNING id`,
		strconv.Itoa(int(userID)), mode).Scan(&id)
	if err != nil {
		t.Fatalf("seed audit: %v", err)
	}
	return id
}

type feedbackRow struct {
	AuditID int32
	UserID  string
	Rating  int32
	Comment *string
}

func (h *harness) feedbackRows(t *testing.T) []feedbackRow {
	t.Helper()
	rows, err := h.pool.Query(t.Context(),
		`SELECT audit_id, user_id, rating, comment FROM public.answer_feedback ORDER BY id`)
	if err != nil {
		t.Fatalf("feedback rows: %v", err)
	}
	defer rows.Close()
	var out []feedbackRow
	for rows.Next() {
		var r feedbackRow
		if err := rows.Scan(&r.AuditID, &r.UserID, &r.Rating, &r.Comment); err != nil {
			t.Fatalf("scan feedback row: %v", err)
		}
		out = append(out, r)
	}
	return out
}

// --- POST /feedback (test_feedback_api.py) ---

func TestFeedbackRequiresAuth(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	resp := h.do(t, "POST", "/feedback", "", map[string]any{"audit_id": 1, "rating": 1})
	if resp.StatusCode != 401 {
		t.Fatalf("status = %d, want 401", resp.StatusCode)
	}
	if body := decode(t, resp); body["detail"] != "authentication required" {
		t.Fatalf("detail = %v", body["detail"])
	}
}

func TestFeedbackThumbsUpExactBodyAndRow(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	cookie := h.login(t, testEmail, testPassword)
	auditID := h.seedAudit(t, uid, "qa")

	resp := h.do(t, "POST", "/feedback", cookie, map[string]any{"audit_id": auditID, "rating": 1})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	body := decode(t, resp)
	wantKeys(t, body, "audit_id", "rating", "comment")
	if body["audit_id"] != float64(auditID) || body["rating"] != float64(1) || body["comment"] != nil {
		t.Fatalf("body = %v", body)
	}
	rows := h.feedbackRows(t)
	if len(rows) != 1 || rows[0].AuditID != auditID ||
		rows[0].UserID != strconv.Itoa(int(uid)) || rows[0].Rating != 1 || rows[0].Comment != nil {
		t.Fatalf("rows = %+v", rows)
	}
}

func TestFeedbackThumbsDownWithCommentEchoesAndStores(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	cookie := h.login(t, testEmail, testPassword)
	auditID := h.seedAudit(t, uid, "qa")

	resp := h.do(t, "POST", "/feedback", cookie,
		map[string]any{"audit_id": auditID, "rating": -1, "comment": "cited the wrong dosage form"})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if body := decode(t, resp); body["comment"] != "cited the wrong dosage form" {
		t.Fatalf("comment echo = %v", body["comment"])
	}
	rows := h.feedbackRows(t)
	if len(rows) != 1 || rows[0].Rating != -1 || rows[0].Comment == nil ||
		*rows[0].Comment != "cited the wrong dosage form" {
		t.Fatalf("rows = %+v", rows)
	}
}

func TestFeedbackReratingReplacesNullsCommentKeepsCreatedAt(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	cookie := h.login(t, testEmail, testPassword)
	auditID := h.seedAudit(t, uid, "qa")

	if resp := h.do(t, "POST", "/feedback", cookie,
		map[string]any{"audit_id": auditID, "rating": -1, "comment": "bad"}); resp.StatusCode != 200 {
		t.Fatalf("first rating: %d", resp.StatusCode)
	}
	var createdAt time.Time
	if err := h.pool.QueryRow(t.Context(),
		`SELECT created_at FROM public.answer_feedback`).Scan(&createdAt); err != nil {
		t.Fatalf("created_at: %v", err)
	}
	if resp := h.do(t, "POST", "/feedback", cookie,
		map[string]any{"audit_id": auditID, "rating": 1}); resp.StatusCode != 200 {
		t.Fatalf("re-rating: %d", resp.StatusCode)
	}
	// One row, latest rating wins, the stale comment does not linger, and the
	// original created_at survives the replace (ON CONFLICT updates only
	// rating and comment).
	rows := h.feedbackRows(t)
	if len(rows) != 1 || rows[0].Rating != 1 || rows[0].Comment != nil {
		t.Fatalf("rows = %+v", rows)
	}
	var after time.Time
	if err := h.pool.QueryRow(t.Context(),
		`SELECT created_at FROM public.answer_feedback`).Scan(&after); err != nil {
		t.Fatalf("created_at after: %v", err)
	}
	if !after.Equal(createdAt) {
		t.Fatalf("created_at changed on re-rate: %v -> %v", createdAt, after)
	}
}

func TestFeedbackOwnership404s(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	otherID := h.seedUser(t, "other@example.com", testPassword, true)
	cookie := h.login(t, testEmail, testPassword)

	foreign := h.seedAudit(t, otherID, "qa")
	nonQA := h.seedAudit(t, uid, "whitepaper")
	for name, auditID := range map[string]any{
		"missing":       999999,
		"foreign":       foreign,
		"non-qa":        nonQA,
		"int32-outside": int64(1) << 40, // Python: arbitrary int, row not found
	} {
		resp := h.do(t, "POST", "/feedback", cookie, map[string]any{"audit_id": auditID, "rating": 1})
		if resp.StatusCode != 404 {
			t.Fatalf("%s: status = %d, want 404", name, resp.StatusCode)
		}
		if body := decode(t, resp); body["detail"] != "answer not found" {
			t.Fatalf("%s: detail = %v", name, body["detail"])
		}
	}
	if rows := h.feedbackRows(t); len(rows) != 0 {
		t.Fatalf("404 paths must write nothing, got %+v", rows)
	}
}

func TestFeedbackValidation(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	uid := h.seedUser(t, testEmail, testPassword, true)
	cookie := h.login(t, testEmail, testPassword)
	auditID := h.seedAudit(t, uid, "qa")

	// Out-of-range ratings: 422 with the pinned value_error message.
	for _, bad := range []int{0, 2, -2, 5} {
		resp := h.do(t, "POST", "/feedback", cookie, map[string]any{"audit_id": auditID, "rating": bad})
		if resp.StatusCode != 422 {
			t.Fatalf("rating=%d: status = %d, want 422", bad, resp.StatusCode)
		}
		raw, _ := io.ReadAll(resp.Body)
		if !strings.Contains(string(raw), "rating must be -1 (thumbs down) or 1 (thumbs up)") {
			t.Fatalf("rating=%d: body = %s", bad, raw)
		}
	}
	// Oversized comment: max_length counts runes.
	resp := h.do(t, "POST", "/feedback", cookie,
		map[string]any{"audit_id": auditID, "rating": -1, "comment": strings.Repeat("x", 2001)})
	if resp.StatusCode != 422 {
		t.Fatalf("oversized comment: %d", resp.StatusCode)
	}
	// Missing fields accumulate in DECLARATION order (audit_id before
	// rating), like pydantic's error list.
	resp = h.do(t, "POST", "/feedback", cookie, map[string]any{})
	if resp.StatusCode != 422 {
		t.Fatalf("empty object: %d", resp.StatusCode)
	}
	raw, _ := io.ReadAll(resp.Body)
	auditIdx := strings.Index(string(raw), `["body","audit_id"]`)
	ratingIdx := strings.Index(string(raw), `["body","rating"]`)
	if auditIdx < 0 || ratingIdx < 0 || auditIdx > ratingIdx {
		t.Fatalf("missing-field items absent or misordered: %s", raw)
	}
	if rows := h.feedbackRows(t); len(rows) != 0 {
		t.Fatalf("422 paths must write nothing, got %+v", rows)
	}
	// Both valid ratings accepted.
	for _, rating := range []int{-1, 1} {
		resp := h.do(t, "POST", "/feedback", cookie, map[string]any{"audit_id": auditID, "rating": rating})
		if resp.StatusCode != 200 {
			t.Fatalf("rating=%d: status = %d", rating, resp.StatusCode)
		}
		if body := decode(t, resp); body["rating"] != float64(rating) {
			t.Fatalf("rating echo = %v", body["rating"])
		}
	}
}

// --- GET /settings (test_settings_wire_keys_exact, test_settings_no_secrets) ---

func TestSettingsRequiresAuth(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	if resp := h.do(t, "GET", "/settings", "", nil); resp.StatusCode != 401 {
		t.Fatalf("status = %d, want 401", resp.StatusCode)
	}
}

func TestSettingsExactWireBody(t *testing.T) {
	topK := (*int)(nil)
	h := newHarness(t, Config{
		SessionTTL:            72 * time.Hour,
		EmbeddingProvider:     "openai",
		LLMProvider:           "openai",
		LLMModel:              "gpt-5.4-nano",
		RetrievalTopK:         topK,
		RefusalScoreThreshold: 0.30,
		CompanyName:           "Amneal",
	})
	h.seedUser(t, testEmail, testPassword, true)
	cookie := h.login(t, testEmail, testPassword)

	resp := h.do(t, "GET", "/settings", cookie, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	raw, _ := io.ReadAll(resp.Body)
	// Byte-exact wire pin (prod values): declaration-order keys, PRESENT null
	// retrieval_top_k, 0.3 float rendering. FastAPI emits the same JSON minus
	// Go's trailing encoder newline.
	want := `{"embedding_provider":"openai","llm_provider":"openai","llm_model":"gpt-5.4-nano",` +
		`"retrieval_top_k":null,"refusal_score_threshold":0.3,"company_name":"Amneal"}` + "\n"
	if string(raw) != want {
		t.Fatalf("settings body = %s, want %s", raw, want)
	}
}

func TestSettingsTopKPresentWhenSet(t *testing.T) {
	topK := 8
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour, RetrievalTopK: &topK})
	h.seedUser(t, testEmail, testPassword, true)
	cookie := h.login(t, testEmail, testPassword)

	body := decode(t, h.do(t, "GET", "/settings", cookie, nil))
	wantKeys(t, body, "embedding_provider", "llm_provider", "llm_model",
		"retrieval_top_k", "refusal_score_threshold", "company_name")
	if body["retrieval_top_k"] != float64(8) {
		t.Fatalf("retrieval_top_k = %v", body["retrieval_top_k"])
	}
}

// --- /products (test_api.py + contract freeze) ---

var productWireKeys = []string{
	"id", "active_ingredient", "normalized_name", "stripped_name", "dosage_form",
	"route", "rld_name", "rld_application_number", "company_status", "source", "source_url",
}

func (h *harness) authed(t *testing.T) string {
	t.Helper()
	h.seedUser(t, testEmail, testPassword, true)
	return h.login(t, testEmail, testPassword)
}

func TestProductsRequireAuth(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	for _, c := range []struct {
		method, path string
		body         any
	}{
		{"GET", "/products", nil},
		{"POST", "/products", map[string]any{"active_ingredient": "Foo", "source": "manual"}},
		{"DELETE", "/products/1", nil},
	} {
		if resp := h.do(t, c.method, c.path, "", c.body); resp.StatusCode != 401 {
			t.Fatalf("%s %s: status = %d, want 401", c.method, c.path, resp.StatusCode)
		}
	}
}

func TestProductCreateListDeleteWire(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	resp := h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "Romidepsin", "source": "manual"})
	if resp.StatusCode != 201 {
		t.Fatalf("create status = %d", resp.StatusCode)
	}
	raw, _ := io.ReadAll(resp.Body)
	// `added` is the inserted-row COUNT (int), never a bool on the wire.
	if !strings.Contains(string(raw), `"added":1`) {
		t.Fatalf("added must serialize as the int 1: %s", raw)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	wantKeys(t, body, "added", "products")
	products := body["products"].([]any)
	if len(products) != 1 {
		t.Fatalf("products = %v", products)
	}
	product := products[0].(map[string]any)
	wantKeys(t, product, productWireKeys...)
	if product["normalized_name"] != "romidepsin" || product["stripped_name"] != "romidepsin" {
		t.Fatalf("computed names: %v", product)
	}

	// Re-adding the same identity merges instead of inserting: added == 0.
	again := decode(t, h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "Romidepsin", "source": "manual"}))
	if again["added"] != float64(0) || len(again["products"].([]any)) != 1 {
		t.Fatalf("re-add: %v", again)
	}

	listing := decode(t, h.do(t, "GET", "/products", cookie, nil))
	wantKeys(t, listing, "count", "products")
	if listing["count"] != float64(1) {
		t.Fatalf("count = %v", listing["count"])
	}
	wantKeys(t, listing["products"].([]any)[0].(map[string]any), productWireKeys...)

	id := int(product["id"].(float64))
	removed := h.do(t, "DELETE", fmt.Sprintf("/products/%d", id), cookie, nil)
	if removed.StatusCode != 200 {
		t.Fatalf("delete status = %d", removed.StatusCode)
	}
	rbody := decode(t, removed)
	wantKeys(t, rbody, "removed", "products")
	if rbody["removed"] != true || len(rbody["products"].([]any)) != 0 {
		t.Fatalf("delete body = %v", rbody)
	}
	if got := decode(t, h.do(t, "GET", "/products", cookie, nil)); got["count"] != float64(0) {
		t.Fatalf("count after delete = %v", got["count"])
	}
	// Idempotent: the kept, already-unwatched row still answers removed=true
	// (the deleted pytest pinned the BODY of the second delete, not just the
	// status -- the caller's goal state holds, never a 404 or removed=false).
	resp2 := h.do(t, "DELETE", fmt.Sprintf("/products/%d", id), cookie, nil)
	if resp2.StatusCode != 200 {
		t.Fatalf("re-delete status = %d", resp2.StatusCode)
	}
	if body := decode(t, resp2); body["removed"] != true {
		t.Fatalf("re-delete body = %v, want removed=true", body)
	}
	// The row itself survives (soft delete, INV-4).
	var onWatchlist bool
	if err := h.pool.QueryRow(t.Context(),
		`SELECT on_watchlist FROM public.product WHERE id = $1`, id).Scan(&onWatchlist); err != nil {
		t.Fatalf("row must survive delete: %v", err)
	}
	if onWatchlist {
		t.Fatal("on_watchlist must be false after delete")
	}
}

func TestProductCreateAllFieldsRoundTrip(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	payload := map[string]any{
		"active_ingredient":      "Romidepsin",
		"dosage_form":            "Injection",
		"route":                  "Intravenous",
		"rld_name":               "Istodax",
		"rld_application_number": "208574",
		"company_status":         "approved",
		"source":                 "anda_letter",
		"source_url":             "file://internal/approval.pdf",
	}
	if resp := h.do(t, "POST", "/products", cookie, payload); resp.StatusCode != 201 {
		t.Fatalf("create status = %d", resp.StatusCode)
	}
	listing := decode(t, h.do(t, "GET", "/products", cookie, nil))
	p := listing["products"].([]any)[0].(map[string]any)
	for key, want := range payload {
		if p[key] != want {
			t.Fatalf("%s = %v, want %v", key, p[key], want)
		}
	}
}

func TestProductCreateBlankIngredientAndPadding(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	for _, bad := range []string{"", "   ", "\t\n"} {
		resp := h.do(t, "POST", "/products", cookie,
			map[string]any{"active_ingredient": bad, "source": "manual"})
		if resp.StatusCode != 422 {
			t.Fatalf("blank %q: status = %d, want 422", bad, resp.StatusCode)
		}
	}
	// Padded-but-real names are stored stripped, not rejected.
	if resp := h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "  Padded Name  ", "source": "manual"}); resp.StatusCode != 201 {
		t.Fatalf("padded: status = %d", resp.StatusCode)
	}
	listing := decode(t, h.do(t, "GET", "/products", cookie, nil))
	if got := listing["products"].([]any)[0].(map[string]any)["active_ingredient"]; got != "Padded Name" {
		t.Fatalf("stored ingredient = %v, want stripped", got)
	}
}

func TestProductCreateFieldLimits(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)
	for name, payload := range map[string]map[string]any{
		"active_ingredient>200": {"active_ingredient": strings.Repeat("x", 201), "source": "manual"},
		"appl_no>40":            {"active_ingredient": "Foo", "rld_application_number": strings.Repeat("9", 41), "source": "manual"},
		"source_url>2000":       {"active_ingredient": "Foo", "source": "manual", "source_url": "https://" + strings.Repeat("x", 2000)},
		"missing source":        {"active_ingredient": "Foo"},
	} {
		resp := h.do(t, "POST", "/products", cookie, payload)
		if resp.StatusCode != 422 {
			t.Fatalf("%s: status = %d, want 422", name, resp.StatusCode)
		}
	}
}

func TestProductCreateINV5SourceGate(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	resp := h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "Foo", "source": "model_memory"})
	if resp.StatusCode != 422 {
		t.Fatalf("bad source: status = %d", resp.StatusCode)
	}
	if body := decode(t, resp); body["detail"] != "source must be one of ['anda_letter', 'manual'] (INV-5)" {
		t.Fatalf("detail = %v", body["detail"])
	}

	resp = h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "Foo", "source": "drugsfda"})
	if resp.StatusCode != 422 {
		t.Fatalf("drugsfda: status = %d", resp.StatusCode)
	}
	want := "source 'drugsfda' is machine-verified provenance: those rows come only " +
		"from the automated Drugs@FDA import, never manual entry (INV-5). " +
		"Use one of ['anda_letter', 'manual']."
	if body := decode(t, resp); body["detail"] != want {
		t.Fatalf("drugsfda detail = %v", body["detail"])
	}
	// Nothing persisted under the fabricated provenance.
	if listing := decode(t, h.do(t, "GET", "/products", cookie, nil)); listing["count"] != float64(0) {
		t.Fatalf("count = %v, want 0", listing["count"])
	}
}

func TestProductIdentityMatchingAndMerge(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)
	add := func(payload map[string]any) float64 {
		t.Helper()
		resp := h.do(t, "POST", "/products", cookie, payload)
		if resp.StatusCode != 201 {
			t.Fatalf("create %v: status = %d", payload, resp.StatusCode)
		}
		return decode(t, resp)["added"].(float64)
	}

	// Case-insensitive form identity: TABLET merges into Tablet.
	if got := add(map[string]any{"active_ingredient": "Foo", "dosage_form": "Tablet", "source": "manual"}); got != 1 {
		t.Fatalf("first insert added = %v", got)
	}
	if got := add(map[string]any{"active_ingredient": "Foo", "dosage_form": "TABLET", "source": "manual"}); got != 0 {
		t.Fatalf("case-mismatch form must merge, added = %v", got)
	}
	// Modifier forms and unknown (null) forms are DISTINCT identities.
	if got := add(map[string]any{"active_ingredient": "Foo", "dosage_form": "Tablet ER", "source": "manual"}); got != 1 {
		t.Fatalf("distinct form must insert, added = %v", got)
	}
	if got := add(map[string]any{"active_ingredient": "Foo", "source": "manual"}); got != 1 {
		t.Fatalf("null form vs Tablet must insert, added = %v", got)
	}
	// Name identity goes through canonicalName: order/case/separator variants
	// of a combo merge into one row.
	if got := add(map[string]any{"active_ingredient": "Hydrocodone Bitartrate and Acetaminophen", "source": "manual"}); got != 1 {
		t.Fatalf("combo insert added = %v", got)
	}
	if got := add(map[string]any{"active_ingredient": "ACETAMINOPHEN; HYDROCODONE BITARTRATE", "source": "manual"}); got != 0 {
		t.Fatalf("canonical-equal combo must merge, added = %v", got)
	}
	// Application number is part of the identity (NULL matches only NULL).
	if got := add(map[string]any{"active_ingredient": "Bar", "rld_application_number": "111111", "source": "manual"}); got != 1 {
		t.Fatalf("appl insert added = %v", got)
	}
	if got := add(map[string]any{"active_ingredient": "Bar", "rld_application_number": "222222", "source": "manual"}); got != 1 {
		t.Fatalf("different appl_no must insert, added = %v", got)
	}
	if got := add(map[string]any{"active_ingredient": "Bar", "rld_application_number": "111111", "source": "manual"}); got != 0 {
		t.Fatalf("same appl_no must merge, added = %v", got)
	}
}

func TestProductMergeTrustGate(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)
	get := func() map[string]any {
		t.Helper()
		return decode(t, h.do(t, "GET", "/products", cookie, nil))["products"].([]any)[0].(map[string]any)
	}

	// Seed an anda_letter row with status set, rld_name empty.
	if resp := h.do(t, "POST", "/products", cookie, map[string]any{
		"active_ingredient": "Foo", "company_status": "approved", "source": "anda_letter",
	}); resp.StatusCode != 201 {
		t.Fatalf("seed: %d", resp.StatusCode)
	}

	// Higher trust (manual) merges: incoming non-empty wins, incoming EMPTY
	// STRING falls through to the existing value (Python `or` falsiness), and
	// the row is relabeled to the higher-trust source.
	resp := h.do(t, "POST", "/products", cookie, map[string]any{
		"active_ingredient": "Foo", "company_status": "", "rld_name": "Istodax", "source": "manual",
	})
	if resp.StatusCode != 201 || decode(t, resp)["added"] != float64(0) {
		t.Fatalf("manual merge failed: %d", resp.StatusCode)
	}
	p := get()
	if p["source"] != "manual" || p["rld_name"] != "Istodax" || p["company_status"] != "approved" {
		t.Fatalf("after manual merge: %v", p)
	}

	// Lower trust (anda_letter onto a manual row) may only FILL empty fields:
	// status stays, source label stays, but the empty source_url fills.
	resp = h.do(t, "POST", "/products", cookie, map[string]any{
		"active_ingredient": "Foo", "company_status": "discontinued",
		"source_url": "file://letter.pdf", "source": "anda_letter",
	})
	if resp.StatusCode != 201 || decode(t, resp)["added"] != float64(0) {
		t.Fatalf("anda merge failed: %d", resp.StatusCode)
	}
	p = get()
	if p["source"] != "manual" || p["company_status"] != "approved" || p["source_url"] != "file://letter.pdf" {
		t.Fatalf("after lower-trust merge: %v", p)
	}
}

func TestProductMergeRewatchesUnwatchedRow(t *testing.T) {
	// The import-refresh model: re-asserting an identity that was soft-deleted
	// flips it back on the watchlist instead of minting a duplicate row.
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	created := decode(t, h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "Foo", "source": "manual"}))
	id := int(created["products"].([]any)[0].(map[string]any)["id"].(float64))
	if resp := h.do(t, "DELETE", fmt.Sprintf("/products/%d", id), cookie, nil); resp.StatusCode != 200 {
		t.Fatalf("delete: %d", resp.StatusCode)
	}
	body := decode(t, h.do(t, "POST", "/products", cookie,
		map[string]any{"active_ingredient": "Foo", "source": "manual"}))
	if body["added"] != float64(0) {
		t.Fatalf("re-add of unwatched identity must merge, added = %v", body["added"])
	}
	products := body["products"].([]any)
	if len(products) != 1 || int(products[0].(map[string]any)["id"].(float64)) != id {
		t.Fatalf("row must be re-watched, not duplicated: %v", products)
	}
}

func TestProductListOrderingAndEmptyShape(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	// Empty watchlist: products is [] (never null), count 0.
	resp := h.do(t, "GET", "/products", cookie, nil)
	raw, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(raw), `"products":[]`) {
		t.Fatalf("empty list body = %s", raw)
	}
	for _, name := range []string{"Charlie", "Alpha", "Bravo"} {
		if resp := h.do(t, "POST", "/products", cookie,
			map[string]any{"active_ingredient": name, "source": "manual"}); resp.StatusCode != 201 {
			t.Fatalf("create %s: %d", name, resp.StatusCode)
		}
	}
	// id ASC == insertion order (the PR-A deterministic refinement).
	listing := decode(t, h.do(t, "GET", "/products", cookie, nil))
	var names []string
	lastID := -1
	for _, p := range listing["products"].([]any) {
		m := p.(map[string]any)
		names = append(names, m["active_ingredient"].(string))
		id := int(m["id"].(float64))
		if id <= lastID {
			t.Fatalf("ids not ascending: %v", listing["products"])
		}
		lastID = id
	}
	if strings.Join(names, ",") != "Charlie,Alpha,Bravo" {
		t.Fatalf("order = %v", names)
	}
}

func TestProductDeleteEdges(t *testing.T) {
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	cookie := h.authed(t)

	// Never-existed id: 404 with the pinned detail.
	resp := h.do(t, "DELETE", "/products/999999", cookie, nil)
	if resp.StatusCode != 404 {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
	if body := decode(t, resp); body["detail"] != "product not found" {
		t.Fatalf("detail = %v", body["detail"])
	}
	// Out-of-int32 id: Python accepts the int and 404s on lookup.
	if resp := h.do(t, "DELETE", "/products/1099511627776", cookie, nil); resp.StatusCode != 404 {
		t.Fatalf("big id status = %d, want 404", resp.StatusCode)
	}
	// Beyond int64 too: Python's int is unbounded, so a 20-digit id is a
	// well-formed integer that finds no row -- 404, never int_parsing.
	if resp := h.do(t, "DELETE", "/products/99999999999999999999", cookie, nil); resp.StatusCode != 404 {
		t.Fatalf("beyond-int64 id status = %d, want 404", resp.StatusCode)
	}
	// Non-integer path: FastAPI int-converter 422 shape.
	resp = h.do(t, "DELETE", "/products/abc", cookie, nil)
	if resp.StatusCode != 422 {
		t.Fatalf("non-int status = %d, want 422", resp.StatusCode)
	}
	raw, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(raw), `"int_parsing"`) || !strings.Contains(string(raw), `["path","product_id"]`) {
		t.Fatalf("non-int body = %s", raw)
	}
}

func TestPRCPathsMethodMismatch405s(t *testing.T) {
	// Since C2 deleted the Python handlers, method mismatches on the PR C
	// paths must NOT relay (the upstream would 404): FastAPI-shaped 405 with
	// the empirically-probed first-match Allow values.
	h := newHarness(t, Config{SessionTTL: 72 * time.Hour})
	for _, c := range []struct{ method, path, allow string }{
		{"PUT", "/products", "GET"},
		{"PATCH", "/products", "GET"},
		{"GET", "/feedback", "POST"},
		{"PUT", "/feedback", "POST"},
		{"POST", "/settings", "GET"},
		{"DELETE", "/settings", "GET"},
		{"GET", "/products/1", "DELETE"},
		{"PATCH", "/products/1", "DELETE"},
	} {
		resp := h.do(t, c.method, c.path, "", nil)
		if resp.StatusCode != 405 {
			t.Fatalf("%s %s: %d, want 405", c.method, c.path, resp.StatusCode)
		}
		if got := resp.Header.Get("Allow"); got != c.allow {
			t.Fatalf("%s %s Allow=%q, want %q", c.method, c.path, got, c.allow)
		}
		if resp.Header.Get("X-Upstream") == "python" {
			t.Fatalf("%s %s leaked to the relay", c.method, c.path)
		}
		if body := decode(t, resp); body["detail"] != "Method Not Allowed" {
			t.Fatalf("405 body: %v", body)
		}
	}
}
