# RegWatch Refactor & Improvement Backlog — 2026-07-09

_Source: 19-lane multi-agent audit (12 module deep-reads + 7 cross-cutting sweeps), Opus 4.8 @ max effort, every finding adversarially re-verified against live code. 132 raw findings -> 127 verified survivors -> 120 primaries after dedup. Supersedes/extends the 2026-07-07 backend backlog (whose items are excluded as already-applied)._

## Executive summary

127 findings collapse to 120 primaries after merging 7 duplicate pairs (89->17, 92->0, 116->19, 93->41, 88->54, 113->61, 24->22). The critical cluster is uneven application of the audited-error-boundary pattern that commit 009cc41 established for build_dossier/grounded_qa: build_whitepaper (0) and the ask() retrieve+rerank hot path (17) can still throw a naked, unaudited 500, breaking INV-6. Two 'do-now' frontend/render defects also stand out: the whitepaper template fetch/render path can permanently 500 a working fallback (7, 8) and the whitepaper intake fields freeze after re-scope (71) - all three are high-severity, small, high-confidence. A second theme is INV-5 form-blend correctness in dossier.py (54 hyphen bypass, 55 cross-base-form blend) plus its untestable in-DB-loop placement (94). LLM completion-integrity guards (truncation/terminal-state/empty-choices, 22/23/100/102) are applied only on the OpenAI Responses path. The long tail is duplication (shared-helper extraction), store N+1/schema-recheck perf, tri-state FDA source robustness, and dead-code/doc-drift cleanup - mostly opportunistic. Sequencing hot spots: grounded_qa ask()/meta (17,18,19,21,97), dossier form guard (54,55,94,99), and llm.py chat path (22,102).

## Themes

- **Audited-error boundaries / INV-6 durability** (11) — The fail-soft audited-degrade pattern is applied unevenly across external-call sites; several failure paths still escape as unaudited 500s or drop the run ledger.
- **INV-5 form-blend guard: correctness + seam** (8) — Dosage-form blend guards have real leaks (hyphen spelling, cross-base-form, unpaired form/route sets) and live inside DB loops with two drifting implementations claimed to be in lockstep.
- **LLM completion integrity** (5) — Truncation-must-refuse, terminal-state, and shape guards exist only on the OpenAI Responses path; Anthropic/chat/stream/empty paths can ship truncated or crash on shape.
- **FDA source robustness & tri-state** (12) — Byte caps, response-shape guards, unparseable-input raises, transport retries, and empty-result caching are inconsistently applied, collapsing present/absent/unknown into wrong answers.
- **Store/persistence lifecycle & query perf** (15) — N+1 loads, repeated live schema/has_table round-trips, engine-init races, and duplicated DDL across the two store modules.
- **Duplication / shared-helper extraction** (15) — App-number/letter-code maps, openFDA fetch idioms, sentence splitters, date/scope UI blocks, and INV-8 evidence builders are re-implemented across modules.
- **Frontend correctness / a11y / timeouts** (14) — Re-scope field freeze, missing label associations, unbounded logout/body-read timeouts, silent delete, and generated-union drift between live and reloaded turns.
- **Dead code & doc drift** (11) — Unwired drugsfda import path, dead settings knobs/properties, and spec/README/status copy that no longer matches shipped behavior.

## Tier counts

| Tier | Count |
|---|---|
| Do now | 22 |
| Do soon | 28 |
| Opportunistic | 51 |
| Marginal | 19 |

## Do now (22)

### #1 — ensure_template leaks httpx.InvalidURL (and StreamError) past its except clause -> 500 on a misconfigured template URL

- **Where:** `src/regwatch/whitepaper/template_fetch.py:157`
- **Class:** HIGH severity / S effort / error-handling · lane `wp-rest` · score 8.3 · confidence 0.92 · verdict CONFIRMED
- **Now:** The except tuple enumerates the transport-error family but misses httpx's URL/Exception-rooted errors, so a malformed-config URL bypasses the module's whole 'ANY fetch problem -> structured warning + None + loud fallback' promise (template_fetch.py:11-14).
- **Fix:** Add httpx.InvalidURL (and httpx.StreamError) to the except tuple at line 157: `except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, TemplateTooLargeError) as exc:`. _safe_error already returns only type(exc).__name__ for anything outside TemplateTooLargeError/HTTPStatusError, and the log passes url=_redacted(url), so neither the class name nor the message leaks the signed token.
- **Risk:** None material -- a malformed URL is exactly a broken-config case that should degrade to the loud fallback, and the redaction path already covers the new exception types. Do not broaden to bare `except Exception` (would swallow genuine bugs and drop the house-style explicit tuple).
- **Test:** Call ensure_template(tmp_path/'t.docx', 'https://host/p\nX') (a URL httpx rejects with InvalidURL); assert it returns None, writes no file, and emits the whitepaper_template_fetch_failed log rather than raising.
- **Note:** changes behavior (intended correction)

### #2 — Whitepaper intake fields freeze after a scope is cleared then re-scoped (sibling assemble page already fixed this)

- **Where:** `regwatch/frontend/app/(shell)/whitepaper/page.tsx:150`
- **Class:** HIGH severity / S effort / correctness · lane `fe-ui` · score 8.1 · confidence 0.9 · verdict CONFIRMED
- **Now:** The remembered-scope ref is advanced to the empty value on a clear, desyncing it from the field's actual current value. The assemble page hit the exact same pattern and fixed it by bailing on an empty scope (assemble/page.tsx:30 `if (!scopeRld) return;` with a comment: 'bailing on an empty scope keeps the guard pointed at the field's current value ... rather than desyncing the ref and freezing the field forever'). Whitepaper never got that fix.
- **Fix:** Do not advance a remembered field to an empty value; mirror assemble's per-field bail. Replace line 150 with: `lastScope.current = { rld: referenceProductName || lastScope.current.rld, applNo: applicationNumber || lastScope.current.applNo };`. This preserves the never-blank-on-clear behavior (lines 148-149 unchanged) while keeping the guard pointed at the field's value across a clear, so a later distinct scope is adopted onto an untouched field.
- **Risk:** In a compliance tool this can drive a wrong-product populate: an analyst who scopes 'metformin' in the top bar but sees the stale 'albuterol' still prefilled may click Populate against the wrong product. The scope bar and the field visibly disagree, which is the mitigation, but the divergence is silent. The fix's only edge is the shared limitation of this dirty-guard family (manually typing a value equal to the remembered scope reads as untouched) — present already in both pages, not introduced here.
- **Test:** Render WhitepaperView with a useCurrentProduct mock whose value changes across rerenders: {rp:'albuterol',appl:'020503'} -> {rp:'',appl:''} -> {rp:'metformin',appl:'090111'}, never touching the inputs. Assert the Reference product input value is 'metformin' after the third render. Fails today (stays 'albuterol').
- **Note:** changes behavior (intended correction)

### #3 — Corrupt-but-PK template poisons the on-disk cache into a permanent docx 500 (no render-time fallback)

- **Where:** `src/regwatch/whitepaper/docx_writer.py:263`
- **Class:** HIGH severity / S effort / error-handling · lane `wp-rest` · score 7.6 · confidence 0.85 · verdict CONFIRMED
- **Now:** Magic-byte validation is necessary but not sufficient (a .docx is a ZIP with a specific [Content_Types].xml part), and the render path trusts any present template file with no fallback when python-docx cannot open it. This directly violates the module's own contract: 'A broken storage bucket must never turn a working fallback render into a 500' (template_fetch.py:13-14).
- **Fix:** Wrap the real-template branch in write_whitepaper_docx (the doc = _fill_template(...) call at line 263) in try/except; on failure log a structured warning (e.g. whitepaper_template_unreadable) and fall back to doc = _build_from_scratch(result, inputs) -- the already-documented LOUD path that stamps FALLBACK_MARKER. This guarantees 'never a 500' regardless of how the bad file arrived. Complementary self-healing companion (template_fetch side, in-lane): after the magic check at line 164 also reject bodies that are not loadable, e.g. `import zipfile; if not zipfile.is_zipfile(io.BytesIO(data)): log not_docx; return None` -- stdlib, no new dep -- so the poison file is never cached and the next render re-fetches.
- **Risk:** Behavior change on the corrupt-template path only: it now degrades to the loud scratch document instead of raising (the happy path with a valid template is byte-identical). Scope the except to Exception around the single Document-open/fill call so a genuine _fill_template logic bug still surfaces in tests; keep the warning loud. is_zipfile still passes for a valid-but-non-OOXML zip (e.g. an .xlsx), so the render-side fallback is the categorical guard and must be the primary fix.
- **Test:** Write b'PK\x03\x04' + b'\x00'*64 to a template path, call write_whitepaper_docx(result, template_path=that_path); today it raises BadZipFile. Assert it returns bytes that Document(BytesIO(...)) can open and whose paragraphs contain FALLBACK_MARKER.
- **Note:** changes behavior (intended correction)

### #4 — retrieve() + rerank_passages() are unguarded I/O — a vector-store/embedder stall becomes a naked, unaudited 500 (INV-6 gap in the hot path)

- **Where:** `src/regwatch/generate/grounded_qa.py:1346`
- **Class:** HIGH severity / M effort / error-handling · lane `grounded-qa` · score 6.8 · confidence 0.9 · verdict CONFIRMED _(merges [89])_
- **Now:** The audited-error boundary pattern is applied to only three of the pipeline's external-call sites — `current_dosage_form_routes` (1294-1316), `provider.complete/stream` (1442-1470), and the final `log_query` (1554-1588) — but not to the retrieve+rerank stage that every answerable query passes through. The code comment at 1300-1306 states the exact rationale ('letting the DB error escape would be an unaudited 500 the stream-fallback client re-runs into the same down DB') yet the guard right below it at retrieve() is missing.
- **Fix:** Wrap the `passages = retrieve(...)` / `rerank_passages(...)` block in try/except mirroring the catalog-error path: on Exception, `log.warning("qa_retrieval_error", ...)`, `capture_exception(exc)`, and `return _decline(_refuse, reason="retrieval_error", response_mode="refused", passages=[], status="error", answer_text=_SERVICE_UNAVAILABLE_TEXT)`.
- **Risk:** Intentionally changes the failure-path only: a retrieval outage now returns an audited status="error" refuse instead of a 500, identical to the existing provider-error precedent. Success path is byte-identical. Edge case: `_emit("Reading ...")` progress line won't fire on failure (correct — nothing was read).
- **Test:** In the style of tests/test_provider_failure.py: seed the corpus, `monkeypatch.setattr(qa_mod, "retrieve", <raises RuntimeError>)`, call `qa_mod.ask("What study design is recommended?")`, assert `result.status == "error"`, `result.refused`, `result.audit_id` is set, and a QueryLog row exists with status="error". Fails today (ask raises).
- **Sequencing:** Same ask() region as 18/19/21/97; wrap retrieve+rerank first, then guard resolution (19) and meta reads (18).

### #5 — build_whitepaper is not audit-safe: a mid-build failure or a DB blip during the audit write escapes without an INV-6 row (and can mask the 422)

- **Where:** `src/regwatch/whitepaper/populator.py:2313`
- **Class:** HIGH severity / M effort / error-handling · lane `wp-populator` · score 6.5 · confidence 0.9 · verdict CONFIRMED _(merges [92])_
- **Now:** The audit boundary here was never hardened the way its sibling build_dossier was. Commit 009cc41 split build_dossier into a thin try/except boundary + body and introduced _log_query_safe (assemble/dossier.py:90) so 'every assemble -- refused, assembled, OR errored mid-build -- leaves an audit row'; grounded_qa has the same idiom (_log_query_or_skip, grounded_qa.py:697). build_whitepaper still calls the raw, throwing log_query and has no catch-all boundary.
- **Fix:** Mirror the assemble fix: (1) add a module-local _log_query_safe wrapper (try/except -> log.warning + capture_exception, never raises) and route BOTH log_query calls through it so an audit-write failure can never mask the SpineResolutionError->422 or 500 a successful populate; (2) wrap the body (_build_context + _build_sections + audit) so an unexpected exception writes a status="error" whitepaper audit row (failure-safe) before re-raising. Keep SpineResolutionError re-raised as-is so the route still 422s.
- **Risk:** Behavior changes in two failure corners (both corrections): a DB blip on a name-mismatch now yields the intended 422 instead of a 500, and a mid-build crash now leaves an audit row before the 500. The happy path and the normal 422/200 paths are unchanged. Care: the status="error" row must not itself raise (hence _log_query_safe).
- **Test:** Add tests mirroring tests/test_assemble_audit.py: (1) monkeypatch _build_context to raise RuntimeError, call build_whitepaper, assert it raises AND a QueryLog row with mode='whitepaper', status='error' was written; (2) on a real name mismatch, monkeypatch populator.log_query to raise on first call, assert build_whitepaper still raises SpineResolutionError (not the DB error) so the route can 422. Both fail today.
- **Sequencing:** Covers both the try/except boundary and the final success-path log_query in build_whitepaper (92).
- **Note:** changes behavior (intended correction)

### #6 — Whitepaper intake `Field` has no programmatic label/input association (a11y); assemble's `Field` does it correctly

- **Where:** `app/(shell)/whitepaper/page.tsx:916`
- **Class:** MEDIUM severity / S effort / correctness · lane `fe-ui` · score 5.7 · confidence 0.95 · verdict CONFIRMED
- **Now:** The whitepaper page has its own copy of `Field` that predates / diverged from the assemble page's `Field`, which was written correctly with an `id` prop wired to both `htmlFor={id}` and `id={id}` (assemble/page.tsx:129-150). The correct pattern already exists two files away.
- **Fix:** Add an `id` prop to whitepaper's `Field` and wire `htmlFor={id}` on the label and `id={id}` on the input, exactly like assemble's `Field`; pass stable ids at the two call sites (e.g. `id="wp-rld"`, `id="wp-appl"`). Ids must be unique on the page — both intake and, if ever rendered together, other fields.
- **Risk:** None to behavior/layout. Only caveat: ids must not collide with any other element id on the page (they don't today).
- **Test:** Render WhitepaperView and assert `screen.getByLabelText('Reference product name')` and `screen.getByLabelText('Application number')` resolve to the intake inputs. Both throw today.

### #7 — fact_recall (and faithfulness) are computed but never gated -- a citation-correct, content-wrong answer passes the eval gate

- **Where:** `src/regwatch/eval/run_eval.py:34`
- **Class:** MEDIUM severity / S effort / test-gap · lane `eval-misc` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The metric was introduced without a corresponding threshold entry; recall@k/precision only verify which pages were retrieved/cited, not that the prose states the correct facts, so the content-error class is unguarded.
- **Fix:** Add fact_recall to THRESHOLDS with a conservative floor (it already denominates only over fact-bearing items, so fact-less items don't drag it), or, if it is deliberately advisory, document that in the module docstring so it isn't mistaken for a gate. Leave faithfulness advisory if that is intentional but say so.
- **Risk:** Gating fact_recall could flip CI red on genuine content regressions (the point) but also on tolerant-substring false-negatives; pick the floor from the current gold-set distribution and pin it. Behavior change to the gate.
- **Test:** Gold item whose answer cites the correct source but omits an expected_fact; run with check_thresholds and assert non-zero exit once fact_recall is gated. Today it exits 0.
- **Note:** changes behavior (intended correction)

### #8 — OpenAI embedder disables SDK retries but never retries timeouts/connection errors

- **Where:** `src/regwatch/process/embedder.py:172`
- **Class:** MEDIUM severity / S effort / error-handling · lane `ingest-process` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The design intent (llm_clients.py:15-16 comment: 'embedder ... owns its own retry loop and passes max_retries=0') replaced the SDK's default retries but the replacement loop only handles HTTP-status errors, not transport-level ones the SDK default WOULD have retried. Net effect: the embedder is strictly less resilient to transient faults than both the SDK default and the synthesizer path (llm.py keeps max_retries>=2). Embeddings are pure/idempotent, so not retrying a timeout is a pure loss.
- **Fix:** Extend `_is_retryable` to also treat OpenAI timeout/connection errors as retryable -- import openai lazily and `isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError))` (or check the class names to avoid a hard import), returning True. Keep the 429/5xx branch unchanged.
- **Risk:** A persistent connection failure (bad DNS, key/endpoint down) now burns all 6 attempts with backoff before surfacing, up to ~a minute of added latency for that batch -- same tradeoff the crawler already accepts by retrying `httpx.TransportError`. Idempotent operation, so no double-charge/correctness risk. Behavior change: previously-fatal timeouts now retry.
- **Test:** Inject a fake embeddings client whose `.create` raises a stand-in `APITimeoutError` (no `status_code`) once then returns valid data; assert `embed(['x'])` succeeds with 2 calls. Today it raises after 1 call.
- **Note:** changes behavior (intended correction)

### #9 — Sidebar conversation delete fails silently — no feedback, row stays, user is told nothing

- **Where:** `regwatch/frontend/components/Sidebar.tsx:184`
- **Class:** MEDIUM severity / S effort / error-handling · lane `fe-ui` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The History component has no error surface; the catch swallows the outcome to a boolean used only to decide navigation.
- **Fix:** Add a small per-row error state (e.g. `const [delErr, setDelErr] = useState<{id:string,msg:string}|null>(null)`), set it in the catch with the error message, and render it inline under the row (mirroring the whitepaper RunRow delete-error line). Clear it when a new confirm opens. Keep the existing 401-redirects-centrally behavior.
- **Risk:** Low; adds UI only on the failure path. Keep `refresh()` in a finally so the list still reconciles. Don't clear the active session / navigate on failure (already correct).
- **Test:** Mock `deleteSession` to reject with a non-401 error, click delete then 'yes', and assert an inline error is shown and the session row is still present. Fails today (nothing rendered).
- **Note:** changes behavior (intended correction)

### #10 — Empty alias-discovery result is written to cache and served as authoritative, suppressing discovery until manual refresh

- **Where:** `src/regwatch/watch/aliases.py:113`
- **Class:** MEDIUM severity / S effort / error-handling · lane `watch-retrieve` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** A transient/empty crawl result is persisted with the same trust as a real result, and the non-refresh read treats any root-matching cache (even empty) as final. Note get_aliases() deliberately treats an empty cache as fall-through (env/company fallback), so the two readers disagree on what an empty cache means.
- **Fix:** Only persist a non-empty discovery (`if ordered: cache.write_text(...)`), or symmetrically have the discover read path treat an empty cached aliases list as a miss (re-query) the way get_aliases already does. Smallest safe fix is the `if ordered:` write guard.
- **Risk:** A company genuinely absent from Drugs@FDA would then re-query every call instead of caching []; that is rare and fail-open (re-query) is the safer default here. Blast radius today is limited (the `regwatch aliases` CLI and any direct caller; the watchlist build itself already falls back via get_aliases), but the silent-empty cache is exactly the kind of latent failure the standards warn against.
- **Test:** Patch _fetch to return {'results': []}, call discover_applicant_aliases(cache_path=tmp), assert it returns [] AND that no cache file was written (or that a subsequent non-refresh call re-queries rather than returning the cached []).
- **Note:** changes behavior (intended correction)

### #11 — change_detector.summarize_change LLM call is not isolated, so a transient provider outage aborts ingest of a genuinely-changed PSG revision

- **Where:** `src/regwatch/process/change_detector.py:48`
- **Class:** MEDIUM severity / S effort / error-handling · lane `x-failure` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** A strictly best-effort/cosmetic field (the cited diff summary) is computed on the critical path to committing a new version, and its LLM call was never given the fail-soft isolation that the equally-optional BE extraction call received.
- **Fix:** Isolate the LLM call inside `summarize_change` (or at its call site in pipeline.py): on provider failure, log with error_type and fall back to `None` / the "Initial version ingested" marker string, so the version still commits and gets chunked/extracted. Mirror `_extract_and_save_be`'s try/except-and-degrade shape. diff_summary is already nullable everywhere it is consumed (dossier "Latest change", QueryCitation.diff_summary, watch digest).
- **Risk:** Changes the outcome on the summarize-failure branch from "error" (nothing ingested) to "revised" with a null/marker diff_summary. That is the safer direction (fresh content indexed; only the prose diff is lost and it self-heals on the next run's summarize), but it does alter ingest classification counts and the watch digest for that PSG on that run. Ensure the marker/None path does not later read as a real cited change.
- **Test:** Seed a doc with a prior version, patch `change_detector.get_llm_provider().complete` (or `summarize_change`) to raise, call `ingest_listing` with a listing whose content hash differs, and assert the return is "revised", a new psg_version row exists, chunks_exist() is true, and diff_summary is None/marker. Today it returns "error" and commits no version.
- **Note:** changes behavior (intended correction)

### #12 — Extractor's non-dict-JSON guard is positioned after the .get() that crashes on it

- **Where:** `src/regwatch/process/extractor.py:142`
- **Class:** MEDIUM severity / S effort / type-safety · lane `x-types` · score 5.4 · confidence 0.9 · verdict REVISED
- **Now:** The defensive `isinstance(..., dict)` check guards `raw_fields` but the crashing dict-access is on `payload`, which is assumed to be a dict. OpenAI json_object mode guarantees an object, but the extractor is provider-agnostic and the Anthropic fallback provider (llm.py AnthropicProvider) enforces JSON-object shape by PROMPT ONLY ('Return ONLY a valid JSON object'), so a model that returns a JSON array satisfies json.loads but not the dict assumption.
- **Fix:** Guard payload before the dict-access while preserving the flat-or-nested tolerance: `raw_fields = (payload.get("fields") or payload) if isinstance(payload, dict) else None`. Then the existing `if not isinstance(raw_fields, dict): return ExtractionResult(...)` on line 143-144 handles the non-dict case, making a valid-but-non-dict top-level payload behave like the invalid-JSON path (empty result) instead of raising. This keeps the flat-dict fallback (`or payload`) that the naive proposed fix would have broken. Add a _StubLLM variant returning json.dumps([1,2,3]) (and one returning json.dumps(None)) and assert extract_be returns an all-None ExtractionResult without raising; also keep/add a flat-dict test to prove the fallback still extracts.
- **Risk:** None to the happy path (dict payloads behave identically). The only behavior change is that a valid-but-non-dict payload now returns an empty ExtractionResult instead of raising — which is what the existing isinstance guard already intends.
- **Test:** Add a _StubLLM variant that returns `json.dumps([1,2,3])` (and one returning `json.dumps(None)`); assert extract_be returns an all-None ExtractionResult and does NOT raise. Today it raises AttributeError, so the test would fail before the fix.

### #13 — OpenAI stream() treats a stream with no terminal event (final is None) as a successful completion

- **Where:** `src/regwatch/generate/llm.py:387`
- **Class:** MEDIUM severity / S effort / error-handling · lane `llm-common` · score 5.3 · confidence 0.88 · verdict CONFIRMED
- **Now:** Terminal-state validation in stream() keys off which event TYPES were observed, but never asserts that a completion event WAS observed. The buffered path always has a concrete resp object and validates its status; the streaming path has no 'no terminal state seen' backstop, so the two paths disagree on an un-terminated stream.
- **Fix:** After the event loop and before assembling the terminal chunk, add `if final is None: raise RuntimeError('openai stream ended without a terminal event')`. This propagates through _stream_synthesis into ask()'s try/except and degrades to the audited status='error' refusal, matching the response.failed/incomplete paths that already raise after emitting deltas.
- **Risk:** If a future SDK version legitimately ends a stream without response.completed, working answers would flip to refusals; current SDK always emits it (the existing stream tests rely on response.completed). Empty-text un-terminated streams already refuse via ask()'s empty_completion branch, so only the non-empty un-terminated case changes.
- **Test:** In tests/test_llm_provider.py add a _stream_provider([_event('response.output_text.delta', delta='partial')]) with no terminal event; assert list(provider.stream([LLMMessage('user','q')])) raises RuntimeError. Today it yields a done chunk with response.text=='partial' and the test would fail on regression.
- **Note:** changes behavior (intended correction)

### #14 — Anthropic complete() ships max_tokens-truncated answers as if complete (truncation guard missing)

- **Where:** `src/regwatch/generate/llm.py:444`
- **Class:** MEDIUM severity / S effort / correctness · lane `llm-common` · score 5.1 · confidence 0.85 · verdict CONFIRMED _(merges [24])_
- **Now:** The 'a silently truncated answer must refuse, not ship' invariant is enforced ONLY on the OpenAI Responses path: _complete_responses raises on status in ('failed','incomplete') (llm.py:307-310) and stream() raises on the response.incomplete event (llm.py:374-386). The Anthropic completion path has no analogous guard. grep confirms stop_reason/finish_reason appear nowhere in src/regwatch.
- **Fix:** After building `text`, add `if getattr(resp, 'stop_reason', None) == 'max_tokens': raise RuntimeError(f'anthropic response truncated: {resp.stop_reason}')` mirroring _complete_responses. ask()'s provider try/except (grounded_qa.py:1456) already degrades any provider exception to an audited status='error' refusal, so no new plumbing is needed.
- **Risk:** Behavior change scoped to truncated Anthropic completions: they flip from ship-truncated to audited refusal, which is the direction the invariant demands. Normal completions carry stop_reason end_turn/stop_sequence and are unaffected. Prod runs OpenAI, so live impact is gated on LLM_PROVIDER=anthropic (the documented fallback).
- **Test:** New test in tests/test_llm_provider.py: stub an anthropic client whose messages.create returns content=[text block 'partial'] and stop_reason='max_tokens'; assert AnthropicProvider(...).complete([LLMMessage('user','q')]) raises RuntimeError. Today it returns LLMResponse(text='partial') and the test would fail after a regression that dropped the guard.
- **Sequencing:** llm.py truncation guard; land with 102 (empty-choices) which touches the same chat completion function.
- **Note:** changes behavior (intended correction)

### #15 — DailyMed _paged_listings dereferences resp.json() without the isinstance guard its sibling fetch_media uses

- **Where:** `src/regwatch/sources/dailymed.py:340`
- **Class:** MEDIUM severity / S effort / error-handling · lane `sources` · score 5.1 · confidence 0.85 · verdict CONFIRMED
- **Now:** Response-shape validation was applied inconsistently: one function in the module hardened the `.json()` result against non-dict bodies, the pagination path did not. On the populator/resolve_setid path a non-dict 200 body should read as 'genuinely absent' or a clean HTTP error, but instead surfaces as an AttributeError, degrading the tri-state signal and the failure diagnostics.
- **Fix:** Guard the payload exactly like fetch_media: `data = payload.get('data') if isinstance(payload, dict) else []` (and pass `payload.get('metadata') if isinstance(payload, dict) else None`, though _has_next_page already tolerates non-dict). Apply the same one-line isinstance guard in fetch_openfda_results. Copies the proven pattern already in the file.
- **Risk:** None for well-formed responses (identical output). Only the malformed-body path changes: an AttributeError becomes a graceful empty page / no-next-page. A truly non-JSON body still raises JSONDecodeError (out of scope for this guard).
- **Test:** Mock spls.json to return httpx.Response(200, json=None) (or json=[]); call _spl_listings('NDA020503') and assert it returns [] instead of raising AttributeError. Fails today.

### #16 — DailyMed silently treats an unparseable application number as 'genuinely absent' instead of raising like Orange Book does

- **Where:** `src/regwatch/sources/dailymed.py:308`
- **Class:** MEDIUM severity / S effort / correctness · lane `sources` · score 5.1 · confidence 0.85 · verdict CONFIRMED
- **Now:** Boundary validation was skipped on this path: unparseable input and true absence both collapse to None/[]. This is inconsistent with orange_book._split_application_number (line 284), which raises ValueError('unparseable application number') precisely so that 'no rows' keeps meaning 'queried and absent'. Two sibling FDA handlers treat the same malformed input in opposite ways.
- **Fix:** Make DailyMed match Orange Book: when the caller passed a non-empty application_number but clean_application_number returns None, raise a ValueError rather than returning [] (leave the genuinely-empty '' case returning []). That turns unqueryable input into a visible failure (which the router isolates and the populator sees) instead of a false 'absent'.
- **Risk:** Behavior changes for malformed input: a false 'No' becomes an error. Contract C1 says the populator always sends a valid prefixed number, so production input is unaffected; only garbage input changes outcome. Verify no caller relies on the current silent-empty behavior for blank strings (guard the empty-string case explicitly).
- **Test:** Call DailyMedHandler().search(SourceQuery(application_number='12345678')) (8 digits, unparseable) and assert it raises rather than returning []. Fails today (returns []).
- **Note:** changes behavior (intended correction)

### #17 — Watch scope buttons expose no product context to assistive tech (all read 'scope'/'scoped')

- **Where:** `regwatch/frontend/app/(shell)/watch/page.tsx:494`
- **Class:** MEDIUM severity / S effort / correctness · lane `fe-ui` · score 5.1 · confidence 0.85 · verdict CONFIRMED
- **Now:** The accessible name is the generic action verb; the product identity that distinguishes the rows lives only in adjacent cells, not on the control.
- **Fix:** Add a descriptive `aria-label` naming the product, e.g. `aria-label={scoped ? \`Scoped to ${name}\` : \`Scope to ${name}\`}` on both the watchlist and alert-card scope buttons, and add `type="button"` to the watchlist button for consistency and to prevent any accidental implicit submit.
- **Risk:** None to layout or visible text. The `name` used in the label is the same canonical value the button already writes, so the label can't disagree with the action.
- **Test:** Render WatchPage with two watchlist products and assert `getByRole('button', { name: /scope to <product A>/i })` and `.../B/i` both resolve (distinct accessible names). Today both buttons share the name 'scope' and the query is ambiguous.

### #18 — Shortages dosage_form post-filter runs on an already-capped result set, silently under-counting form-specific shortages

- **Where:** `src/regwatch/sources/shortages.py:40`
- **Class:** MEDIUM severity / S effort / correctness · lane `sources` · score 4.9 · confidence 0.82 · verdict CONFIRMED
- **Now:** dosage_form is deliberately a post-filter (the comment on lines 56-58 correctly explains why it must not be an openFDA search term), but the fetch budget was not widened to compensate. The sibling handlers get this right: orange_book filters while iterating the full local file, and psg over-fetches `limit * 5` before filtering. Shortages fetches exactly `limit` then filters, so the cap bites before the filter.
- **Fix:** Over-fetch before the post-filter and trim after it, mirroring psg: fetch with a larger budget (e.g. fetch_openfda_results(..., limit=query.limit * 5)), apply the dosage_form filter, then slice to query.limit. Smallest change that restores correct counting for the one handler that post-filters a capped set.
- **Risk:** Slightly more data transferred from openFDA per shortage query (payloads are small). No change when dosage_form is unset. Behavior intentionally changes: form-specific queries now return records they previously dropped.
- **Test:** Mock the shortages endpoint to return limit+2 rows where the first `limit` are dosage_form='INJECTION' and the last two are 'TABLET'; call search(SourceQuery(active_ingredient='x', dosage_form='tablet', limit=<small>)) and assert both TABLET records are returned. Fails today (they fall outside the fetched cap).
- **Note:** changes behavior (intended correction)

### #19 — refusal_accuracy counts a wrong clarify on an answerable item as a correct decision

- **Where:** `src/regwatch/eval/metrics.py:256`
- **Class:** MEDIUM severity / S effort / correctness · lane `eval-misc` · score 4.9 · confidence 0.82 · verdict CONFIRMED
- **Now:** The metric infers "answered correctly" as "answerable minus refused", assuming refusal is the only failure mode for an answerable item; it does not account for clarify/scope_warning/other non-answer statuses that carry refused=False.
- **Fix:** Count correct answers positively instead of by subtraction. In the answerable path, before the standard-metrics block, treat a non-answering status as a wrong decision: if result.status not in {"answer","summary"} (and not refused), record it as an incorrect decision and continue; otherwise increment an explicit answered_correctly counter used as refusal_accuracy's numerator term. recall/precision still score genuine answers; a wrong clarify now correctly lowers refusal_accuracy instead of silently passing.
- **Risk:** Behavior change to the reported metric (intended). A clarify on an answerable item no longer contributes a 0 to recall/precision, so the signal moves from the content metrics to refusal_accuracy -- cleaner, and still caught by the gate. Confirm no gold item legitimately expects a clarify without must_clarify=True.
- **Test:** Feed evaluate() a stub ask_callable that returns status="clarify", refused=False for an answerable (must_refuse=False, must_clarify=False) item; assert scorecard.refusal_accuracy < 1.0. Currently 1.0.
- **Note:** changes behavior (intended correction)

### #20 — logout() bypasses the timeout wrapper, so a hung backend deadlocks the whole logout flow

- **Where:** `regwatch/frontend/lib/api.ts:409`
- **Class:** MEDIUM severity / S effort / error-handling · lane `fe-lib` · score 4.9 · confidence 0.82 · verdict CONFIRMED
- **Now:** logout was written before/around the fetchWithTimeout convention and never routed through it, directly contradicting this file's own invariant comment (lines 264-266): 'Every fetch gets a bound so a hung backend can't leave a page spinning forever.' Because the AuthProvider awaits the promise before setUser(null)/router.replace, a fetch that never resolves means the catch never fires and the user is stuck logged-in with the logout button already spent.
- **Fix:** Route logout through the shared wrapper, e.g. reuse postJSON('/auth/logout', undefined, false) or call fetchWithTimeout(...) with DEFAULT_TIMEOUT_MS, then handle<void>(res,'POST','/auth/logout',false). On timeout it rejects with the defined ApiError, AuthProvider's catch fires, and the client-side logout (clear user + redirect) completes anyway.
- **Risk:** None meaningful: the backend logout 'never errors' and is idempotent, so a timed-out logout still leaves the server session revoked-or-expiring while the client proceeds. Only behavior change is that a hung backend now completes logout client-side instead of hanging.
- **Test:** In apiTimeout.test.ts, stub a hangingFetch and assert logout()/apiLogout() rejects with ApiError status 504 within the bound (advance fake timers past DEFAULT_TIMEOUT_MS) rather than remaining pending; assert getTimerCount()===0 after.
- **Note:** changes behavior (intended correction)

### #21 — write_digest crashes run_watch on an ephemeral-disk JSONL write error AFTER durable alerts commit, dropping the watch-run ledger row (INV-4 last_run)

- **Where:** `src/regwatch/watch/alerts.py:249`
- **Class:** MEDIUM severity / S effort / error-handling · lane `x-failure` · score 4.8 · confidence 0.8 · verdict CONFIRMED
- **Now:** A best-effort/backward-compat artifact write (explicitly secondary to the DB per the module docstring) is on the same unguarded path as, and sequenced before, the authoritative run-ledger write, so its failure defeats the very durability signal (last_run) it is subordinate to.
- **Fix:** Guard the JSONL write in write_digest with try/except (log `digest_write_failed` with error_type and continue -- the DB is the source of truth), OR reorder run_watch so `record_watch_run` is written before/independently of the JSONL artifact. Prefer guarding the file write so a completed run always records its ledger row.
- **Risk:** Low: only changes the error branch (JSONL write failure now logs-and-continues instead of crashing). The returned WatchRunResult.digest_path would point at a file that may not exist on this branch -- callers already treat the JSONL as non-authoritative, but confirm nothing asserts the file exists.
- **Test:** Patch the digest file write (e.g. `Path.open`) to raise OSError while `_persist_alerts` succeeds, run `run_watch`, and assert it does not raise, the durable alerts are present, and `latest_watch_run()` returns a row. Today run_watch raises and no ledger row is written.
- **Note:** changes behavior (intended correction)

### #22 — Hyphenated release-type spelling ("Extended-release") bypasses the INV-5 form-blend guard

- **Where:** `src/regwatch/assemble/dossier.py:86`
- **Class:** MEDIUM severity / S effort / correctness · lane `eval-misc` · score 4.8 · confidence 0.8 · verdict CONFIRMED _(merges [88])_
- **Now:** The tokenizer's delimiter set ({whitespace, comma}) does not include the hyphen, so a hyphenated modifier collapses into one opaque token that matches no entry in _FORM_MODIFIERS, silently disabling the release-type guard for that spelling. FDA data uses both "Extended Release" and "Extended-release"/"Delayed-release".
- **Fix:** Split on hyphens (and slashes) as well: in both _form_tokens and _form_modifiers, tokenize with re.split(r"[\s,\-/]+", value.lower()) instead of .replace(",", " ").split(). "extended-release" then yields ["extended","release"], the "extended" modifier is seen, and the guard fires exactly as it does for the space spelling.
- **Risk:** Splitting hyphens could over-split a base-form token that legitimately contains a hyphen, but FDA dosage-form vocabulary has no such base forms; the only hyphens in practice join release-type modifiers, which is precisely what we want split. Preserves the existing space-spelled behavior (already correct) and the lenient aerosol case.
- **Test:** In tests/test_assemble_audit.py seed a PSG with dosage_form="Tablet, Extended-release" (hyphen) for an ingredient, then assert _find_matching_psgs(ingredient, "Tablet") == [] (excludes the ER sibling). This fails today (returns the ER doc) and passes after the hyphen split.
- **Sequencing:** dossier _find_matching_psgs form guard; do with 55, then extract predicate (94); resolves lockstep drift 99.
- **Note:** changes behavior (intended correction)

## Do soon (28)

### #23 — REMS index app-number regex is copy-pasted into populator despite it already importing sources.rems

- **Where:** `src/regwatch/whitepaper/populator.py:1301`
- **Class:** MEDIUM severity / S effort / duplication · lane `x-duplication` · score 5.7 · confidence 0.95 · verdict CONFIRMED
- **Now:** The REMS row-text format ('NDA #022549') is a property of the REMS source, but the populator re-derived the extraction regex locally instead of importing the one that ships with the REMS handler that produced those rows.
- **Fix:** Export the regex from sources/rems.py (rename _APP_NO_IN_TEXT_RE to a public REMS_APP_NO_IN_TEXT_RE, or simply add it to populator's existing `from regwatch.sources.rems import (...)` block) and delete populator's _REMS_APP_NO_TEXT_RE. Both call sites (rems.py's _row_application_numbers and populator's _rems_record_matches_application) then share one grammar.
- **Risk:** None functional — identical pattern. Only a naming/visibility change on the rems.py constant.
- **Test:** Parametrize a test over a REMS-style string like 'Program X [NDA #022549]' asserting both sources.rems and whitepaper.populator extract '022549' identically; assert `sources.rems.REMS_APP_NO_IN_TEXT_RE is whitepaper.populator._REMS_APP_NO_TEXT_RE`-equivalent (same object after the import) so a future edit to the REMS format can't update only one copy.

### #24 — api_host/api_port Settings fields are dead; API_HOST/API_PORT env vars are silent cargo across 4 deploy files

> **DONE (2026-07-16, phase-2 dual-stack listener).** Fields + all env copies
> deleted (.env.example, Dockerfile ENV, fly.toml, compose.yaml, plus stale
> docs in DEPLOY.md and DOCKER.md). This item's "ALTERNATIVE if the port must
> be configurable: make the bind read the env" was deliberately NOT taken, and
> is now actively forbidden: `regwatch serve` HARDCODES the bind list, because
> a launcher honouring `API_HOST=0.0.0.0` would bind IPv4-only, pass every IPv4
> gate, ship green, and break only at the phase-3 Go-proxy flip. See
> docs/GO_PROXY_ROLLOUT.md phase 2.

- **Where:** `config/settings.py:280`
- **Class:** MEDIUM severity / S effort / dead-code · lane `x-dead-doc` · score 5.5 · confidence 0.92 · verdict CONFIRMED
- **Now:** The settings fields predate the hardcoded uvicorn CMD; the env vars are copy-paste cargo the env-drift test (test_env_example_drift.py) then FORCES into .env.example (every Settings field must appear there), so the dead knob looks operator-facing. Result: an operational footgun -- a port change that is silently ignored.
- **Fix:** Smallest behavior-preserving fix: delete the api_host/api_port fields and remove API_HOST/API_PORT from .env.example, Dockerfile ENV, fly.toml, and compose.yaml (the drift test passes only when both field and .env.example line are removed together). ALTERNATIVE if the port must be configurable: make the bind read the env (shell-form CMD or entrypoint exec `uvicorn --host "$API_HOST" --port "$API_PORT"`) -- but that is a behavior change, so pick deliberately.
- **Risk:** Removal preserves behavior (nothing reads them; bind stays 8000). Fly routing depends on fly.toml internal_port, not this env, so unaffected. Only risk is an operator who believed the port was configurable -- it never was.
- **Test:** tests/test_env_example_drift.py already couples fields<->.env.example; after removal both test_env_example_documents_every_settings_field and test_env_example_has_no_phantom_vars stay green, and would fail if only one side were removed. Optionally add `assert not hasattr(Settings(), 'api_port')`.

### #25 — README and watchlist docstring document a drugsfda auto-populate flow that never executes

- **Where:** `README.md:264`
- **Class:** MEDIUM severity / S effort / doc-drift · lane `x-dead-doc` · score 5.5 · confidence 0.92 · verdict CONFIRMED
- **Now:** Docs describe the originally-intended data flow; the drugsfda import was never wired, so the docs drifted from the shipped behavior (manual/anda_letter only, via POST /products). INV-5 provenance copy claiming an active drugsfda source is misleading in a compliance product.
- **Fix:** Reconcile toward the shipped reality: either (a) if the code is deleted per the dead-code finding, rewrite README 264-271 and the watchlist.py docstring to say the watchlist is populated via POST /products (manual + anda_letter), and note drugsfda auto-import is not currently wired; or (b) if the feature is restored, keep the docs and add the missing entry point. Do not leave docs asserting a source that produces zero rows.
- **Risk:** Doc-only; zero runtime risk. Must stay consistent with whichever resolution the dead-code finding takes.
- **Test:** No unit test for prose; if you keep an INV-5 sources assertion, point it at the ALLOWED_SOURCES set actually reachable through add_manual_product rather than the documented-but-dead drugsfda path.

### #26 — whitepaper_sources.normalize_appl_no re-implements _utils app-number normalization and has diverged on >6-digit input

- **Where:** `src/regwatch/store/whitepaper_sources.py:45`
- **Class:** MEDIUM severity / S effort / duplication · lane `x-duplication` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The store layer needed a 'raise on unparseable' variant and grew its own copy rather than wrapping the canonical _utils normalizer, so the two normalizers now disagree at the boundary that INV-5 cares about (over-long/malformed numbers).
- **Fix:** Implement normalize_appl_no on top of bare_application_number: `digits = bare_application_number(value); if digits is None: raise ValueError(...); return digits` (bare_application_number already returns the six-digit, prefix-stripped form). Delete the local _APP_PREFIX regex. This makes the store agree with every source handler on what a valid application number is.
- **Risk:** Behavior change on the >6-digit / malformed edge case: normalize_appl_no would now raise where it previously coerced. That aligns it with INV-5 (and with the rest of the codebase), but confirm no caller relies on the lenient coercion — create_run passes user-supplied application_number through it, so an over-long value would 422 instead of storing a bogus number (arguably a fix). Flagged as a deliberate behavior change.
- **Test:** Assert normalize_appl_no('NDA 020503')=='020503' and normalize_appl_no('12345')=='012345' unchanged, and add normalize_appl_no('1234567') now raises ValueError — matching clean_application_number('1234567') is None.
- **Sequencing:** Reconcile with sources/_utils normalizer that INV-5 relies on; relates to 80/81 app-number dedup.
- **Note:** changes behavior (intended correction)

### #27 — NDA/ANDA/BLA letter-code map is defined four times across four modules

- **Where:** `src/regwatch/whitepaper/populator.py:295`
- **Class:** MEDIUM severity / S effort / duplication · lane `x-duplication` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The tuple APPLICATION_PREFIXES was centralized in sources/_utils.py and imported widely, but the correlated letter<->prefix mapping never was, so each module that needed it re-declared it locally.
- **Fix:** Keep the single forward map in sources/_utils.py (promote _SINGLE_LETTER_TYPES to a public name, e.g. APP_TYPE_BY_LETTER, next to APPLICATION_PREFIXES). Import it in populator.py (replace _OB_TYPE_TO_APP) and whitepaper_sources.py (replace _TYPE_BY_LETTER); in orange_book.py derive the inverse once as {v: k for k, v in APP_TYPE_BY_LETTER.items()} instead of hand-listing it. populator.py already imports clean_application_number from sources._utils, and whitepaper_sources.py imports canonical_name from common.text_normalize, so neither adds a new cross-package edge or a cycle (sources/_utils imports only httpx/config).
- **Risk:** Import direction only: store/whitepaper_sources importing from sources/_utils is a new (but acyclic) edge — verify no import cycle at load time. Values are identical so runtime behavior is unchanged.
- **Test:** Add a test asserting sources._utils.APP_TYPE_BY_LETTER == {"N": "NDA", "A": "ANDA", "B": "BLA"} and that orange_book's inverse maps NDA->N/ANDA->A/BLA->B; a mutation test flipping one entry in _utils should now break populator's identity resolution AND orange_book's split in the same run (proving the single source of truth).

### #28 — get_recent_turns tangles the memory-eligibility policy with the DB read

- **Where:** `src/regwatch/common/conversation.py:142`
- **Class:** MEDIUM severity / S effort / domain-io-separation · lane `x-domain-io` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The INV-1-adjacent rule 'never thread a refused/clarify/meta turn back in as conversational context' is pure list processing but is welded to the DB read, so exercising it (e.g. proving a refusal is excluded, or newest-per-role wins) requires seeding ChatMessage rows through a live DB. The read already hands off plain tuples, so the seam is right there.
- **Fix:** Extract `_fold_turns(raw: list[tuple[str,str,str,str|None]], *, exclude_turn_id: str|None, limit: int) -> list[PriorTurn]` containing lines 141-168; get_recent_turns keeps only the try/except DB read and returns `_fold_turns(raw, ...)`. Pure code motion.
- **Risk:** None - same ordering and filtering. Preserve the newest-first fold + final reverse and the `(a_status or 'answer')` legacy-row handling exactly.
- **Test:** DB-free: _fold_turns([(t,'assistant','sorry','refused'),(t,'user','q',None)], exclude_turn_id=None, limit=3) == [] (refusal excluded); and a mixed set asserts oldest-first order and per-role newest-wins. Fails today - the policy is unreachable without seeding a DB.

### #29 — No test for the model-refusal -> CLARIFY (guide) branch when the drug was named

- **Where:** `src/regwatch/generate/grounded_qa.py:1497`
- **Class:** MEDIUM severity / S effort / test-gap · lane `x-tests` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** The only model-refusal test (test_invariants.py::test_inv2_refuses_when_model_outputs_refusal_string) seeds a SINGLE product, so resolution takes the single-product fallback (by_name=False) and exercises only the refuse side. No test seeds 2+ products, names one, and returns the refusal sentinel, so the resolved_by_name=True -> clarify branch has zero coverage. This branch has no backstop -- it is the sole implementation of guide-instead-of-refuse for a named-drug model refusal.
- **Fix:** Add a test: seed >=2 products (so resolution is genuinely by_name), stub the provider to return exactly get_settings().refusal_text, ask a question naming one drug (e.g. 'What study design for propranolol?'); assert status=='clarify', reason=='model_refusal', refused is False, clarify non-empty and citations==[]. Keep a contrast case with a single-product corpus asserting status=='refused'.
- **Risk:** A refactor that inverts the condition, drops resolved_by_name, or collapses both branches to _refuse would silently turn a named-drug model refusal into a hard refusal (regressing the clarify-over-refuse UX and flipping must_clarify/answerable eval items) with no failing test.
- **Test:** tests/test_clarify.py: seed ['propranolol hydrochloride','metformin hydrochloride'], monkeypatch qa_mod.get_llm_provider to a stub whose complete() returns get_settings().refusal_text, r=qa_mod.ask('what study design for propranolol?') with REFUSAL_SCORE_THRESHOLD=0.0; assert r.status=='clarify' and r.reason=='model_refusal' and not r.refused and r.clarify and r.citations==[].

### #30 — citations-only 'answer' (no prose body) refuse disjunct is untested

- **Where:** `src/regwatch/generate/grounded_qa.py:1524`
- **Class:** MEDIUM severity / S effort / test-gap · lane `x-tests` · score 5.4 · confidence 0.9 · verdict CONFIRMED
- **Now:** Every test that reaches this guard (test_invariants::test_inv2_refuses_when_answer_has_no_valid_citations, streaming, etc.) exercises only the `not citations` half via a FABRICATED citation. No test provides an answer that is ONLY a VALID citation marker with empty prose, so the `not answer_body` disjunct -- the sole guard against emitting a bare-bracket 'answer' -- has no coverage.
- **Fix:** Add a test seeding a corpus and stubbing the provider to return only a valid marker ('[PSG_020503, p.3]', no surrounding prose). Assert result.refused is True and reason=='no_valid_citations'. A regression that simplifies the condition to `if not citations:` would return the bare marker as a non-refused answer and this test would fail.
- **Risk:** Someone 'simplifying' line 1524 to `if not citations:` (the citations are non-empty for a valid-marker-only answer) would emit a citation-only answer with no claim -- an INV-1 violation on a real answer surface -- with the whole suite still green.
- **Test:** tests/test_grounded_qa_citations.py: _seed_corpus([...PSG_020503 p.3...]); monkeypatch get_llm_provider to _stub_llm('[PSG_020503, p.3]'); r=qa_mod.ask('What study design is recommended?'); assert r.refused is True and r.reason=='no_valid_citations' and r.citations==[].

### #31 — INV-5 form-compatibility predicate is trapped inside the _find_matching_psgs DB scan loop

- **Where:** `src/regwatch/assemble/dossier.py:123`
- **Class:** MEDIUM severity / S effort / domain-io-separation · lane `x-domain-io` · score 5.1 · confidence 0.85 · verdict CONFIRMED
- **Now:** The compliance rule 'a plain Tablet must not blend with Tablet, Extended Release' (INV-5) lives inside the persistence loop rather than as a standalone predicate, so it cannot be exercised without a live/seeded DB. The three tests that cover this rule (tests/test_assemble_audit.py:256-300) each call init_db() and insert PsgDocument rows just to probe pure string logic.
- **Fix:** Extract the lines 123-137 body into a pure `_form_matches(want: str, have: str) -> bool` (reusing the existing _form_tokens/_form_modifiers) and call it from the loop: `if dosage_form and d.dosage_form and not _form_matches(dosage_form, d.dosage_form): continue`. Pure code motion; the loop keeps identical semantics.
- **Risk:** None functionally - identical predicate, same short-circuits. Only edge case: keep the existing `if dosage_form and d.dosage_form` outer guard so a null form on either side still passes (unchanged).
- **Test:** Add a DB-free unit test: assert _form_matches('Tablet','Tablet, Extended Release') is False, _form_matches('Tablet','Tablet') is True, _form_matches('inhalation aerosol','Aerosol, Metered') is True. This would fail today because the rule is unreachable without _find_matching_psgs + a seeded DB.
- **Sequencing:** Extract INV-5 predicate out of the DB loop AFTER the 54/55 bug fixes land in it.

### #32 — Post-audit assistant record_message failure swallow (_finish_turn) is untested

- **Where:** `src/regwatch/generate/grounded_qa.py:342`
- **Class:** MEDIUM severity / S effort / test-gap · lane `x-tests` · score 5.1 · confidence 0.85 · verdict CONFIRMED
- **Now:** test_conversational_memory covers the INPUT-side best-effort reads (get_session_filters / get_recent_turns degrade on DB error), but no test exercises the OUTPUT-side _finish_turn write raising. The try/except at line 342-343 -- which protects a fully-answered, already-audited turn from turning into a 500 -- has no coverage.
- **Fix:** Add a test: seed a corpus + valid grounded answer, monkeypatch record_message so it raises ONLY on the assistant-role write (role=='assistant') while succeeding for the user write. Assert ask() returns the grounded answer (refused False, citations present, audit_id valid) without raising, and that the audit row exists.
- **Risk:** Removing or narrowing the try/except (a plausible 'clean up' refactor) would let a chat-history/FK error propagate and 500 an already-audited, already-synthesized turn -- the exact silent-500-after-audit failure the code guards against -- undetected.
- **Test:** Seed corpus; stub provider to a valid cited answer; monkeypatch qa_mod.record_message with a wrapper that raises RuntimeError when kwargs.get('role')=='assistant' and delegates otherwise; r=qa_mod.ask(q); assert not r.refused and r.citations and r.audit_id>0 (no exception).

### #33 — GET /sessions has no pagination — unbounded per-user fetch with a per-row correlated subquery

- **Where:** `src/regwatch/api/main.py:1755`
- **Class:** MEDIUM severity / S effort / performance · lane `x-perf` · score 4.9 · confidence 0.82 · verdict CONFIRMED
- **Now:** Missing pagination, inconsistent with the sibling list endpoints /whitepaper/runs (limit/offset, main.py:1349-1350) and /watch/latest (limit/offset, main.py:1579-1580) which are bounded.
- **Fix:** Add `limit` (bounded default e.g. 100, max ~200) and `offset` Query params, keep order_by updated_at desc, and return count/total/limit/offset like /whitepaper/runs so a truncated page is visible to the client.
- **Risk:** The frontend must consume pages; returning `total` keeps truncation detectable. Otherwise semantically identical (newest page first).
- **Test:** Seed more than `limit` sessions for one user; assert the response is capped at `limit`, ordered updated_at desc, and that `total` reflects the full count.
- **Note:** changes behavior (intended correction)

### #34 — STATUSES is an unchecked hand-mirror of the generated QueryStatus union and can silently drift

- **Where:** `regwatch/frontend/lib/turns.ts:37`
- **Class:** MEDIUM severity / S effort / type-safety · lane `fe-lib` · score 4.8 · confidence 0.8 · verdict CONFIRMED
- **Now:** STATUSES duplicates the generated QueryResponse['status'] union (api-types.ts:718) but the compiler cannot check it (`string[]`, not `QueryStatus[]`). If the backend adds a status, regen updates QueryStatus but STATUSES stays stale with no error. A rehydrated message carrying the new status would then be mapped to null on reload (losing its declined/clarify semantics) while the live path (assistantTurn, line 156, uses r.status directly) keeps it - the exact live-vs-reload INV-2 drift this file guards against for the error case (lines 58-61).
- **Fix:** Make drift a compile error: build the guard from an exhaustive map, e.g. `const STATUS_SET: Record<QueryStatus, true> = { answer:true, summary:true, clarify:true, scope_warning:true, meta:true, refused:true, error:true }` and test membership via `Object.prototype.hasOwnProperty` / `st in STATUS_SET`. Adding a QueryStatus without updating the map then fails typecheck.
- **Risk:** Purely additive typing; current values already match the union, so behavior is unchanged today. Only a compile-time guard is added.
- **Test:** A type-level test (or runtime loop over a canonical QueryStatus[] literal) asserting every QueryStatus value is accepted by turnFromMessage; it would fail to compile/pass if a status were added to the union but not the guard.
- **Sequencing:** Pairs with 69 (same QueryStatus union drift, api.ts comment).

### #35 — INV-5 trust-hierarchy merge policy is embedded in the upsert_entries DB loop, mutating ORM rows in place

- **Where:** `src/regwatch/watch/watchlist.py:240`
- **Class:** MEDIUM severity / S effort / domain-io-separation · lane `x-domain-io` · score 4.7 · confidence 0.78 · verdict CONFIRMED
- **Now:** The 'higher-trust source wins on update; equal takes incoming; lower may only fill empty fields' rule is a compliance control (INV-5, guarding a manual override from being silently reverted by a drugsfda re-import), but it is expressed as in-place mutation of a DB entity, so it is untestable as a pure unit. tests/test_watchlist.py:157-190 must round-trip through a real DB twice to assert the trust rules.
- **Fix:** Extract a pure `_resolve_merge(existing_source: str, existing: dict, incoming: WatchlistEntry) -> dict` that returns the merged field values (company_status/rld_name/source/source_url) per the rank rule; the loop then just assigns them to `row`. The rank comparison and field-precedence logic become a table-testable pure function.
- **Risk:** Low. Must preserve the exact `incoming or existing` vs `existing or incoming` asymmetry per branch and the equal-rank-takes-incoming edge. on_watchlist=True is still set unconditionally by the caller.
- **Test:** DB-free test: _resolve_merge('manual', {'company_status':'approved','source':'manual'}, drugsfda_entry_with_status_none) keeps company_status='approved' and source='manual'; equal-rank case takes the incoming value. Fails today (rule only reachable via upsert_entries + DB).

### #36 — Naive-UTC timestamp parsing/formatting duplicated across four files; `relTime` is byte-identical in two

- **Where:** `regwatch/frontend/app/(shell)/whitepaper/page.tsx:64`
- **Class:** MEDIUM severity / M effort / duplication · lane `fe-ui` · score 3.1 · confidence 0.92 · verdict CONFIRMED
- **Now:** No shared date/time module; each surface re-implemented the same subtle 'treat a missing offset as UTC' convention inline.
- **Fix:** Add `lib/datetime.ts` exporting `parseNaiveUtcMs(iso): number` (the normalize+Date.parse, returning NaN on failure — the contract watch already relies on) and a shared `relTime(iso)`. Rewrite watch's `parseUtcMs`, whitepaper's `fmtWhen`, and watch's `fmtDetected` on top of `parseNaiveUtcMs`, and import the shared `relTime` in Sidebar and whitepaper. Smallest-change: at minimum dedupe the identical `relTime`.
- **Risk:** Very low — pure extraction of identical logic. Keep watch's NaN-return contract intact (RunFreshness/staleness depends on `Number.isFinite`). Verify the extracted `relTime` keeps the same `Math.max(0, ...)` clamp so future clock skew still renders 'now'.
- **Test:** Unit-test `lib/datetime.ts`: `parseNaiveUtcMs('2026-07-08T10:00:00')` equals the same value as with a trailing 'Z'; a bad string returns NaN; `relTime` of ~90m ago returns '1h'. A future edit to only one inline copy would then diverge from the shared, tested one.
- **Sequencing:** Shared date util also consumed by 74's refactor.

### #37 — Meta path: `_meta_answer_text` system-state reads (and the meta-gate resolve_product) are unguarded — a DB hiccup on a meta question is an unaudited 500

- **Where:** `src/regwatch/generate/grounded_qa.py:995`
- **Class:** MEDIUM severity / M effort / error-handling · lane `grounded-qa` · score 3 · confidence 0.9 · verdict CONFIRMED
- **Now:** The meta handler was hardened against the audit-*write* failure (the excluded backlog item / test_t5) but not against the system-state *read* failures that produce the answer. The existing test_t5_meta_audit_write_failure only monkeypatches `log_query`, so the read path's exposure is untested.
- **Fix:** Guard the answer assembly inside `_meta` (or the gate): wrap `_meta_answer_text(question)` in try/except, and on Exception `capture_exception` + `log.warning` and fall through to an audited status="error" refuse (reuse `_SERVICE_UNAVAILABLE_TEXT`). Optionally wrap the meta-gate resolve_product together with the resolution finding below.
- **Risk:** Failure-path-only change (500 -> audited error refuse), consistent with the provider/catalog precedent. Success path unchanged. Distinct from the already-fixed meta audit-write degrade.
- **Test:** Seed >=2 products (so meta does not resolve to a drug), `monkeypatch.setattr(qa_mod, "latest_digest_records", <raises>)`, call `qa_mod.ask("what changed?")`, assert it returns an audited status="error" refuse rather than raising. Fails today.
- **Sequencing:** Guard meta reads in _meta_answer_text; do with/after 19 and before the 97 seam extraction.

### #38 — Refetch-on-focus + in-flight-guard block duplicated verbatim between Watch and Whitepaper pages

- **Where:** `app/(shell)/whitepaper/page.tsx:210`
- **Class:** MEDIUM severity / M effort / duplication · lane `fe-ui` · score 3 · confidence 0.9 · verdict CONFIRMED
- **Now:** The whitepaper page was written by copying the watch page's pattern (its comment at line 190 even says 'the Watch page pattern') instead of sharing it.
- **Fix:** Extract `hooks/useRefetchOnVisible(load: () => void)` that attaches/detaches the window `focus` + document `visibilitychange` listeners and calls `load` on visible. Each page keeps its own loader + in-flight ref and passes the loader in. This removes the duplicated effect and its cleanup, the easiest place for the two copies to drift (e.g. one forgetting to remove a listener).
- **Risk:** Low. `load`/`loadRuns` are already stable useCallbacks, so the hook's effect deps stay `[load]` and it attaches once. Preserve the `document.visibilityState === 'visible'` gate so a background visibilitychange doesn't refetch.
- **Test:** Test the hook in isolation: fire a `visibilitychange` with `document.visibilityState` stubbed 'visible' and assert the callback ran once; stub 'hidden' and assert it did not; unmount and assert listeners are removed (a second event does nothing).

### #39 — The validate-structured-token -> build-evidence sequence is copy-pasted at 7+ sites and is the ONLY INV-8 guard on manual/analyst-cell evidence

- **Where:** `src/regwatch/whitepaper/populator.py:1852`
- **Class:** MEDIUM severity / M effort / duplication · lane `wp-populator` · score 2.8 · confidence 0.85 · verdict CONFIRMED
- **Now:** No shared helper for 'validated structured-token evidence,' so each extractor re-implements the guard, and the safety of the un-centrally-guarded analyst cells depends on every author remembering to copy it.
- **Fix:** Add one helper, e.g. `_validated_evidence(token, known, source, **kwargs) -> dict | None` that returns the _evidence dict only when the token validates (None otherwise), and route all 8 structured-token sites through it. DRYs the code and makes the INV-8 guard structural rather than by-convention.
- **Risk:** Pure refactor; behavior identical if the helper reproduces the exact skip semantics (skip the row on invalid). Watch the two different current behaviors (some sites `continue` inside a loop, others build a single-element list) -- keep both by having callers drop None results.
- **Test:** Add a test that seeds a patent row whose obpat token is NOT in ctx.known_tokens and asserts _ext_patent_block emits no evidence entry for it; keep it green across the refactor. Guards against a future site dropping validation.

### #40 — Cross-base-form blend: "Capsule, Extended Release" query matches a "Tablet, Extended Release" PSG (INV-5)

- **Where:** `src/regwatch/assemble/dossier.py:129`
- **Class:** MEDIUM severity / M effort / correctness · lane `eval-misc` · score 2.8 · confidence 0.85 · verdict CONFIRMED
- **Now:** The lenient token-overlap test treats release-type modifier words ("extended","release") as if they were base-form tokens, so two different base forms that merely share a release modifier satisfy the "shares a form token" condition. The modifier-set-equality guard cannot catch it because both sides carry the same modifier.
- **Fix:** Require the lenient overlap to include a BASE token: compute the intersection on tokens with the release/administration vocabulary removed, e.g. base = _form_tokens(x) - _FORM_MODIFIERS - {"release","immediate"}; keep only if want is a substring of have OR base(want) & base(have) is non-empty. The intended lenient case survives because "inhalation aerosol" vs "Aerosol, Metered" overlaps on the genuine base token "aerosol" (I verified it stays True). Note the docstring's claim that this mirrors whitepaper._form_compatible "in lockstep" is inaccurate -- that function uses exact normalized equality, which is stricter than this lenient path; worth reconciling the comment.
- **Risk:** Stricter matching could produce a false "no PSG found" if a legitimate sibling overlaps ONLY on a modifier word, but such a pair would already be two clinically distinct forms that INV-5 says must not blend, so refusing is the correct outcome. "release"/"immediate" must be added to the subtracted set since they are not in _FORM_MODIFIERS.
- **Test:** Seed "Capsule, Extended Release" and "Tablet, Extended Release" PSGs for one ingredient; assert _find_matching_psgs(ingredient, "Capsule, Extended Release") returns only the capsule. Fails today (returns both), passes after the base-token fix.
- **Sequencing:** Same dossier form-guard predicate as 54/94.
- **Note:** changes behavior (intended correction)

### #41 — Assemble page has zero tests; its scope-prefill dirty-guard effect is entirely uncovered

- **Where:** `regwatch/frontend/app/(shell)/assemble/page.tsx:23`
- **Class:** MEDIUM severity / M effort / test-gap · lane `fe-ui` · score 2.8 · confidence 0.85 · verdict CONFIRMED
- **Now:** Coverage was added for the three data-heavy surfaces but assemble (and the shared scope-prefill logic) was skipped.
- **Fix:** Add `test/AssemblePage.test.tsx` following the WatchPage mock pattern: (1) adopt-on-untouched — a changing useCurrentProduct mock prefills then re-adopts the RLD field when untouched, and does NOT clobber a user-edited field; (2) a clear-then-distinct-scope IS adopted (the exact case whitepaper fails); (3) `result.refused` renders the 'Insufficient basis' stamp vs the Dossier block. Case (2) doubles as the regression net for the whitepaper fix.
- **Risk:** None (test-only).
- **Test:** This finding is itself the test. The clear->re-scope case will pass on assemble and, if ported to whitepaper, fail until the line-150 fix lands.

### #42 — inspect().has_table() re-issued ~6x per answered Ask (repeated pg_catalog round-trips for a test-only fallback)

- **Where:** `src/regwatch/retrieve/retriever.py:123`
- **Class:** MEDIUM severity / M effort / performance · lane `x-perf` · score 2.8 · confidence 0.85 · verdict CONFIRMED
- **Now:** The 'vector-only / test-mode' guard (some unit tests seed the vector store without the SQLite catalog, so psg_document/psg_version may be absent) is re-checked live on every request, even though the set of catalog tables never changes during a running process. Nothing memoizes the result the way pgvector_store._schema_ready memoizes its one-time DDL check.
- **Fix:** Memoize the 'catalog tables present' answer per engine — a module-level boolean set on first check and cleared by store.db.reset_for_tests() (mirroring pgvector_store._schema_ready / _metadata_values_cache), or a single shared helper both queries.py functions and the retriever call. After warmup the 6 round-trips/query drop to zero while the test-mode fallback still fires on a fresh no-catalog DB.
- **Risk:** Tests swap DATABASE_URL via reset_for_tests and some intentionally run with no catalog, so a naive never-reset module cache would go stale (return True against a table-less DB). The memo MUST be reset in reset_for_tests (both store/db and, if it owns state, the helper) or keyed on engine identity; otherwise behavior is identical.
- **Test:** Patch sqlalchemy Inspector.has_table (or the shared helper) with a call counter; assert a full ask() with the catalog present triggers it at most once, and assert that after reset_for_tests re-pointed at a table-less DB the guard re-detects absence (returns None / [] as today).

### #43 — Generated cell/provenance values are not stripped of XML-illegal control chars (unlike the analyst overlay), so one control char permanently 500s a run's docx

- **Where:** `src/regwatch/whitepaper/docx_writer.py:362`
- **Class:** MEDIUM severity / M effort / correctness · lane `wp-rest` · score 2.8 · confidence 0.83 · verdict CONFIRMED
- **Now:** The docx writer is the XML boundary but only defends against blanks (_cell_value, line 163), not control chars -- an asymmetry with _clean_value two modules away. No component between the populator and lxml guarantees XML-safe generated text.
- **Fix:** Add a module-private _xml_safe(text) in docx_writer that strips re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]') (the exact set python-docx rejects; deliberately keeps \t/\n, and does NOT strip \x7f-\x9f which are legal XML 1.0), mirroring the proven whitepaper_runs pattern rather than importing across the domain/store boundary. Route the generated-value choke points (_cell_value/_render_value/_marker) and the provenance/appendix .text assignments through it.
- **Risk:** Stripping a control char changes the rendered value by dropping a non-printing, already-format-illegal byte -- acceptable and invisible. Must preserve tab/newline (as _clean_value does) so multi-line SPL text still renders. Behavior change is limited to values that today crash the render.
- **Test:** Build a result whose one populated cell value contains '\x0c' (or '\x00'), render with the synthetic in-test template; today write_whitepaper_docx raises ValueError. Assert it returns openable bytes and the rendered cell text has the control char removed.
- **Note:** changes behavior (intended correction)

### #44 — search_sources swallows per-source failures with no channel to distinguish 'query failed' from 'queried and absent'

- **Where:** `src/regwatch/sources/router.py:112`
- **Class:** MEDIUM severity / M effort / error-handling · lane `sources` · score 2.7 · confidence 0.8 · verdict CONFIRMED
- **Now:** The isolation boundary (try/except per handler) was built to keep one bad source from killing the request, but it collapses the tri-state (present / absent / unknown) into two states at the return boundary: a failed source silently reads as 'absent'. The codebase already knows this is unsafe — whitepaper/populator.py:136-141 explicitly bypasses search_sources with hand-written per-handler wrappers, commenting 'search_sources swallows exceptions, which would mask a failed query as "no rows" and emit a false "No", an INV-5 violation'. The API path did not get the same treatment.
- **Fix:** Have search_sources also return the set/list of sources that raised (e.g. return (routed, records, failed) or attach failed sources to a small result object), so callers can render 'unknown' for a failed source instead of implying absence. Keep the isolation (still don't crash the whole request). Update the two in-repo callers and the /sources/search response model to surface failed_sources. This lets the API path get the same tri-state safety the populator hand-rolled, and removes the reason the populator has to bypass the router.
- **Risk:** Changes the search_sources return contract, so both callers and the SourceSearchResponse model must be updated in the same change. Purely additive at the data level (no source stops being queried); the only behavior change is that failed sources become visible.
- **Test:** Monkeypatch _HANDLERS[REMS] to raise httpx.TimeoutException and _HANDLERS[PSG] to return one record; assert the new failed-sources channel contains REMS and not PSG, and that PSG's record is still returned. Fails today because there is no failed-sources channel — a failed source is indistinguishable from an empty one.
- **Note:** changes behavior (intended correction)

### #45 — Stale-chunk cleanup failure never self-heals: revised-PSG old chunks stay retrievable forever

- **Where:** `src/regwatch/ingest/pipeline.py:379`
- **Class:** MEDIUM severity / M effort / correctness · lane `ingest-process` · score 2.7 · confidence 0.8 · verdict CONFIRMED
- **Now:** The idempotency/backfill gate detects 'current version missing chunks' but has a blind spot for 'old version chunks still present'. Because the current version's chunks DO exist, the backfill never re-attempts the cleanup, so a one-time delete failure is permanent until the doc gets another revision. Retrieval can then serve stale, superseded content next to a doc row whose metadata already advertises the new revision (staleness / INV-7).
- **Fix:** In the 'unchanged' branch, add a cheap probe for leftover foreign-version chunks (a single indexed `SELECT 1 FROM chunk WHERE doc_id=:d AND version_id IS DISTINCT FROM :keep LIMIT 1`, mirroring the delete's WHERE) and, when it finds any, re-run `delete_chunks_for_doc_except_version(doc_id, latest_version_id)`. This makes the same backfill that heals missing chunks also heal an un-purged prior version. No write on the healthy path (probe returns nothing).
- **Risk:** Adds one read-only query per 'unchanged' listing (cheap, indexed). Must key the probe/keep on `latest_version_id` (already available in the branch). Chroma path needs the equivalent metadata filter. No behavior change for correctly-cleaned docs.
- **Test:** Ingest v1 then a revision (v2) with `delete_chunks_for_doc_except_version` monkeypatched to raise on the revised run; assert both v1 and v2 chunks are still indexed. Then ingest again (unchanged) with a healthy delete and assert only v2 chunks remain -- fails today because the unchanged path never re-purges.

### #46 — The request timeout covers only time-to-headers, not the JSON body read

- **Where:** `regwatch/frontend/lib/api.ts:356`
- **Class:** MEDIUM severity / M effort / error-handling · lane `fe-lib` · score 2.7 · confidence 0.8 · verdict CONFIRMED
- **Now:** fetch() resolves on headers; the body is read lazily by json()/blob(). The timeout is armed only across the header phase, so the documented guarantee (line 264-266: 'a hung backend can't leave a page spinning forever') does not hold for the body-deserialization phase. The existing apiTimeout tests only exercise the headers-never-arrive case (hangingFetch never resolves), so this gap is untested.
- **Fix:** Keep the abort armed until the body is consumed for the JSON wrappers. Smallest contained option: in getJSON/postJSON/deleteJSON, race/await the res.json() inside the same abortable scope (e.g. have fetchWithTimeout optionally own the parse, or pass the controller through so handle() can clear the timer only after json() settles). The streaming/docx paths intentionally stream long and already have their own bounds (TTFB + SSE idle watchdog; blob), so scope the fix to the JSON wrappers.
- **Risk:** A stalled body after headers is uncommon (needs chunked/partial responses or a proxy dying mid-body), so this is hardening, not a hot bug. The fix must not shorten legitimate large-JSON reads - use the same DEFAULT_TIMEOUT_MS bound already applied to headers.
- **Test:** In apiTimeout.test.ts, mock fetch to resolve a Response whose body ReadableStream never enqueues/closes; assert me()/getJSON rejects with the timeout ApiError (status 504) within the bound instead of pending.
- **Note:** changes behavior (intended correction)

### #47 — Entity resolution and clarify-interpretation reads (resolve_product / suggest_products / resolve_brand / _interpretation_for) are unguarded; meta-gate also resolves twice

- **Where:** `src/regwatch/generate/grounded_qa.py:1180`
- **Class:** MEDIUM severity / M effort / error-handling · lane `grounded-qa` · score 2.6 · confidence 0.78 · verdict REVISED _(merges [116])_
- **Now:** Same failure-path non-uniformity: the pre-retrieval resolution/clarify I/O sits outside the audited-error boundary applied to the catalog/provider/audit sites, so not every decline path degrades identically. The meta gate re-derives resolution instead of reusing the resolution computed at 1188.
- **Fix:** Compute resolution ONCE, guarded, and reuse it in both the meta gate and the resolution branch. Just before the meta gate (when not caller-pinned), do `resolution = resolve_product(question)` inside a try/except that mirrors the existing audited status="error" degrade (log.warning + capture_exception + `_decline(_refuse, reason="resolver_error", status="error", answer_text=_SERVICE_UNAVAILABLE_TEXT, passages=[])`, as at 1307-1316). Then change the meta gate at 1180 to read `resolution.status != "resolved"` from that value (not a second call), and delete the `resolution = resolve_product(question)` re-derivation at 1188. This single guarded call closes BOTH raise sites (1180 and 1188) and removes the double call, while preserving the meta HARD-VETO ordering (meta fires only when the phrase matches AND resolution is not "resolved"). Guard `suggest_products`(1216)/`resolve_brand`(1226) too if wrapping the whole 1187-1247 block is simpler, but note they are practically shielded by the warm TTL cache once the hoisted resolve succeeds. Separately, guard `_interpretation_for` at its two call sites (1281 and 1503) -- 1503 is deep in the post-synthesis model_refusal branch, far outside any "1187-1247" block, so it must be handled explicitly. For those two, the smallest safe degrade is to make `_doc_count` swallow its DB error and return 0 (so _interpretation_for falls back to its existing n==0 copy "I have its FDA guidance" and the clarify still renders), rather than converting a clarify into an error refuse. Tests: monkeypatch resolve_product to raise, ask an unpinned question, assert audited status="error"; monkeypatch _doc_count to raise, ask a bare drug name, assert the clarify still returns (audited, no 500). Both fail today.
- **Risk:** Failure-path-only change. `distinct_metadata_values` is TTL-cached so the raise window is narrow (cache miss/expiry under outage), which is why this is medium not high. De-duping the resolve call must preserve the meta HARD-VETO ordering (meta fires only when the phrase matches AND resolution != resolved).
- **Test:** `monkeypatch.setattr(qa_mod, "resolve_product", <raises>)`, ask an unpinned question, assert audited status="error" refuse (not an exception). Second test: `monkeypatch.setattr(qa_mod, "_doc_count", <raises>)`, ask a bare drug name (vague_input clarify path), assert audited error. Both fail today.
- **Sequencing:** Dedup the double resolve_product AND guard resolution reads; touches same ask() prelude as 17/18.

### #48 — _ASK_LIMITER liveness/concurrency isolation is not extended to the sibling heavy endpoints (/assemble, /whitepaper, /resolve, /sources/search)

- **Where:** `src/regwatch/api/main.py:782`
- **Class:** MEDIUM severity / M effort / performance · lane `api` · score 2.4 · confidence 0.72 · verdict REVISED
- **Now:** The isolation was applied narrowly to the two /query handlers rather than to the whole class of handlers with a 'holds a thread for a minutes-long LLM/FDA pipeline' profile. rate_limit_per_minute defaults to 30 (config/settings.py:289) and each call can run for minutes (llm_timeout_s=60 x retries; http_timeout_s=30 x many FDA calls), so one authenticated user's rate budget alone can pin dozens of default-pool threads and stall /health and /ready.
- **Fix:** Add ONE dedicated bounded limiter next to _ASK_LIMITER (e.g. _HEAVY_LIMITER = anyio.CapacityLimiter(N), N sized independently of ask's 16). Convert assemble, whitepaper, resolve, sources_search to `async def` and move their ENTIRE blocking body into `await anyio.to_thread.run_sync(partial(fn, ...), limiter=_HEAVY_LIMITER)`, wrapped by the same read-then-acquire + defined 503 shed used in _dispatch_ask (statistics().borrowed_tokens >= total_tokens -> HTTPException 503). Keep _enforce_query_rate_limit BEFORE the dispatch on each. Do NOT fold build_dossier into _ASK_LIMITER: that binds /assemble's minutes-long FDA fetch + PSG matching to /query's synthesis token budget and lets one starve the other; _ASK_LIMITER must stay exclusive to the /query ask() dispatch. build_dossier's inner direct ask() call does not acquire a limiter token, so there is no nested-acquire deadlock either way. This preserves the 503-under-saturation behavior change the finding already flags (preserves_behavior=false) and mirrors the existing, reviewed _dispatch_ask pattern -- smallest safe extension. Under min_machines_running=2 the starvation is softened but not removed, so the isolation is still warranted.
- **Risk:** Behavior change: adds a 503-under-saturation path these endpoints do not have today (identical to /query's contract). Must move the ENTIRE blocking body inside run_sync so no partial migration leaves a blocking call on the loop. Under min_machines_running=2 the starvation is softened but not removed.
- **Test:** Monkeypatch build_dossier to block on a threading.Event; fire (default_pool_size + 1) concurrent POST /assemble; assert GET /health still returns 200 within ~1s (today it stalls) OR that the saturating call returns a bounded 503 rather than queueing behind a minutes-long hold.
- **Note:** changes behavior (intended correction)

### #49 — Four whitepaper-run endpoints repeat the same run_store-error -> HTTPException ladder

- **Where:** `src/regwatch/api/main.py:1430`
- **Class:** MEDIUM severity / M effort / duplication · lane `x-duplication` · score 2.4 · confidence 0.72 · verdict CONFIRMED
- **Now:** The status-per-error-type policy is a single fact but is spelled out inline in every endpoint, so adding a new run_store error type or changing a status means editing four try/except blocks.
- **Fix:** Encode the mapping once — either register a FastAPI exception handler for WhitepaperRunError, or a module-level dict `_RUN_ERROR_STATUS = {RunNotFoundError: (404, _RUN_NOT_FOUND_DETAIL), RunNotOwnedError: (403, 'only the run\'s creator may delete it'), ...}` with a default of `(status_from_type, str(exc))` and a `_to_http(exc)` helper the endpoints call in one `except run_store.WhitepaperRunError as exc: raise _to_http(exc) from exc`. IntegrityMismatchError keeps its special _stored_corruption_500 path. Preserve the per-error detail strings exactly.
- **Risk:** Mappings are not 100% uniform (delete adds 403; set_cell groups ConcurrentEditError with 409), so the dict must capture each type's (status, detail) precisely or a code/detail silently changes. Medium effort because it touches four endpoints.
- **Test:** For each endpoint, force each run_store error type and assert the HTTP status and detail are unchanged (404 _RUN_NOT_FOUND_DETAIL, 409 for finalized/concurrent, 403 creator-only, 422 invalid-cell/too-long, 500 integrity) — the same assertions must pass before and after the refactor.

### #50 — INV-5 form guard decomposes the (dosage_form, route) pair into independent set checks, admitting a cross-pair PSG for multi-form/multi-route applications

- **Where:** `src/regwatch/whitepaper/populator.py:800`
- **Class:** MEDIUM severity / M effort / correctness · lane `wp-populator` · score 2.4 · confidence 0.72 · verdict CONFIRMED
- **Now:** The compatibility check validates form and route against unpaired sets instead of against the set of actual (form,route) pairs the application carries. For single-form/single-route applications (the common case) the decomposition is harmless; it only leaks when both sets have >1 element with a non-existent cross pair.
- **Fix:** Have _ob_forms_and_routes also expose a set of normalized (form, route) PAIRS, and in _filter_psg_by_form gate a name-only match on pair membership: `(_normalized_form(doc form), _normalized_form(doc route)) in ob_pairs` (falling back to form-only when the application has no recorded routes, preserving today's route-less behavior). matched_by_appl docs still short-circuit unchanged.
- **Risk:** For single-pair applications behavior is identical (pairs == {(form,route)}). Only multi-form-AND-multi-route applications change: some previously-kept name matches now go to psg_other_form_docs / the analyst path -- the INV-5-correct outcome. Keep the existing route-empty short-circuit so inhalation-style single-route drugs are unaffected.
- **Test:** Seed OB product_rows = [(dosage_form_route 'TABLET;ORAL'), ('SOLUTION;INTRAVENOUS')] and a name-matched PSG doc with dosage_form='Tablet', route='Intravenous'; assert build_whitepaper puts it in spine/psg_other_form_docs (be_guidance_available -> analyst, not 'Yes'). Fails today (kept as this-form guidance), passes after the pair check.
- **Sequencing:** Populator INV-5 pair-check; keep consistent with dossier 54/55/94.
- **Note:** changes behavior (intended correction)

## Opportunistic (51)

### #51 — api.ts QueryStatus comment enumerates 6 statuses, omitting 'meta'

- **Where:** `regwatch/frontend/lib/api.ts:31`
- **Class:** LOW severity / S effort / doc-drift · lane `fe-lib` · score 2.9 · confidence 0.95 · verdict CONFIRMED
- **Now:** The generated union QueryResponse['status'] (api-types.ts:718) has SEVEN members including 'meta', and turns.ts STATUSES (line 42) and REASON_COPY handle meta turns - so 'meta' is a real emitted status. The comment claims to be the authoritative backend Literal but is stale, misleading any reader who trusts it that 'meta' is impossible.
- **Fix:** Add 'meta' to the enumerated list (…scope_warning | meta | refused | error…), or replace the hand-list with a pointer to the generated union to prevent re-drift.
- **Risk:** Comment-only; no runtime effect.
- **Test:** Same STATUSES/QueryStatus exhaustiveness test as finding #4 doubles as the guard - if 'meta' were dropped from the union the test would fail, flagging the comment too.
- **Sequencing:** Fix alongside 66 STATUSES drift.

### #52 — Resolver full-ingredient-name tie-break (salt disambiguation) is untested

- **Where:** `src/regwatch/retrieve/resolver.py:251`
- **Class:** LOW severity / S effort / test-gap · lane `x-tests` · score 2.9 · confidence 0.95 · verdict CONFIRMED
- **Now:** test_resolver only exercises the AMBIGUOUS side of this pair (test_real_salt_forms_clarify_without_salt_only_junk uses the bare 'amlodipine', which correctly clarifies). No test names a specific salt to exercise the tie-break that RESOLVES one candidate, so lines 249-251 have no coverage.
- **Fix:** Add a test: resolve_product('amlodipine besylate dissolution method', products=JUNK_CORPUS); assert status=='resolved', normalized_name=='amlodipine besylate', by_name is True. Pair it with the existing bare-'amlodipine' ambiguous assertion to lock both sides of the tie-break.
- **Risk:** Removing the _full_ingredient_words tie-break would turn every salt-specific question among same-primary-token products into a clarify (a safe but degraded UX -- an unnecessary hop on questions the user already disambiguated), undetected.
- **Test:** tests/test_resolver.py: r=resolve_product('amlodipine besylate dissolution method', products=JUNK_CORPUS); assert r.status=='resolved' and r.normalized_name=='amlodipine besylate' and r.by_name; keep test_real_salt_forms_clarify_without_salt_only_junk for the bare-name ambiguous side.

### #53 — Dead public Orange Book parsers parse_patent_text / parse_exclusivity_text

- **Where:** `src/regwatch/sources/orange_book.py:226`
- **Class:** LOW severity / S effort / dead-code · lane `x-dead-doc` · score 2.9 · confidence 0.95 · verdict CONFIRMED
- **Now:** When products/patent/exclusivity each had a public text parser, all three were kept for symmetry; the caching refactor routed patent/exclusivity through the internal parser but left the public wrappers behind.
- **Fix:** Delete parse_patent_text and parse_exclusivity_text (lines 226-231). They are not in sources/__init__.py __all__.
- **Risk:** Public module-level names, so an out-of-tree importer could in theory use them (none in repo). Trivial to restore if ever needed; PATENT_COLUMNS/EXCLUSIVITY_COLUMNS remain used by the live path.
- **Test:** test_source_handlers.py still exercises parse_products_text and the ZIP-backed patent/exclusivity rows via product_rows; add `assert not hasattr(orange_book, 'parse_patent_text')` to lock the removal.

### #54 — Dead ExtractionResult.populated_field_count property

- **Where:** `src/regwatch/process/extractor.py:40`
- **Class:** LOW severity / S effort / dead-code · lane `x-dead-doc` · score 2.9 · confidence 0.95 · verdict CONFIRMED
- **Now:** Convenience property added with the ExtractionResult dataclass but never consumed by the pipeline, audit log, or tests.
- **Fix:** Delete the property (lines 40-42).
- **Risk:** Public attribute on a dataclass; no in-repo consumer. If a future observability metric wants it, re-adding is one line.
- **Test:** Existing extractor tests build ExtractionResult and assert on fields/citations; they stay green. Add `assert not hasattr(ExtractionResult(fields={}, citations={}, model_name='x'), 'populated_field_count')` if you want to pin it.

### #55 — Vestigial crawl_concurrency knob (self-labeled no-effect) surfaced to operators via .env.example

- **Where:** `config/settings.py:243`
- **Class:** LOW severity / S effort / dead-code · lane `x-dead-doc` · score 2.9 · confidence 0.95 · verdict CONFIRMED
- **Now:** A speculative concurrency knob kept 'in case a concurrent fetch path is added' -- exactly the kind of unused generality the engineering standard says to flag, not ship.
- **Fix:** Delete crawl_concurrency and its .env.example line (both together, or the drift test fails). If/when a concurrent crawl path lands, add the knob then with a real reader.
- **Risk:** Removal preserves behavior (no reader). Only cost is re-adding later if concurrency is implemented -- cheap.
- **Test:** tests/test_env_example_drift.py enforces the field<->.env.example coupling; removing both keeps it green and would fail if only one side were dropped.

### #56 — PROJECT_SPEC 'return exactly' refusal string drifts from the shipped refusal_text

- **Where:** `docs/PROJECT_SPEC.md:339`
- **Class:** LOW severity / S effort / doc-drift · lane `x-dead-doc` · score 2.8 · confidence 0.92 · verdict CONFIRMED
- **Now:** The refusal copy was intentionally softened for warmth (see the settings.py comment on the sentinel), but the spec of record was not updated to match.
- **Fix:** Update PROJECT_SPEC.md:339 to quote the current refusal_text verbatim (code is the source of truth for product copy), or drop the literal and reference Settings.refusal_text.
- **Risk:** Doc-only. The refusal_text is prefix-matched by grounded_qa, so the exact string is load-bearing in code; keeping the spec in sync avoids a future 'fix' that reverts the copy and breaks warmth.
- **Test:** Add a drift test asserting the string literal in the spec (or a shared constant) equals Settings().refusal_text, mirroring the .env.example drift guard.

### #57 — _related_from_passages re-implements _options_from_names, defeating its single-source pill contract

- **Where:** `src/regwatch/generate/grounded_qa.py:412`
- **Class:** LOW severity / S effort / duplication · lane `grounded-qa` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The related-pointer builder predates or diverged from the shared helper; the duplication is exactly the drift the shared helper was written to prevent (a future field added to the pill contract would reach clarify options but silently not the 'related' refusal pointers).
- **Fix:** Reduce `_related_from_passages` to compute the ordered-distinct names then `return _options_from_names(names)` — e.g. build `names` preserving first-occurrence order, then delegate. Removes the inline ClarifyOption construction.
- **Risk:** Behavior-preserving: output is identical ClarifyOptions in the same order. Only touches a helper used by the low_top_score refuse (1365).
- **Test:** Unit test: passages with normalized_name ["albuterol sulfate", "albuterol sulfate", "atorvastatin calcium"] -> assert `[asdict(o) for o in _related_from_passages(passages)] == [asdict(o) for o in _options_from_names(["albuterol sulfate", "atorvastatin calcium"])]`. Would fail if the two builders drift.

### #58 — strip_sources_trailer misses a single-line 'Sources:' trailer, leaking unbracketed re-citable pointers into conversation memory

- **Where:** `src/regwatch/common/citations.py:33`
- **Class:** LOW severity / S effort / correctness · lane `llm-common` · score 2.7 · confidence 0.9 · verdict REVISED
- **Now:** The trailer detector hard-codes the exact multi-line prompt format and is the sole guard against unbracketed trailer pointers entering conversation memory; a plausible single-line trailer variant defeats it.
- **Fix:** Replace line 33 with: _SOURCES_TRAILER = re.compile(r"\n[ \t]*Sources:.*\Z", re.IGNORECASE | re.DOTALL). This actually strips BOTH the multi-line prompt-mandated trailer AND a single-line 'Sources: X, p.4' variant by consuming from the line-start 'Sources:' to end-of-string. Verified empirically: multi-line -> prose only; single-line -> prose only; a mid-prose lowercase 'sources:' NOT at line start is left untouched. Use [ \t]* (not \s*) so the line-start anchor is preserved and a bare 'sources:' inside a sentence is not truncated. strip_sources_trailer's .split(text, maxsplit=1)[0] then yields the prose. (The finding's proposed regex does NOT work.)
- **Risk:** INV-1 is NOT breached today: _validate_citations only honors pairs grounded in the current turn's passages, so a leaked pointer cannot become a valid citation regardless. The fix is context hygiene. Over-broadening the regex could truncate an answer that legitimately contains 'Sources:' mid-prose; anchoring to line-start plus the trailing newline/EOL keeps that risk low.
- **Test:** Assert strip_sources_trailer('The USP paddle method is recommended.\nSources: PSG_020503, p.4') returns only the prose (no 'PSG_020503'/'p.4'). Today it returns the input unchanged and the test would fail.
- **Note:** changes behavior (intended correction)

### #59 — LLMResponse.raw is populated via model_dump() on every completion but never read

- **Where:** `src/regwatch/generate/llm.py:35`
- **Class:** LOW severity / S effort / dead-code · lane `llm-common` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** A response-debugging affordance was carried on the contract but no consumer ever materialized, so it is dead generality that also pays an unnecessary model_dump() per LLM call (the codebase standard forbids unused generality).
- **Fix:** Stop populating it: pass raw={} (or drop the assignments) so the field keeps its default-factory dict for any out-of-tree caller while the per-call model_dump() serialization is removed. Dropping the field entirely is also viable but is a wider contract change, so keeping the field and not populating it is the smaller step.
- **Risk:** Nothing in-tree reads .raw, so behavior is preserved; any external/debug tooling that inspected it would now see {} instead of the full payload. Reversible.
- **Test:** A test asserting complete() returns a usable LLMResponse whose .raw is {} locks the decision in; more usefully, a guard test/grep asserting no production code reads LLMResponse.raw prevents silent re-introduction. (Low testability is itself a signal the field is unused.)

### #60 — fetch_citation_recency pays 2 redundant catalog round-trips (inspect().has_table x2) per answered query; the surrounding try/except already covers missing tables

- **Where:** `src/regwatch/store/queries.py:77`
- **Class:** LOW severity / S effort / performance · lane `store` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** A schema-introspection pre-check duplicates the failure handling the try/except already provides. In any booted production DB these tables always exist (retrieval requires versioned docs), so the guard never fires yet always costs 2 catalog queries.
- **Fix:** Delete the inspect()/has_table pre-check and let the try/except return the empty index when a SELECT hits a missing table.
- **Risk:** Behavior change on a fresh/missing-table DB: it now flows through the except branch, emitting log.warning('citation_recency_lookup_failed') instead of returning empty silently (arguably better observability). The returned empty-index outcome is preserved; only the log line + the (removed) round-trips differ. Note: current_dosage_form_routes has NO try/except, so its has_table guard is load-bearing and must stay.
- **Test:** With psg_version/psg_document present, install a SQLAlchemy before_cursor_execute counter and assert no pg_catalog has_table query is issued by fetch_citation_recency; separately drop the tables and assert it still returns RecencyIndex(by_version={}, doc_dates={}).
- **Note:** changes behavior (intended correction)

### #61 — PSG handler's query_text ingredient fallback is dead: computed then never consulted

- **Where:** `src/regwatch/sources/psg.py:29`
- **Class:** LOW severity / S effort / dead-code · lane `sources` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The `or query.query_text` fallback expresses an intent to match PSGs by free text when no structured ingredient is supplied, but the guarding condition was left as `query.active_ingredient` only, so the fallback branch is unreachable. It reads as working relevance filtering while doing nothing.
- **Fix:** Smallest behavior-preserving fix: drop the dead `or query.query_text` from lines 29-30 (and simplify to canonical_name(query.active_ingredient or '')), so the code no longer implies a filter it does not perform. If free-text PSG relevance is actually wanted, instead widen the gates to `if query.active_ingredient or query.query_text:` and the Python check accordingly — but that changes behavior and should be a deliberate decision.
- **Risk:** Dropping the fallback is a pure no-op at runtime (the value was unused), so observable behavior is identical. Choosing the wire-it-in alternative WOULD change results for query_text-only PSG searches and should be tested against the gold set.
- **Test:** Characterization test: against a DB with unrelated PSGs, assert PsgHandler().search(SourceQuery(query_text='albuterol')) returns the same rows as SourceQuery(query_text='') — proving query_text has no filtering effect today; the dead-code removal must keep this test green.

### #62 — Fuzzy fallback re-normalizes every watchlist product on every non-matching listing

- **Where:** `src/regwatch/watch/matcher.py:213`
- **Class:** LOW severity / S effort / performance · lane `watch-retrieve` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The per-product canonical/stripped keys computed in _index_watchlist are discarded (only the value->products maps are kept), so the fuzzy stage has no handle on them and recomputes.
- **Fix:** Compute the product keys once before the listing loop -- e.g. product_keys = [(p, canonical_name(p.get('active_ingredient','')), stripped_name(p.get('active_ingredient',''))) for p in products] -- and have both _index_watchlist and the fuzzy loop consume that list instead of re-deriving. The fuzzy loop then iterates product_keys and reuses canon/strip. No behavior change; the same scores/matches result.
- **Risk:** Pure hoist; the only risk is transcription (using the wrong precomputed key). Watch runs are a daily cron, not latency-critical, so this is efficiency/cleanliness, not a correctness fix. Keep the empty-key `if not key: continue` guard.
- **Test:** A parity test: run match_listings on a fixed (listings, products) fixture that exercises canonical/stripped/combo/fuzzy branches and assert the exact set of (product id, rationale, confidence) is identical before/after the refactor; the existing test_fuzzy_handles_minor_typos must still pass.

### #63 — watchlist and aliases hand-roll the owned-client lifecycle instead of the shared owned_client/get_openfda_client helpers

- **Where:** `src/regwatch/watch/watchlist.py:113`
- **Class:** LOW severity / S effort / duplication · lane `watch-retrieve` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** Two more copies of the resource-lifecycle idiom the codebase has otherwise centralized; each is a place a future timeout/header/close change must be repeated, and a hand-rolled try/finally is where a resource leak on an error path can slip in.
- **Fix:** Replace both bodies with `with owned_client(client, get_openfda_client) as active_client:` and drop the `owned` flag + inline httpx.Client(...) construction, mirroring fetch_openfda_results in sources/_utils.py.
- **Risk:** get_openfda_client() constructs the identical client (same timeout + User-Agent), so behavior is preserved; verify the import doesn't create a cycle (sources/_utils imports nothing from watch, so it is safe -- same reasoning the crawler used).
- **Test:** Call fetch_drugsfda_for_company(client=sentinel) with a mock httpx.Client and assert sentinel.close() is NOT called (caller-owned), then call it with client=None patched to a mock factory and assert the created client IS closed on both the success and exception paths.

### #64 — SSE field-parse logic is duplicated between the read loop and the post-close drain

- **Where:** `regwatch/frontend/lib/api.ts:566`
- **Class:** LOW severity / S effort / duplication · lane `fe-lib` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The drain path re-implements the loop's line parser to handle a final record lacking its blank-line terminator. Two copies of the same grammar can drift if one is fixed (e.g. handling a stray BOM, a new field, or CRLF nuance) and the other missed - a subtle correctness hazard for the citation-bearing result frame.
- **Fix:** Extract a local `const parseField = (line: string) => { ... assign eventName/data ... }` (closing over eventName/data or returning them) and call it from both the loop and the drain, so the SSE field grammar lives in one place.
- **Risk:** Pure refactor; keep the existing leading-space-strip and colon-less-line semantics identical. Covered by existing sse.test.ts cases (CRLF, split reads, no-trailing-newline drain).
- **Test:** Existing 'dispatches a final result record with no trailing blank line' plus a new case where the drained final data line carries a leading space (`data:  {json}`) - both must parse identically through the shared helper.

### #65 — normalizeQuery guards only `clarify`, not `related`, and its comment is inaccurate

- **Where:** `regwatch/frontend/lib/api.ts:39`
- **Class:** LOW severity / S effort / correctness · lane `fe-lib` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The generated QueryResponse gives BOTH clarify and related a `@default []` (api-types.ts:727 and 732), so related is equally omittable on a degraded/partial wire payload - and the sse.test.ts fixture literally omits `related` (resultFrameData, lines 37-46). A normalized QueryResponse therefore still has `related: undefined`; only assistantTurn's separate `r.related ?? []` (turns.ts:151) saves the one consumer that reads it. The asymmetry is a latent trap for any future consumer that reads normalized.related directly.
- **Fix:** Guard both arrays in one place: `return { ...r, clarify: r.clarify ?? [], related: r.related ?? [] }`, and correct the comment to say both clarify and related are defaulted-empty.
- **Risk:** Behavior-preserving (the sole current reader already null-coalesces related); this just makes the normalized object internally consistent so downstream code cannot hit undefined.related.
- **Test:** normalizeQuery applied to a QueryResponse object missing `related` should return related===[] (and clarify===[]).

### #66 — Ask page clones+reverses the whole turns array every render (per streamed token) to find the last assistant turn

- **Where:** `regwatch/frontend/app/(shell)/page.tsx:380`
- **Class:** LOW severity / S effort / performance · lane `fe-ui` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** An eager clone+reverse where a backward scan suffices.
- **Fix:** Use `turns.findLast((t) => t.role === 'assistant')` (ES2023, supported by the Next 16 browser targets) — no allocation, no reverse. If a broader target is required, a small reverse `for` loop returning on the first assistant turn is equivalent.
- **Risk:** Negligible; `findLast` returns the same element the reversed `find` does. Confirm the build's TS lib/target includes ES2023 array methods (Next 16 defaults do).
- **Test:** Existing askPage clarify tests already assert the composer hint reacts to the last assistant turn's status; they must stay green. Optionally assert the placeholder switches to the clarify-reply copy after a clarify turn to lock the behavior through the refactor.

### #67 — watch/aliases._fetch and watch/watchlist._fetch_page are the identical openFDA retry+429+404 GET

- **Where:** `src/regwatch/watch/aliases.py:38`
- **Class:** LOW severity / S effort / duplication · lane `x-duplication` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** Both watch builders needed the same 'poll openFDA page-by-page where 404 = end-of-results' primitive and each wrote it; there is no shared watch-layer openFDA fetch helper (the source handlers use _utils.get_with_retry, but that returns the response instead of raising to drive tenacity, so it isn't reused here).
- **Fix:** Keep the parameterized watchlist._fetch_page as the single home and make aliases._fetch delegate: `return _fetch_page(client, DRUGSFDA_URL, params)` (watchlist already imports aliases lazily inside a function, so a lazy `from regwatch.watch.watchlist import _fetch_page` inside _fetch avoids an import cycle), or hoist _fetch_page into a small shared watch/_openfda.py. Removes the duplicated decorator+body.
- **Risk:** Import ordering (watchlist<->aliases) — use the same lazy-import pattern watchlist already uses for get_aliases to stay acyclic. Retry semantics are identical so no behavior change.
- **Test:** Drive both functions with a fake client returning 404 (expect {"results": []}, no retry) and 429 (expect it raises HTTPStatusError and the tenacity wrapper makes 3 attempts); after the refactor the 429/404 test on aliases._fetch exercises the shared _fetch_page body.

### #68 — Sentence-splitter regex (?<=[.!?])\s+ duplicated in populator and eval metrics

- **Where:** `src/regwatch/whitepaper/populator.py:1634`
- **Class:** LOW severity / S effort / duplication · lane `x-duplication` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** Both the populator's pregnancy-registry sentence scan and the eval's per-sentence faithfulness metric need 'split prose into sentences,' but there is no shared sentence utility, so each embedded the lookbehind split.
- **Fix:** Add one `split_sentences(text) -> list[str]` to common/text_normalize.py (or common/citations.py, next to the other shared prose helpers) and call it from both. Keep the strip/non-empty filtering the populator already applies.
- **Risk:** Very low; the grammar is identical. Confirm metrics still filters empties the same way populator does after consolidation.
- **Test:** Table-test split_sentences on multi-sentence input including abbreviations and trailing whitespace; assert populator._split_sentences and metrics sentence-splitting produce identical lists for the same input.

### #69 — get_aliases drops the isinstance-dict cache guard that its sibling discover_applicant_aliases has

- **Where:** `src/regwatch/watch/aliases.py:132`
- **Class:** LOW severity / S effort / type-safety · lane `x-types` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The isinstance-dict guard proven one function above was not copied to get_aliases; the except clause narrows to JSONDecodeError only, so a shape mismatch (not a parse failure) escapes.
- **Fix:** Guard the shape: `if isinstance(data, dict): cached = list(data.get("aliases") or []); if cached and data.get("root") == s.company_name.strip(): return cached` — matching discover_applicant_aliases exactly.
- **Risk:** Very low: the cache file is written by this app (always an object), so the failure needs external tampering/corruption. The fix only makes a corrupt cache fall through to the env/COMPANY_NAME fallback instead of raising.
- **Test:** Write `["x"]` (a JSON array) to the cache path and call get_aliases; assert it falls back to the env aliases rather than raising AttributeError. Fails today.

### #70 — Buffered path: model appending prose after the refusal sentinel is untested

- **Where:** `src/regwatch/generate/grounded_qa.py:1488`
- **Class:** LOW severity / S effort / test-gap · lane `x-tests` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** test_streaming_synthesis::test_stream_synthesis_holds_whitespace_prefixed_refusal covers the STREAM guard, but no test drives the BUFFERED ask() path with a completion of the form refusal_text + trailing prose. The `.startswith()` branch, the deviation log, and the guarantee that trailing prose does not leak are uncovered in the buffered path.
- **Fix:** Add a test: seed a single-product corpus, stub provider to return get_settings().refusal_text + ' However, sponsors typically run a fed study.'; assert result.refused is True, result.answer is the canned refusal (not the trailing prose), and 'fed study' not in result.answer. Optionally assert the log event fired.
- **Risk:** A refactor tightening the check to exact equality (`answer == s.refusal_text`) would let a sentinel-plus-trailing-prose completion fall through to synthesis and leak the uncited trailing prose as an answer, with the suite green.
- **Test:** Seed one product; stub complete() -> LLMResponse(text=get_settings().refusal_text + ' However, a fed study is typical [PSG_999999, p.9].'); r=qa_mod.ask(q); assert r.refused and 'fed study' not in r.answer and r.answer==get_settings().refusal_text.

### #71 — Meta answer empty-state branches (no watchlist / no flagged changes) are untested

- **Where:** `src/regwatch/generate/grounded_qa.py:945`
- **Class:** LOW severity / S effort / test-gap · lane `x-tests` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** test_meta_routing::test_t3b covers the 'what changed?' branch WITH a durable alert present, and test_t3 seeds a watchlist product. Neither empty-state branch is exercised, so the no-watchlist and no-alerts copy paths (and the guard that they never emit a regulatory claim in the empty state) are uncovered.
- **Fix:** Add a test: seed a corpus (so meta fires) but NO watchlist rows and NO durable alerts; ask 'what changed?'; assert status=='meta', citations==[], and both 'Watch is not monitoring any products yet.' and 'Watch has not flagged any guidance changes yet.' appear.
- **Risk:** A refactor of the empty-state assembly could emit a misleading or malformed line (e.g. an empty 'Watch monitors 0 products: .') on a fresh install with no watchlist/alerts, with no failing test.
- **Test:** tests/test_meta_routing.py: _seed(['atorvastatin calcium','metformin hydrochloride']) with no add_manual_product and no _persist_alerts; r=qa_mod.ask('what changed?'); assert r.status=='meta' and 'not monitoring any products' in r.answer and 'not flagged any guidance changes' in r.answer.

### #72 — GET /sessions/{id} issues a redundant title query after already loading all messages

- **Where:** `src/regwatch/api/main.py:1794`
- **Class:** LOW severity / S effort / duplication · lane `x-perf` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The title fallback is derived via its own query instead of from the messages list the handler just fetched.
- **Fix:** Derive the title from the loaded messages (row.title or the first m with role=='user', which is already first in the asc-ordered list), dropping the extra query. _session_title is only used here, so it can be inlined or take the messages list.
- **Risk:** Messages are already ordered created_at asc, so the first role=='user' entry equals the current subquery result; equivalent output.
- **Test:** Assert get_session returns the same title as today for a titled and an untitled session, and (patching query execution) that it issues no message query beyond the single messages load.

### #73 — `regwatch status` prints the deprecated retrieval_top_k (None) instead of the effective retrieval config

- **Where:** `src/regwatch/cli.py:44`
- **Class:** LOW severity / S effort / doc-drift · lane `x-dead-doc` · score 2.7 · confidence 0.9 · verdict CONFIRMED
- **Now:** The status command was written against the old single-knob name and not updated when RERANK_TOP_K/VECTOR_TOP_K replaced RETRIEVAL_TOP_K (see effective_rerank_top_k).
- **Fix:** Print the effective retrieval config instead: `"vector_top_k": s.vector_top_k, "rerank_top_k": s.effective_rerank_top_k` (drop the deprecated field, or keep it clearly labeled 'retrieval_top_k (deprecated)').
- **Risk:** Behavior change: the status JSON output keys change. Low blast radius (diagnostic command; no code parses its output in-repo). Any external tooling scraping the key would need updating.
- **Test:** Add a CLI test invoking `status` and asserting the output reports rerank_top_k=8 (the effective value) rather than None, so a future regression to the deprecated field is caught.
- **Note:** changes behavior (intended correction)

### #74 — suggest_products feeds a multi-ingredient name into _primary_token, producing a separator-suffixed token for combos

- **Where:** `src/regwatch/retrieve/resolver.py:294`
- **Class:** LOW severity / S effort / correctness · lane `watch-retrieve` · score 2.6 · confidence 0.88 · verdict CONFIRMED
- **Now:** _primary_token is designed for a single ingredient (resolve_product/resolve_brand always call it via split_ingredients first through _product_tokens); suggest_products is the one caller that passes the whole possibly-multi-ingredient name, so the semicolon separator leaks into the token.
- **Fix:** Score against the product's clean primary token set instead: `primaries = _product_tokens(name); best = max((int(fuzz.ratio(tok, p)) for tok in candidates for p in primaries), default=0)`. This matches how resolve_product/resolve_brand already consume _product_tokens and removes the separator artifact.
- **Risk:** Changes suggestion scores for COMBO products only (single-ingredient products are unaffected -- their _primary_token has no separator). A combo whose ingredient the user typoed may now clear the 82 threshold where it previously fell just short, so a few more (correct) suggestions can appear. Suggestions only ever ASK, never auto-substitute, so INV-2 is unaffected.
- **Test:** resolve.suggest_products('what is the be study for buseonide?', products={'albuterol sulfate; budesonide'}) should suggest the combo; assert the current code returns [] (or a lower score) and the fixed code returns the combo.
- **Note:** changes behavior (intended correction)

### #75 — Orange Book ZIP download and member decompression have no size bound, unlike the PDF ingest path

- **Where:** `src/regwatch/sources/orange_book.py:355`
- **Class:** LOW severity / S effort / security-boundary · lane `sources` · score 2.7 · confidence 0.8 · verdict REVISED _(merges [93])_
- **Now:** The input-at-the-boundary size guard that the PDF ingest path enforces (settings.pdf_max_bytes, comment at settings.py:250-260: 'A malformed or oversized ... must not be able to hang or OOM that run') was never applied to the Orange Book ZIP, even though it is the same class of untrusted-remote-body boundary. A redirected/compromised URL or a decompression bomb would buffer/expand unbounded and could OOM the process.
- **Fix:** Keep the gap (real, low severity, defense-in-depth consistent with the proven PDF/template caps), but fix the guard placement, cheapest-first. (1) Primary, truly minimal, in-memory guard against the decompression bomb: in _file_texts_from_zip, cap cumulative decompressed size while reading -- accumulate len(zf.read(name)) across selected members (or pre-check sum(zi.file_size for zi in zf.infolist())) and raise when it exceeds a wide-margin cap (add settings.orange_book_max_bytes, same '0 disables' convention as pdf_max_bytes; real file is tens of MB, so e.g. a few hundred MB is a safe ceiling). This is a pure in-memory check at line 355, no I/O refactor, and it is the guard that actually stops the OOM class for a ZIP. (2) Optional download-buffer guard to genuinely match the PDF pattern: replace _fetch_zip_files' get_with_retry (which fully buffers) with a streamed, byte-capped fetch mirroring psg_crawler._stream_capped / template_fetch._fetch_capped -- only streaming bounds the buffered download; checking resp.content length after get_with_retry does not. Do NOT anchor the fix on line 337's resp.content length as the smallest-safe change: by that point the body is already buffered and the zip-bomb surface is untouched. Both preserve behavior on normal ZIPs.
- **Risk:** Must set the cap well above the real ~few-MB file (the PDF path uses a 25x margin). fda.gov is trusted today, so severity is low; the value is defense-in-depth consistent with the proven PDF pattern. No effect on normal ZIPs.
- **Test:** Mock ORANGE_BOOK_ZIP_URL to return a body larger than the configured cap and assert _fetch_zip_files raises/aborts instead of parsing; assert a normal small ZIP still parses. Fails today (no cap exists).
- **Sequencing:** Single byte-cap fix for the Orange Book ZIP download+decompress (93 is the same boundary).

### #76 — _fetch_psg_store opens three separate DB sessions per build for one logical PSG lookup

- **Where:** `src/regwatch/whitepaper/populator.py:638`
- **Class:** LOW severity / S effort / performance · lane `wp-populator` · score 2.6 · confidence 0.85 · verdict CONFIRMED
- **Now:** Each helper independently manages its own session rather than accepting an open session from the orchestrating _fetch_psg_store.
- **Fix:** Open one session_scope() in _fetch_psg_store and thread it into _matching_psg_docs(ctx, s) and _latest_be_requirement(ctx, s) as a parameter (they already run entirely inside a session). Keeps the single try/except tri-state boundary that already wraps them.
- **Risk:** Low; all reads, same transaction is fine and slightly more consistent. Ensure the shared session is still closed on every path (the existing session_scope context manager handles it).
- **Test:** Behavioral parity test: build_whitepaper over the seeded PSG corpus yields identical psg_docs / be_requirement before and after. Optionally assert session_scope is entered once during _fetch_psg_store via a spy.

### #77 — Eval recall_at_k / citation_precision are blind to a vanished gold cell

- **Where:** `src/regwatch/eval/whitepaper_metrics.py:103`
- **Class:** LOW severity / S effort / test-gap · lane `wp-rest` · score 2.6 · confidence 0.85 · verdict CONFIRMED
- **Now:** The missing-cell early-continue bypasses precisely the two metrics that are supposed to fail when the expected content/provenance is absent; the harness treats 'cell gone' as 'not applicable' rather than 'expected but missing'.
- **Fix:** In the cell-is-None branch, before continue: if item.expect_value_contains is not None, value_checked += 1 (no value_correct); if item.expect_evidence_source is not None, evidence_checked += 1 (no evidence_correct). A vanished value-bearing/evidence-bearing cell then fails recall/precision too.
- **Risk:** Intentional metric change: recall_at_k/citation_precision drop for gold sets that currently contain not-found items -- but a not-found item is already a real regression, so surfacing it in all three thresholds is the point. No production runtime impact (eval-only).
- **Test:** Add a WhitepaperGoldItem(cell_id='nonexistent', expect_status='populated', expect_value_contains='x') against a result lacking that cell; assert scorecard.recall_at_k == 0.0 (today it is 1.0).
- **Note:** changes behavior (intended correction)

### #78 — Single scalar httpx timeout gives a 30s connect timeout, and get_with_retry never retries connection/read errors

- **Where:** `src/regwatch/sources/_utils.py:114`
- **Class:** LOW severity / S effort / error-handling · lane `sources` · score 2.6 · confidence 0.85 · verdict REVISED
- **Now:** The timeout knob is one scalar and the retry helper is status-code-only. On the daily `watch` cron (the sole alerting driver per settings.py:250-255) a dead host or a single dropped connection therefore hangs for 30s per source and drops that source with no retry, even though the retry helper's stated purpose is transient-fault resilience.
- **Fix:** Keep the two-part fix but correct the root-cause framing: the affected path is the API query / source-handler path (fetch_openfda_results -> get_openfda_client -> get_with_retry, plus dailymed/orange_book/rems callers of get_with_retry), NOT the watch cron (which uses psg_crawler's tenacity retry and watchlist._fetch_page). Fix (1) timeout shape: in both get_openfda_client (line 114) and _dailymed_client (dailymed.py:517) build httpx.Timeout(connect=min(s.http_timeout_s, 10.0), read=s.http_timeout_s, write=s.http_timeout_s, pool=s.http_timeout_s) so a dead host fast-fails on connect. Fix (2) retry scope: in get_with_retry wrap client.get() so httpx.ConnectError/httpx.ReadTimeout are retried with the same 0.5*2**attempt backoff as 5xx (GETs are idempotent; re-raise after the last attempt). This lifts all four get_with_retry callers, not just openFDA. Note the timeout-shape change is per-factory and would fold naturally into the tracked 4x-factory consolidation; the retry-scope change is the higher-value, factory-independent part. Behavior changes (adds latency on a truly-down host, fails slow connects sooner) are correctly flagged; keep attempts=3. Tests: assert get_openfda_client().timeout.connect < .read; and side_effect=[httpx.ConnectError('x'), httpx.Response(200, json={'results':[]})] returns the 200.
- **Risk:** A shorter connect timeout will fail genuinely slow connects sooner (tune the connect value). Retrying network errors adds latency on the API path for a truly-down host — keep the attempt count small (already 3) and only retry connection-establishment/read errors, not arbitrary exceptions.
- **Test:** Assert get_openfda_client().timeout.connect is materially smaller than .read. Separately, mock a route with side_effect=[httpx.ConnectError('x'), httpx.Response(200, json={'results':[]})] and assert get_with_retry returns the 200 (fails today — ConnectError propagates on the first attempt).
- **Note:** changes behavior (intended correction)

### #79 — Extractor silently truncates long PSGs at 18k chars with no observability and a wasted empty-passage call

- **Where:** `src/regwatch/process/extractor.py:51`
- **Class:** LOW severity / S effort / correctness · lane `ingest-process` · score 2.6 · confidence 0.85 · verdict CONFIRMED
- **Now:** Greedy truncation with a hard break and zero instrumentation: a systematically-truncated long PSG is invisible in logs, so the recall gap can't be triaged, and the empty-passages edge wastes an LLM round-trip and writes a misleading all-null BE row.
- **Fix:** When the loop truncates (dropped page count > 0), emit `log.warning('be_passages_truncated', included=..., total=len(pages), max_chars=...)`. If the assembled `passages` is empty, skip the provider call and return the all-null result directly (same outcome, no wasted paid call and a clear log).
- **Risk:** Minimal: adds a warning and an early-return whose extraction outcome (all-null) is identical to today's. Does not raise the char budget, so no token-cost increase on normal PSGs.
- **Test:** Call `extract_be` with a single page of >18_000 chars and a stub provider that fails the test if `complete` is invoked; assert result fields are all None and a 'be_passages_truncated' warning was logged. Fails today (provider IS called with empty passages, no log).

### #80 — pairs_without_alert inlines the latest-PSG-version query that _fetch_version_for_listing already encapsulates

- **Where:** `src/regwatch/watch/alerts.py:142`
- **Class:** LOW severity / S effort / duplication · lane `watch-retrieve` · score 2.6 · confidence 0.85 · verdict REVISED
- **Now:** The latest-version-for-document ordering is copy-pasted rather than extracted, so a future change to the tie-break rule has to be made in several places and can silently drift (pipeline._latest_version_text_path already omits the desc(id) tie-break, showing the drift is real).
- **Fix:** Extract one session-scoped helper `_latest_version(s: Session, psg_document_id: int) -> PsgVersion | None` that runs `select(PsgVersion).where(psg_document_id==...).order_by(desc(captured_at), desc(id)).limit(1)` and returns the ORM row (not just the id). Call it from BOTH sites inside their existing open session: pairs_without_alert uses `ver.id`; _fetch_version_for_listing uses `v.id / v.diff_summary / v.captured_at`. This keeps the ordering (incl. the desc(id) tie-break) in one place, preserves the per-appl_no cache and the single session_scope in pairs_without_alert (no extra round-trips), and is behavior-preserving. Optionally align ingest/pipeline.py's latest-version lookups to the same tie-break separately, but that is a distinct change (different return column, content_hash) and out of scope for the smallest safe fix.
- **Risk:** Must preserve the per-appl_no caching in pairs_without_alert (several products share one listing) and keep it inside the existing session_scope; a naive extraction that opens a new session per pair would add round-trips. Behavior is otherwise identical.
- **Test:** Seed a document with two PsgVersion rows that share captured_at but differ in id; assert pairs_without_alert and build_alerts both key off the same (highest-id) version -- i.e. an alert already written for that version makes the pair non-missed.

### #81 — assistantTurn and turnFromMessage compute `refused` with divergent predicates

- **Where:** `regwatch/frontend/lib/turns.ts:148`
- **Class:** LOW severity / S effort / correctness · lane `fe-lib` · score 2.6 · confidence 0.85 · verdict CONFIRMED
- **Now:** The two constructors encode the same INV-2 concept ('is this turn in the declined register?') with different rules. It is correct today only because the backend always pairs status='error' with refused=True (verified: grounded_qa _decline routes error declines through _refuse, which sets refused=True). That cross-layer guarantee is unstated in the frontend. If any future error path emitted refused=false, a live error turn would render dressed as an answer while its reloaded twin (turnFromMessage) renders declined - the drift the file's own comment warns about.
- **Fix:** Mirror turnFromMessage in assistantTurn: `refused: r.refused || r.status === 'refused' || r.status === 'error'`. Single rule for the declined register in both paths.
- **Risk:** Behavior-preserving under the current backend contract (error already implies refused). Belt-and-suspenders against a backend regression; no downside.
- **Test:** assistantTurn built from a QueryResponse with status='error' and refused=false should yield turn.refused===true (matches turnFromMessage for the same status).

### #82 — Bidirectional-containment-with-min-length-floor written twice (dailymed vs populator), self-documented as a mirror

- **Where:** `src/regwatch/sources/dailymed.py:394`
- **Class:** LOW severity / S effort / duplication · lane `x-duplication` · score 2.5 · confidence 0.83 · verdict CONFIRMED
- **Now:** Both the DailyMed listing selection and the populator's RLD-name verification independently needed 'is one name a >=4-char substring of the other,' and the shared primitive was never extracted, so the two floors (and the containment logic) are kept in sync only by a comment.
- **Fix:** Extract a `contains_with_floor(a: str, b: str, floor: int = 4) -> bool` (plus the single MIN_CONTAINMENT_CHARS constant) into common/text_normalize.py alongside names_match, and have both _normalized_contains and _name_matches call it. The per-module normalization (dailymed's punctuation-fold vs populator's whitespace-collapse) stays local; only the containment+floor core is shared.
- **Risk:** Low; keep each module's own normalization step so no matching behavior changes. Only the containment comparison and the '4' constant move to one home.
- **Test:** Unit-test contains_with_floor: ('proventil hfa','proventil')->True, ('hfa','proventil')->False (below floor), exact-equal->True; then a mutation lowering the floor to 3 should change results for both dailymed name selection and populator RLD verification in the same run.

### #83 — empty_completion refuse branch (distinct reason, pre-validation short-circuit) is untested

- **Where:** `src/regwatch/generate/grounded_qa.py:1477`
- **Class:** LOW severity / S effort / test-gap · lane `x-tests` · score 2.6 · confidence 0.85 · verdict CONFIRMED
- **Now:** No test returns an empty or whitespace-only LLMResponse.text through ask(); grep confirms no LLMResponse(text="") in tests/. The branch and its distinct reason are uncovered. (Note: the downstream `not answer_body` guard at line 1524 is a backstop, so a missing 1477 guard would not open an INV hole -- but it would silently relabel the reason and stop recording usage on the empty path.)
- **Fix:** Add a test seeding a corpus that reaches the synthesizer and stubbing the provider to return LLMResponse(text='   '); assert result.refused is True and result.reason=='empty_completion'.
- **Risk:** A refactor removing the explicit empty guard would reclassify empty completions as 'no_valid_citations' and skip the usage recording (response.usage) on that path; audit/reason analytics drift with no failing test.
- **Test:** tests/test_provider_failure.py: reuse _seed_corpus(); monkeypatch get_llm_provider to a stub returning LLMResponse(text='  ', model='stub'); r=qa_mod.ask('What study design is recommended?'); assert r.refused and r.reason=='empty_completion'.

### #84 — _stream_synthesis held-prefix flush (on_emit(buffer)) only tested when it equals a single delta

- **Where:** `src/regwatch/generate/grounded_qa.py:614`
- **Class:** LOW severity / M effort / test-gap · lane `x-tests` · score 2.5 · confidence 0.9 · verdict CONFIRMED
- **Now:** test_stream_synthesis_streams_a_real_answer uses chunks whose FIRST delta already diverges from the refusal, so buffer==chunk.delta at the flush and a regression from on_emit(buffer) to on_emit(chunk.delta) would be indistinguishable. No test accumulates MULTIPLE held deltas (a genuine multi-delta prefix of the sentinel) before diverging, so the 'flush the accumulated prefix' semantics are unverified.
- **Fix:** Add a unit test driving _stream_synthesis with a chunk sequence whose first two deltas together form a strict prefix of refusal_text and whose third delta diverges into a grounded answer; assert ''.join(emitted) equals the full accumulated text (held deltas flushed exactly once, no loss and no duplication).
- **Risk:** A regression emitting only chunk.delta (dropping the held prefix) at the divergence point would silently truncate the streamed 'typing' answer for any answer whose opening coincides with the refusal wording; the recorded answer stays correct so only the cosmetic stream regresses, uncaught.
- **Test:** refusal=get_settings().refusal_text; pick a divergence so buffer holds 2+ deltas of refusal[:n] then a delta that breaks the prefix into a cited answer; resp=qa_mod._stream_synthesis(prov, msgs, on_emit=emitted.append, refusal_text=refusal); assert ''.join(emitted)==resp.text and resp.text.count(refusal[:len(held)])==1.

### #85 — _pg_connect_args validates connect_timeout at engine construction but passes GUC duration strings through unvalidated, so a typo fails opaquely on every connection instead of loudly at boot

- **Where:** `src/regwatch/store/db.py:235`
- **Class:** LOW severity / S effort / error-handling · lane `store` · score 2.5 · confidence 0.82 · verdict REVISED
- **Now:** Inconsistent input validation at the config boundary (CLAUDE.md: validate inputs at boundaries): one timeout is validated at construction, the sibling durations are not, so the failure surfaces late and far from the cause.
- **Fix:** In _pg_connect_args, validate each stripped GUC duration before embedding it, mirroring the connect_timeout int() precedent: match against the full Postgres time-unit set with NO embedded whitespace, e.g. re.fullmatch(r"\d+(us|ms|s|min|h|d)?", value) on the already-stripped string; on mismatch raise RuntimeError naming the offending DB_* env setting. Because internal whitespace is disallowed, this also catches the '30 s'-style value that would otherwise corrupt the space-delimited options string. Apply the identical check to _migration_connect_args' lock_timeout (db.py:264-267), which the original finding missed. Keep the existing '0'/'' skip logic. Prefer implementing the check once as a small module-level helper (or a field_validator in config/settings.py) so both call sites share it. A test should set DB_STATEMENT_TIMEOUT='30sec' and assert get_engine() raises at construction naming the setting, while '30s'/'500ms'/'1h'/'0' still build.
- **Risk:** Valid configs are unaffected; only the failure timing for an invalid duration moves from first-connect to boot. Keep the accepted-unit set aligned with Postgres so a currently-valid value is not newly rejected.
- **Test:** Set DB_STATEMENT_TIMEOUT='30sec' (invalid unit) and assert get_engine() raises at construction with the setting named, rather than connecting and failing on first query. A valid '30s'/'500ms'/'0' must still build.

### #86 — Chroma-path similarity_search sets Hit.text = docs[i] without the None-guard the pgvector path uses

- **Where:** `src/regwatch/store/vector_store.py:192`
- **Class:** LOW severity / S effort / correctness · lane `store` · score 2.5 · confidence 0.82 · verdict CONFIRMED
- **Now:** The two backends were meant to be behaviorally identical (shared score convention noted in-file) but the None-normalization was applied to only one path.
- **Fix:** Change vector_store.py:192 to `text=docs[i] or ''` to match the pgvector twin.
- **Risk:** For non-None documents (the normal case) the output is identical; the change only maps a None document to '' instead of propagating None. Chroma is the SQLite/dev backend, so prod impact is nil, but it removes a latent type violation.
- **Test:** Monkeypatch the Chroma collection query to return a result whose documents[0] contains a None entry and assert the returned Hit.text == '' (str), not None. Fails today.

### #87 — Local embedder zip(strict=False) hides a wrong-count model result behind a misleading downstream dim error

- **Where:** `src/regwatch/process/embedder.py:90`
- **Class:** LOW severity / S effort / error-handling · lane `ingest-process` · score 2.5 · confidence 0.82 · verdict CONFIRMED
- **Now:** Inconsistent hardening: the OpenAI provider explicitly checks `len(data) != len(batch)` (embedder.py:196), but the local provider trusts the encode count and swallows a mismatch, surfacing it far from the cause with a confusing message.
- **Fix:** Use `zip(misses, vecs, strict=True)` (or an explicit `len(vecs) != len(misses)` guard raising a clear RuntimeError) so a wrong-count encode fails at the source with an accurate message. For a correctly-behaving model this is a no-op.
- **Risk:** None in normal operation (a correct SentenceTransformer.encode over a list always returns one row per input). Only changes the failure mode of an already-broken state, making it clearer and earlier.
- **Test:** Monkeypatch the model's `encode` to return an array with one fewer row than inputs; assert `embed(['a','b'])` raises at the provider (not later as a dim error). Fails today (returns `[[...],[]]`).

### #88 — resolve_brand parses openFDA JSON outside its try/except, raising an unaudited exception into ask()

- **Where:** `src/regwatch/retrieve/resolver.py:334`
- **Class:** LOW severity / S effort / error-handling · lane `x-types` · score 2.5 · confidence 0.82 · verdict CONFIRMED
- **Now:** The function's docstring promises 'any error / no match returns [] and the caller refuses', but only the FETCH is inside the try; the shape-assuming parse of external openFDA JSON is not. DailyMed's fetch_media does the same parse with isinstance guards (dailymed.py:236-246); that discipline was not applied here. Secondary: `openfda.get("generic_name") or []` iterating a string yields characters as bogus generics (no crash, wrong result).
- **Fix:** Either extend the try to cover the parse loop, or add isinstance guards mirroring dailymed: `openfda = row.get("openfda"); if not isinstance(openfda, dict): continue`, and `names = openfda.get("generic_name"); if not isinstance(names, list): continue`. Smallest safe fix is the isinstance guards (matches the two-functions-away pattern).
- **Risk:** None for well-formed openFDA responses (openfda is always an object). The change only converts a latent AttributeError into the documented empty-list degrade, honoring INV-6 (every query audited) on that path.
- **Test:** Monkeypatch fetch_openfda_results to return `[{"openfda": ["not-a-dict"]}]` and `[{"openfda": {"generic_name": "albuterol"}}]`; assert resolve_brand returns `[]` (or correct matches) and never raises. Today the first case raises AttributeError.

### #89 — OpenAI chat path indexes resp.choices[0] with no empty-choices guard, unlike its Responses twin

- **Where:** `src/regwatch/generate/llm.py:338`
- **Class:** LOW severity / S effort / error-handling · lane `x-types` · score 2.4 · confidence 0.8 · verdict CONFIRMED
- **Now:** The chat path trusts the external SDK response shape (non-empty choices) where the responses path validates it. An IndexError is a less-clean failure than the RuntimeError the responses path raises; both ultimately degrade to an audited refusal inside ask()'s broad except, but the extractor/change_detector callers see a raw IndexError instead of the intended RuntimeError.
- **Fix:** Before indexing, add `if not resp.choices: raise RuntimeError("openai chat response has no choices")`, mirroring the responses path's explicit raise so both modes fail the same way.
- **Risk:** None; empty choices is already a failure, this only names it. Chat mode is opt-in legacy (default is responses), so blast radius is small.
- **Test:** Inject a stub chat client whose create() returns an object with `choices=[]`; assert _complete_chat raises RuntimeError (not IndexError). Would fail today.
- **Sequencing:** Same llm.py chat completion function as 22; land together.

### #90 — Pre-stream 429/404 collapse into the silent 'retry without streaming' fallback and fire a duplicate request

- **Where:** `regwatch/frontend/lib/api.ts:651`
- **Class:** LOW severity / S effort / correctness · lane `fe-lib` · score 2.4 · confidence 0.8 · verdict REVISED
- **Now:** The `!res.ok` guard treats all non-401 failures as 'stream unavailable, degrade to plain /query', but 429/404 are terminal decisions, not stream transport failures. A rate-limited user is shown a misleading 'retrying without streaming' line and the client makes a SECOND request that hits the same shared limiter (defeating the point of rate limiting) before /query's handle() finally surfaces the 429; a foreign-session 404 likewise double-requests.
- **Fix:** Narrow the fix to 429 only. Immediately before the generic fallback at line 651, add: `if (res.status === 429) { return handle<never>(res, "POST", path, true); }` — this reuses handle()'s friendly rate-limit ApiError and its JSON-detail parsing, throws once, and avoids the duplicate POST /query against the shared limiter plus the misleading STREAM_FALLBACK_STATUS line. Do NOT special-case 404: the line-652 comment documents that a 404 legitimately means 'endpoint not deployed yet' and should keep degrading to POST /query; a 404 is ambiguous (ownership vs endpoint-missing) and forcing it to throw would regress the deploy-transition fallback. Leave 5xx / non-SSE-200 / network errors on the existing fallback path. Test: mock /query/stream -> 429 (JSON detail); assert askQueryStream rejects with ApiError status 429, fetch called exactly once, no STREAM_FALLBACK_STATUS emitted. Add a companion test that a 404 still falls back to /query (one extra fetch) to lock in the not-deployed behavior.
- **Risk:** Intentional behavior change for exactly 429/404: they now reject immediately with ApiError instead of double-requesting. The stream error body is JSON (real HTTP error), so handle() parses it fine. Verify no caller relied on the fallback masking a 404 into a fresh-session /query success.
- **Test:** Add an sse.test.ts case: mock /query/stream -> Response 429 (JSON detail); assert askQueryStream rejects with ApiError status 429 and fetch was called exactly once (no fallback, no STREAM_FALLBACK_STATUS emitted).
- **Note:** changes behavior (intended correction)

### #91 — openFDA api_key param injection re-inlined in three call sites that bypass the openfda_params helper

- **Where:** `src/regwatch/assemble/dossier.py:196`
- **Class:** LOW severity / S effort / duplication · lane `x-duplication` · score 2.4 · confidence 0.8 · verdict REVISED
- **Now:** openfda_params bundles search+limit+api_key together and can't be reused when a caller needs pagination params, so the api_key half of it gets duplicated instead of being factored out on its own.
- **Fix:** Reuse the EXISTING openfda_params helper instead of adding a new one. (1) dossier._fetch_rld_label: replace lines 196-198 with `params = openfda_params(query, 1)` (identical output, drops the inlined conditional and the local dict literal). (2) watch/aliases.py: replace the params literal + api_key block (91-97) with `params = openfda_params(f\"sponsor_name:{root_upper}*\", page_limit); params[\"skip\"] = page * page_limit`. (3) watch/watchlist.py: replace lines 123-128 with `params = openfda_params(_drugsfda_query(alias), page_limit)` and keep the existing per-page `params[\"skip\"] = page * page_limit` assignment in the inner loop. This removes all three inlined copies with zero new abstraction; the api_key policy already lives solely in openfda_params. Import openfda_params where not already imported. Behavior is byte-identical (same keys, same values). Test per the original suggestion but assert against the reused helper: with a recording fake client, each of _fetch_rld_label / discover_applicant_aliases / fetch_drugsfda_for_company emits api_key when settings.openfda_api_key is set and omits it when unset.
- **Risk:** None — pure extraction of an existing conditional; each caller keeps building its other params exactly as today.
- **Test:** Unit-test apply_openfda_api_key with the setting set vs unset (injects vs omits 'api_key'); assert dossier._fetch_rld_label, aliases.discover_applicant_aliases, and watchlist.fetch_drugsfda_for_company include api_key in their outgoing params when configured (via a recording fake client).

### #92 — K1 backend switch is computed two different ways (settings-only vs settings-or-os.environ), so the structured store and vector store can silently split

- **Where:** `src/regwatch/store/vector_store.py:44`
- **Class:** LOW severity / S effort / correctness · lane `store` · score 2.3 · confidence 0.78 · verdict CONFIRMED
- **Now:** Two independent definitions of the same K1 switch read from two sources that can disagree. The os.environ fallback was added for integration-order robustness, but it makes the vector backend decision diverge from the structured-store decision.
- **Fix:** Make _database_url() read settings only (single source of truth), or route both db.py and vector_store through one shared helper, so the switch is computed once.
- **Risk:** Removing the os.environ fallback changes behavior only in the (already-broken) split state; a test that sets DATABASE_URL after the settings cache is populated without clearing it could start returning None. In normal deploys env is set before process start, so both sources agree and there is no observable change.
- **Test:** Monkeypatch settings so database_url is None but set os.environ['DATABASE_URL'] to a postgres URL, then assert _pg_mode() and db.get_engine().dialect.name are consistent (both SQLite or both Postgres). Fails today.
- **Note:** changes behavior (intended correction)

### #93 — REMS evidence construction is duplicated verbatim between _ext_rems and _ext_restricted_distribution

- **Where:** `src/regwatch/whitepaper/populator.py:1257`
- **Class:** LOW severity / S effort / duplication · lane `wp-populator` · score 2.4 · confidence 0.8 · verdict CONFIRMED
- **Now:** No shared _rems_evidence(records, ctx) helper; the two REMS-backed cells each inline the list comprehension.
- **Fix:** Extract `_rems_evidence(records: list[SourceRecord], ctx) -> list[dict]` and call it from both sites (passing confirmed-or-records vs records respectively). Fold in the fetched_at fix from the shortage/REMS I/O finding so provenance is corrected once.
- **Risk:** Pure refactor; identical output. Keep the two different record inputs (confirmed-or-records vs all records) at the call sites.
- **Test:** Assert _ext_rems (confirmed match) and _ext_restricted_distribution over the same seeded REMS record produce the same evidence source/locator/source_url; keep green across the extraction.

### #94 — Duplicated whitepaper run audit-write shape between _log_run_workflow and the inline docx block

- **Where:** `src/regwatch/api/main.py:1517`
- **Class:** LOW severity / S effort / duplication · lane `api` · score 2.4 · confidence 0.7 · verdict REVISED
- **Now:** The generic whitepaper-workflow audit row was factored into _log_run_workflow, but the docx route was written with its own inline copy rather than reusing/extending that helper, so the two audit shapes can drift (a route_json key added to one but not the other).
- **Fix:** Add one helper, e.g. _log_whitepaper_run_audit(user, run_id, *, appl_no, status, reason, answer_text, model_name, extra_route=None), that builds the common log_query skeleton (mode='whitepaper', retrieved=[], citations=[], refused=False, route_json={'route':'whitepaper','reason':reason,'run_id':run_id,'application_number':appl_no, **(extra_route or {})}). Crucially, pass appl_no IN rather than looking it up inside the helper: finalize/reopen call it with appl_no=_run_application_number(run_id) and model_name='(workflow)'; the docx route calls it with appl_no=detail.application_number (already loaded -- no redundant query), model_name='(docx-render)', reason='docx_render', extra_route={'source_audit_id': detail.source_audit_id}. This keeps both route_json shapes in lockstep on the required keys while preserving each path's distinct markers and avoiding an extra DB round-trip on docx. Severity is genuinely low -- most fields differ, so the shared skeleton is small; the payoff is drift prevention, not line savings.
- **Risk:** Preserves emitted rows if the extra_route hook carries docx's source_audit_id and the answer_text/model_name markers ('(workflow)' vs '(docx-render)') are passed through. Low risk, pure consolidation.
- **Test:** Finalize a run and assert exactly one audit row with route_json.reason=='finalized'; render its docx and assert that row carries the same required route_json keys (route/run_id/application_number) plus source_audit_id -- a shared helper keeps both in lockstep and the test fails if one path drops a key.

### #95 — assemble maps build_dossier output via AssembleResponse(**dossier) instead of explicit field mapping

- **Where:** `src/regwatch/api/main.py:1106`
- **Class:** LOW severity / S effort / type-safety · lane `api` · score 2.4 · confidence 0.72 · verdict CONFIRMED
- **Now:** An implicit dict-shape contract between the domain function and the wire model that isn't enforced anywhere, inconsistent with the explicit-mapping style used for the other, larger responses in the same file.
- **Fix:** Map explicitly: `AssembleResponse(markdown=dossier['markdown'], sections=dossier['sections'], refused=dossier['refused'])`. Makes the domain->wire contract visible and fails loudly at the mapping site if a key is renamed.
- **Risk:** None functionally today (build_dossier returns exactly these three keys on every path, verified at dossier.py:321 and 509). Purely defensive against future drift.
- **Test:** Feed the handler a build_dossier stub returning a dict missing 'refused' (or with a renamed key) and assert a clear failure at the mapping boundary rather than a silently-dropped field in the 200 body.

### #96 — _meta_answer_text mixes INV-1 answer assembly with four un-injected I/O reads

- **Where:** `src/regwatch/generate/grounded_qa.py:901`
- **Class:** LOW severity / S effort / domain-io-separation · lane `x-domain-io` · score 2.4 · confidence 0.8 · verdict CONFIRMED
- **Now:** The prose-assembly domain logic (pluralization, 5-item sampling, and the INV-1 'system facts only' constraint) has no seam separating it from the four module-level catalog/watch reads, so the compliance-relevant formatting can only be tested by monkeypatching four I/O functions.
- **Fix:** Split into a pure `_render_meta(corpus_names, corpus_doc_count, watch_names, change_records, is_change)` and a thin `_meta_answer_text` that does the four reads and calls it. The renderer becomes directly unit-testable for the INV-1 'no diff_summary leaks' guard.
- **Risk:** Low; pure text refactor. Keep the change-request branch gated on is_change so a non-change meta question still omits the digest line.
- **Test:** DB-free: _render_meta(['x'], 1, [], [{'active_ingredient':'Y','captured_at':'2026-01-01T..','diff_summary':'SECRET'}], is_change=True) - assert 'SECRET' not in output and dates are date-only. Fails today because assembly requires stubbing four I/O calls.
- **Sequencing:** Meta-answer seam extraction; do after 18 guards its reads.

### #97 — dossier and populator INV-5 form guards claim to be kept 'in lockstep' but are structurally different algorithms

- **Where:** `src/regwatch/assemble/dossier.py:51`
- **Class:** LOW severity / S effort / doc-drift · lane `x-domain-io` · score 2.3 · confidence 0.75 · verdict CONFIRMED
- **Now:** Two independent implementations of the same compliance-critical 'don't blend dosage forms' rule, coupled only by a comment asserting a lockstep that cannot hold: they already disagree on lenient wording (dossier matches 'inhalation aerosol' to 'Aerosol, Metered'; populator's exact-equality rejects it). A future edit to one silently diverges from the other with no test enforcing agreement.
- **Fix:** Minimal/behavior-preserving: correct the comment to state the two guards intentionally differ (lenient-with-modifier-guard vs exact-equality) and why. Stronger (flag as behavior-changing): promote a single shared form-compatibility predicate to common/text_normalize alongside names_match and have both call it, choosing one semantics deliberately.
- **Risk:** Comment-only fix is zero-risk. Consolidating onto one predicate WOULD change matching behavior in one of the two flows (dossier's lenient path vs populator's strict path) and must be gated behind the INV-5 form tests in both suites.
- **Test:** Add a cross-check test feeding identical (form_a, form_b) pairs to both guards; it currently proves they diverge (e.g. 'inhalation aerosol' vs 'Aerosol, Metered' -> dossier True, populator False), documenting that 'lockstep' is false today.
- **Sequencing:** Resolved by aligning dossier/populator guards (54/55/1); add a cross-check test.

### #98 — REGWATCH_INIT_DB is set by the orchestration runner but read by nothing -- a dead control

- **Where:** `src/regwatch/orchestration/definitions.py:48`
- **Class:** LOW severity / S effort / dead-code · lane `eval-misc` · score 2.7 · confidence 0.9 · verdict REVISED
- **Now:** Either a planned "skip init in child" optimization was never wired into init_db(), or a formerly-honored flag was removed and this setter left behind.
- **Fix:** Remove all four dead REGWATCH_INIT_DB setters, not just one, since nothing in the tree reads the var: delete env.setdefault("REGWATCH_INIT_DB", "false") at definitions.py:48 AND the three `REGWATCH_INIT_DB: "false"` lines in compose.yaml (128, 163, 192). All removals are behavior-preserving no-ops. Correct the finding's false 'only occurrence in the tree' claim. Do NOT wire init_db() to honor the flag (that would let the child skip schema convergence, a behavior change with no present need given deploy is the sole migration authority).
- **Risk:** Removing a no-op cannot change runtime behavior. Wiring it (the alternative) would let the child skip schema convergence -- acceptable only because deploy is the sole migration authority; not needed.
- **Test:** A test asserting init_db()'s behavior is independent of REGWATCH_INIT_DB (set it "false" and confirm tables still created) documents that the flag is inert, preventing someone from relying on it.

### #99 — Password policy comment contradicts the code -- the canonical long all-lowercase passphrase is rejected

- **Where:** `src/regwatch/auth/passwords.py:22`
- **Class:** LOW severity / S effort / doc-drift · lane `eval-misc` · score 2.7 · confidence 0.9 · verdict REVISED
- **Now:** The stated NIST-length-first intent and the implemented 2-class composition floor diverged; the comment was not updated (or the class check was added contrary to the documented policy).
- **Fix:** Doc-only fix (preserves behavior). Rewrite the last clause of the comment at lines 19-22 to match the code, e.g.: \"12 is the NIST-aligned floor for human-chosen secrets; the class check additionally requires at least two of {lower, upper, digit, symbol}, so even a long all-lowercase passphrase is rejected.\" Do NOT relax the line-113 class floor -- that would be a deliberate security-policy change, not a doc fix. Optionally pin behavior with a test asserting validate_password_strength(\"correcthorsebatterystaple\") returns the 2-class rejection string.
- **Risk:** The doc-only fix preserves behavior. The policy-change alternative weakens composition requirements and must be approved deliberately; do not change it unilaterally.
- **Test:** Assert validate_password_strength("correcthorsebatterystaple") behavior and pin it: it returns the 2-class rejection string today. A test that pins this makes the intended policy explicit and catches accidental drift either way.

### #100 — create-user prompts (and does an HIBP network call) before the duplicate check; a concurrent duplicate surfaces as a raw IntegrityError traceback

- **Where:** `src/regwatch/cli.py:99`
- **Class:** LOW severity / S effort / error-handling · lane `eval-misc` · score 2.2 · confidence 0.72 · verdict CONFIRMED
- **Now:** Ordering (prompt before existence check) plus reliance on an advisory pre-check instead of handling the authoritative UNIQUE-constraint failure; ORM exceptions from the race aren't translated to the CLI's exit-code convention.
- **Fix:** Check existence first (before prompting), and wrap the insert/commit to catch IntegrityError and exit 2 with "user already exists". For set-password/deactivate-user, catch NoResultFound from the second .one() and exit 2 with a clear message. This shrinks the wasted-prompt window and turns races into clean, conventional exits.
- **Risk:** Minimal; admin-only path so the race is unlikely, but the traceback-vs-exit-2 inconsistency is real. Reordering the existence check is behavior-preserving for the success path.
- **Test:** Monkeypatch so a user with the same email is inserted between the pre-check and the commit (or pre-create it and stub the prompt), then invoke create-user and assert exit code 2 rather than an unhandled IntegrityError.

### #101 — /metrics aggregates an unbounded query_log with a full scan and no supporting index

- **Where:** `src/regwatch/api/main.py:380`
- **Class:** LOW severity / S effort / performance · lane `x-perf` · score 2.2 · confidence 0.72 · verdict REVISED
- **Now:** An unbounded audit table aggregated with no covering index and no time window; as the table grows the per-scrape cost grows linearly.
- **Fix:** Add a low-cardinality composite index on query_log(mode, refused) to keep lifetime-total counter semantics (do NOT switch to a rolling window). Because query_log is the hottest write path, the index MUST be built without blocking INSERTs: create it CONCURRENTLY in a non-transactional alembic migration (op.create_index('ix_query_log_mode_refused','query_log',['mode','refused'], postgresql_concurrently=True) inside op.get_context().autocommit_block(), with a reversible drop_index), and also declare it in QueryLog.__table_args__ so create_all/autogenerate agree with the migration (the pattern already used for ix_chat_session_user_id_updated_at at models.py:189). Note honestly that on an unfiltered full-table GROUP BY the planner may still seq-scan; the benefit is scanning a narrow 2-column index rather than the wide heap and is marginal at current table sizes, consistent with the low severity. Given the existing graceful-degradation guard (try/except -> {}) and low scrape frequency, this is optional hardening, not urgent.
- **Risk:** The index adds maintenance to query_log INSERT, which is on the hottest write path (every request is audited) — though mode/refused are very low cardinality so the index is small and cheap. Bounding to a window instead would change the exposed counter semantics (no longer lifetime totals), which Prometheus rate() tolerates but is a behavior change.
- **Test:** EXPLAIN the counters query on Postgres before/after and assert an index(-only) scan replaces the seq scan; functionally assert the emitted counters are unchanged for a fixed query_log fixture.

## Marginal (19)

### #102 — build_dossier loads every PsgDocument into memory and issues per-doc N+1 sessions

- **Where:** `src/regwatch/assemble/dossier.py:119`
- **Class:** LOW severity / M effort / performance · lane `eval-misc` · score 1.4 · confidence 0.8 · verdict REVISED _(merges [113])_
- **Now:** names_match does salt-stripping fuzzy comparison that isn't expressed in SQL, so the code loads everything and filters client-side; helper functions each own a session rather than sharing the caller's.
- **Fix:** Drop the SQL LIKE prefilter (it can silently drop a valid salt-stripped match because that branch keys off stripped_name(active_ingredient), a runtime-computed value, not normalized_name; a dropped match is a false refusal on the INV-1 name-matching surface -- worse than a slow scan). Keep only the safe, behavior-identical half: eliminate the 1+2N sessions. After _find_matching_psgs returns, collect the matched integer doc_ids and, in a SINGLE session_scope, batch-fetch BE requirements and latest PsgVersion for all of them with WHERE psg_document_id IN (:ids) queries, then index the results by doc_id in Python (mirroring the existing desc(version_id)/desc(captured_at) ordering to pick the latest per doc). That turns 1 + 2N round-trips into a bounded 3. Leave the full PsgDocument scan as-is: at ~1,795 rows and low assemble QPS it is acceptable, and safely narrowing it would require a stored+indexed stripped_name column (a schema change out of scope for a low-sev efficiency refactor). Test: wrap the session in a statement counter and assert build_dossier for a seeded multi-PSG ingredient issues a bounded, small number of statements, plus assert matched PSGs and BE/version content are byte-identical to today for the fixture.
- **Risk:** The LIKE prefilter must not drop a name that names_match would accept (keep it a superset of the fuzzy match -- anchor on the shared prefix, not the full canonical). Assemble is low-QPS, so severity is low; the change is purely an efficiency/consolidation refactor with identical results.
- **Test:** Wrap the session in a query counter (or event listener) and assert build_dossier for a single-PSG ingredient issues a bounded, small number of statements; also assert matched PSGs are unchanged vs today for a seeded fixture.
- **Sequencing:** build_dossier N+1 covers both the PsgDocument load and the per-doc BE/version-summary lookups (113).

### #103 — Watch build_alerts opens one session per match and re-queries the same appl_no's latest version repeatedly

- **Where:** `src/regwatch/watch/alerts.py:165`
- **Class:** LOW severity / M effort / performance · lane `x-perf` · score 1.4 · confidence 0.85 · verdict CONFIRMED
- **Now:** Per-match session + no dedupe/caching, and no reuse across pairs_without_alert -> build_alerts. pairs_without_alert itself (alerts.py:130-145) demonstrates the correct per-appl_no cache inside a single session; build_alerts does not adopt it.
- **Fix:** Dedupe appl_nos and resolve their latest (doc_id, version_id, diff_summary, captured_at) once inside a single session (cache per appl_no like pairs_without_alert), or a single join; optionally thread the map through from run_watch to avoid re-querying what pairs_without_alert already fetched.
- **Risk:** Cron-only path (not request-facing); low. Must keep the same latest-version tie-break (captured_at, id DESC).
- **Test:** build_alerts with several matches sharing appl_nos; patch session_scope/query execution to count and assert it is constant rather than 2*len(matches), with identical Alert output.

### #104 — Citation cleanup parses the answer three times and re-implements the case-fold that _validate_citations already did

- **Where:** `src/regwatch/generate/grounded_qa.py:1542`
- **Class:** LOW severity / M effort / complexity · lane `grounded-qa` · score 1.5 · confidence 0.88 · verdict CONFIRMED
- **Now:** `_validate_citations` discards the as-emitted (short_name, page) keys it already computed and only returns the deduped Citation objects, forcing ask() to reconstruct the exact-match key set for `filter_citations` from scratch.
- **Fix:** Have `_validate_citations` return a third value: the set of as-emitted `(short_name, page)` keys whose fold validated (add `(short_name, page)` to it whenever `passage is not None`, including on a duplicate fold). ask() then passes that set straight to `filter_citations`, deleting the folded_valid/valid_keys reconstruction and its comment. It has exactly one production caller (1516) plus one test.
- **Risk:** Behavior-preserving: `cleaned_answer` is identical. Internal signature change; update the one caller and the `_validate_citations` unit test (test_grounded_qa_citations.py:144) which currently unpacks a 2-tuple.
- **Test:** Regression guard: force a provider answer that cites the same valid passage in two casings ("[PSG_020503, p.3]" and "[psg_020503, p.3]") and assert BOTH markers survive in `result.answer`. Locks the mixed-case behavior the refactor must preserve.
- **Sequencing:** Citation-cleanup refactor in ask(); touches the same tail as 17.

### #105 — Entire Drugs@FDA watchlist-import path is unreachable dead code (~150 lines)

- **Where:** `src/regwatch/watch/watchlist.py:89`
- **Class:** LOW severity / M effort / dead-code · lane `x-dead-doc` · score 1.4 · confidence 0.82 · verdict REVISED
- **Now:** The automated drugsfda->watchlist import was built (with retry + pagination + status-mapping) but never wired to any entry point (no CLI command, no API route, no cron). `regwatch aliases` discovers and prints/caches applicant aliases but nothing ever turns them into Products.
- **Fix:** Do NOT delete. The finding correctly identifies fetch_drugsfda_for_company (+ _fetch_page, _drugsfda_query, _status_from_marketing_status, and aliases.get_aliases) as currently unreachable, but docs/typescript-ui-replaces-streamlit-golden-pudding.md:302-304 names this exact function as the reuse foundation for the planned multi-source rebuild, so it is latent code with documented intent, not orphaned leftover. Smallest safe change = either (a) wire a thin entrypoint (e.g. a `regwatch import-watchlist` CLI command or POST route that calls fetch_drugsfda_for_company -> upsert_entries, mirroring cmd_aliases/cmd_seed) to turn the latent path live, or (b) explicitly park it as-is with a one-line comment noting it is the reuse target per the multi-source design doc. Keep _status_from_marketing_status and its scalar-coercion regression tests either way. Deletion is only correct AFTER the product owner confirms drugsfda auto-import is permanently abandoned; until then it does not preserve documented future behavior.
- **Risk:** If a future feature intends to resume drugsfda auto-population, deletion cements the manual-only reality -- confirm intent with the product owner first. parse-side symbols like WatchlistEntry/upsert_entries/add_manual_product stay (live via POST /products). Low blast radius: nothing imports the dead names.
- **Test:** After removal, tests/test_watchlist.py's live cases (add_manual_product roundtrip, INV-5 source rejection, trust-rank upsert) still pass and prove the real watchlist path is intact; add an assert that `from regwatch.watch.watchlist import fetch_drugsfda_for_company` raises ImportError so the dead entrypoint cannot silently return.
- **Sequencing:** Dead drugsfda import path; pairs with doc-drift 120.

### #106 — Unchanged-content backfill re-parses the PDF in a subprocess even though the version's parsed text is already persisted

- **Where:** `src/regwatch/ingest/pipeline.py:382`
- **Class:** LOW severity / M effort / performance · lane `ingest-process` · score 1.3 · confidence 0.8 · verdict CONFIRMED
- **Now:** The backfill re-derives data it already has. `parsed.text` is exactly `_PAGE_SEP.join(pages)` ('\n\f\n'.join), so pages are recoverable by `persisted_text.split(_PAGE_SEP)` without touching the PDF or the subprocess. If extraction persistently fails (row never lands), this re-parse recurs on every daily run for that PSG, on top of the LLM re-pay the code comment already notes.
- **Fix:** On the backfill branch, prefer the persisted text: read `_latest_version_text_path(doc_id)`, and if present, reconstruct `pages = text.split(_PAGE_SEP)` and a `ParsedPdf` from it instead of calling `parse_pdf`. Fall back to `parse_pdf(pdf_bytes)` only when no persisted text exists (pre-feature versions).
- **Risk:** Reconstruction assumes no page's normalized text contains a literal form-feed separator ('\n\f\n'); pdfplumber/pypdf page text effectively never does, but guard by falling back to a real parse if the split count != stored page count is detectable. Behavior change: uses stored (normalized) text rather than a fresh parse -- byte-identical to what was originally indexed.
- **Test:** Force the 'unchanged + need_chunks' path with `parse_pdf` monkeypatched to raise; assert the backfill still regenerates chunks from the persisted `parsed_text_path`. Fails today (backfill calls parse_pdf and errors).
- **Note:** changes behavior (intended correction)

### #107 — whitepaper_run_docx re-implements the INV-3 fingerprint integrity check inline instead of delegating to run_store like finalize does

- **Where:** `src/regwatch/api/main.py:1495`
- **Class:** LOW severity / M effort / domain-io-separation · lane `api` · score 1.3 · confidence 0.8 · verdict REVISED
- **Now:** INV-3 enforcement was split across layers as routes were added: the store became the authority for finalize/reopen but the docx route kept an inline copy of the fingerprint math, so a future change to how the fingerprint is computed or where sections_sha256 lives must be made in two places or the two routes silently diverge.
- **Fix:** Add a verification-only store helper next to get_run, e.g. get_run_verified(run_id) -> RunDetail | None, that internally calls the same read path and raises run_store.IntegrityMismatchError when result_fingerprint(detail.sections) != detail.sections_sha256 (reusing the exact check finalize_run uses). Do NOT move docx rendering into the store -- write_whitepaper_docx/template_fetch stay at the boundary. In whitepaper_run_docx, replace the None-check + inline fingerprint compare with: call get_run_verified inside try; map None -> 404 and IntegrityMismatchError -> _stored_corruption_500(run_id, 'whitepaper_run_integrity_mismatch'), exactly as finalize does at main.py:1457-1458. Drop the result_fingerprint import from main.py. Keep get_run non-verifying so the read-only detail GET (main.py:1385) is unaffected. Test: corrupt a stored run's sections so the fingerprint no longer matches sections_sha256; assert POST /whitepaper/runs/{id}/docx returns 500 with the stored-corruption detail and writes NO docx_rendered audit row (check the check still runs before log_query).
- **Risk:** Preserves the observable 500 + no-docx outcome; only the layer that raises moves. Ensure the store still returns sections verbatim (INV-3) so serialization never reshapes the fingerprinted payload.
- **Test:** Corrupt a stored run's sections so the fingerprint no longer matches sections_sha256; assert POST /whitepaper/runs/{id}/docx returns 500 with the stored-corruption detail and writes NO docx_rendered audit row -- the same path finalize takes for IntegrityMismatch, proving both routes agree on one enforcement point.

### #108 — Extractors perform network I/O (Drug Shortages fetch; REMS lazy fetch), contradicting the _Ctx 'fetched once, extractors read from it' contract

- **Where:** `src/regwatch/whitepaper/populator.py:1158`
- **Class:** LOW severity / M effort / domain-io-separation · lane `wp-populator` · score 1.3 · confidence 0.8 · verdict REVISED
- **Now:** Shortage/REMS were bolted on as in-cell fetches rather than following the established _fetch_X(ctx)->ctx.X_records pattern used by every other source (e.g., _fetch_ndc / ctx.ndc_records, 625-633/200). Laziness buys nothing because every cell is always built, so the REMS index and shortage query always run anyway.
- **Fix:** Two independent, behavior-preserving fixes. (1) SHORTAGE: add _fetch_shortages(ctx) to the _build_context pipeline storing ctx.shortage_records (list on success, None on failure, mirroring ctx.ndc_records) plus ctx.shortage_fetched_at set at the actual fetch; make _ext_shortage read ctx.shortage_records and use ctx.shortage_fetched_at, preserving the exact tri-state mapping (None -> analyst 'query failed'; [] -> verified_absent; rows -> populated incl. the all-Resolved-history guard). This half is safe because shortage is unconditionally fetched today. (2) REMS: do NOT unconditionally move the fetch into _build_context — that adds a new network call in the appl-no-only path where both REMS extractors currently short-circuit via `if not ctx.ingredient and not brand`. Instead, either (a) keep the existing lazy memoized _rems_index_results and simply record ctx.rems_fetched_at at the moment the fetch actually runs (line 1226) so evidence stamps the true fetch time instead of ctx.now — smallest fix that cures the overstated-freshness defect while preserving the guard; or (b) if eager fetch is desired, replicate the ingredient/brand guard in _build_context (brand is derivable from ctx.drugsfda_records, already populated) so the appl-no-only path still performs zero REMS I/O. Preserve REMS parse-sanity (total_rows==0 branch) verbatim.
- **Risk:** Behavior-preserving if the tri-state mapping is kept verbatim (None->analyst 'query failed', []->verified_absent, rows->populated) and REMS's parse-sanity (total_rows==0) branch is preserved. Slightly changes timing of when the fetch happens within one request (harmless). fetched_at becomes more accurate (a fix).
- **Test:** Add a test that constructs a _Ctx with ctx.shortage_records preset and calls _ext_shortage(spec, ctx) WITHOUT monkeypatching _shortage_records; assert no network call occurs (e.g., set _shortage_records to raise) and the cell reflects ctx. Fails today because the extractor calls the network wrapper.

### #109 — Two divergent implementations of 'ensure pgvector extension' across db.py and pgvector_store.py

- **Where:** `src/regwatch/store/pgvector_store.py:191`
- **Class:** LOW severity / M effort / duplication · lane `store` · score 1.3 · confidence 0.8 · verdict CONFIRMED
- **Now:** The same idempotent DDL concern is implemented twice with no shared helper (the chunk table/index DDL dedup was already flagged; this extension-ensure dedup is separate). Divergent probes mean a future fix to one path can silently miss the other.
- **Fix:** Extract one ensure-extension helper (prefer db.py's explicit namespace-probe form, which avoids relying on catching ProgrammingError for control flow) and call it from both the db bootstrap and the pgvector store.
- **Risk:** Behavior is equivalent today, so consolidation is low-risk; verify the chosen form still works on both Supabase (extensions schema present) and a vanilla local Postgres (falls back to public). Keep it a pure move with no logic change.
- **Test:** Against a Postgres without an 'extensions' schema, call the consolidated helper and assert the `vector` type resolves (a subsequent CREATE TABLE with a vector column succeeds); against one with the schema, assert it lands in extensions. A single helper exercised by both bootstrap entry points guards the convergence.

### #110 — SPL upsert does not handle a concurrent same-setid insert, spuriously rolling back the whole provenance snapshot

- **Where:** `src/regwatch/store/whitepaper_sources.py:364`
- **Class:** LOW severity / M effort / error-handling · lane `wp-rest` · score 1.3 · confidence 0.8 · verdict CONFIRMED
- **Now:** The upsert is read-then-insert with no conflict handling, and it shares the snapshot's transaction, so a unique-constraint race poisons an otherwise-good all-or-nothing write.
- **Fix:** Make the insert conflict-tolerant. Cleanest within the shared transaction is a Postgres ON CONFLICT (setid) DO UPDATE (pg_insert(...).on_conflict_do_update) so the winner's row stands and no exception fires; a SAVEPOINT (s.begin_nested()) around the insert with a re-select on IntegrityError is the dialect-neutral alternative. Note the naive 'catch IntegrityError then re-select in the same session' does NOT work -- the transaction is already aborted -- which is why a savepoint or ON CONFLICT is required.
- **Risk:** Impact today is benign (winner persisted equivalent data; no corruption), so this is noise-reduction, not a data-loss fix; weigh against added complexity. ON CONFLICT couples to Postgres (already the prod/CI dialect); the savepoint path stays portable. Either preserves the observable upsert result.
- **Test:** Simulate the race: insert an SplDocument with a setid, then call persist_whitepaper_snapshot(ob=<valid snapshot>, spl=SplSnapshot(setid=<same>, ...)) from a second session that pre-read absence; assert the OB rows persist and no IntegrityError escapes (today the whole snapshot rolls back).

### #111 — Citation-label rule (_short_name) is duplicated across the store-IO layer and the retrieve layer, and the two copies have already drifted

- **Where:** `src/regwatch/store/pgvector_store.py:355`
- **Class:** LOW severity / M effort / duplication · lane `store` · score 1.3 · confidence 0.78 · verdict CONFIRMED
- **Now:** Compliance-relevant domain logic (how a citation is labeled, INV-8-adjacent) lives in two places on opposite sides of the IO boundary with no shared source, so they drift independently. Today the retriever silently discards the label the store computed and stored, and a future edit to one copy won't reach the other.
- **Fix:** Extract one shared helper (e.g. keep retriever._short_name as the single owner, or move it to a small common citation module) and import it from pgvector_store's add_chunks path. Pick one rule for the explicit-short_name case and delete the other copy.
- **Risk:** Consolidation changes behavior for the explicit-short_name branch: unifying on the retriever's rule stops honoring a pre-supplied short_name at ingest; unifying on the store's rule makes the retriever honor stored labels. Choose deliberately and state which. Import direction: store importing retrieve, or both importing a new common module, to avoid a cycle.
- **Test:** Feed a meta dict carrying an explicit short_name (e.g. {'short_name':'CUSTOM','appl_no':'020503'}) to both helpers and assert equal output. It fails today (store returns 'CUSTOM', retriever returns 'PSG_020503') and passes once both call the shared helper.
- **Note:** changes behavior (intended correction)

### #112 — _matching_psg_docs loads the entire PsgDocument table into ORM objects for any salt-form ingredient (the common case)

- **Where:** `src/regwatch/whitepaper/populator.py:701`
- **Class:** LOW severity / M effort / performance · lane `wp-populator` · score 1.3 · confidence 0.75 · verdict REVISED
- **Now:** The salt-stripped secondary match key is computed in Python per row and has no stored/indexed counterpart, so the code cannot push it to the DB and falls back to loading everything -- even to satisfy the indexable normalized_name==canon / appl_no disjuncts.
- **Fix:** Drop the 'push indexable predicates' framing for the strip branch: it cannot reduce the row set because the salt-stripped disjunct must evaluate stripped_name(active_ingredient) on every row (no stored column), so all ~1,795 rows load regardless. The only behavior-preserving interim is column projection -- select just the 11 read fields (id, appl_no, source_url, psg_type, recommended_date, last_seen_at, dosage_form, route, normalized_name, active_ingredient, rld_or_rs_number) instead of full entities to cut ORM materialization overhead. However, given the impact is negligible (bounded ~1,795 lightweight rows, dominated by per-build FDA fetches) and projection adds Row-tuple loop churn, the smallest-correct call is to EITHER leave line 701 as-is OR do the durable fix directly: a separate, reversible, backward-compatible migration adding a nullable stored+indexed stripped_name column populated at ingest, turning the whole match into an indexed OR. Do not bundle the migration here; flag it. Add a matcher test asserting a salt-form ingredient still matches only the intended PSG doc so any projection stays correctness-preserving.
- **Risk:** Column-projection is behavior-preserving (same match logic). The stored-column path needs a backfill and an ingest write, and must stay backward compatible (nullable, computed on write) -- larger, so gate it. Note the practical impact is bounded (~1,795 rows) and small next to the per-build FDA fetches, so treat as a hot-path tidy, not an emergency.
- **Test:** Add a matcher test asserting a salt-form ingredient still matches only the intended PSG doc (correctness must survive the projection); pair with a seeded-corpus timing/row-count assertion if a perf harness exists. The correctness test fails if the projection drops a field the match needs.

### #113 — Login-time session sweep does a row-by-row delete over an unindexed expires_at column

- **Where:** `src/regwatch/auth/sessions.py:80`
- **Class:** LOW severity / M effort / performance · lane `eval-misc` · score 1.2 · confidence 0.72 · verdict CONFIRMED
- **Now:** The opportunistic-sweep design (acknowledged as pilot-scale) uses an ORM iterate-and-delete plus an unindexed predicate; both cost grow with accumulated expired rows.
- **Fix:** Replace the loop with a single bulk statement: s.execute(delete(AuthSession).where(AuthSession.expires_at < now)). Add an index on expires_at via a backward-compatible Alembic migration (CREATE INDEX; on Postgres prefer CONCURRENTLY to avoid locking under live reads, consistent with the project's lock-safety rule). This keeps the login-time sweep O(index) instead of O(table).
- **Risk:** Bulk delete bypasses ORM cascade/events, but AuthSession has no dependents, so semantics are identical. The migration must be reversible (drop index) and non-locking. Behavior (expired rows purged at login) is preserved.
- **Test:** Seed several expired and one live AuthSession, call create_session, assert only the live row plus the new row remain -- passes before and after, guarding the semantics while the implementation is optimized.

### #114 — _extract accepts a pdfplumber parse where only one page has text, skipping pypdf recovery

- **Where:** `src/regwatch/ingest/pdf_parser.py:109`
- **Class:** LOW severity / M effort / correctness · lane `ingest-process` · score 1.1 · confidence 0.68 · verdict CONFIRMED
- **Now:** The engine-selection guard is 'at least one non-empty page', a very weak signal, rather than 'all reasonably-extractable pages have text'. Blank pages are silently accepted, so content that is present in the PDF is never chunked/indexed -- later surfacing as INV-1 'absent' when it is actually present. Page indices stay 1:1 (good), but the text is lost.
- **Fix:** When pdfplumber leaves some pages blank, also run pypdf and per-page merge: for each page pdfplumber left empty, substitute pypdf's text if non-empty (keep pdfplumber's where it has text). Only fully-blank pages across both engines stay empty. Tag engine e.g. 'pdfplumber+pypdf'. This only ADDS recovered text, never removes correctly-extracted text.
- **Risk:** Behavior change: pages previously blank may now carry (lower-quality) pypdf text -- but any text beats a blank page for a page pdfplumber failed on. Doubles parse time only for PDFs with at least one blank page. Must preserve exact page count/order (merge by index).
- **Test:** Monkeypatch `_try_pdfplumber` to return pages=['I. Intro text','',] and `_try_pypdf` to return ['','II. BE recommendations text']; assert `_extract().pages[1]` contains the pypdf text. Fails today (returns pdfplumber's blank page 2).
- **Note:** changes behavior (intended correction)

### #115 — Pure domain module watch/matcher imports PsgListing from the I/O-heavy ingest.psg_crawler

- **Where:** `src/regwatch/watch/matcher.py:27`
- **Class:** LOW severity / M effort / dependency · lane `x-domain-io` · score 1.1 · confidence 0.68 · verdict CONFIRMED
- **Now:** The domain data type PsgListing lives in the I/O module that fetches it, so every pure consumer of the type pulls the crawler's third-party I/O dependencies. Dependencies point outward (domain -> I/O) instead of inward, and a slim build without selectolax could not even import the pure matcher.
- **Fix:** Move the PsgListing dataclass to a dependency-light module (e.g. regwatch/ingest/types.py or regwatch/common/) and re-export it from psg_crawler for back-compat; point matcher.py/run.py at the new home. No logic change.
- **Risk:** Medium mechanical surface - several call sites import PsgListing; keeping a re-export in psg_crawler avoids touching them all. Verify no circular import (types module must not import the crawler).
- **Test:** Add an import-isolation test: in a subprocess with selectolax made unimportable, `import regwatch.watch.matcher` should succeed. Fails today because the matcher force-loads selectolax via psg_crawler.

### #116 — pgvector_store.get_engine()/_ensure_ready() lack the double-checked lock db.py.get_engine uses, so concurrent first-use can duplicate the engine and race Table.create

- **Where:** `src/regwatch/store/pgvector_store.py:151`
- **Class:** LOW severity / M effort / correctness · lane `store` · score 1.1 · confidence 0.68 · verdict REVISED
- **Now:** Lifecycle-init parity gap between the two store modules. In production the shared-engine branch dominates and init_db creates chunk before serving, so the races are mostly reachable only via the documented 'self-heal on first use without db.py init' path (eval/ingest CLI, tests) under concurrency — but the missing lock is a real inconsistency and the ProgrammingError is unhandled.
- **Fix:** Mirror db.py's TWO-lock split, not a single lock. Add module-level `_engine_lock = Lock()` and a SEPARATE `_schema_lock = Lock()`. In get_engine() wrap the `_engine is None` construction with `_engine_lock` double-checked locking. In _ensure_ready() wrap the `_schema_ready` check + ensure_schema(...) step with `_schema_lock` double-checked locking; because _ensure_ready calls get_engine() (line 301) which takes _engine_lock, the two locks MUST be distinct (a single shared non-reentrant Lock self-deadlocks) -- either use two locks (get_engine's _engine_lock is a different object, so no deadlock), or compute `engine = get_engine()` before acquiring _schema_lock, exactly as db.py.init_db does at lines 567-568. Keep the critical sections minimal so the DDL round-trips don't hold longer than needed. Separately, since the concurrent-first-use table.create TOCTOU is the concrete unhandled error, consider having _ensure_ready serialize the schema step (the lock alone fixes this) rather than also catching ProgrammingError from line 223.
- **Risk:** Adding a lock only serializes first-use init (idempotent DDL already), so no behavior change for callers. Keep the critical section minimal so it does not hold across the DDL round-trips longer than needed.
- **Test:** Point the store at the owned-engine (SQLite-shared) fallback and spawn N threads calling get_engine() concurrently; assert exactly one engine object identity is returned. Separately, race two threads through _ensure_ready() before the table exists and assert neither raises. Fails intermittently today.

### #117 — Orange Book cache refresh is non-atomic: a concurrent reader can get old rows paired with the new fetched_at

- **Where:** `src/regwatch/sources/orange_book.py:328`
- **Class:** LOW severity / M effort / correctness · lane `sources` · score 1 · confidence 0.6 · verdict CONFIRMED
- **Now:** The module comment disclaims thread safety and calls a race 'a benign re-fetch', but the actual worst case is not a re-fetch — it is stale-rows-with-fresh-provenance, because the _ZIP_CACHE swap and the _PARSED_CACHE invalidation are not a single atomic step and the parsed cache is keyed independently of the zip cache identity.
- **Fix:** Tie the parsed cache to the zip-cache identity so it cannot outlive its source: store parsed rows on the _ZipCache object itself (or key _PARSED_CACHE by id(cache)/a snapshot token), and clear/replace them in the same assignment that swaps _ZIP_CACHE. Alternatively guard _cached_zip's miss path with a short lock. Either removes the mismatched-provenance window without adding a hot-path lock.
- **Risk:** Single-threaded behavior is unchanged (the day-long TTL and monthly data make the window practically irrelevant, hence low severity). If a lock is chosen, keep it only around the fetch/replace, not the cache-hit read path.
- **Test:** After a refresh, assert every OrangeBookRows returned within the TTL carries a fetched_at equal to the current _ZIP_CACHE.fetched_at; or unit-test that parsed rows are stored on/keyed to the same cache object so a swap cannot expose old rows under a new timestamp.

### #118 — Chat-session data-access SQL is inlined across three HTTP handlers instead of a store module

- **Where:** `src/regwatch/api/main.py:1729`
- **Class:** LOW severity / M effort / domain-io-separation · lane `api` · score 1 · confidence 0.6 · verdict REVISED
- **Now:** The /sessions endpoints were written before (or without) a store seam, so ~90 lines of SQL/ORM query construction accreted directly in the HTTP layer, violating the 'business rules/data access never live in handler code; dependencies point inward' standard and leaving main.py the lone inline-SQL island on the surface.
- **Fix:** Keep the extraction as described (store/sessions.py with list_sessions_for_user, get_owned_session, delete_session, each owning its session_scope and materializing rows/dicts inside the transaction to avoid detached-instance regressions, mirroring whitepaper_runs.py), but correct the framing: main.py is NOT the lone inline-SQL island -- lines 379, 671, 975, 1003, and 1316 all inline data access in the same module. Treat this as a low-severity, opt-in consistency refactor, and if pursued, either move the sibling chat/feedback data-access (671 adopt, 975 _upsert_feedback, 1003 ownership check) into the same store seam in one pass or explicitly note why only /sessions is being extracted, so the codebase does not end up with an arbitrary partial store. Preserve the two-query no-N+1 shape and title-fallback/ownership semantics byte-for-byte, with the characterization test as proposed.
- **Risk:** Pure refactor; the two-query no-N+1 shape and the title-fallback/ownership semantics must be preserved byte-for-byte. Keep session_scope ownership of the transaction inside the store to avoid a detached-instance regression on the returned rows.
- **Test:** Characterization test: seed a user with 2 sessions (one titled, one relying on first-user-message fallback) and assert GET /sessions returns identical ordering, titles, and message_count before and after extraction; assert still exactly 2 SQL round-trips (no N+1).

### #119 — whitepaper/page.tsx is 1271 lines / 16 components; the presentational cluster should move out of the route file

- **Where:** `app/(shell)/whitepaper/page.tsx:119`
- **Class:** LOW severity / L effort / complexity · lane `fe-ui` · score 0.9 · confidence 0.85 · verdict CONFIRMED
- **Now:** Organic growth: the Phase-2 durable-runs workflow was added inline rather than split as it grew.
- **Fix:** Move the presentational cluster (RunRow/RunView/Sections/Cell/CellOverlay/AnalystValue/AnalystEditor/EvidenceRow/SpineCard/SpineItem/Tally/StatusChip) into `components/whitepaper/` (or a `_components` sibling), leaving `page.tsx` with the container, data/effects, and handlers. Fold the reusable `Field` and the timestamp helpers into the shared modules from the other findings so the split also removes duplication rather than just relocating it.
- **Risk:** Pure refactor if done as file moves with unchanged props; the `key={run.id}` remount on Sections and the memo-free editors must be preserved. Verify the WhitepaperRuns test still passes unchanged (it imports the default page export).
- **Test:** No new test; the existing WhitepaperRunsPage.test.tsx must remain green after the move (it exercises the runs list, run hydration, save, finalize, download, and the degraded-inline path through the same public component).

### #120 — get_recent_turns window query has no stable tiebreaker; equal created_at makes turn pairing nondeterministic

- **Where:** `src/regwatch/common/conversation.py:123`
- **Class:** LOW severity / S effort / correctness · lane `llm-common` · score 0.9 · confidence 0.72 · verdict CONFIRMED
- **Now:** Ordering relies solely on a non-unique timestamp column with no deterministic secondary sort key, and the fold logic assumes a stable newest-first order to keep the newest message per (turn,role) and to bound the scan window.
- **Fix:** Add a deterministic secondary sort, e.g. .order_by(desc(col(ChatMessage.created_at)), desc(col(ChatMessage.id))). id is not chronological but it makes ties total-ordered and reproducible, which is all the fold and the window boundary need.
- **Risk:** Minimal: only disambiguates rows that currently tie. Existing tests use distinct explicit timestamps so ordering there is unaffected.
- **Test:** Seed two messages of different turns with identical created_at and assert get_recent_turns returns the same, correctly-paired result across repeated calls (or across both possible DB orderings by forcing each). Fails today because the outcome depends on undefined tie ordering.
