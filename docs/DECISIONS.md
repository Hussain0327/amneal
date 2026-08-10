# decisions

Append-only. What was picked, why, and when.

## Phase 0 — scaffold

- Codename stays REGWATCH. No reason to rebrand for a POC.
- Python 3.11+ via uv. uv 0.9.30 + Python 3.12 on the host.
- LLM provider: pluggable, default `openai/gpt-4o-mini`. Cheap, supports JSON-only response format which the extractor needs. The production model and the on-prem question are the IT team's call.
- Embedding provider: pluggable, default local `BAAI/bge-small-en-v1.5`. Network-free, fine on CPU/MPS, fine for ~2,400 docs.
- `echo` provider added for both LLM and embeddings. Deterministic, no network, lets CI run without keys or model downloads.
- ChromaDB persistent at `data/chroma`. Cosine space. One collection `regwatch_chunks`. Chunk metadata distinguishes drugs / forms. (removed in R5: Postgres + pgvector is now the only vector store, see the R5 entry below.)
- SQLite via SQLModel at `data/regwatch.db`. Per spec §7. (removed in R5: Postgres is now the only structured store.)
- Refusal text is a verbatim constant in `Settings.refusal_text`. Spec §11 nails the exact string; keeping it in one place makes it testable and uniform.
- ruff + black + mypy strict on `src/`. pytest as the gate.

## Phase 1 — ingest + extract

- PSG page is server-side rendered. Verified by direct inspection: `https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm` returns the full table (~2,400 rows) in one HTML response when fetched with a browser User-Agent. Client-side DataTables only handles paging/filtering. No backing JSON endpoint to call instead.
- Akamai bot challenge requires a browser User-Agent. Scraper UAs get 503. We send a current Safari/macOS UA.
- PDF URL pattern is stable: `https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_{appl_no}.pdf`. URL doesn't change across revisions — version on `content_hash`, not URL.
- Chunking: heading-aware + 1000-token sliding window with 150-token overlap. Page boundaries preserved so every chunk's `page` metadata is correct.
- BE extractor drops fields whose citation quote does not appear verbatim (whitespace-tolerant) in the source pages, or whose page number is out of range. INV-1 is enforced at extraction time, not just at answer time.
- `download_pdf` hashes BYTES, not extracted text. Two parser runs that produce slightly different text from the same publication don't trigger a fake revision.

## Phase 2 — retrieval + cited Q&A

- Refusal threshold lives in settings (`REFUSAL_SCORE_THRESHOLD=0.30` default). Tune on the gold set.
- Two-layer refusal: pre-LLM (top-1 similarity below threshold) and post-LLM (explicit refusal string OR no valid citations). Both write the same audit row with `refused=true`.
- Citation grammar: `[short_name, p.N]`. `short_name` uses `PSG_{appl_no}` when available; otherwise the normalized ingredient name. Anything else is a "bad cite" and triggers refusal.
- Reranker is off by default behind `RERANKER_ENABLED=true`. The cross-encoder hook exists so the IT team can plug their preferred reranker without touching `grounded_qa`.

## Phase 3 — watch

- INV-5 enforced at three layers: `WatchlistEntry.__post_init__` (Python), `upsert_entries` filter, and the `/products` POST (HTTP 422).
- Matcher fuzzy threshold = 88 (rapidfuzz `token_sort_ratio`). Conservative. Allows common typos ("beclometasone" vs "beclomethasone") but rejects unrelated drugs.
- INV-4: `build_alerts` skips any match whose `psg_version` is not in the DB. A match cannot create a version on the fly.
- Digest format = JSONL on disk at `data/processed/alerts/digest-YYYY-MM-DD.jsonl`. Email/Slack delivery is a future plug-in; the on-disk file is the demo surface.

## Phase 4 — assemble + UI + API

- Dossier refuses on no matching PSG. A brief with no source is worse than no brief — the refusal markdown tells the user to seed first.
- Every section of the dossier links to a source. Even Section C (RLD label) carries `source_url`. Section D (applicable guidance) goes through `grounded_qa.ask()` so it inherits the same citation/refusal rules.
- FastAPI uses `lifespan` for DB init. Avoids per-test engine pinning and lets tests use `with TestClient(app)` to swap data dirs.
- Streamlit treats refusals as warnings, not errors. Refusal is a correct behavior, not a failure — the UX should communicate that.

## Phase 5 — eval

- Mechanical metrics in v1, no LLM-as-judge. `recall@k` and `citation_precision` check exact `(short_name, page)` equality. `faithfulness` checks that each sentence carries a `[X, p.N]` token. Auditable and reproducible; LLM-as-judge can plug in later behind a flag.
- CI thresholds match spec §12: `recall@8 ≥ 0.90`, `citation_precision ≥ 0.95`, `refusal_accuracy ≥ 0.95`. `faithfulness` is reported, not gated — sentence-level citation is too strict for prose.
- Empty corpus is a CI no-op. `run_eval.py` exits clean when the vector store (Chroma at the time; pgvector since R5) is empty, so CI on a fresh checkout passes; gates only fire once data is present.
- Gold set ships small (10 items: 6 real + 4 must-refuse). Production rollout expands to 30–50 per spec.

## Post-review fixes (after live demo)

- **Retrieval params now match the diagram.** Was a single `RETRIEVAL_TOP_K=8` which contradicted the two-stage diagram. Now `VECTOR_TOP_K=50` (stage 1 wide) + `RERANK_TOP_K=8` (stage 2 narrow). When the reranker is off, stage 2 is `passages[:rerank_top_k]`. Legacy `RETRIEVAL_TOP_K` honored if set.
- **Applicant aliases derived from Drugs@FDA, not guessed.** Was a hardcoded env list (`AMNEAL PHARMS,AMNEAL PHARMACEUTICALS,...`). Now `regwatch aliases --refresh` queries `sponsor_name:AMNEAL*` (URL-encoded wildcard) and caches all distinct variants. First live discovery returned 8 variants including `AMNEAL EU LTD`, `AMNEAL IRELAND LTD`, `AMNEAL PHARMS NY`. Env list is fallback only.
- **`openai` SDK moved to runtime install.** Was in `[project.optional-dependencies].llm` only, so first seed silently skipped BE extraction with `No module named 'openai'`. The extras group still exists for `anthropic`; `openai` is mandatory when `LLM_PROVIDER=openai`.

## Live-test observations (worth fixing next)

- Gold set was authored before we knew what the seed would ingest. Seed pulled Levalbuterol (substring match on "albuterol") and not Romidepsin (no PSG under that name in FDA's listing). Real eval scorecard on the seeded corpus: `recall@8 = 0.667`, `citation_precision = 0.000`, `refusal_accuracy = 0.750`. Three things to address:
  - Pin the gold set to the PSG URLs that actually got ingested (`PSG_020503`, `PSG_214070`, `PSG_207921`, `PSG_020911`, `PSG_021730`), not the ones we planned to ingest.
  - Citation regex doesn't split compound citations like `[PSG_020503, p.4; PSG_021730, p.4]`. The model emits these; we should accept them.
  - Tighten the seed filter so substring matches (`albuterol` ⇒ `levalbuterol`) don't sneak in unless we want them.

## Phase 0 — eval green + entity-resolution-first (cross-drug leak fix)

The eval was RED on the real corpus (`recall@8=0.667, citation_precision=0.000, refusal_accuracy=0.750`). Root-caused and fixed; gate now green (`1.000 / 1.000 / 1.000`).

- **Deterministic seed by application number.** `filter_listings`' substring match pulled `levalbuterol` from a seed of `albuterol`. Fixed two ways: the seed is now pinned by appl_no (`psg_crawler.SEED_APPL_NOS` = 020503, 214070, 207921, 020911, 021730), and the name path uses whole-word (`\bterm\b`) matching, not substring — so "beclomethasone" still matches "beclomethasone dipropionate" but "albuterol" never matches "levalbuterol". Romidepsin has no PSG; it is intentionally NOT seeded and is carried as a watchlist must-refuse case.
- **Centralized, compound-aware citation grammar.** The `[short, p.N]` regex was duplicated in `grounded_qa` and `eval/metrics` and could not parse compound citations like `[PSG_020503, p.4; PSG_021730, p.4]` (the `citation_precision=0` cause). New `common/citations.py` is the single owner (`iter_psg_citations`, `has_citation`, `filter_citations`); both consumers import it; compound brackets now split into multiple pairs.
- **Entity resolution BEFORE semantic retrieval.** FDA templates reuse identical language across drugs ("Single actuation content (SAC)", "two-way crossover" appear verbatim in many PSGs), so embeddings cannot safely disambiguate the product — a beclomethasone question retrieved albuterol boilerplate and cited the wrong drug. New `retrieve/resolver.py` `resolve_product()` resolves the product first (explicit filter → whole-word ingredient match against the corpus's distinct `normalized_name` → single-product-corpus fallback), then `grounded_qa.ask()` forces a `normalized_name` Chroma filter. Resolution outcomes: resolved → filter; ambiguous → refuse (`ambiguous_product`); none → refuse (`no_product`). A post-retrieval guard refuses if returned passages span >1 `normalized_name` (defense in depth for a caller that bypasses the resolver). `dossier.py`'s applicable-guidance Q&A now passes `filters={"normalized_name": canonical_name(active_ingredient)}`. Rich clickable clarify (vs. plain refusal) is deferred to the Phase-3 synthesizer/UI; the resolver already returns the candidate list.
- **Gold set re-pinned.** `gold_set.jsonl` now references the PSGs that actually ingest, with `expected_sources` pinned to the (grounded, temperature-0, correct-drug) pages the system cites; romidepsin flipped to `must_refuse`. Note: mechanical metrics are sensitive to the LLM version — a model change may require re-pinning pages.
- New invariant **INV-9** (PSG queries are always product-resolved and ingredient-filtered; no cross-drug citation can survive) covered by `tests/test_cross_drug_leak.py` with synthetic shared-boilerplate chunks, plus `tests/test_resolver.py`, `tests/test_citations.py`, `tests/test_seed_filter.py`.

## OpenAI Responses API + role-specific models

- **Provider migrated to the OpenAI Responses API.** `OpenAIProvider` now defaults to `client.responses.create` (`OPENAI_API_MODE=responses`), the native surface for GPT-5.x; the legacy `client.chat.completions.create` path is preserved behind `OPENAI_API_MODE=chat`. The `LLMProvider.complete()` interface is unchanged, so retrieval / resolver / citation logic is untouched. Mapping: system messages → `instructions`, the rest → `input`, `max_tokens` → `max_output_tokens`, JSON via `text={"format":{"type":"json_object"}}`, output read from `resp.output_text`. SDK pinned behavior verified against `openai==2.40.0`.
- **Reasoning models reject `temperature`.** Verified empirically: `gpt-5-nano` returns `400 Unsupported parameter: 'temperature'` while `gpt-5.4-nano` accepts it. The provider sends `temperature` and, on that specific 400, retries once without it — so reasoning and non-reasoning models both work through one interface.
- **Role-specific models.** `get_llm_provider(role=...)` / `current_model_name(role=...)` resolve `ROUTER_MODEL` / `SYNTHESIZER_MODEL` / `EXTRACTOR_MODEL`, each falling back to `LLM_MODEL`. Wired: BE extraction → `extractor` (`gpt-5.4-nano`), grounded synthesis → `synthesizer` (`gpt-5.4-nano`). `ROUTER_MODEL=gpt-5-nano` is config-ready but has no consumer yet (the resolver is rule-based / no LLM; the LLM router is Phase 2). Defaults moved off `gpt-4o-mini` to `gpt-5.4-nano`.
- **Eval stayed green across the model swap.** After switching the synthesizer to `gpt-5.4-nano`, the gate still reports `recall=1.0, citation_precision=1.0, refusal_accuracy=1.0` — the gold `expected_sources` page-sets are broad enough to cover both `gpt-4o-mini` and `gpt-5.4-nano` citations. Mechanical metrics remain LLM-version sensitive; a future model change may require re-pinning.
- Tests: `tests/test_llm_provider.py` asserts Responses-not-Chat, JSON `text.format`, system→instructions mapping, the temperature-retry, and role→model selection with `LLM_MODEL` fallback.

## Stage A production hardening

- **Schema management moved to Alembic.** `regwatch init-db` now applies migrations instead of directly calling `SQLModel.metadata.create_all`. The first migration captures the current POC schema; future FDA source tables must land as migrations before loader/handler code depends on them.
- **Low-risk correctness debt paid down.** Dossier BE/version lookups are deterministic with explicit ordering, RLD/RS join keys are sorted before DB matching, internal DB id assumptions raise runtime errors instead of `assert`, `/watch/latest?since=` is datetime-validated by FastAPI, and Streamlit DB init is cached across reruns.
- **Resolver product metadata is cached and invalidated.** `distinct_metadata_values()` no longer full-scans the vector store (Chroma at the time; pgvector since R5) on every query; `add_chunks()` and test resets clear the cache so newly ingested products become resolvable.
- **CI hardened for production drift.** CI now tests Python 3.11 and 3.12, installs LLM extras, type-checks tests as well as `src`, and runs the eval gate after unit tests.

## Docker container baseline

- **One Python image now serves API, temporary UI, and ingest.** `Dockerfile` builds the shared app image, `compose.yaml` exposes API, optional Streamlit UI, and one-shot ingest services, and `docker/entrypoint.sh` creates data directories before running `regwatch init-db`.
- **Ingest is separate from API startup.** Large source loads are run as commands, not as boot-time work, so a 30-minute PSG/drug load does not hold the API health check hostage.
- **The host `data/` directory is mounted into `/app/data`.** SQLite, Chroma, raw PDFs, and processed files survive container restarts without baking data into the image. (removed in R5: SQLite/Chroma are gone; only raw PDFs and processed files live under `data/` now — the structured store and vectors are both in Postgres.)
- **The baseline image defaults to `EMBEDDING_PROVIDER=echo`.** This keeps health checks and local API smoke tests lightweight. Broad PSG ingest must use a real embedding provider, for example `INSTALL_LOCAL_EMBEDDINGS=true` plus `EMBEDDING_PROVIDER=local-bge-small`.
- **Local embeddings moved behind an optional extra.** `sentence-transformers`, `torch`, and `transformers` now live in `regwatch[local-embeddings]`. This avoids pulling heavy CUDA/NVIDIA packages into the default API image while still allowing a heavier local-embedding build when needed.
- **CI now builds the Docker image.** The image build is a separate CI job, so container breakage is caught independently of lint/type/test failures.

## Conversational sessions

- **Conversation memory is context, not evidence.** The assistant may carry a safe product filter across turns (for example, `normalized_name=albuterol sulfate`) so follow-ups like "What about dissolution?" work, but every answer still reruns retrieval and validates citations from FDA evidence.
- **`POST /query` now returns session metadata.** Callers may pass `session_id`; otherwise the backend creates one. Every response returns `session_id`, `turn_id`, status, citations, and audit ID so a future TypeScript UI can render a real chat thread.
- **Audit rows link to chat turns.** `query_log` now carries `session_id`, `turn_id`, `status`, and `route_json`. `chat_session` and `chat_message` persist the thread and the user/assistant turns.
- **Response statuses are broader than answer/refuse.** `answer`, `summary`, `clarify`, `scope_warning`, and `refused` allow the assistant to guide users without guessing. Regulatory strategy/submission-drafting asks produce `scope_warning`, not a fabricated answer.

## Frontend/backend workspace split

- **Backend source stays in `src/regwatch`.** `regwatch/backend/` is a workspace for backend deployment notes and future API-adjacent assets, not a second Python package. No `regwatch/backend/__init__.py` should be added.
- **TypeScript UI moved to `regwatch/frontend/`.** The Next.js App Router app now lives beside the backend workspace and keeps its same `/api/*` proxy to FastAPI.
- **Demo sharing follows the new path.** `scripts/share-demo.sh` builds and starts the UI from `regwatch/frontend/`, while FastAPI still runs `regwatch.api.main:app` from the canonical Python package.

## Streamlit POC retired

- **The Streamlit POC (`src/regwatch/ui/`) is removed; the Next.js app is the UI.** Once the TypeScript UI reached parity (Ask / Assemble / Watch, clickable clarify options, cited sources), the parallel Streamlit surface was retired rather than maintained as a second UI. Deleted `ui/app.py` + `ui/branding.py`, dropped the `streamlit` dependency from `pyproject.toml`/`uv.lock`, and removed the `ui` service (port 8501) from `compose.yaml`. Nothing imported `regwatch.ui`, so it was a pure subtraction. The UI runs as its own process; it is not in the compose stack today. (This supersedes the Phase-4 "UI (Streamlit)" and Docker-baseline "optional Streamlit UI" notes above.)

## Entity-resolution hardening

- **Product keys are canonicalized at the filter boundary.** A caller-supplied `normalized_name` filter (API / clarify option) is run through `canonical_name()` in `grounded_qa.ask()`, so a casing/salt-order variant ("Albuterol Sulfate") no longer misses the vector store's exact-match filter (Chroma's `$eq` at the time; pgvector's equality filter since R5) and turns a real product into a wrong refusal.
- **Explicit comparisons clarify instead of collapsing.** `resolve_product()` detects comparison markers (`compare`/`comparison`/`versus`/`vs`/`difference between`/`compared to`; deliberately NOT `and`/`with`, which denote a single combination product) and, when 2+ distinct products are named, returns `ambiguous` before the subset-collapse — so "compare X vs the X+Y combo" asks which rather than silently picking the superset.
- **Mixed-product evidence clarifies, it does not refuse.** The post-retrieval guard in `ask()` now offers the distinct products found (clickable clarify) instead of a blunt refusal when evidence spans more than one product. Zero citations on the clarify path, one audit row (INV-6) — unchanged.

## Eval upgrade — fact scoring + a CI gate that actually fires

- **`fact_recall` scores answer content.** New metric = fraction of a gold item's `expected_facts` present (tolerant substring: lowercased, de-hyphenated) in the answer. The gold set already carried `expected_facts`; they were never scored. The gate now checks the answer states the right facts, not just that it cited the right pages.
- **A deterministic, offline eval gate runs in CI.** `tests/test_eval_gate.py` seeds a fixed echo-embedded corpus and a faithful LLM stub, drives the real `grounded_qa.ask()` pipeline, and hard-gates `recall@k`/`citation_precision`/`refusal_accuracy`/`faithfulness`/`fact_recall`. Because it runs inside `uv run pytest`, the gate fires in CI — where the live `run_eval --check-thresholds` no-ops on the empty fresh-checkout corpus.
- **`faithfulness`/`fact_recall` stay observability-only on the live `run_eval`.** Measured real-corpus `faithfulness` is ~0.59 (the per-sentence-citation metric is strict for prose, per the Phase-5 note), so hard-gating it on the non-deterministic live model would be flaky. They print on the scorecard; the deterministic pytest gate is where they have teeth.

## Pilot hardening — vague-input guard, multi-form clarify, watch pipeline, fail-fast providers

- **The vague/no-topic guard fires on any pinned product, not just name-resolved ones.** The pre-LLM guard in `grounded_qa.ask()` now keys on `resolved_name` alone (dropped the `resolved_by_name` requirement), so "Hello" / "thanks!" with a product pinned via the API/UI `normalized_name` filter clarifies with options instead of reaching the synthesizer and coming back as a cited greeting. `hey`/`thanks`/`thank` joined `_FILLER`. One audit row (INV-6), zero citations — `tests/test_filter_pinned_vague.py`.
- **Multi-form drugs clarify which (dosage_form, route) before retrieval.** Once a product is resolved, `store/queries.py::current_dosage_form_routes` enumerates its CURRENT documents' distinct (dosage_form, route) combos (only docs with a `psg_version`, honoring an already-pinned form/route); >1 combo returns `clarify` with one option per combo pinning `normalized_name`+`dosage_form`+`route` and re-asking the same question — so a wrong-form PSG can never be blended in and cited (e.g. estradiol: 8 combos). A post-retrieval guard mirrors the mixed-products guard as defense in depth (skipped on incomplete chunk metadata). Form-less follow-ups inherit the session's chosen form/route; `dossier.py` pins the matched PSGs' combo when they agree on exactly one.
- **`must_clarify` gold items fold into `refusal_accuracy`, scored reason-aware.** A `must_clarify` item is correct iff the system fires the MULTI-FORM clarify — `status == "clarify"` AND `reason == "multi_form"` AND every clarify option pins a concrete `(dosage_form, route)`. An unrelated clarify (`did_you_mean`/`brand_lookup`/`vague_input`) does NOT satisfy it. Like `must_refuse` it is excluded from recall/precision/faithfulness denominators. Thresholds unchanged (0.90/0.95/0.95). The estradiol must-clarify item was added to `gold_set.jsonl` (the loader skips `#` comment lines); the deterministic offline gate in `tests/test_eval_gate.py` covers the clarify behavior with a synthetic two-form drug. **Live-eval corpus-membership gate (corrected):** CI's live eval runs TODAY whenever `OPENAI_API_KEY` is set (`ci.yml`: `regwatch seed` → `run_eval --check-thresholds`), and `regwatch seed` ingests only the 5 inhalation PSGs — estradiol is absent, so the resolver refuses (`reason == "no_product"`). `metrics.evaluate`/`run_eval` therefore SKIP that item (excluded from `refusal_accuracy` with a printed `skipped` notice — never a silent pass) until estradiol is in the corpus, at which point it is scored. The skip can only fire when the product is genuinely absent (a present multi-form drug clarifies before retrieval; any other refusal reason still counts), so it never masks a regression. Threshold left at 0.95.
- **`scope_warning` renders as its own UI stamp.** `ResultView` in `regwatch/frontend/app/page.tsx` branches on `status === "scope_warning"` ("Out of scope" + the backend's policy text + audit footer) BEFORE the `refused` branch (the backend sets `refused=true` for scope warnings); "Declined · not in corpus" is now only for real corpus-miss refusals. The Watch page no longer references the nonexistent `regwatch watchlist add` CLI.
- **The watch pipeline is wired end to end and scheduled.** `watch/run.py::run_watch` does crawl (full A–Z catalog) → match against the watchlist → ingest ONLY matched listings (de-duped by appl_no) → `build_alerts` only for listings actually ingested as added/revised this run (INV-4) → `write_digest`. New `regwatch watch` CLI (`--extract`/`--no-extract`, exit 2 on ingest errors); Dagster `watch_digest_job` + `watch_daily_schedule` (06:00 UTC, default RUNNING) provided local/Compose scheduling at the time (removed in R5 — GitHub Actions cron is now the sole scheduler). A clean same-day re-run overwrites that day's digest by design (idempotent: an unchanged PSG re-writes an empty digest, never duplicates) — the DB `psg_version` rows are the durable version record. EXCEPTION: an ERRORED run that produced no alerts does NOT write an empty all-clear digest (`run.py` skips the write when `stats.errors and not alerts`), so a failed run cannot masquerade as a quiet day in `/watch/latest` or clobber an earlier same-day digest (INV-4). Known residual: a version committed to SQL before its chunks embed (then erroring) is recorded but never alerted on a later run (`_latest_version_hash` matches → "unchanged"); recovering that needs an `alerted_at` marker or durable-diff alert derivation — tracked, not yet built.
- **Echo providers fail fast against a real corpus; `/health` diagnoses the stack.** New `allow_test_providers` setting (env `REGWATCH_ALLOW_TEST_PROVIDERS`, default false): the API lifespan raises with a remediation message when an `echo` embedding/LLM provider faces a NON-empty vector-store corpus (Chroma at the time; pgvector since R5) without the override; empty corpus + echo still boots so a fresh compose stack can seed. `GET /health` now returns `{status, components:{db, chroma+corpus_count, llm provider+key_present bool, embedding provider}, warnings[]}` — a superset of `{"status":"ok"}` (compose healthcheck unchanged), HTTP 503 only when DB or the vector store is unreachable. `tests/conftest.py` opts the echo-on-purpose suite in. (R5 note: the `/health` component key is now `vector_store`, not `chroma`, and `compose.yaml`'s `EMBEDDING_PROVIDER` default moved from `echo` to `openai` since the R5 pgvector `db` service enforces the 1536-dim K6 assert — see `docs/DOCKER.md`.)

## Cookie-session auth + per-user chat history (Jun 10 2026)

- **DB-backed opaque session tokens over JWT.** `POST /auth/login` issues `secrets.token_urlsafe(32)` in an HttpOnly `regwatch_session` cookie (SameSite=Lax, Max-Age = `AUTH_SESSION_TTL_HOURS`, Secure only when `AUTH_COOKIE_SECURE` — false for the localhost pilot). The DB stores only the token's sha256 (`auth_session` table), so a leaked DB cannot replay a session, and revocation (logout, deactivate-user, set-password) is immediate — no JWT expiry-window problem. Login always inserts a fresh session row (no fixation).
- **bcrypt directly, not passlib.** passlib is unmaintained; the `bcrypt` library is used straight. Login verifies against a module-level dummy hash when the email is unknown, so unknown email / wrong password / inactive user share one 401 body ("invalid email or password") and one bcrypt-shaped timing profile.
- **Users are CLI-provisioned; no self-signup.** `regwatch create-user EMAIL --name NAME [--role analyst|admin]` with the password prompted (`hide_input`, confirmed) — never a flag, which would leak into shell history and `ps`. Also `list-users` (no hashes), `set-password`, `deactivate-user` (both revoke live sessions).
- **One authorization chokepoint.** Every endpoint except `GET /health` and `/auth/*` is registered on an `APIRouter(dependencies=[Depends(require_user)])`, so an accidentally-unauthenticated route is structurally impossible. CORS gained `allow_credentials=True` (plus DELETE) against the existing origin allowlist.
- **Chat history is per-user; foreign sessions 404.** `POST /query` no longer accepts a client-supplied `user_id` — identity comes from the session. A `session_id` owned by another user returns 404 (not 403 — existence is never confirmed); a NULL-user legacy session is adopted on first authenticated use. New `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` serve only the caller's threads.
- **Audit rows carry identity (INV-6 extension).** `query_log.user_id` (nullable, indexed) is filled on every authenticated `/query` and `/assemble` — including the dossier's inner Q&A row.
- **In-memory per-user rate limiting.** `RATE_LIMIT_PER_MINUTE` (default 30, 0 disables) on `/query` + `/assemble` (the LLM cost surface) and a fixed 10/email/minute brute-force cap on `/auth/login`; both are a lock + deque sliding window, per process — distributed limiting stays gateway work.
- **OIDC/SSO deferred to the IT gateway.** App-layer cookie sessions are the pilot boundary; TLS termination and the enterprise identity provider remain environment decisions (docs/PROD_READINESS.md #1).

## Gate 2 — CRA White Paper populator (Jun 11 2026)

- **Cell-mode taxonomy is enforced in code, not convention.** `whitepaper/template.py` encodes `docs/whitepaper_schema.md` as an ordered `CellSpec` registry (the single source of truth: id, section, label, source, lookup, mode, extractor key). Three modes carry the compliance line: **auto** (deterministic source joins, no LLM, value + structured locator + `fetched_at`), **evidence_only** (verbatim cited SPL LOINC sections / the scoped PSG `ask()` for Requirements — no generation), **manual** (`analyst_input_required` always, value=null, evidence attached). `populator._build_cell` structurally forces every manual cell to value=null even if an extractor returned one, and `tests/test_whitepaper_invariants.py` walks the registry to prove it (INV-3).
- **Yes/No auto cells are tri-state, never a bare boolean (INV-5).** REMS and Drug Shortages call their handlers **per-handler** (not `search_sources`, which swallows exceptions and would mask an HTTP failure as "no rows" → a false "No"). A successful, identity-filtered, empty query → `verified_absent` ("No", with the query recorded as evidence); an exception/timeout → `analyst_input_required` with the failure reason in `note`. Shortages is queried by application number ONLY (an ingredient search could return another product's shortage → false "Yes").
- **Orange Book `type` ≠ combination-product Type 1–9.** OB's `type` column is marketing status (RX/OTC), not the 21 CFR 3.2(e) combination type. The Combination Product cell is `manual`: dosage form / delivery system from Drugs@FDA are surfaced as evidence; the determination is the analyst's (INV-3). Same ruling for Patents/Priority/First-to-Market/eFTF/Labeling Carveouts — OB supplies the raw patent/exclusivity rows as evidence, never the paragraph classification or eligibility.
- **No-FDA-source cells stay manual.** R&D Center, USP Monograph (USP-NF is paywalled), Salable Unit, Emergency Use (EUA is unstructured), and the four Action Items have no machine-readable FDA source → `manual` with a note, never filled from model memory (INV-5).
- **Structured-token INV-8 rule.** `common/citations.py` carries a structured grammar (`SPL_{setid}#{loinc}`, `OB_{applno}/{productno}`, `OBPAT_{patentno}`, `OBEXCL_{code}`) alongside the existing `[short_name, p.N]` page grammar — the two never collide. The populator builds a `known_tokens` set from the rows it actually fetched; `populator._enforce_structured_citations` collapses any populated cell whose structured locator is not in that set to `analyst_input_required` (a fabricated/unfetched citation can never ride a populated value).
- **EPC + DEA come from the NDC directory's openFDA row.** Rather than add an openFDA-label surface, EPC reads `pharm_class` ([EPC] entries) and DEA reads `dea_schedule` from the NDC handler's `raw` (one fetch also serves packaging). Absence of `dea_schedule` collapses to `analyst_input_required` — absence of the field does NOT prove "N/A" (a controlled-substance status is too consequential to infer from a missing field).
- **Persistence write-through for freshness provenance.** New SQLModel tables `ob_product` / `ob_patent` / `ob_exclusivity` / `spl_document` (migration `0005_whitepaper_sources`, chained off `0004_auth_users`) each carry `last_fetched_at`; the populator upserts on fetch (best-effort — a DB hiccup warns, never fails the populate) so a cell cites a durable row. Raw rows only — no classification stored (INV-3).
- **DOCX output: fill the real template, fall back to scratch.** `whitepaper/docx_writer.py` opens `settings.whitepaper_template_path` (default `./CRA White Paper Template May 2026 - Raja.docx`) with python-docx, maps registry cells onto the template's table label cells by normalized label, writes values in place, and APPENDS an explicit `-> Yes/No/Analyst input required` marker to checkbox-style rows (no attempt to tick symbol checkboxes). When the template is absent (CI), it builds a structurally-equivalent document from the registry. Both paths append a Provenance appendix (cell → source/locator/fetched_at). The docx tests use a synthetic in-test template fixture, so CI passes without the gitignored Word file. python-docx ships only partial type hints, so a `[[tool.mypy.overrides]] module=["docx.*"]` `follow_imports=skip` was added (matching the `ignore_missing_imports` posture).
- **Both endpoints audit on success AND on 422.** `POST /whitepaper` and `POST /whitepaper/docx` join the existing `require_user` protected router and the `/assemble`-style per-user rate limit; `build_whitepaper` writes exactly one `log_query` row (mode="whitepaper", caller's `user_id`) on success and one on resolution failure before re-raising `SpineResolutionError` (→ 422 with an explanatory detail listing what WAS found, never a guess). The PSG-Requirements cell's scoped `ask()` writes its own audit row, like the dossier's inner Q&A.
- **Eval.** The romidepsin must-refuse gold item was REPLACED with a fictional product (zorbifexol): FDA's full PSG catalog lists Romidepsin (PSG_022393), so refusing on it is corpus-dependent — correct against the 5-PSG CI seed but wrong after a full ingest — not an absence test. The replacement is genuinely absent from the catalog and carries no outcome-coaching clause in the question. A White-Paper gold set (`eval/whitepaper_gold.jsonl`, 16 items) is scored by `eval/whitepaper_metrics.py` and gated offline in `tests/test_eval_gate.py` with a faithful structured stub (no network), mapping onto the unchanged shared thresholds (recall@k≥0.90 = value content, citation_precision≥0.95 = expected-evidence source, refusal_accuracy≥0.95 = cell-status decision incl. manual `analyst_input_required` graded like `must_refuse`).

## Supabase migration — Postgres + pgvector, OpenAI embeddings, custom auth kept (Jun 12 2026)

> **(Superseded by R5, 2026-07-21):** this entry records the original
> dual-mode design (`DATABASE_URL` toggling between SQLite/Chroma and
> Postgres/pgvector). R5 deleted the SQLite/Chroma side entirely — Postgres +
> pgvector is now the only datastore, `DATABASE_URL` is unconditionally
> mandatory, and `scripts/migrate_to_supabase.py` (referenced below) was
> deleted as one-time migration tooling. Left as-is below for the historical
> record; see `docs/ARCHITECTURE.md` §9 for current behavior.

- **One switch: `DATABASE_URL`.** Empty/unset → SQLite at `SQLITE_PATH` + Chroma at `CHROMA_DIR`, byte-for-byte today's dev/test behavior. Set → the structured store moves to Postgres (SQLAlchemy psycopg v3, `postgresql+psycopg://`; a bare `postgresql://` is normalized) AND vectors move to a pgvector `chunk` table in the same database — vectors live in pgvector iff `DATABASE_URL` is set, no separate vector-backend toggle. `store/vector_store.py` keeps its exact public surface and dispatches to `store/pgvector_store.py` in Postgres mode; pgvector cosine distances are mapped to the same similarity scale Chroma produced, so the refusal threshold is backend-independent.
- **Fresh-Postgres bootstrap = `create_all` + `alembic stamp head`, never history replay.** Migrations 0001–0006 are SQLite batch-op migrations and will never run on Postgres; `migrations/env.py` renders batch mode only on the sqlite dialect. On a stamped database, startup verifies the stamp matches head and refuses on mismatch. The bootstrap runs `CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions` (a no-op on Supabase, where pgvector 0.8 already lives in `extensions`; lands in `public` on the local docker pg) and creates `chunk` (`vector(1536)`, HNSW cosine m=16/ef_construction=64, btree on `normalized_name`/`doc_id`/`appl_no`).
- **RLS deny-all on every public table is part of the bootstrap.** Supabase auto-exposes `public` tables over its REST Data API, so the bootstrap enables row level security with NO policies — `anon`/`authenticated` see nothing. Our API connects as the `postgres` role, which bypasses RLS. This is a hard requirement, not hygiene.
- **Embedding switch for prod: OpenAI `text-embedding-3-small` (1536 dims).** New `EMBEDDING_PROVIDER=openai` (batches ≤512 inputs, backoff on 429/5xx); the `EmbeddingProvider` interface is unchanged. Dimension is a property of the provider and the chunk table is fixed at 1536, so Postgres-mode startup asserts provider dim == table dim and fails fast. `local-bge-small` (384) stays the SQLite-mode/dev default; `compose.yaml`'s default flipped `echo` → `local-bge-small` because the echo default bricked any seeded stack at boot (known issue). The slim Docker image (no torch) + `EMBEDDING_PROVIDER=openai` is the production pairing.
- **Auth mode = custom, zero changes.** The cookie-session auth shipped Jun 10 is kept exactly as-is; `user`/`auth_session` migrate like any other table. Supabase Auth is deliberately NOT adopted — the Supabase project is used as managed Postgres only.
- **One-shot migration: `scripts/migrate_to_supabase.py`.** Bootstraps the target through the same `init_db()` code path, copies every SQLModel table from a SQLite *snapshot* in FK-dependency order (1000-row executemany batches, ids preserved, JSON→JSONB via the shared Table objects), re-embeds every Chroma chunk through the OpenAI provider into pgvector, then prints a per-table source-vs-target count table and **exits nonzero on any mismatch** — the count verification is the load-bearing step because a silent partial copy is the worst failure mode. Postgres sequences are reset via `setval(max(id)+1, false)` after the copy (the `sqlite_sequence` equivalent) so new inserts can't collide with copied ids. Idempotency: refuses a non-empty target without `--truncate`; `--skip-embed` rehearses the relational copy without embedding spend (the 384-dim Chroma vectors are dimensionally unusable for the 1536 table — a real cutover must re-embed). A dialect guard aborts before any write if the store layer didn't pick up `DATABASE_URL`, so the script can never fall through to the live SQLite/Chroma paths.
- **Deploy shape (docs/DEPLOY.md is the runbook).** Vercel hosts `regwatch/frontend` with `API_PROXY_TARGET` pointing at the API on Fly.io (or Railway) — the `/api/*` rewrite keeps the HttpOnly session cookie same-origin, and `AUTH_COOKIE_SECURE=true` because both hops are HTTPS. The API uses the Supabase **session** pooler URI (transaction pooler breaks session-mode features). Smoke gate ends with the analyst flow: log in → cited Q&A → populate a white paper → download the docx.

## Unified shell + URL-as-truth product scope (Jun 15 2026)

- **One App Router shell for all four surfaces (commit 2720f1b).** Ask, Assemble, Watch, and White Paper now render inside a single `(shell)` route-group layout (`app/(shell)/layout.tsx`) — one sidebar, one canvas, one set of design tokens — rather than four standalone pages with duplicated chrome. The layout owns `SessionsProvider` (keyed on `user.id`, so a re-login remounts) and `CurrentProductProvider`; `AuthProvider` is reduced to a gate that renders children only when authed. Frontend-only: no API/auth/request-shape, retrieval/generate, or audit-logging changes.
- **Product scope is URL state, not React state (commit 2720f1b).** `CurrentProductProvider` reads `?rp=` (reference product name) and `?appl=` (application number) straight from `useSearchParams` — there is no shadow copy in component state — and `setProduct`/`clearProduct` mutate the scope via `router.replace`. The scope therefore survives a reload and is shareable by URL, and all four surfaces read the same `{referenceProductName, applicationNumber}` off the URL. The carry helper preserves `rp`/`appl` across navigation while deliberately NOT carrying the Ask page's `session` param, so the conversational thread and the product scope are independent.
- **Scope is settable from three surfaces, always canonical.** The "Under review" bar picker, White Paper on a successful resolve, and a Watch per-row scope button all write the same canonical pair (normalized name + six-digit application number); White Paper additionally prefills the scope, Assemble prefills its RLD field. No matter which surface pins it, the result is one `rp=`/`appl=` value.

## Ask rebuilt as a cited conversational chat (Jun 16 2026)

- **Ask is a chat, not a document/ledger register (commit f30eaef).** The editorial document/ledger cards were replaced with a conversational surface: right-aligned user bubbles, a gold RW avatar on assistant turns, citation chips that link to the FDA source (the full snippet is one tap away in a Sources disclosure), clarify option pills, and a bottom-pinned composer with a round send button and Enter-to-send. This is a re-skin only — grounding (INV-1), refuse-over-guess (refusals keep their distinct declined treatment), the one-audit-row-per-turn path, and the session/abort/race-guard/refocus logic are all preserved.
- **No fake streaming.** The backend still has no streaming endpoint, so the chat client falls back to a blocking `POST /query`; the thinking ticker is an honest activity indicator, not faked token motion. Real token-by-token streaming (a `/query/stream` endpoint that respects INV-1 — no answer text before a validated citation — and still writes exactly one audit row) is an open item tracked in docs/ROADMAP.md.
- **The scoped product moved out of the sidebar into a slim sticky "Under review" bar (commit f30eaef).** The bar spans all four surfaces, replacing the old sidebar product badge, and is the front-door scope setter (see the next entry). `ProductScopeBar.tsx` is the shared component; the design is specified by the committed `regwatch_ask_chat_redesign.html` mockup and the `regwatch_workflow_spine.png` diagram.

## POST /resolve — deterministic, no-audit-row scope resolution (Jun 16 2026)

- **`POST /resolve` is the front-door entity resolver, not an LLM turn (commit c5e7f93).** It reuses the White Paper's `_build_context` to validate an RLD name + application number into the canonical spine WITHOUT running a populate, calling an LLM, or generating an answer. `populator.resolve_spine` (built on the extracted `_spine_from_ctx`, the single source of truth for the spine shape) writes NO `log_query` audit row on success OR failure — unlike `build_whitepaper`, which logs exactly one. Refuse-over-guess: an unresolved or mismatched application 422s with the resolver's own detail (what WAS found), and no scope is set. The endpoint joins the `require_user` protected router and the `/assemble`-style rate limit because it hits live FDA sources. Asserted by `tests/test_resolve_api.py`: bare-spine 200 with no `audit_id`/answer, mismatch 422, 401, 429, and ZERO `QueryLog` rows on both the success and failure paths (the no-audit invariant).
- **The "Under review" bar is resolve-backed (commit c5e7f93).** The bar's inline picker calls `resolveProduct()` → `POST /resolve` and pins only the canonical `{normalized_name, application_number}` the resolver returns; a 422 is shown inline and leaves the scope unset, so an unresolvable pair can never be pinned. White Paper and Watch canonicalize their own setters (normalized name + six-digit appl) so a product pinned from any surface collapses to one `rp=`/`appl=` value. A multi-agent adversarial review of this change confirmed four findings (Watch appl canonicalization, empty-name fallback, cancel race, the 429 test), all fixed before commit.

## Refusal threshold 0.30 revalidation (Jun 25 2026) -- PROPOSED, pending human sign-off

> STATUS: **PROPOSED -- pending human sign-off.** This entry records a
> RECOMMENDATION only. `refusal_score_threshold` is UNCHANGED at `0.30`
> (`config/settings.py:142`). No runtime code, settings value, or Fly secret was
> modified. Full packet: `docs/THRESHOLD_VALIDATION_2026-06-25.md`.

- **Recommendation: KEEP 0.30 (provisional); generate the real sweep before any retune.** The advisory threshold sweep (`src/regwatch/eval/threshold_sweep.py`, wired NON-GATING into `watch-daily.yml`) has **never run in the production OpenAI-1536 + pgvector space**, so there is **no measured cosine distribution** to retune against. Verified 2026-06-25 against repo `Hussain0327/amneal`: the 7 most recent `watch-daily` runs are all 8-11s and take the `skipped (secret not configured)` path (the `threshold sweep` + `upload threshold sweep artifact` steps both have conclusion `skipped`), `artifacts total_count = 0`, and `gh run download -n threshold-sweep` returns "no valid artifacts found" for every run. Root cause: the repo secret `WATCH_DATABASE_URL` is unset, so every real step is gated out by `if: env.DATABASE_URL != ''`.
- **Why KEEP (lowest regret).** `0.30` was calibrated in the bge-384 era; prod now embeds OpenAI-1536 (a different cosine distribution), and CI structurally cannot revalidate it (CI runs the 1536-dim `echo` test provider against pgvector, not real OpenAI embeddings; see R5 note above). With NO real data, retuning would be guessing in an unmeasured vector space -- exactly the "fill gaps from memory" failure the project forbids. 0.30 is the long-standing operating point, the offline eval gate (`refusal_accuracy >= 0.95`, `tests/test_eval_gate.py`) is green against it, and there is no evidence of a prod regression. The harness's own selection rule never recommends a value that refuses an item 0.30 currently answers (it only ever recommends safer-or-equal), so "no data" correctly defaults to "no change."
- **INV-1 trigger to RAISE.** The instant a real `threshold_sweep.json` exists: if `recommendation.leaking_at_current` is non-empty (a must-refuse / wrong-drug / absent-product item ANSWERS at 0.30), that is an active cross-drug leak -> RAISE the threshold above the highest leaking must-refuse `max_score`. Per INV-1 (a wrong cited answer is worse than a wrong refusal) this dominates any over-refusal concern; err toward refuse. 0.30 is explicitly **provisional**, not validated, until that artifact exists.
- **To regenerate (run in a prod-credentialed env, not CI):** set the `WATCH_DATABASE_URL` + `OPENAI_API_KEY` repo secrets then `gh workflow run watch-daily.yml` and download the `threshold-sweep` artifact; OR run directly: `EMBEDDING_PROVIDER=openai DATABASE_URL=<prod pooler URL ?sslmode=require> OPENAI_API_KEY=... uv run python -m regwatch.eval.threshold_sweep --out threshold_sweep.json` (R5 note: `REQUIRE_DATABASE_URL` no longer exists — `DATABASE_URL` is unconditionally required). Then fill the table in `docs/THRESHOLD_VALIDATION_2026-06-25.md` and apply the decision framework there.

## SSE progress streaming + open operational probes (Jun 29 2026)

- **`POST /query/stream` is live as progress streaming, not answer-token streaming.** The backend now exposes an authenticated SSE endpoint that shares `ask()` with `POST /query`, emits progress `status` frames while the pipeline runs, and emits exactly one terminal `result` frame using the same validated `QueryResponse` serializer as the blocking endpoint. The frontend still falls back to `POST /query` if the stream closes before a result frame. This supersedes the Jun 16 "no fake streaming" note: token-delta answer streaming remains open, but progress streaming is implemented.
- **INV-1 still controls the boundary.** The SSE endpoint never emits answer text before citation validation; answer text appears only in the terminal validated result frame. The turn still writes exactly one audit row through the shared `ask()` path.
- **Operational endpoints are deliberately outside the protected router.** `GET /health`, `GET /ready`, and `GET /metrics` are app-level probes. `/metrics` is open by default for scraper compatibility and becomes bearer-gated when `METRICS_TOKEN` is set. Product/data endpoints remain behind `require_user`.

## R5 — SQLite/Chroma dual-mode deleted; Postgres + pgvector is the only datastore

- **The dual-mode toggle is gone.** `DATABASE_URL` is now unconditionally mandatory — `store/db.py` refuses to build an engine when it is empty, with no `REQUIRE_DATABASE_URL` flag and no SQLite/Chroma fallback. Tests run against `TEST_DATABASE_URL`, a disposable local Postgres, instead of an in-process SQLite/Chroma pair.
- **The vector column is `vector(1536)`, so the `echo` test provider became 1536-dim** to keep it usable in tests without tripping the pgvector dimension assert (`assert_embedding_provider_dim`, the "K6" check) that this same cutover made unconditional. `local-bge-small` (384-dim) is rejected against the app datastore at boot; it remains available only for offline/eval tooling.
- **`compose.yaml` gained a `db` service** (the `pgvector/pgvector:pg17` image, same family as CI's service container) and `EMBEDDING_PROVIDER`'s Compose default flipped to `openai` (from `local-bge-small`) to match the mandatory 1536-dim table.
- **`/health`'s `components` key renamed `chroma` → `vector_store`.** Same shape otherwise (`ok`, `corpus_count`); it now always reports pgvector.
- **`scripts/migrate_to_supabase.py` and `scripts/restore_drill.sh` were deleted** as one-time SQLite/Chroma-to-Supabase migration tooling with no further use once the dual-mode path is gone. A PG-native `pg_dump`/`pg_restore` drill script is the noted open follow-up (`docs/DEPLOY.md` §6.4) — the monthly restore drill is a manual Supabase-backup restore in the meantime.
- **GitHub Actions cron remains the sole scheduler.** Dagster (`src/regwatch/orchestration/`, already dormant before R5) was deleted entirely as part of this cutover; nothing in that deletion changed the watch-daily cron, which was already the production scheduler.
- **Docs updated for R5:** `README.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`, `docs/DOCKER.md`, `docs/CLAUDE.md`, `docs/PROD_READINESS.md`, `docs/POLYGLOT_TARGET_2026-07-10.md`, and `.env.example` all had their SQLite/Chroma/Dagster/migration-script references corrected or annotated `(removed in R5)`; earlier entries in this log are left as the historical record of the pre-R5 design.

## Token-delta streaming for Ask (Jul 2 2026)

- **`POST /query/stream` now emits provisional `token` delta frames** between
  the progress frames and the terminal frame, so the UI renders a live draft
  while synthesis runs. The deltas are cosmetic: the ONLY authoritative answer
  remains the single validated terminal `result` frame (same serializer as
  blocking `POST /query`), so INV-1 -- no authoritative answer text before
  citation validation -- holds at the authority boundary, and each turn still
  writes exactly one audit row. Supersedes the Jun 29 note that token-delta
  streaming was an open item.

## Polyglot strangler -- Go owns the public edge and /query orchestration (Jul 10-24 2026)

- **Four-runtime target approved (Jul 10):** TS/Go/Python/Rust via a 9-step
  strangler plan (docs/POLYGLOT_TARGET_2026-07-10.md). Python keeps the
  stateless RAG core; Go takes the public edge and the control plane.
- **Steps 0-4 (Jul 10-21):** the Go proxy took the public port (two Fly process
  groups, with the dual-stack `regwatch serve` listener behind it), then auth
  and sessions, then feedback/settings/products -- all served natively from a
  sqlc store over the SAME Postgres; the Python copies were deleted
  (net -1,594 lines on the auth cutover alone).
- **Cross-service contract harness (Jul 22, #123):** `tests_contract/` boots
  the real Go proxy + uvicorn + Postgres and pins the public wire contract
  (S1-S23) across the runtime boundary, so a cutover cannot silently change
  the API surface.
- **Step 5 (Jul 23-24, #124/#126/#127):** Go serves `POST /query` natively --
  it runs the gates (401/422/429/ownership 404), persists and finalizes the
  `query_log` audit row, and calls Python's token-gated
  `POST /internal/query/compute` (INTERNAL_RAG_TOKEN, fail-closed; the
  /internal/ subtree is never exposed at the edge). Flip proven live Jul 24;
  `GO_NATIVE_QUERY = "true"` is pinned in fly.toml [env], so instant rollback
  is `fly secrets set GO_NATIVE_QUERY=false -a amneal` (secrets override
  [env]). Remaining: the Python legacy-path deletion PR, R3 (stream
  terminal-frame move), steps 6-9.

## Open-model machinery shipped dormant -- embedding profiles + Databricks providers (Jul 23 2026, #125)

- **Migration 0015 adds profile-keyed chunk embeddings:** `chunk_embedding`
  rows keyed by an immutable named profile (`vector(d)` up to 2000 dims,
  `halfvec` above), so adopting a new embedder is a blue/green re-embed into a
  shadow profile plus an `ACTIVE_EMBEDDING_PROFILE` flip -- never an in-place
  rewrite of the legacy `vector(1536)` column.
- **`Qwen3EmbeddingProvider` + `DatabricksProvider` shipped fully dormant:**
  legacy profile active, OpenAI providers still serving, no endpoints
  configured. The machinery exists to serve the D1 residency move, not a
  model preference.

## D1 data residency -- Databricks inference plane adopted; generation flipped to gpt-oss-20b (Jul 28 2026)

- **Verdict: inference plane ONLY.** Databricks Model Serving hosts the models
  inside the company tenant; Supabase stays the datastore. Lakebase received a
  full, verified staging snapshot but is NOT live and takes no writes. Full
  analysis and cost model: docs/DATABRICKS_ADOPTION_2026-07-28.md.
- **Prod generation flipped the same day:** `LLM_PROVIDER=databricks`; ONE
  small open-weight model (endpoint alias `workspace.default.regwatch`,
  served id `gpt-oss-20b-080525`) serves router, synthesizer, and extractor.
  OpenAI is the rollback path (`fly secrets set LLM_PROVIDER=openai`, ~60s)
  and still serves embeddings -- the one remaining exfil point, plus the
  watch cron's OpenAI env.
- **Same-day truncation incident -> two knobs (merged Jul 29, #138):**
  gpt-oss-20b spent the whole 900-token budget thinking and returned
  finish_reason=length on every turn. `DATABRICKS_REASONING_EFFORT` (default
  `low` -- the only level measured to finish under the cap) and
  `SYNTHESIZER_MAX_TOKENS` (default 900, pinned so the OpenAI rollback path
  stays byte-identical).

## D1 runtime served-model guard (Jul 29 2026, #138)

- **The guard checks what the endpoint REPORTS, not what the config says.** A
  Unity Catalog alias can be repointed with no deploy, so every completion and
  stream is checked against `D1_ALLOWED_LLM_MODELS` using the served model id
  in the response; the allowlist must carry BOTH names (alias AND served id)
  or the first armed boot refuses every turn.
- **Violations raise a dedicated `D1ResidencyError`** that `stream()`'s SSE
  fallback re-raises instead of swallowing: the fallback's buffered retry
  would re-send the analyst question to the very endpoint the guard fences
  off. The boot guard additionally refuses half-migrated configs (generation
  moved but query embedding not, or vice versa) once armed.
- **Shipped LIVE but UNARMED** (auto-deployed Jul 29 14:41 UTC;
  `D1_ENFORCED` unset). Arming waits for the embedding flip so the
  both-halves boot rule can pass. Migration 0016 (`query_log.latency_ms`)
  rode along as the measurement column for the single-model router-latency
  verdict.

## Qwen3 embedding Model Service created (Jul 29 2026)

- **`workspace.default.regwatch-embed`:** Databricks pay-per-token Model
  Service running Qwen3-Embedding-0.6B -- 1024-dim native (fits
  `vector(1024)`, no halfvec needed), served id `qwen3-embedding-0-6b-112025`,
  verified by a live call. Pay-per-token chosen over provisioned throughput
  (which this workspace does not enable anyway): zero idle cost and no
  scale-from-zero cold start on the interactive path.
- **Not wired into the app yet.** Next: a 1024-dim embedding profile, a
  resumable corpus re-embed runbook, a retrieval benchmark against the legacy
  1536 profile, then the `ACTIVE_EMBEDDING_PROFILE` flip -- and only then
  arming `D1_ENFORCED`.

## Tier-1 knowledge graph landed; graph-assisted retrieval proposed (Jul 30 2026)

- **Landed foundation, zero query behavior change.** Migration
  `0018_knowledge_graph` and `store/graph_store.py` derive deterministic
  `application`, `psg_doc`, and `psg_section` nodes; `HAS_PSG`, `HAS_SECTION`,
  and `FOLLOWS` edges; and `primary` / `member` references back to source
  chunks. Derivation shares the caller's chunk-write transaction. There are no
  node embeddings and no runtime traversal consumer.
- **Chunks remain the only evidence authority.** Graph nodes, edges, paths, and
  any future generated descriptions may navigate to evidence but can never be
  cited or sent to generation as a substitute for source chunks (INV-1).
- **Proposed runtime shape.** Start with product/form/current-version-scoped
  seed chunks, map them to graph nodes, traverse allowlisted edges within hard
  hop/candidate/token budgets, collect referenced chunks, rerank, and test
  evidence sufficiency. One targeted additional expansion may run for a named
  missing aspect; incomplete evidence still refuses.
- **No promotion before measurement.** Runtime traversal stays behind a
  default-off flag until the Q&A eval set is expanded and a shadow comparison
  proves reduced false refusals or ranking misses with zero product/form/version
  leakage and no citation-precision regression. Full design:
  `docs/GRAPH_ASSISTED_RETRIEVAL.md`.

## Evaluation evidence correction; 0.30 remains provisional (Jul 30 2026)

- **The documented `0.917` was mislabeled.** The inspected watch artifact's
  value was `threshold_sweep.current_decision_accuracy`, not
  `run_eval.refusal_accuracy`. The old sweep put the valid `must_clarify` case
  in the must-answer bucket and counted its correct clarification as a
  threshold-induced answer failure.
- **Current provider-backed `run_eval` status is unknown.** The latest inspected
  CI run passed the deterministic offline fixture but skipped provider-backed
  seed/eval because the repo-wide `OPENAI_API_KEY` was absent. No current
  live-corpus pass or fail claim is accepted without a preserved run artifact.
- **The live sweep did not calibrate a cutoff.** All six ordinary must-answer
  rows had cosine scores (0.812-0.896), while all five must-refuse rows stopped
  before retrieval and had no score. Resolver and scope refusals are useful
  safety evidence but cannot define a boundary in cosine-score space.
- **Evaluator correction.** `must_clarify` rows are now excluded from the
  numeric cutoff curve, and a no-score clarification is no longer silently
  relabeled as a refusal. The harness returns no recommendation when either
  scored distribution is empty, instead of calling absent negative scores
  "cleanly separable" and recommending `0.00`.
- **Decision: KEEP `0.30` PROVISIONAL.** Add reviewed scored hard negatives,
  expand the 12-row Q&A gold set, and rerun both provider-backed `run_eval` and
  the corrected threshold sweep on a controlled corpus snapshot before any
  threshold change. Current evidence: `docs/EVAL_STATUS.md`.

## DefPredict integration + recommendation-policy amendment (Jul 30-31 2026)

- **Deficiency analysis joins the product.** The other intern's DefPredict
  pipeline (DevDesai444/deficiency-chatbot, commit bdad5c5: PyMuPDF parse ->
  heading section split -> 4-stage deterministic-first fault detection) is
  vendored into `src/regwatch/deficiency/` and rewired onto regwatch seams
  rather than run as a second service: LLM calls go through
  `regwatch.generate.llm` providers (so the D1 served-model guard covers
  them -- the upstream raw OpenAI SDK stack would have bypassed it),
  precedents come from pgvector (`deficiency_kb`, Qwen3 1024-dim in-tenant
  embeddings), and job state is the `deficiency_run` table (migration 0019).
- **Owner amendment to the "no regulatory recommender" rule (docs/CLAUDE.md
  Hard rule 3 / the "Don't" list).** Recorded 2026-07-30 from the product
  owner: recommendation output IS now allowed. The refuse-or-cite discipline
  stays -- every recommendation must carry citations or not exist -- and the
  system should prefer escalating / gathering more context over dead-ending
  in a refusal. Scope note: the vendored pipeline currently emits NO
  recommendations (upstream prompts forbid proposing fixes); this entry
  authorizes the future recommender work, it does not ship one.
- **Public data first.** Uploads are expected to be public documents while
  the pilot runs; the app still treats every upload as sensitive (D1: parsing
  and all inference stay in-tenant, the PDF bytes never persist -- only a
  sha256 -- and the temp file is deleted when the run ends).
- **In-process background analysis is a deliberate exception** to "the Fly
  image never parses a PDF": upload->analyze runs as a background task inside
  the API process, bounded by `deficiency_analyze_concurrency` (limiter),
  `deficiency_analyze_timeout_s` (deadline; compare-and-set state transitions
  make the abandoned worker thread harmless), and
  `deficiency_run_stale_minutes` (read-time reinterpretation of rows stranded
  by a restart). A durable queue is the known upgrade path if volume arrives.
- **Progress events are logs, not WebSockets.** Upstream's in-process event
  bus was dropped; the UI polls the run row. A durable event feed can replace
  `deficiency/events.py` without touching the vendored detection code.

## Constrained AI guidance for every healthy Ask turn (Aug 4 2026)

- **One valid turn, one AI role.** Every healthy Ask message now gets exactly one
  model role and contract. An answerable, sufficiently grounded retrieval result uses the
  synthesizer; a pre-synthesis product, dosage-form, scope, capability,
  vague-input, or weak-retrieval outcome uses the configured `router` role as a
  guidance planner. Operational errors are not AI-routed, and a post-synthesis
  refusal never triggers a second model role. The existing bounded truncation
  retry may repeat the same structured completion without changing authority.
- **The application retains authority.** Product resolution, dosage-form
  selection, regulatory-scope policy, status/reason, filters, citation gates,
  and user-visible copy remain deterministic. The guidance model can select only
  one server-allowlisted `next_step` and up to three IDs for options already
  created by the application. It cannot author display prose, invent an option,
  change the route, or produce an uncited regulatory claim.
- **The 0.30 boundary still blocks answer generation.** Removing the threshold
  would let irrelevant but real passages give unsupported claims plausible
  citations. Below-threshold passages therefore go to neither the synthesizer
  nor the guidance planner; the planner sees only the question and trusted route
  context. This supersedes Phase 2's old “pre-LLM refusal” behavior without
  weakening INV-1 or INV-2.
- **No live-model result is claimed here.** This decision records the shipped
  prompt/schema boundary and its deterministic validation; provider-backed
  conversational quality remains an explicit evaluation step.

## v6 prose synthesis ships dark; live evals serialize (Aug 7 2026)

- **Format A/B, not a policy change.** `REGWATCH_PROSE_SYNTHESIS` (default
  off) switches synthesis from the v5 claims-JSON envelope to v6 natural prose
  with model-facing `[n]` markers, parsed server-side (`generate/prose_turn.py`)
  and admitted by the same gate. The refuse-or-cite policy is unchanged in
  BOTH modes: every rendered sentence is cited or the turn declines. The gate's
  lexical re-stamp corrector is live under the flag (a corrected claim is a
  cited claim); the uncited-downgrade path is explicitly off
  (`admit_turn(downgrade_uncited=False)`) because serving gate-framed uncited
  prose is the v7 selective-citation policy shift, not a format change.
- **The wire is untouched.** Rendered answers keep canonical
  `[SHORT_NAME, p.N]` markers plus the `Sources:` trailer; audit rows stamp the
  flag-active prompt identity (v5/v6) so cohorts are distinguishable; the new
  parse/corrector forensics ride inside the existing `route_json["synthesis"]`
  and `route_json["turn"]` blocks.
- **Exactly one live Databricks eval at a time.** The blocking CI eval moved
  out of `lint-type-test` into `.github/workflows/databricks-eval.yml`,
  entered via `workflow_call` (blocking arm, prose off) and `workflow_dispatch`
  (dark v6 arm, prose on), all under the non-canceling `databricks-eval`
  concurrency group. Concurrent evals collide on shared workspace QPS, and a
  dispatch eval must never cancel PR CI. RUNBOOK: never add a live-eval step
  outside this group; if branch protection pins required checks by name, add
  the `databricks-eval / eval` check alongside `lint-type-test`.
- **An empty flag secret reads as OFF.** `REGWATCH_PROSE_SYNTHESIS=""` falls
  back to the default instead of failing settings validation at boot -- the
  rollback story is "unset the secret", and a blank value must not become an
  outage.

## Conversation-first routing with application-validated scope (Aug 10 2026)

- **The accepted design supports explicit bounded corpus questions.** A user
  may ask across the inhalation PSG corpus without naming one drug. This is not
  a search-everything escape hatch: corpus intent must be positive, map to an
  allowlisted corpus policy, and expand to a non-empty bounded set of current
  document versions before retrieval can execute as `EXACT_CORPUS`. Execution
  remains deferred to PR12; the PR11b stage below only measures the proposal.
- **The model proposes; the application authorizes.** The route contract may
  return a standalone question, conversational mode, scope hint, product hint,
  and allowlisted corpus-policy hint. It cannot return filters, document IDs,
  version IDs, or an executable retrieval mode. Deterministic product
  resolution and scope compilation alone may produce `EXACT_SCOPED` or
  `EXACT_CORPUS`; missing, conflicting, or ambiguous scope clarifies.
- **No product is not corpus permission.** `What are the bioequivalence
  requirements?` remains ambiguous and must clarify. Route failure returns to
  deterministic product resolution or clarification; there is no raw-question
  broad-retrieval fallback.
- **Session scopes do not contaminate each other.** A corpus turn never
  overwrites the active product filters. Corpus scope may be inherited only
  from a prior audited corpus-scoped turn carrying its validated policy and
  prior version ledger, never from a model guess; executable membership is
  re-expanded against the current-version catalog on every follow-up.
- **#163 measures routing before ranking.** The five product-less rows must
  first reach bounded `EXACT_CORPUS`; the beclomethasone row must remain
  `EXACT_SCOPED`; outside-set passage leakage must be zero and per-source
  application provenance must survive. Duplicate capping remains deferred
  unless those executed traces show duplicate members displacing required
  evidence from the top eight.
- **PR11b is observation only.** `REGWATCH_ROUTE_CALL` defaults to `off`.
  `shadow` makes one bounded route call and deterministically compiles its hint
  for audit, but neither the standalone rewrite nor compiled scope is read by
  retrieval, response rendering, or session updates. Even the reserved `live`
  value is forced to effective `shadow` until PR12. Non-residency failures are
  logged, counted, and ignored; a D1 residency violation stays fail closed.

## v7 selective citation ships dark; the NO_EVIDENCE sentinel is abolished for v7 (Aug 10 2026)

- **A policy change, not another format A/B.** `REGWATCH_SELECTIVE_CITATION`
  (default off, honored only when `REGWATCH_PROSE_SYNTHESIS` is also on)
  replaces v6's refuse-or-cite policy with three epistemic kinds per sentence:
  SOURCE FACT (cite-required, unchanged from v6), REASONING (model-framed,
  admitted uncited), and CONVERSATION (plain, admitted uncited). A turn with
  zero admitted source facts renders as the model's own conversational
  decline rather than the shared `NO_EVIDENCE` code word -- v7 has no
  sentinel to mangle, which is the direct fix for the 11-of-11 v6 refusal
  defect (a bare `NO_EVIDENCE` with no terminal period truncates to zero
  parsed sentences and degrades to `malformed_structure`).
- **Two lexicons keep INV-1 intact once uncited text is admitted by design.**
  `turn_gate.MATERIALITY_WORDS` (existing) and the new
  `SOURCE_ASSERTION_WORDS` (verb-anchored -- "recommends", "requires",
  "states", not noun-bearing "guidance"/"fda", which measurably fired on 3 of
  the design's own 4 uncited exemplar sentences) both reclassify an uncited
  sentence back to `source_fact` before it can render, on the gate's
  drop-or-correct path. An admitted `source_fact` is unaffected by any of
  this: still cite-required, still `DROP_NO_CITES` when uncited, never
  downgraded.
- **The decline re-crosses the gate boundary.** `turn_gate.render_decline`
  re-scans every sentence of an admitted conversational-decline turn against
  both lexicons immediately before it can be served, independent of whatever
  `kinds` the caller supplied. A guard fire falls back to the existing
  canned refusal copy (never partial disclosure of the guarded text) and is
  ledgered (`decline_guard`) so the fallback rate is measurable. On the
  natural path (parser and gate agree) the guard essentially never fires --
  a materially-worded or source-asserting sentence is already reclassified
  `source_fact` upstream and drops on `no_valid_citations` before reaching
  this function.
- **Every new byte is conditional, so flag-off stays byte-identical.**
  `renderer_version` (2, `RENDERER_VERSION_SELECTIVE`) and the ledger's
  `kind_counts`/`decline_guard` keys are emitted only when selective mode
  actually ran; the `prose_parse.kinds` telemetry key is emitted only under
  selective, protecting `tests/test_prose_synthesis.py`'s exact-dict pin on
  the v6 ledger shape. A full `pytest -q` run with both flags off is
  byte-for-byte the same suite that passed before this build.
- **The decline never forks to clarify.** `VERDICT_NO_EVIDENCE`'s existing
  resolved-by-name clarify branch is untouched; the new
  `VERDICT_CONVERSATIONAL_DECLINE` always calls `_refuse`, never `_clarify`,
  because `_clarify` replaces the answer text with application copy and
  would discard the model's prose -- the entire feature. The accepted
  product delta: a resolved-by-name v7 decline no longer offers clarify
  pills; a later PR owns humanizing that affordance.
- **`eval/metrics.py` and `eval/run_eval.py` are untouched.** The v7 arm is
  scored by the exact code that scored v6, so the comparison is legitimate.
  `sentence_citation_rate` is expected to fall (uncited reasoning/
  conversation is the feature); kind-aware `faithfulness` must not.
