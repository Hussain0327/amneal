# White-Paper Runs - Phase 2 design (persistence + analyst completion)

**Status:** PROPOSED - design for review, no code written.
**Date:** 2026-07-07
**Depends on:** nothing in Phase 1 (can ship independently), but Phase 1's quality
fixes (form-scoped PSG ask, RLD-labeler SPL pick) make the persisted output worth
completing. Template distribution (Supabase Storage) is specified in section 8
because the server-side docx path consumes it.

## 1. Problem

The populator (`src/regwatch/whitepaper/`) produces a compliant, cited 46-cell
result, but the product around it is a one-shot lookup:

- The result exists only in client state; a page refresh loses it.
- `POST /whitepaper/docx` requires the client to echo the exact result back and
  rejects any modification (`sections_sha256` check, `api/main.py:1235`), so the
  27 analyst cells can never be completed inside the product.
- No run history, no team visibility, no resume, no review workflow. Analysts
  download the .docx and finish in Word, where provenance and audit stop.

Phase 2 turns a run into a durable, org-shared document with an attributed
analyst-input layer, while keeping the generated layer immutable and fingerprinted.

## 2. Compliance model (why this preserves INV-3/INV-5)

Two layers, structurally separated:

| Layer | Table | Mutability | Content |
|---|---|---|---|
| Generated | `whitepaper_run.sections_json` | IMMUTABLE after create; integrity = `sections_sha256` | Everything the populator produced: values, statuses, evidence |
| Analyst | `whitepaper_input` (one row per run+cell) | Editable until finalize | Attributed human text only |

- INV-3 holds structurally: there is NO code path that updates `sections_json`.
  Analyst values live in a different table, are attributed to a `user.id`, and
  render distinctly (section 6). A manual cell's generated `value` stays `None`
  forever; the human answer is visibly human.
- INV-5 holds: evidence/fetched_at in the generated layer are untouched. A
  reopened old draft is honest about freshness (the UI shows "data as of
  <created_at>"); refreshing data = a NEW run, never an in-place mutation.
- The docx fingerprint check MOVES server-side: render reads sections from the
  DB and re-verifies `result_fingerprint(sections_json) == sections_sha256`
  before rendering. A mismatch is server-data corruption -> 500 + Sentry, not a
  client 422. The client-echo dance is deleted.

## 3. Schema (migration `0013_whitepaper_runs`, chained off `0012_watch_runs`)

Follows the house conventions exactly: SQLModel classes mirror the migration
(fresh-Postgres boots via `create_all` and never replays migrations), int
surrogate PKs, `_json_column()` for JSON fields, naive-UTC datetimes, named
constraints in `__table_args__`, plain `sa.JSON()` in the migration, no
`server_default`, additive + reversible (`drop_index`/`drop_table` downgrade).
RLS needs no DDL - the 0011 event trigger + boot sweep cover new tables.

```python
class WhitepaperRun(SQLModel, table=True):
    __tablename__ = "whitepaper_run"
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by_user_id: int = Field(foreign_key="user.id", index=True)
    # Spine keys denormalized for listing/filtering and Phase-3 staleness joins.
    rld_name_input: str
    application_number: str = Field(index=True)   # six digits, normalized
    application_type: str                          # NDA | ANDA | BLA
    ingredient: str
    normalized_name: str = Field(index=True)
    # The full generated payload, immutable after insert.
    spine_json: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
    sections_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=_json_column())
    warnings_json: list[str] = Field(default_factory=list, sa_column=_json_column())
    sections_sha256: str
    # Ties the run to its existing QueryLog audit row (mode="whitepaper").
    source_audit_id: int = Field(index=True)
    # Workflow: draft -> final. Finalize freezes the analyst layer too.
    status: str = Field(default="draft", index=True)
    finalized_at: datetime | None = None
    finalized_by_user_id: int | None = Field(default=None, foreign_key="user.id")

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'final')", name="ck_whitepaper_run_status"),
    )


class WhitepaperInput(SQLModel, table=True):
    """Attributed analyst overlay - one CURRENT value per (run, cell)."""
    __tablename__ = "whitepaper_input"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="whitepaper_run.id", index=True)
    cell_id: str                    # validated in code against template.CELL_SPECS
    value: str                      # non-empty; clearing deletes the row
    author_user_id: int = Field(foreign_key="user.id")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("run_id", "cell_id", name="uq_whitepaper_input_run_cell"),
    )
```

Notes / rejected alternatives:

- **Edit history**: deferred. Updates overwrite in place (author + updated_at
  travel with the row). A `whitepaper_input_history` table is speculative until
  someone asks "who changed this before me" - flagged, not built.
- **Author name**: joined from `user` at read/render time, not snapshotted.
  Users are never deleted (only `is_active=False`), so the join is stable.
- **Stale flag / diff columns**: Phase 3, its own additive migration.
- **Value bound**: `value` capped at 4000 chars (same `_MAX_VALUE_CHARS` bound
  as generated cells) and control characters stripped - validated at the API
  boundary.

## 4. Store module

`src/regwatch/store/whitepaper_runs.py` - free functions per the house pattern
(each opens `session_scope`), writes here, reads here (small enough that a
separate queries module is not warranted):

- `create_run(*, user_id, rld_name_input, result) -> int` - inserts the run from
  a `build_whitepaper` result; called by the API handler in the same request.
- `list_runs(*, limit, offset, application_number=None, normalized_name=None, status=None) -> tuple[list[RunSummary], int]`
  - org-shared (no user filter), newest-updated first, same-filter COUNT for
  `total` (the `watch_latest` pattern). `RunSummary` includes the status counts
  (computed from `sections_json` at write time and stored in the summary query,
  see below) and analyst-progress (`filled` overlay rows vs analyst-required
  cells).
- `get_run(run_id) -> RunDetail | None` - run + overlay rows + author display
  names (single joined query, no N+1).
- `upsert_input(*, run_id, cell_id, value, user_id) -> InputView` /
  `clear_input(*, run_id, cell_id, user_id)` - reject when run is `final`.
- `finalize_run(*, run_id, user_id)` / `reopen_run(*, run_id, user_id)`.
- `delete_run(*, run_id, user_id)` - creator-only, drafts-only; cascades the
  overlay rows (explicit delete, no DB-level cascade, matching house style).

Status counts: computed once in `create_run` from `sections_json` and stored in
`spine_json["counts"]`-adjacent fashion? No - simplest correct: computed in the
list query is O(rows x cells) JSON work; instead `create_run` stores the three
counts as plain int columns on the run (`populated_count`,
`analyst_input_count`, `verified_absent_count`). They describe the immutable
generated layer, so denormalization cannot drift. (Added to the model above at
implementation time - three `int` columns, part of migration 0013.)

## 5. API (all on the `protected` router; POST/GET/DELETE only - CORS has no PATCH)

Every endpoint takes `user: User = Depends(require_user)`. New request/response
shapes are typed Pydantic `response_model`s so `npm run gen:types` yields real
types and the hand-written interfaces in `api.ts` can shrink over time. The
regenerated `api-types.ts` must be committed (CI `frontend-contract` gate).

1. **`POST /whitepaper` (extended, backward compatible)** - after
   `build_whitepaper` succeeds, persist via `create_run` and add `run_id` to the
   response. Existing rate limit + audit unchanged. If the persist fails, the
   response still ships with `run_id: null` and a warning appended (populate is
   the expensive step; losing durability degrades, not fails - same philosophy
   as `_persist`'s snapshot write-through).
2. **`GET /whitepaper/runs`** - org-shared list.
   Query params: `limit` (default 50, le=200), `offset`, optional
   `application_number`, `normalized_name`, `status`.
   Returns `{count, total, limit, offset, runs: [RunSummary]}`.
   `RunSummary`: id, application_number/type, ingredient, status, counts,
   analyst_filled/analyst_required, created_by (display name), created_at,
   updated_at.
3. **`GET /whitepaper/runs/{run_id}`** - full detail: spine, sections (verbatim
   generated payload), warnings, status, created_by, timestamps, and
   `inputs: {cell_id: {value, author, updated_at}}`. 404 for a missing id
   (org-shared means existence is not a secret; the old uniform-422 ownership
   pattern applies only to the legacy per-user audit lookup).
4. **`POST /whitepaper/runs/{run_id}/cells/{cell_id}`** - body
   `{"value": "..."}`; empty/whitespace or `null` clears (deletes the overlay
   row). 409 when the run is final; 422 when `cell_id` is not in
   `template.CELL_SPECS` or the value exceeds the bound. Returns the updated
   input view. No query-rate-limit (pure DB write, no FDA/LLM); body size is
   bounded by validation.
5. **`POST /whitepaper/runs/{run_id}/finalize`** and
   **`POST /whitepaper/runs/{run_id}/reopen`** - flips status, stamps
   finalized_by/at. Finalize re-verifies the stored fingerprint first. Both
   write a `log_query` audit row (mode="whitepaper",
   status="finalized"/"reopened", route_json carries run_id) - consistent with
   `docx_rendered` using QueryLog as the generic audit trail.
6. **`POST /whitepaper/runs/{run_id}/docx`** - server-side render. Loads run +
   overlay, re-verifies `result_fingerprint(sections_json) == sections_sha256`
   (mismatch -> 500 + Sentry: stored-data integrity, not client error), renders
   with the overlay (section 6), returns the attachment with the existing
   Content-Disposition contract. Writes the `docx_rendered` audit row. Keeps
   `_enforce_query_rate_limit` (rendering is CPU-bound docx assembly).
7. **`DELETE /whitepaper/runs/{run_id}`** - creator-only (403 otherwise),
   drafts-only (409 for final runs - a finalized paper is a record).

**Removed:** the legacy `POST /whitepaper/docx` client-echo endpoint. The only
client is our frontend and it migrates in the same PR; keeping both would leave
two render paths to maintain and test. `_validated_docx_result`,
`_require_owned_whitepaper_audit`, and the echo-side fingerprint check go with
it (the fingerprint itself stays, enforced server-side). This is the one
breaking API change; api-types regen covers the contract.

## 6. Docx rendering with the overlay

`write_whitepaper_docx(result, *, template_path, inputs=None)` gains an optional
`inputs: dict[cell_id, InputView]`:

- **analyst_input_required cell + overlay value** -> the value cell renders the
  analyst text suffixed `[analyst: <display name>]`. Checkbox-style cells render
  the marker line as `->  <analyst text> [analyst: <name>]` (appended, same as
  today's marker discipline - never overwriting template checkbox text).
- **populated / verified_absent cell + overlay value** -> the generated value
  and its citations render exactly as today; the analyst text is APPENDED as a
  separate paragraph: `Analyst note (<name>): <text>`. A human note never
  replaces or restyles a cited value, so a reader can always tell sourced fact
  from human judgment. (The template itself anticipates this: the PLR/PLLR
  cells carry "analyst override expected" notes.)
- **Provenance appendix** gains a second table, "Analyst inputs": cell, value,
  author, updated_at. The existing evidence table is unchanged.
- The from-scratch fallback path gets the identical treatment (and keeps the
  visible FALLBACK_MARKER discipline).

The UI mirrors the same rule: analyst cells get an inline editor; populated
cells get an "add note" affordance whose text renders visually distinct from
the cited value.

## 7. Frontend

No new nav entry - the White Paper page (04) becomes the workflow surface.
`?run=<id>` joins `rp`/`appl` as a URL param (the URL is the state of record,
same as Ask's `?session=`):

- **Intake + recent runs**: the existing form on top; below it, a runs list
  (org-shared, from `GET /whitepaper/runs`) modeled on the Watch page's
  guarded-`load()` + Refresh pattern - status chip (draft/final), product,
  counts, analyst progress ("12 of 23"), author, relative time. Clicking a run
  sets `?run=` (scope-preserving, via the `withScope` pattern).
- **Populate** now navigates to the created run (`?run=<id>`) instead of holding
  ephemeral state; a refresh resumes exactly where the analyst was.
- **Run view**: today's sections/cells render, hydrated from
  `GET /whitepaper/runs/{id}` (Ask's `getSession` URL-sync effect is the
  template), plus:
  - inline editor per analyst cell (textarea + explicit Save / Clear; author +
    relative time shown after save),
  - "add note" on populated cells (same editor, rendered as an annotation),
  - a freshness line: "Data as of <created_at> - re-populate to refresh"
    (re-populate = new run; the old one stays),
  - Finalize (confirm dialog) -> freezes editing; Reopen for drafts-gone-wrong;
  - Download .docx -> `POST /whitepaper/runs/{id}/docx` (existing blob+anchor
    helper, LONG_TIMEOUT_MS).
- **api.ts**: `listWhitepaperRuns`, `getWhitepaperRun`, `saveWhitepaperInput`,
  `finalizeWhitepaperRun`, `reopenWhitepaperRun`, `deleteWhitepaperRun`,
  reworked `downloadWhitepaperDocx(runId)`. Types come from the regenerated
  `api-types.ts` where the new Pydantic response models land.
- **Tests**: `test/WhitepaperRunsPage.test.tsx` following the
  `WatchPage.test.tsx` mock/fixture structure (mock `@/lib/api` +
  `CurrentProductProvider` + `next/navigation`); cover list -> open -> edit ->
  finalize-freeze -> download, and the dirty-guard interaction with `?run=`.

## 8. Template distribution (decided: Supabase Storage fetch)

Prod docx renders currently ALWAYS fall back: the template is gitignored, not in
the image, and `fly.toml` has no `[mounts]` (ephemeral disks - an operator
drop-in cannot survive a deploy).

- Upload the official .docx once to a **private** Supabase Storage bucket
  (`regwatch-internal/templates/cra_white_paper_template.docx`).
- New setting `whitepaper_template_url: str | None = None` (env
  `WHITEPAPER_TEMPLATE_URL`, set as a Fly secret) - a long-lived signed URL.
  Rotation = re-sign + update the secret; no SDK, no service key in the app.
- **Lazy fetch-and-cache on first render** (not boot - a storage outage must
  not block boot): if `whitepaper_template_path` is absent and the URL is set,
  fetch with httpx (explicit 10s timeout, size cap ~5 MB, content-type sanity
  check), atomic write (`.tmp` + `rename`) to the template path, then proceed.
  Any failure -> today's loud fallback (warning log + FALLBACK_MARKER document);
  the next render retries.
- **Observability**: `/health` gains `whitepaper_template: "present" | "absent"`
  so a fallback-rendering prod is visible at a glance, and the render path keeps
  logging `whitepaper_template_missing`.

## 9. Testing plan (backend)

- **Migration**: extend the `test_migrate_script` coverage to 0013; verify
  upgrade + downgrade round-trip and that `create_all` (fresh boot) and the
  migration produce identical schemas (existing convergence discipline).
- **Store**: round-trip create/list/get/upsert/clear/finalize/delete; org-shared
  listing (two users see each other's runs); creator-only delete; final freezes
  edits; unique (run_id, cell_id) upsert semantics.
- **API**: every status path per endpoint (401 via router, 404 missing run,
  409 final-frozen + final-delete, 422 bad cell_id/oversized value, 403 foreign
  delete); `run_id` present in the extended `POST /whitepaper` response;
  persist-failure degrades with warning instead of failing the populate.
- **Docx**: overlay on analyst cell renders value + attribution; overlay on
  populated cell APPENDS and never replaces the cited value (assert the
  generated value still present verbatim); analyst-inputs appendix; fallback
  path parity; stored-fingerprint mismatch -> 500 and no document.
- **Invariants** (`test_whitepaper_invariants.py`): no code path mutates
  `sections_json` (the store module exposes no update for it); a manual cell
  with an overlay still carries `value=None` in the generated layer; the
  fingerprint of a run's sections never changes across overlay edits.
- **Template fetch**: URL unset -> unchanged behavior; fetch success caches and
  fills real template; fetch failure -> fallback marker + retry on next render;
  size/content-type rejection.
- Run the full gate (ruff/black/mypy over `src tests migrations`, pytest, and
  `npm run gen:types` diff check + `vitest run`) before handing over.

## 10. Rollout / risk

- Migration 0013 is additive-only and reversible; `create_table` takes no locks
  on existing tables; RLS auto-applies (0011 trigger). Fly `release_command`
  runs it before the new code serves traffic.
- The one breaking change (legacy `/whitepaper/docx` removal) ships in the same
  deploy as the frontend that stops calling it; there are no external API
  consumers in the pilot.
- Persist-on-populate adds one transaction to the populate path; failure is
  non-fatal by design.
- Org-shared visibility is a product decision (decided): any authenticated
  analyst can read all runs and edit analyst cells; deletes stay creator-only;
  finalize/reopen are open to any analyst and audit-logged. Tightening later
  (roles) has a seam: the `user.role` column already exists, unused.

## 11. Explicitly deferred (Phase 3)

Batch populate (candidate list -> DB job queue worked by the existing cron
pattern), watch-driven staleness (cross-ref saved runs against changed PSG/OB/
SPL snapshots -> stale flag + Slack digest line), run-to-run section diff, and
overlay edit history.
