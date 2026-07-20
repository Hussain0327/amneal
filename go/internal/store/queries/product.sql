-- Product (org-shared watchlist) store surface for /products CRUD.
-- Mirrors src/regwatch/watch/watchlist.py semantics: DELETE is a SOFT delete
-- (on_watchlist=false) because alert history references product_id (INV-4);
-- the INV-5 source allowlist (user-assertable sources only) is handler
-- policy, not a query concern. List ordering: Python's list_watchlist() has
-- NO ORDER BY (unspecified heap order), so id ASC here is a deliberate
-- deterministic refinement -- PR C's contract tests pin it as the NEW order.
--
-- PR C NOTE: CreateProduct alone does NOT implement POST /products. The
-- Python handler runs watchlist.upsert_entries -- an identity MERGE matching
-- on (normalized_name, rld_application_number) with NULL-safe comparison
-- (IS NOT DISTINCT FROM) and a source-rank-gated field merge. PR C adds that
-- candidate-select + merge-update pair; porting POST onto bare CreateProduct
-- would mint duplicate identity rows and always report added=1.

-- name: ListWatchlistProducts :many
SELECT * FROM public.product
WHERE on_watchlist
ORDER BY id ASC;

-- name: GetProduct :one
SELECT * FROM public.product
WHERE id = $1;

-- name: CreateProduct :one
INSERT INTO public.product (
  active_ingredient, normalized_name, dosage_form, route, rld_name,
  rld_application_number, company_status, source, source_url,
  on_watchlist, added_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING *;

-- name: SetProductWatchlist :execrows
UPDATE public.product
SET on_watchlist = $2
WHERE id = $1;
