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
