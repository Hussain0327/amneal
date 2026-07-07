# RegWatch Backend Refactor Backlog — Final Synthesis (2026-07-07)

## 1. Executive summary

The backend is in good shape: zero architectural rot, the compliance machinery (INV-1..9, tri-state absence, refuse-over-guess) is intact and consistently applied, and every confirmed finding is small and behavior-preserving. Three themes dominate: (a) **copy-paste plumbing duplication** — the same httpx client factory, owned-client pattern, endpoint URL, regexes, and product-id coercion each written 2-4 times instead of reusing the shared helper that already exists; (b) **inconsistent application of the codebase's own proven failure-path patterns** — the highest-severity item (`ensure_schema()`'s unprotected `CREATE INDEX` on the hot `chunk` table, spot-re-verified today) re-exposes the exact lock-pileup class that wedged prod on 2026-06-18, and three other spots skip the `_log_query_or_skip` / `error_type` / logged-degrade conventions their sibling functions follow; (c) **dead pre-refactor leftovers and doc drift** (two dead functions, a dead script, a factually false INV-7/8 claim in the README, an unshipped Dagster closure baked into the prod image). Nothing here requires new abstractions or dependencies — nearly every fix is "copy the pattern already proven two functions away."

## 2. Do now (after the in-flight build lands)

Ordered by score desc, then size asc. Items marked **[ACTIVE-BUILD]** touch files the concurrent workflow owns — re-verify line anchors and surrounding code against the landed build before applying; do not apply concurrently.

### 2.1 (score 9, S/low) ensure_schema()'s CREATE INDEX on `chunk` is not lock-safe
`src/regwatch/store/pgvector_store.py:223` — 5 `CREATE INDEX IF NOT EXISTS` statements on the hot `chunk` table run in a bare `engine.begin()` with no `SET LOCAL lock_timeout` and no `OperationalError` handling, while `_enable_chunk_rls` five lines below and db.py's `_ensure_postgres_objects` (twin DDL, same table) both use the lock-safe skip pattern created after the 2026-06-18 prod lock-pileup incident. The path is live on every cold boot: `_ensure_ready()` fires on the first `similarity_search`/`add_chunks`, redundantly re-attempting DDL that `init_db()` already did lock-safely — `CREATE INDEX IF NOT EXISTS` still takes a ShareLock even as a no-op, so a first request racing a concurrent ingest writer surfaces as an uncaught 500 instead of a logged skip. **Proposal:** wrap each statement with the same per-statement `SET LOCAL lock_timeout='3s'` + `except OperationalError: log.warning(...); break` pattern already in `_enable_chunk_rls`/db.py; add a contention test mirroring `test_ensure_postgres_objects_degrades_gracefully_on_contended_lock`. Reduces a proven, incident-backed hazard. touches_active_build=false.

### 2.2 (score 8, S/low) Consolidate the 4x-duplicated httpx.Client factory *(merges the score-7 rems.py:100 finding)*
`src/regwatch/sources/_utils.py:112` (canonical), duplicated at `orange_book.py:340`, `rems.py:100`, `dailymed.py:515-517` — the timeout/User-Agent policy for every non-DB source fetch is byte-identical in four places; a future policy change silently misses untouched copies. **Proposal:** swap orange_book.py and rems.py's inline lambdas for `owned_client(client, get_openfda_client)` (the pattern already used at `_utils.py:147`); **must also** delete the now-dead `s = get_settings()` locals in both files and rems.py's now-unused `get_settings` import, or ruff F841/F401 fails CI. Defer dailymed.py's `_dailymed_client` swap until the whitepaper build lands (that file is active-build). Optional ride-along: rename to something non-openFDA-specific. touches_active_build=false (dailymed portion deferred).

### 2.3 (score 8, S/low) Delete dead `_retention_at()` in threshold_sweep
`src/regwatch/eval/threshold_sweep.py:219` — superseded by `_point_at` (line 248, what `recommend()` actually calls); grep re-confirmed today: only the definition exists, zero call sites in src or tests. **Proposal:** delete lines 219-224. Six-line mechanical dead-code removal. touches_active_build=false.

### 2.4 (score 8, S/low) Delete dead `_cached_products_text()` in orange_book
`src/regwatch/sources/orange_book.py:291` — pre-refactor leftover superseded by `_cached_parsed`/`product_rows`; grep re-confirmed today: zero callers anywhere. **Proposal:** delete the two-line function. touches_active_build=false.

### 2.5 (score 8, S/low) shortages.py reimplements `first_str` without clean_text normalization
`src/regwatch/sources/shortages.py:86` — single-use `_first_openfda_value` duplicates `first_str`'s list-unwrap but skips `clean_text()`, leaving shortages' `application_number` the only un-normalized identifier across handlers. **Proposal:** replace the sole call site (line 63) with `first_str(row.get("openfda") or {}, "application_number")`, delete the helper. Matches sibling drugsfda.py's trust-the-shape idiom; router.py's per-handler except bounds any edge-case blast radius. touches_active_build=false.

### 2.6 (score 8, S/low) psg_crawler hand-rolls the owned-client pattern 3x
`src/regwatch/ingest/psg_crawler.py:130` (also 179-213, 388-420) — three copies of the manual `owned = False / try+finally: client.close()` dance that `sources/_utils.owned_client` (tested, used by all six source handlers) already implements; no circular import (verified: nothing in sources imports ingest). **Proposal:** `from regwatch.sources._utils import owned_client` + three `with owned_client(client, factory):` blocks (two small local factories for the distinct HTML/PDF header sets, mirroring dailymed's pattern). Removes ~15 duplicated lines; download_pdf's owned path is already test-covered. touches_active_build=false.

### 2.7 (score 8, S/low) Delete scripts/seed.py (dead duplicate of `regwatch seed`)
`scripts/seed.py:36` — same fetch->filter->ingest->stats flow as `cli.py:189-214`; nothing invokes it (compose runs `["regwatch","seed"]`, CI runs `uv run regwatch seed`, no imports, scripts/ isn't a package). Duplicate entry points invite silent drift (script skips the explicit `init_db()` the CLI does). **Proposal:** delete the file; update **both** stale doc references — `docs/PROJECT_SPEC.md` Phase-1 DoD line **and** `docs/TECH_GUIDE_SIMPLE.md:80`'s folder map — to `uv run regwatch seed`. touches_active_build=false.

### 2.8 (score 8, S/low) pgvector fallback engine omits pool_recycle
`src/regwatch/store/pgvector_store.py:179` — the fallback `create_engine` reuses db.py's sslmode/connect-args hardening but drops `pool_recycle=s.db_pool_recycle_s` (db.py:281-289 has it); connections on this path never recycle, the one gap in an otherwise deliberate parity block (its own comment claims "SAME hardening as db.py"). **Proposal:** add the one parameter; optionally extend `test_db_connect_timeout.py`'s parity assertions to cover it. touches_active_build=false.

### 2.9 (score 8, S/low) **[ACTIVE-BUILD]** Extract the shared Sources-trailer strip regex
`src/regwatch/generate/grounded_qa.py:566` + `src/regwatch/eval/metrics.py:101` — the identical `re.split(r"\n\s*Sources:\s*\n", ...)` literal is maintained in two places; one guards INV-1 memory-context safety, the other feeds the eval gate's faithfulness score, and grounded_qa's comment names the coupling without enforcing it. **Proposal:** add `strip_sources_trailer()` to `common/citations.py` (its charter is exactly this) and point both call sites at it. Pure extraction, no behavior change. grounded_qa.py is on the active-build list — re-anchor after landing.

### 2.10 (score 8, S/low) **[ACTIVE-BUILD]** `_meta()`'s audit write can 500 where every sibling decline degrades
`src/regwatch/generate/grounded_qa.py:995` — `_refuse()` and `_clarify()` route audit writes through `_log_query_or_skip` (degrade to audit_id=-1, logged + Sentry); `_meta()`, reached via the same `_decline()` ceremony, calls `log_query` bare — a transient Postgres error on a "what do you cover?" turn propagates as a naked 500, the exact failure mode the module's own docstring says it exists to avoid. Verified no test covers log_query failure on the meta path. **Proposal:** swap to `_log_query_or_skip(...)` with identical kwargs, plus a regression test mirroring `test_audit_write_failure_degrades_to_error_refusal_not_500` for a meta question. Re-anchor after the build lands.

### 2.11 (score 8, S/low) **[ACTIVE-BUILD]** PDF page-count bound (last of 4 prior-recommended parse guards)
`src/regwatch/ingest/pdf_parser.py:101` — byte cap, %PDF magic check, and subprocess timeout all shipped; the page bound (named in the prior parsing-architecture audit) is still missing — a sub-50MB PDF with a huge page count is bounded only by the 60s timeout and can burn child-process memory first. Real PSGs are 2-8 pages. **Proposal:** add `pdf_max_pages` to config/settings.py (same "0 disables" convention as `pdf_max_bytes`), check `len(pdf.pages)` up front in `_try_pdfplumber`/`_try_pypdf`, raise `PdfTooManyPagesError(PdfParseError)` — slots into pipeline.py's documented error-type triage taxonomy, callers already catch broadly. Marked active-build only because config/settings.py is mid-edit (different section); re-anchor the settings block after landing.

### 2.12 (score 8, M/low) Strip the Dagster closure from the deployed Fly image
`Dockerfile:60` — the `orchestration` extra (dagster + webserver + dagster-postgres + grpc/watchdog closure) installs unconditionally into every image, including prod where it structurally cannot run (uvicorn-only CMD, no Fly process; GitHub Actions cron is the documented sole scheduler). Pure dead weight: image size, build time, and Trivy/audit CVE surface for unreachable code. **Proposal:** mirror the existing `INSTALL_LOCAL_EMBEDDINGS` ARG gate — `ARG INSTALL_ORCHESTRATION=false`, branch both `uv sync` lines, set false in fly.toml `[build.args]`, add `INSTALL_ORCHESTRATION=true` to compose.yaml's build args (dev unchanged). Follow-ons: drop `--extra orchestration` from ci.yml's "match shipped prod closure" audit step (line ~108), watch-daily.yml's sync, **and** ci.yml:238's frontend-contract job (same justification comment). uv.lock is a unified resolution, so removal can't shift shared dep versions. touches_active_build=false.

### 2.13 (score 8, M/medium) Fix the false "no INV-7 or INV-8 in force" claim in README
`README.md:197` — verifiably false: PROJECT_SPEC.md (spec of record) defines INV-7 (cross-product integrity), INV-8 (citation grammar guard), INV-9 (resolution-before-retrieval), and four code sites implement INV-8 by name. In a codebase whose invariant IDs are the compliance differentiator, the README denying two enforced guards is a real defect. **Proposal:** fix **toward PROJECT_SPEC.md's three-distinct-guard breakdown** (add INV-7/INV-8 rows, delete the false note) — not the originally suggested grouped "INV-7..9" row, which would under-specify vs. the canonical docs. Doc-only, zero code risk. touches_active_build=false.

### 2.14 (score 7, S/low) Log the swallowed DB error in get_recent_turns
`src/regwatch/common/conversation.py:133` — `get_session_filters`'s docstring promises it mirrors `get_recent_turns` with "(logged)" degrade behavior, and it does log; `get_recent_turns`'s except block silently returns `[]`. Real doc/implementation drift on a silent-failure path — a recurring incident class for this project. **Proposal:** add `log.warning("get_recent_turns_failed", exc_info=True)` before `return []` (logger already at module top); add a mirrored DB-error test analogous to the session-filters one. touches_active_build=false.

### 2.15 (score 7, S/low) **[ACTIVE-BUILD]** Add POST /resolve to main.py's endpoint docstring
`src/regwatch/api/main.py:6` — the "Endpoints per spec §10.16" docstring lists 18 routes but omits POST /resolve (implemented at line 1113, correctly documented in README:218). **Proposal:** add one docstring line matching the README's wording. Note: the source JSON flagged this false, but main.py is on the explicit active-build list — treat as **touches_active_build=true**, apply after landing.

### 2.16 (score 7, S/low) **[ACTIVE-BUILD]** Replace hardcoded Drugs@FDA URL in resolver with the module constant
`src/regwatch/retrieve/resolver.py:321` — `resolve_brand()` spells out `"https://api.fda.gov/drug/drugsfda.json"`, duplicating `sources.drugsfda.DRUGSFDA_ENDPOINT` (the canonical constant). **Proposal:** import and pass the constant; one line, no circular import (retrieve->sources already exists). Follow-up candidate (not required): the same literal also lives in `watch/aliases.py:31` and `watch/watchlist.py:37` — worth folding into the same commit since those files are not active-build. retrieve/* is active-build; re-anchor after landing.

### 2.17 (score 7, S/low) Add error_type to the be_extraction_skipped log
`src/regwatch/ingest/pipeline.py:296` — the except around `_extract_and_save_be` swallows both LLM and DB-write failures under one log carrying only `error=str(exc)`, while the sibling `ingest_failed` log documents `error_type` as the triage key; the unchanged-content backfill silently re-pays the LLM cost every run until the row exists, so distinguishing "bad LLM JSON" from "broken DB write" from logs matters. **Proposal:** add `error_type=type(exc).__name__` — matches the convention used at ~8 other sites. touches_active_build=false.

### 2.18 (score 7, S/low) Promote the WatchMatch product-id coercion to a shared helper
`src/regwatch/watch/run.py:42` + `alerts.py:168-171` — the rule "non-int product id => not alertable/pairable" is encoded twice (private `_product_id` in run.py, inline in `build_alerts`) with nothing keeping them in sync. **Proposal:** move it into matcher.py (which owns `WatchMatch`) as public `product_id()`, import from both; alerts.py keeps its skip-log line. Existing watch tests cover both call paths. touches_active_build=false.

### 2.19 (score 7, S/low) **[ACTIVE-BUILD]** Extract the duplicated punctuation-fold match normalizer
`src/regwatch/sources/dailymed.py:379` + `whitepaper/populator.py:736-740` — byte-identical `[^a-z0-9]+` fold/lowercase/collapse logic written twice; `common/text_normalize.py` is the purpose-built home (canonical_name/stripped_name live there). **Proposal:** add `fold_for_match()` there; both sites call it (dailymed keeps its `clean_text()` pre-pass). Both files are active-build — this is the most collision-prone item; do last, re-verify both helpers against the landed code.

### 2.20 (score 7, M/low) Deduplicate Responses-API kwargs assembly + temperature-retry dance
`src/regwatch/generate/llm.py:224` (complete) vs 315-367 (stream) — the subtle reasoning-model retry-without-temperature workaround is maintained by hand in two paths; a future quirk fix applied to one silently diverges complete() vs stream(). stream()'s retry branch is currently untested, strengthening the case. **Proposal:** extract kwargs-building and a `_create_responses(..., stream=...)` helper preserving exact BadRequestError semantics; keep response_format complete()-only; do **not** merge the legitimately-different sync-status vs streamed-terminal-event failure checks. Generator control flow is safe (exception surfaces at the same iteration point). touches_active_build=false.

## 3. Do opportunistically (score 4-6)

- **(6, S/low) [ACTIVE-BUILD] Log the swallowed FDA-API error in `_generic_names_for_ingredients`** — `src/regwatch/retrieve/resolver.py:325`: fail-open is correct by design, but silent external-call failure makes "why does this query lack product context" undiagnosable. Add module logger + `log.debug("ingredient_resolution_failed", exc_info=True)`. Bundle with item 2.16 (same file, same post-landing window).
- **(6, S/low) Extract `_buffered_stream()` in llm.py** — `src/regwatch/generate/llm.py:308` + 432-435: byte-identical "no real streaming, degrade to one buffered chunk" logic in OpenAI chat-mode and Anthropic providers. Pure code motion, only 3 lines duplicated — ride along with item 2.20 in the same file.
- **(6, M/medium) Deduplicate the chunk-table bootstrap DDL** — `src/regwatch/store/db.py:79`: the table/index/extension DDL exists twice (raw SQL in db.py, SQLModel in pgvector_store.py) with sync enforced only by comment discipline. **Strictly sequenced after item 2.1** (delegating before ensure_schema is lock-safe would reintroduce the Jun-18 incident class). Delegate `_ensure_postgres_objects` -> `pgvector_store.ensure_schema` via the existing lazy-import pattern; preserve per-statement lock semantics, note the vector-schema-qualification vs search_path nuance in the PR.

## 4. Rejected with reasons

- **AlertRecord TypedDict (watch/alerts.py)** — under mypy strict the change isn't S and necessarily ripples into active-build main.py; no incident behind it; would create a second must-stay-in-sync field declaration.
- **Product TypedDict (watch/matcher.py)** — CI type-checks tests; 3 test files build minimal dict literals a total TypedDict rejects; proposal cites a nonexistent `product_no` field (wasn't checked against the real schema).
- **Vague /query/stream docstring (api/main.py)** — premise factually wrong; also an active-build file.
- **Dead module-level Settings singleton (config/settings.py)** — negligible value, incomplete proposal, fabricated citation, and the file is owned by the in-flight build.
- **Refusal-settings section reordering (config/settings.py)** — cosmetic; file under active edit; informational only.
- **Duplicate search() in openFDA handlers** — thin duplication; a base class adds indirection without functional benefit; Protocol callers unaffected either way.
- **dossier.py full-scan optimization** — mislabeled low risk; the proposed branch condition has a silent false-negative failure mode (doc-side stripping) with no test coverage; harder than proposed.
- **AskParams TypedDict for _dispatch_ask** — active-build file; the safer later fix is an explicit keyword-only signature mirroring ask(), not a construct with no codebase precedent.

## 5. Unverified overflow

None — all lane candidates were either verified-confirmed or rejected; the overflow queue is empty.

## 6. Suggested PR batching (respecting touches_active_build ordering)

**PR 1 — "Dead code + plumbing dedup in sources/ingest/watch/llm" (safe to open now).** Items 2.3, 2.4, 2.7 (with both doc-line fixes), 2.2 (orange_book + rems only, incl. F841/F401 cleanup), 2.5, 2.6, 2.18, 2.20 + the llm.py `_buffered_stream` opportunistic item. One theme: delete dead weight, route duplicates through existing shared helpers. All S/low, zero active-build files.

**PR 2 — "Failure-path consistency hardening (store + pipeline + conversation)" (safe to open now).** Items 2.1 (headline, with contention test), 2.8, 2.17, 2.14 (with degrade test). One theme: apply the codebase's own proven lock-safety/logging patterns where siblings already have them. The chunk-DDL dedup (opportunistic) is an explicit follow-up PR gated on 2.1 landing — do not fold it in.

**PR 3 — "Build + docs truth" (safe to open now).** Items 2.12 (Dockerfile/fly.toml/compose + three CI extras follow-ons) and 2.13 (INV-7/8 reconciliation toward PROJECT_SPEC's three-row form). Build-config and docs only; no runtime code.

**PR 4 — "Post-landing wave" (open only after the in-flight whitepaper build merges; re-verify every anchor against the landed code first).** Items 2.9, 2.10 (+ meta-path regression test), 2.11, 2.15, 2.16 (+ optional watch/aliases + watchlist constant swaps), 2.19, the dailymed leg of 2.2, and the opportunistic resolver logging item. One theme: the same dedup/consistency work in files the concurrent build owns — grounded_qa.py, resolver.py, dailymed.py, api/main.py, config/settings.py.