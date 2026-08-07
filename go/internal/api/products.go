package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/Hussain0327/amneal/go/internal/store"
)

// INV-5 at the API boundary (mirrors main.py::USER_ASSERTABLE_SOURCES):
// POST /products is a HUMAN assertion, so it may only claim provenance a
// human can stand behind. "drugsfda" means machine-verified against the
// automated Drugs@FDA import; accepting it on a hand-typed row would
// fabricate that verification. Explicit literal so a future machine source
// fails CLOSED by default.
var userAssertableSources = map[string]bool{"manual": true, "anda_letter": true}

// sourceRank mirrors watchlist.py::_SOURCE_RANK -- the trust hierarchy the
// upsert merge is gated on. Unknown sources rank 0 (map zero value ==
// _SOURCE_RANK.get(x, 0)).
var sourceRank = map[string]int{"manual": 3, "anda_letter": 2, "drugsfda": 1}

// productRecord is one watchlist row as watchlist.py::list_watchlist projects
// it (= ProductRecord in main.py), fields in wire order. stripped_name is
// computed at read time from active_ingredient; it is not a DB column.
type productRecord struct {
	ID                   int32   `json:"id"`
	ActiveIngredient     string  `json:"active_ingredient"`
	NormalizedName       string  `json:"normalized_name"`
	StrippedName         string  `json:"stripped_name"`
	DosageForm           *string `json:"dosage_form"`
	Route                *string `json:"route"`
	RldName              *string `json:"rld_name"`
	RldApplicationNumber *string `json:"rld_application_number"`
	CompanyStatus        *string `json:"company_status"`
	Source               string  `json:"source"`
	SourceURL            *string `json:"source_url"`
}

func productRecordFromRow(p store.Product) productRecord {
	return productRecord{
		ID:                   p.ID,
		ActiveIngredient:     p.ActiveIngredient,
		NormalizedName:       p.NormalizedName,
		StrippedName:         strippedName(p.ActiveIngredient),
		DosageForm:           textPtr(p.DosageForm),
		Route:                textPtr(p.Route),
		RldName:              textPtr(p.RldName),
		RldApplicationNumber: textPtr(p.RldApplicationNumber),
		CompanyStatus:        textPtr(p.CompanyStatus),
		Source:               p.Source,
		SourceURL:            textPtr(p.SourceUrl),
	}
}

// listProductRecords reads the current watchlist OUTSIDE any handler
// transaction, matching Python's list_watchlist (a fresh session after the
// upsert's session_scope committed). Always returns a non-nil slice: an empty
// watchlist serializes as [], never null.
func (s *Server) listProductRecords(ctx context.Context) ([]productRecord, error) {
	rows, err := s.q.ListWatchlistProducts(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]productRecord, 0, len(rows))
	for _, p := range rows {
		out = append(out, productRecordFromRow(p))
	}
	return out, nil
}

type productsResponse struct {
	Count    int             `json:"count"`
	Products []productRecord `json:"products"`
}

// handleListProducts ports main.py::list_products. Ordering is the PR-A
// deliberate refinement: id ASC (Python's list_watchlist had no ORDER BY).
func (s *Server) handleListProducts(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.currentUser(w, r); !ok {
		return
	}
	products, err := s.listProductRecords(r.Context())
	if err != nil {
		s.internalError(w, "list products", err)
		return
	}
	writeJSON(w, http.StatusOK, productsResponse{Count: len(products), Products: products})
}

// orStr is Python's `a or b` over optional strings: empty string is FALSY, so
// a present-but-empty value falls through to b exactly like None does. The
// upsert merge below depends on this (SQL COALESCE only handles NULL, which
// is why the merged values are computed here and not in the query).
func orStr(a, b *string) *string {
	if a != nil && *a != "" {
		return a
	}
	return b
}

func tooLong(field string, v *string, maxRunes int) *validationItem {
	if v != nil && utf8.RuneCountInString(*v) > maxRunes {
		return &validationItem{Type: "string_too_long", Loc: []string{"body", field},
			Msg: fmt.Sprintf("String should have at most %d characters", maxRunes)}
	}
	return nil
}

type productCreateResponse struct {
	// Rows actually inserted by the upsert -- an int on the wire, not a bool
	// (0 when the entry matched an existing row and was merged instead).
	Added    int             `json:"added"`
	Products []productRecord `json:"products"`
}

// handleCreateProduct ports main.py::create_product + the single-entry path
// of watchlist.py::upsert_entries (add_manual_product). Identity match =
// normalized_name + rld_application_number in SQL (IS NOT DISTINCT FROM),
// then casefolded dosage_form/route comparison here; on match, the
// source-rank trust gate decides merge direction (INV-5 covers the DATA
// fields too: a lower-trust source may only FILL empty fields, never
// overwrite, and never relabels the row). Candidate select + merge-or-insert
// run in ONE transaction, like Python's session_scope.
func (s *Server) handleCreateProduct(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.currentUser(w, r); !ok {
		return
	}
	var body struct {
		ActiveIngredient     *string `json:"active_ingredient"`
		DosageForm           *string `json:"dosage_form"`
		Route                *string `json:"route"`
		RldName              *string `json:"rld_name"`
		RldApplicationNumber *string `json:"rld_application_number"`
		CompanyStatus        *string `json:"company_status"`
		Source               *string `json:"source"`
		SourceURL            *string `json:"source_url"`
	}
	if !decodeStrictJSON(w, r, &body) {
		return
	}

	// ProductCreate mirrors, in field-declaration order. Per pydantic
	// semantics the length constraints see the RAW value and the strip-then-
	// nonblank validator only runs when they pass; max lengths count code
	// points, not bytes. Deliberate divergence: an explicit null for a
	// required field is indistinguishable from an absent key here, so it
	// reports "missing" where pydantic reports string_type (422 both ways).
	var problems []validationItem
	var ingredient string
	if body.ActiveIngredient == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "active_ingredient"}, Msg: "Field required"})
	} else if utf8.RuneCountInString(*body.ActiveIngredient) < 1 {
		problems = append(problems, validationItem{Type: "string_too_short", Loc: []string{"body", "active_ingredient"}, Msg: "String should have at least 1 character"})
	} else if p := tooLong("active_ingredient", body.ActiveIngredient, 200); p != nil {
		problems = append(problems, *p)
	} else if ingredient = strings.TrimSpace(*body.ActiveIngredient); ingredient == "" {
		// A whitespace-only name normalizes to "" -- permanently unmatchable
		// junk (DELETE only soft-unwatches; the row is kept forever for
		// alert-history integrity). Reject at the boundary; store stripped.
		problems = append(problems, validationItem{Type: "value_error", Loc: []string{"body", "active_ingredient"}, Msg: "Value error, active_ingredient must not be blank"})
	}
	for _, f := range []struct {
		name string
		v    *string
		max  int
	}{
		{"dosage_form", body.DosageForm, 200},
		{"route", body.Route, 200},
		{"rld_name", body.RldName, 200},
		{"rld_application_number", body.RldApplicationNumber, 40},
		{"company_status", body.CompanyStatus, 200},
	} {
		if p := tooLong(f.name, f.v, f.max); p != nil {
			problems = append(problems, *p)
		}
	}
	if body.Source == nil {
		problems = append(problems, validationItem{Type: "missing", Loc: []string{"body", "source"}, Msg: "Field required"})
	} else if p := tooLong("source", body.Source, 200); p != nil {
		problems = append(problems, *p)
	}
	if p := tooLong("source_url", body.SourceURL, 2000); p != nil {
		problems = append(problems, *p)
	}
	if len(problems) > 0 {
		writeValidationError(w, problems...)
		return
	}

	// INV-5 gate, AFTER shape validation like the Python handler (it runs in
	// the endpoint body). STRING detail, not the pydantic array shape --
	// this is an HTTPException in Python, and the drugsfda wording is pinned
	// by the pytest suite ("automated" in detail).
	if !userAssertableSources[*body.Source] {
		var detail string
		if *body.Source == "drugsfda" {
			detail = "source 'drugsfda' is machine-verified provenance: those rows " +
				"come only from the automated Drugs@FDA import, never manual " +
				"entry (INV-5). Use one of ['anda_letter', 'manual']."
		} else {
			detail = "source must be one of ['anda_letter', 'manual'] (INV-5)"
		}
		writeDetail(w, http.StatusUnprocessableEntity, detail)
		return
	}

	normalized := canonicalName(ingredient)
	ctx := r.Context()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		s.internalError(w, "create product begin", err)
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	qtx := s.q.WithTx(tx)

	candidates, err := qtx.ListProductsByIdentity(ctx, store.ListProductsByIdentityParams{
		NormalizedName:       normalized,
		RldApplicationNumber: textOrNull(body.RldApplicationNumber),
	})
	if err != nil {
		s.internalError(w, "product identity select", err)
		return
	}
	var matched *store.Product
	for i := range candidates {
		c := &candidates[i]
		if identityAttrEqual(textPtr(c.DosageForm), body.DosageForm) &&
			identityAttrEqual(textPtr(c.Route), body.Route) {
			matched = c // first match, like Python's existing[0]
			break
		}
	}

	added := 0
	if matched != nil {
		src := matched.Source
		var companyStatus, rldName, sourceURL *string
		if sourceRank[*body.Source] >= sourceRank[matched.Source] {
			// Equal-or-higher trust: incoming values win where present.
			companyStatus = orStr(body.CompanyStatus, textPtr(matched.CompanyStatus))
			rldName = orStr(body.RldName, textPtr(matched.RldName))
			src = *body.Source
			sourceURL = orStr(body.SourceURL, textPtr(matched.SourceUrl))
		} else {
			// Lower trust may only FILL empty fields; the row keeps its
			// trusted source label.
			companyStatus = orStr(textPtr(matched.CompanyStatus), body.CompanyStatus)
			rldName = orStr(textPtr(matched.RldName), body.RldName)
			sourceURL = orStr(textPtr(matched.SourceUrl), body.SourceURL)
		}
		if err := qtx.MergeProductIdentityFields(ctx, store.MergeProductIdentityFieldsParams{
			ID:            matched.ID,
			CompanyStatus: textOrNull(companyStatus),
			RldName:       textOrNull(rldName),
			Source:        src,
			SourceUrl:     textOrNull(sourceURL),
		}); err != nil {
			s.internalError(w, "product merge", err)
			return
		}
	} else {
		if _, err := qtx.CreateProduct(ctx, store.CreateProductParams{
			ActiveIngredient:     ingredient,
			NormalizedName:       normalized,
			DosageForm:           textOrNull(body.DosageForm),
			Route:                textOrNull(body.Route),
			RldName:              textOrNull(body.RldName),
			RldApplicationNumber: textOrNull(body.RldApplicationNumber),
			CompanyStatus:        textOrNull(body.CompanyStatus),
			Source:               *body.Source,
			SourceUrl:            textOrNull(body.SourceURL),
			OnWatchlist:          true,
			AddedAt:              ts(s.now()),
		}); err != nil {
			s.internalError(w, "product insert", err)
			return
		}
		added = 1
	}
	if err := tx.Commit(ctx); err != nil {
		s.internalError(w, "create product commit", err)
		return
	}

	products, err := s.listProductRecords(ctx)
	if err != nil {
		s.internalError(w, "list products after create", err)
		return
	}
	writeJSON(w, http.StatusCreated, productCreateResponse{Added: added, Products: products})
}

type productDeleteResponse struct {
	Removed  bool            `json:"removed"`
	Products []productRecord `json:"products"`
}

// handleDeleteProduct ports main.py::delete_product: SOFT delete
// (on_watchlist=false, row kept -- durable alerts reference product_id,
// INV-4, and the row's INV-5 provenance survives for audit). Idempotent:
// re-deleting an already-unwatched row still returns removed=true (the UPDATE
// matches the row either way); 404 is reserved for ids no Product row ever
// had.
func (s *Server) handleDeleteProduct(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.currentUser(w, r); !ok {
		return
	}
	id64, err := strconv.ParseInt(r.PathValue("product_id"), 10, 32)
	if err != nil {
		// product.id is a 32-bit integer, so a syntactically valid integer
		// beyond int32 is ErrRange, not a parse failure: Python's unbounded int
		// accepts it and 404s on lookup, so mirror that instead of misreporting
		// it as unparseable (and the int32 conversion below can never wrap).
		if errors.Is(err, strconv.ErrRange) {
			writeDetail(w, http.StatusNotFound, "product not found")
			return
		}
		// FastAPI's int path converter -> pydantic int_parsing item.
		writeValidationError(w, validationItem{Type: "int_parsing", Loc: []string{"path", "product_id"},
			Msg: "Input should be a valid integer, unable to parse string as an integer"})
		return
	}
	rows, err := s.q.SetProductWatchlist(r.Context(), store.SetProductWatchlistParams{
		ID: int32(id64), OnWatchlist: false,
	})
	if err != nil {
		s.internalError(w, "product unwatch", err)
		return
	}
	if rows == 0 {
		writeDetail(w, http.StatusNotFound, "product not found")
		return
	}
	products, err := s.listProductRecords(r.Context())
	if err != nil {
		s.internalError(w, "list products after delete", err)
		return
	}
	writeJSON(w, http.StatusOK, productDeleteResponse{Removed: true, Products: products})
}
