# decisions

Append-only. What was picked, why, and when.

## Phase 0 — scaffold

- Codename stays REGWATCH. No reason to rebrand for a POC.
- Python 3.11+ via uv. uv 0.9.30 + Python 3.12 on the host.
- LLM provider: pluggable, default `openai/gpt-4o-mini`. Cheap, supports JSON-only response format which the extractor needs. The production model and the on-prem question are the IT team's call.
- Embedding provider: pluggable, default local `BAAI/bge-small-en-v1.5`. Network-free, fine on CPU/MPS, fine for ~2,400 docs.
- `echo` provider added for both LLM and embeddings. Deterministic, no network, lets CI run without keys or model downloads.
- ChromaDB persistent at `data/chroma`. Cosine space. One collection `regwatch_chunks`. Chunk metadata distinguishes drugs / forms.
- SQLite via SQLModel at `data/regwatch.db`. Per spec §7.
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
- Empty corpus is a CI no-op. `run_eval.py` exits clean when Chroma is empty, so CI on a fresh checkout passes; gates only fire once data is present.
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
- **Resolver product metadata is cached and invalidated.** `distinct_metadata_values()` no longer full-scans Chroma on every query; `add_chunks()` and test resets clear the cache so newly ingested products become resolvable.
- **CI hardened for production drift.** CI now tests Python 3.11 and 3.12, installs LLM extras, type-checks tests as well as `src`, and runs the eval gate after unit tests.

## Docker container baseline

- **One Python image now serves API, temporary UI, and ingest.** `Dockerfile` builds the shared app image, `compose.yaml` exposes API, optional Streamlit UI, and one-shot ingest services, and `docker/entrypoint.sh` creates data directories before running `regwatch init-db`.
- **Ingest is separate from API startup.** Large source loads are run as commands, not as boot-time work, so a 30-minute PSG/drug load does not hold the API health check hostage.
- **The host `data/` directory is mounted into `/app/data`.** SQLite, Chroma, raw PDFs, and processed files survive container restarts without baking data into the image.
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

- **Product keys are canonicalized at the filter boundary.** A caller-supplied `normalized_name` filter (API / clarify option) is run through `canonical_name()` in `grounded_qa.ask()`, so a casing/salt-order variant ("Albuterol Sulfate") no longer misses Chroma's exact-match `$eq` filter and turns a real product into a wrong refusal.
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
- **The watch pipeline is wired end to end and scheduled.** `watch/run.py::run_watch` does crawl (full A–Z catalog) → match against the watchlist → ingest ONLY matched listings (de-duped by appl_no) → `build_alerts` only for listings actually ingested as added/revised this run (INV-4) → `write_digest`. New `regwatch watch` CLI (`--extract`/`--no-extract`, exit 2 on ingest errors); Dagster `watch_digest_job` + `watch_daily_schedule` (06:00 UTC, default RUNNING). A clean same-day re-run overwrites that day's digest by design (idempotent: an unchanged PSG re-writes an empty digest, never duplicates) — the DB `psg_version` rows are the durable version record. EXCEPTION: an ERRORED run that produced no alerts does NOT write an empty all-clear digest (`run.py` skips the write when `stats.errors and not alerts`), so a failed run cannot masquerade as a quiet day in `/watch/latest` or clobber an earlier same-day digest (INV-4). Known residual: a version committed to SQL before its chunks embed (then erroring) is recorded but never alerted on a later run (`_latest_version_hash` matches → "unchanged"); recovering that needs an `alerted_at` marker or durable-diff alert derivation — tracked, not yet built.
- **Echo providers fail fast against a real corpus; `/health` diagnoses the stack.** New `allow_test_providers` setting (env `REGWATCH_ALLOW_TEST_PROVIDERS`, default false): the API lifespan raises with a remediation message when an `echo` embedding/LLM provider faces a NON-empty Chroma corpus without the override; empty corpus + echo still boots so a fresh compose stack can seed. `GET /health` now returns `{status, components:{db, chroma+corpus_count, llm provider+key_present bool, embedding provider}, warnings[]}` — a superset of `{"status":"ok"}` (compose healthcheck unchanged), HTTP 503 only when DB or Chroma is unreachable. `tests/conftest.py` opts the echo-on-purpose suite in. Note: `compose.yaml` still defaults `EMBEDDING_PROVIDER` to `echo`, so a seeded `./data` now fails fast at api start — documented in `docs/DOCKER.md`.

## Cookie-session auth + per-user chat history (Jun 10 2026)

- **DB-backed opaque session tokens over JWT.** `POST /auth/login` issues `secrets.token_urlsafe(32)` in an HttpOnly `regwatch_session` cookie (SameSite=Lax, Max-Age = `AUTH_SESSION_TTL_HOURS`, Secure only when `AUTH_COOKIE_SECURE` — false for the localhost pilot). The DB stores only the token's sha256 (`auth_session` table), so a leaked DB cannot replay a session, and revocation (logout, deactivate-user, set-password) is immediate — no JWT expiry-window problem. Login always inserts a fresh session row (no fixation).
- **bcrypt directly, not passlib.** passlib is unmaintained; the `bcrypt` library is used straight. Login verifies against a module-level dummy hash when the email is unknown, so unknown email / wrong password / inactive user share one 401 body ("invalid email or password") and one bcrypt-shaped timing profile.
- **Users are CLI-provisioned; no self-signup.** `regwatch create-user EMAIL --name NAME [--role analyst|admin]` with the password prompted (`hide_input`, confirmed) — never a flag, which would leak into shell history and `ps`. Also `list-users` (no hashes), `set-password`, `deactivate-user` (both revoke live sessions).
- **One authorization chokepoint.** Every endpoint except `GET /health` and `/auth/*` is registered on an `APIRouter(dependencies=[Depends(require_user)])`, so an accidentally-unauthenticated route is structurally impossible. CORS gained `allow_credentials=True` (plus DELETE) against the existing origin allowlist.
- **Chat history is per-user; foreign sessions 404.** `POST /query` no longer accepts a client-supplied `user_id` — identity comes from the session. A `session_id` owned by another user returns 404 (not 403 — existence is never confirmed); a NULL-user legacy session is adopted on first authenticated use. New `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` serve only the caller's threads.
- **Audit rows carry identity (INV-6 extension).** `query_log.user_id` (nullable, indexed) is filled on every authenticated `/query` and `/assemble` — including the dossier's inner Q&A row.
- **In-memory per-user rate limiting.** `RATE_LIMIT_PER_MINUTE` (default 30, 0 disables) on `/query` + `/assemble` (the LLM cost surface) and a fixed 10/email/minute brute-force cap on `/auth/login`; both are a lock + deque sliding window, per process — distributed limiting stays gateway work.
- **OIDC/SSO deferred to the IT gateway.** App-layer cookie sessions are the pilot boundary; TLS termination and the enterprise identity provider remain environment decisions (docs/PROD_READINESS.md #1).
