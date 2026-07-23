package api

import (
	"encoding/json"
	"regexp"
	"testing"
)

// Pure-logic unit tests for the step-5 CompleteQuery helpers. DB-free (no
// TEST_DATABASE_URL); the end-to-end behavior is pinned by tests_contract, but
// these catch a helper regression before the full stack boots.

var uuid4Re = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func TestNewUUID4Format(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		id, err := newUUID4()
		if err != nil {
			t.Fatalf("newUUID4: %v", err)
		}
		if !uuid4Re.MatchString(id) {
			t.Fatalf("not a uuid4: %q", id)
		}
		if seen[id] {
			t.Fatalf("duplicate uuid: %q", id)
		}
		seen[id] = true
	}
}

func TestIsScalarJSON(t *testing.T) {
	cases := map[string]bool{
		`"tablet"`: true,
		`5`:        true,
		`5.0`:      true,
		`-3`:       true,
		`true`:     true,
		`false`:    true,
		` "x" `:    true, // leading space tolerated
		`{}`:       false,
		`[]`:       false,
		`null`:     false,
		`{"a":1}`:  false,
		`[1,2]`:    false,
	}
	for in, want := range cases {
		if got := isScalarJSON(json.RawMessage(in)); got != want {
			t.Errorf("isScalarJSON(%s) = %v, want %v", in, got, want)
		}
	}
}

func TestWhitelistFilters(t *testing.T) {
	// nil -> nil (absent/null filters stay absent, matching the pydantic validator).
	if got := whitelistFilters(nil); got != nil {
		t.Errorf("whitelistFilters(nil) = %v, want nil", got)
	}

	in := map[string]json.RawMessage{
		"normalized_name": json.RawMessage(`"albuterol"`),
		"dosage_form":     json.RawMessage(`"tablet"`),
		"route":           json.RawMessage(`"oral"`),
		"psg_type":        json.RawMessage(`"final"`),
		"doc_id":          json.RawMessage(`42`),
		"version_id":      json.RawMessage(`7`),       // NOT whitelisted -> dropped (injection guard)
		"source_url":      json.RawMessage(`"x"`),     // legacy key, not whitelisted -> dropped
		"nested":          json.RawMessage(`{"a":1}`), // whitelisted key would still drop non-scalars
	}
	got := whitelistFilters(in)
	wantKeys := []string{"normalized_name", "dosage_form", "route", "psg_type", "doc_id"}
	if len(got) != len(wantKeys) {
		t.Fatalf("kept %d keys, want %d: %v", len(got), len(wantKeys), got)
	}
	for _, k := range wantKeys {
		if _, ok := got[k]; !ok {
			t.Errorf("missing whitelisted key %q", k)
		}
	}
	if _, ok := got["version_id"]; ok {
		t.Error("version_id must be dropped (it disables current-version scoping)")
	}
	// doc_id integer preserved byte-exact (int stays int, not float).
	if string(got["doc_id"]) != "42" {
		t.Errorf("doc_id = %s, want 42", got["doc_id"])
	}

	// A whitelisted key with a non-scalar value is dropped.
	nonScalar := map[string]json.RawMessage{"normalized_name": json.RawMessage(`["a"]`)}
	if len(whitelistFilters(nonScalar)) != 0 {
		t.Error("a non-scalar whitelisted value must be dropped")
	}
}

func TestFiltersRequestVsObject(t *testing.T) {
	// Absent filters: null on the RAG request, {} for stored payloads.
	if got := string(filtersRequestJSON(nil)); got != "null" {
		t.Errorf("filtersRequestJSON(nil) = %s, want null", got)
	}
	if got := string(filtersObjectJSON(nil)); got != "{}" {
		t.Errorf("filtersObjectJSON(nil) = %s, want {}", got)
	}
	kept := map[string]json.RawMessage{"normalized_name": json.RawMessage(`"x"`)}
	if got := string(filtersObjectJSON(kept)); got != `{"normalized_name":"x"}` {
		t.Errorf("filtersObjectJSON(kept) = %s", got)
	}
}

func TestSpliceAuditIDPreservesNullKeys(t *testing.T) {
	// The endpoint body carries the 11 wire keys minus audit_id, incl. explicit
	// nulls (interpretation/reason). Splicing must ADD audit_id and preserve
	// every other key including the nulls -- a typed struct with omitempty (the
	// bug this guards) would silently drop them.
	body := json.RawMessage(`{"answer":"hi","refused":false,"status":"answer","reason":null,"interpretation":null,"clarify":[],"related":[]}`)
	out, err := spliceAuditID(body, 42)
	if err != nil {
		t.Fatalf("spliceAuditID: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatalf("unmarshal spliced: %v", err)
	}
	if string(m["audit_id"]) != "42" {
		t.Errorf("audit_id = %s, want 42", m["audit_id"])
	}
	for _, k := range []string{"answer", "refused", "status", "reason", "interpretation", "clarify", "related"} {
		if _, ok := m[k]; !ok {
			t.Errorf("key %q was dropped by splice", k)
		}
	}
	if string(m["reason"]) != "null" || string(m["interpretation"]) != "null" {
		t.Errorf("null-valued keys not preserved: reason=%s interpretation=%s", m["reason"], m["interpretation"])
	}

	// The -1 sentinel splices as a real JSON number, not a string.
	out2, _ := spliceAuditID(body, -1)
	_ = json.Unmarshal(out2, &m)
	if string(m["audit_id"]) != "-1" {
		t.Errorf("sentinel audit_id = %s, want -1", m["audit_id"])
	}
}
