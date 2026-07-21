-- Product (org-shared watchlist) store surface for /products CRUD.
-- Mirrors src/regwatch/watch/watchlist.py semantics: DELETE is a SOFT delete
-- (on_watchlist=false) because alert history references product_id (INV-4);
-- the INV-5 source allowlist (user-assertable sources only) is handler
-- policy, not a query concern. List ordering: Python's list_watchlist() has
-- NO ORDER BY (unspecified heap order), so id ASC here is a deliberate
-- deterministic refinement -- PR C's contract tests pin it as the NEW order.
--
-- POST /products is upsert_entries (watchlist.py): ListProductsByIdentity
-- fetches candidates, the HANDLER does the casefolded form/route identity
-- comparison and the source-rank-gated field merge (Python's `or` treats
-- empty string as missing -- SQL COALESCE only handles NULL, so the merged
-- values must be computed in code), then MergeProductIdentityFields or
-- CreateProduct. Porting POST onto bare CreateProduct would mint duplicate
-- identity rows and always report added=1.

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

-- The upsert_entries candidate select: the EXACT two-column identity Python's
-- SQLAlchemy emits (== None compiles to IS NULL, so IS NOT DISTINCT FROM
-- reproduces both branches). Form/route filtering happens in the handler
-- (casefold + whitespace-collapse; None matches only None). ORDER BY id ASC
-- is a determinism refinement: Python takes the first of unordered results.
-- name: ListProductsByIdentity :many
SELECT * FROM public.product
WHERE normalized_name = sqlc.arg(normalized_name)
  AND rld_application_number IS NOT DISTINCT FROM sqlc.narg(rld_application_number)
ORDER BY id ASC;

-- The merge half of upsert_entries: final values are computed in the handler
-- (rank gating + empty-string-falsy semantics), this just writes them.
-- active_ingredient/normalized_name/dosage_form/route/added_at are NEVER
-- touched on merge; on_watchlist always re-sets true (import-refresh model).
-- name: MergeProductIdentityFields :exec
UPDATE public.product
SET company_status = $2,
    rld_name = $3,
    source = $4,
    source_url = $5,
    on_watchlist = true
WHERE id = $1;
