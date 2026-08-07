# slm-layer execution plan -- prompt-layer redesign (research doc section 3)

Date: 2026-08-07. Status: plan only, no code changed, nothing committed.

PACE AMENDMENT (owner decision, 2026-08-07, supersedes the pacing below but
none of the ordering or safety content): FAST PATH. The 18 PRs are batched
into ~5 merges (M1 = Phase 0; M2 = v6+v7 built flag-dark with one dark-eval
battery and ONE collapsed prod flip; M3 = route call, built in parallel
with M2, shadow measured in days not weeks; M4 = gate softening; M5 =
converse + cleanup). Soaks shrink to 1-2 days. Checkpoints 1-3 become
pre-agreed eval thresholds (citation_precision >= 0.74, parse-failure
< 2% vs the 12% malformed baseline, recall_at_k >= 0.80 for the route
rewrite) -- the flips execute on the numbers. Checkpoint 4 (converse:
exposing uncited turns) stays a manual owner flip. Unchanged in any pace:
the build ordering (parser/corrector before flip, faithfulness before
selective citation, converse guard measured before converse), both P0
fixes, wire stability, and commit-only-with-owner-go-ahead.
Companion: docs/PROMPT_LAYER_RESEARCH_2026-08-07.md (the why); this doc is
the how. Produced by a 12-agent pass: 6 code-seam maps, 2 competing drafts
(risk-first vs product-first), a judge merge, then 3 adversarial critics
whose 19 verified findings (2 P0) are folded in below; section 7 is the
verification log.

Ground truth anchors re-verified at branch base 718cd34: v"5" prompt
identity with TURN_SCHEMA_MESSAGE folded into the sha256
(src/regwatch/generate/prompts.py:237-249); blocking eval THRESHOLDS
recall_at_k 0.80 / citation_precision 0.74 and the sanctioned
refusal_accuracy removal note (src/regwatch/eval/run_eval.py:51-77);
_SYNTH_TEMPERATURE = 0.0 constant (grounded_qa.py:338-341); the 7-status
enum (rag_contract.py:20-22); version pins tests/test_prompt_eval_sets.py:60
and tests_contract/test_query_outcomes.py:88 (tests_contract/
test_query_stream.py:126-127 pins the GUIDANCE prompt v"1" and never moves
in this plan); route_json top-level keys set-equality-pinned
(tests_contract/conftest.py:150-167); echo discriminators (llm.py:138-215);
RENDERER_VERSION = 1 (turn_gate.py:60); CI eval is a live blocking
Databricks run inside the required job (.github/workflows/ci.yml:140-216).
Settings live at config/settings.py (NOT src/regwatch/config/) and
get_settings is lru_cached (config/settings.py:703-704) -- flag-
parametrized tests must get_settings.cache_clear().

## 1. Overview, goals, non-goals

Goal. Ship research-doc section 3 in increments: (a) synthesis moves from
claims[]-JSON to natural prose with model-facing [n] markers, parsed
server-side into claims[] and validated/corrected by a deterministic gate
(INV-1 stays in the deterministic layer); (b) a small route call (history
-> standalone question + advisory mode) replaces the heuristic
pre-retrieval stack; (c) declines become conversational, clarifications
name retrieved candidates; (d) true converse mode ships so "hello" gets a
greeting. Owner-visible wins in delivery order: answers read like a
colleague (Phases A/B), clarifies name candidates and declines stop being
ceremonies (Phase D), "hello" gets a greeting (Phase E).

Core risk-management move. Split "prose format" from "citation policy".
v6 = prose + [n] output with the SAME refuse-or-cite policy as v5 (every
sentence cited or NO_EVIDENCE). That exercises the entire new
parser/corrector/renderer chain in prod while eval semantics are unchanged
-- a pure format A/B. v7 = selective citation + epistemic categories,
shipped only after v6 soaks and faithfulness is redefined (the metric must
be redefined BEFORE it is read against uncited-by-design output). Converse
ships last, behind its own flag and owner checkpoint.

Wire stability rule. The model-facing format changes; the wire format does
not. The server keeps rendering canonical [SHORT_NAME, p.N] markers plus
the Sources: trailer (turn_gate.py:493-525), so the frontend
(regwatch/frontend/lib/citations.ts, Markdown.tsx), Go
(go/internal/api/query.go, persist.go), OpenAPI codegen, SSE grammar,
QueryResponse keys, the 7-status enum, and the DB schema are untouched
until Phase D's copy-only changes. No alembic migration anywhere in this
plan: epistemic tags and corrector fields ride route_json["turn"] JSON
verbatim through Go (ragclient.go:105-108, persist.go:253-258).

Non-goals (explicit):
- SFT (data/finetune/ untouched; Trust-Align recipe re-openable later).
- Temperature off 0.0 (dedicated experiment only; see section 6).
- Status-enum, SSE, QueryResponse-key, or query_log schema changes.
- Frontend numeric-[n] wire format (canonical markers stay; a numeric UI
  would need a later coordinated frontend+Go change -- deliberate).
- New provider role (reuse role="router"; role is inert on the one-model
  Databricks path, llm.py:1063-1077).
- Server-side json_schema enforcement (route call = json_object +
  schema-in-prompt + pydantic).
- Streaming redesign (buffered synthesis + post-audit replay stays).
- Removing the 0.30 threshold (becomes an evidence-strength signal; value
  stays provisional pending re-sweep, section 6).

## 2. Decision defaults taken + flip points

| # | Decision | Taken | Flip point / foreclosure |
|---|---|---|---|
| D1 | Phasing | v6 prose format-only -> faithfulness redefinition -> v7 selective citation -> route call shadow-first -> gate softening -> converse last, behind REGWATCH_CONVERSE_MODE | Owner may collapse v6/v7 into one flip at Checkpoint 1 if the dark eval is clean (costs the format-isolated A/B). Reverting converse = never set the secret. |
| D2 | Topology | Two calls (route + respond); route ships shadow (logged, never acted on), promoted in stages | Shadow agreement + QPS at Checkpoint 3 decide promotion; one-call fallback stays possible until PR12; parse layer reusable either way. |
| D3 | Temperature | 0.0 everywhere | Only via the section-6 repetition experiment + an owner call on losing byte-replay determinism. |
| D4 | SFT | Out of scope | Re-open after the prompt layer stabilizes. |
| D5 | Wire markers | Rendered answers keep [SHORT_NAME, p.N]; [n] is model-facing only | Numeric-citation UI deliberately foreclosed here. |
| D6 | Guidance layer | Shrinks, does not die (post-retrieval decline family still needs it) | Full deletion only if converse absorbs all pre-synthesis declines after Phase E soak. |
| D7 | refusal_accuracy removal | Deferred to Phase D, with the 3 must_clarify gold rows explicitly re-homed (finding F8) | Executes in PR14 when decline behavior actually changes; already un-gated. |
| D8 | CI eval arms | Blocking CI eval measures the prod-default path; the flag-ON arm is a workflow_dispatch job in a DEDICATED non-canceling concurrency group shared with the blocking eval job (finding F15) | Every default flip gets its own PR whose green blocking eval is the proof (PR6/PR10/PR12b/PR15b pattern, finding F16). |
| D9 | Renderer version | RENDERER_VERSION 1 -> 2 at v7 (PR9), where the rendered shape actually changes | None. |

## 3. PR sequence

### Phase 0 -- dark foundations (zero behavior change)

PR1 -- Extract ask_core stages into explicit state (mechanical refactor).
- Extract _resolve_and_carry_over (:1596-1697), _pre_retrieval_route
  (:1566-1780), _retrieve_and_group (:1782-1879), _synthesize_and_admit
  (:1900-2135) around a TurnState dataclass; _decline ceremony, both
  shells (ask :2138, compute_turn :2286) unchanged in signature/behavior.
- Behavior delta: none, byte-identical. The existing test wall IS the
  failing test (golden persistence, gate tests, tests_contract). Add one
  test asserting _decline reads the state dataclass.
- Size L (mechanical). Rollback: git revert.

PR2 -- Prose parser + splitter hardening (dark, no callers).
- New src/regwatch/generate/prose_turn.py:
  - numeric-marker grammar [1], [1][2], [1, 2];
  - POSITION RULE (finding F4): only sentence-trailing markers adjacent to
    terminal punctuation are citations; a mid-sentence or intra-quote
    numeric bracket is NOT consumed and falls to the leftover-bracket kill
    (safe direction). Parser-table cases: quoted "[1]" inside passage-echo
    text, user-typed "[n]" reaching the model via the question.
  - sentence-initial-marker reattachment (". [n]" -> "[n]." pre-split);
  - pair-grammar collision: a verbatim [PSG_x, p.N] echo of a passage
    header normalizes to its [n]; a fabricated pair kills its sentence;
  - n -> RetrievedPassage mapping;
  - per-sentence epistemic classification: SOURCE_FACT = resolvable
    trailing marker; REASONING = no marker + allowlisted frame prefix;
    CONVERSATION = residual. MATERIALITY APPLIES TO FRAMED SENTENCES TOO
    (finding F3): a materiality-lexicon hit (turn_gate.py:75-100) inside a
    MODEL-authored frame reclassifies the sentence as unsupported
    SOURCE_FACT (correct-or-drop path); only GATE-authored downgrade
    frames (deterministic prepend, input already passed SOURCE_FACT
    checks) are exempt. Same guard on the CONVERSATION residual (AIS
    guard).
  - marker-scope rule: one trailing [n] binds ONLY its own sentence;
  - truncation rule: final unterminated sentence dropped with a
    materiality check on the tail;
  - output: GroundedTurn-compatible claims + kind tags (pydantic models
    survive as the internal representation).
- common/sentences.py:36-59: add "ph" ("Ph. Eur." false split).
- Tests: new tests/test_prose_turn.py -- marker grammar table,
  position-rule cases, reattachment, scope binding, classification table
  incl. framed-materiality reclassification, unknown/unbalanced markers,
  truncation drop + material tail, pair-echo collision.
  tests/test_sentence_splitting.py gains the Ph. case. Size M.

PR3 -- turn_gate corrector + epistemic ledger fields (dark, gated).
- AdmittedClaim (:163-170) and ledger claim dicts (:558-583) gain kind
  (default "source_fact"), correction_method, original_cites, downgraded.
- correct_unknown_citation: argmax lexical overlap over the evidence
  passages with an absolute floor AND a runner-up margin. TWO HARD
  PRECONDITIONS (findings F1 P0, F6):
  1. MATERIALITY EXEMPTION: if materiality_trigger(text) fires, never
     correct and never downgrade -- keep drop -> VERDICT_MATERIAL_DROP.
     Token-overlap is negation-blind ("NOT required" matches the passage
     saying it IS required); material claims stay on today's
     reject-the-turn path. The exemption is ledgered
     (correction_method="material_exempt") so its rate is measurable.
  2. METADATA UNIFORMITY: correct=True only when every evidence passage
     carries truthy AND uniform (normalized_name, dosage_form, route);
     otherwise unknown cites fall back to today's drop. (The upstream
     mixed-product/form guards skip empty-metadata passages --
     grounded_qa.py:1842, :1861-1865; ingest writes "" when the listing
     lacks them -- so "single product/form evidence" is metadata-
     conditional, not guaranteed.)
- downgrade_to_reasoning: deterministic frame prepend; blocked by the
  materiality exemption above.
- admit_turn gains correct: bool = False; legacy callers unchanged.
  Ledger keys additive-safe (only TOP-level route_json keys are pinned).
- Tests: test_turn_gate.py additions -- correction accept/reject at
  floor/margin boundaries, MATERIAL-CLAIM never corrected/downgraded,
  metadata-nonuniform fallback, downgrade framing, ledger fields. Size M.

### Phase A -- v6 prose, refuse-or-cite policy unchanged

PR4 -- GROUNDED_QA v6 (prose + [n], policy unchanged) behind
REGWATCH_PROSE_SYNTHESIS, default OFF.
- prompts.py: GROUNDED_QA_SYSTEM_V6 (prose + [n], positive if-then rules,
  EVERY-sentence-cited policy retained, NO_EVIDENCE retained), opening
  sentinel line [REGWATCH_GROUNDED_QA_V6] (finding F19: the echo provider
  keys its prose branch on this sentinel, mirroring
  [REGWATCH_QUERY_GUIDANCE_V1], NOT on marker-shape + response_format,
  which would misfire on the deficiency chat_completion seam --
  deficiency/structured.py:203-213). GROUNDED_QA_USER_V6 with numbered
  passage blocks and a tail restatement paragraph as the last
  pre-generation text (the only true tail on Gemma -- llm.py:652-681
  front-loads all system content). Few-shot exemplars as alternating
  user/assistant pairs. Identity: identify_prompt("regwatch.grounded_qa",
  "6", system, user, *exemplars) -- exemplars in the sha256; no
  TURN_SCHEMA_MESSAGE in the v6 hash. Manifest reports the flag-active
  identity.
- config/settings.py: prose_synthesis_enabled (env
  REGWATCH_PROSE_SYNTHESIS, default False).
- grounded_qa.py: _format_passages_numbered beside _format_passages;
  flag-on branch in _synthesize_and_admit -- messages without
  TURN_SCHEMA_MESSAGE; _complete_structured gains response_format=None
  for prose (D1-first exception ordering :790-791 and the 2x truncation
  retry preserved); completion -> prose_turn.parse ->
  admit_turn(correct=True) -> UNCHANGED render_answer. _format_recent
  (:733-757) gains a bare-[n] strip NOW, before any [n] can exist in
  history (a stale [3] in memory would read as a live pointer into this
  turn's numbering).
- llm.py echo provider: prose branch keyed on the v6 sentinel; emits a
  cited [n] prose answer from the scraped passage header;
  REGWATCH_ECHO_FORCE_REFUSAL (prose NO_EVIDENCE sentinel) and
  REGWATCH_ECHO_FORCE_MALFORMED (unterminated garbage) honored in prose
  mode. Json branch unchanged; guidance sentinel untouched.
- Gemma thinking trap: prose makes allow_thinking reachable for the
  synthesizer (llm.py:732), so _visible_gemma_text (:510-526) becomes
  answer-path load-bearing: provider test proving scrubbed output feeds
  the prose gate; boot warning; runbook note GEMMA_THINKING_ENABLED stays
  unset through rollout.
- TEST-STUB CONSOLIDATION NOW, while dark (finding F10):
  tests/test_streaming_synthesis.py:59, tests/test_query_stream.py:60-79,
  tests/test_invariants.py:85-95, tests/test_cross_drug_leak.py:75-90
  each build private raw claims-JSON payloads; consolidate them onto the
  shared synth_turn_json fixture in this PR so the PR6 default flip
  reaches them through one seam.
- Tests: new tests/test_prose_synthesis.py -- flag-on end-to-end via
  ask() with echo: cited answer, NO_EVIDENCE, no-sentences-parsed ->
  malformed_structure, unknown-cite correction, material-claim exemption,
  cross-drug cite dropped with disclosure, replay of gated answer only.
  test_conversational_memory.py gains the [n]-strip case;
  test_databricks_llm_provider.py gains the prose thinking-scrub case;
  test_streaming_synthesis.py gains a flag-on no-torn-marker replay case;
  test_prompt_eval_sets.py parametrized over the flag (with
  get_settings.cache_clear()). Contract env keeps the flag off. Size L.

PR5 -- dark eval harness for v6 + eval-job serialization.
- prompt_eval.py flag-aware _run_qa (:222-271): v6 messages -> prose
  parser -> real gate -> rendered-string scoring (unchanged rendering
  keeps iter_psg_citations scoring valid).
- CONCRETE SERIALIZATION MECHANISM (finding F15): split the eval steps of
  the required CI job into their own job with concurrency group
  "databricks-eval", cancel-in-progress: false; the new workflow_dispatch
  dark-eval workflow joins the SAME group. (Today the blocking eval lives
  inside the lint-type-test job whose group is ci-${ref} with
  cancel-in-progress: true -- ci.yml:16-18,189-216 -- so a dark-eval run
  would either cancel PR CI or serialize nothing.) Runbook rule recorded.
- Deliverable for Checkpoint 1: v6 citation_precision >= 0.74,
  parse-failure rate vs the ~12% prod malformed_structure baseline,
  latency. If cite-prec misses, iterate prompt (quote-first, exemplars,
  corrector redirection); documented fallback = NL-to-Format second call.
- Size S/M.

> OWNER CHECKPOINT 1 -- prod flip of v6 prose. Flip = Fly secret
> REGWATCH_PROSE_SYNTHESIS=true (no deploy; relayed-stream and Go-native
> paths share compute_turn, no mixed-version window). Rollback = unset.
> Monitor: malformed_structure rate, correction_method counts incl.
> material_exempt, cite-prec proxy from audit rows.

PR6 -- v6 default-on + pin move (after >= 1 week prod soak).
- settings default True; tests/test_prompt_eval_sets.py:60 -> "6";
  hash-composition test retargeted schema-in-hash -> exemplars-in-hash;
  tests_contract/test_query_outcomes.py:88 -> version="6"; contract env
  flag-on via _uvicorn_env (tests_contract/conftest.py:683-721); the
  echo forced-refusal payload is re-keyed in llm.py (:210-215), NOT in
  conftest:568 which is only the flavor env var (finding F18).
- FIXTURE RULE (finding F14): PR6 adds a parallel prose fixture for
  ask()-path stubs and leaves synth_turn_json UNTOUCHED -- 13 files
  import it and test_turn_gate.py feeds it straight into the legacy JSON
  gate; rewriting it here would break tests PR7 owns. Blocking CI eval
  now measures v6; its green run is the merge proof. Size S code / M
  tests. Rollback: prod flips back to v5 via secret.

PR7 -- delete the v5 synthesis path (after one further release of soak).
- Deletes v5 GROUNDED_QA copy, TURN_SCHEMA_MESSAGE + hash-test remnants,
  echo json-synthesizer branch, the REGWATCH_PROSE_SYNTHESIS flag, AND
  synth_turn_json together with retargeting test_turn_gate.py
  claims-schema wall cases at prose-gate equivalents (same PR, finding
  F14). GroundedTurn/Claim pydantic models survive as the internal
  representation. Rollback: git revert (wire never changed). Size M.

### Phase B -- selective citation (the policy change)

PR8 -- faithfulness redefinition (MUST land before any uncited sentence
ships).
- metrics.py:261-273: faithfulness = fraction of SOURCE_FACT sentences
  cited, reading the ledger kind tags (QAResult/trace extended --
  Python-internal). THRESHOLDS untouched. Behavior delta: none -- v6
  output is all-cited so old and new definitions coincide; that is why
  it is safe now. Tests: test_eval_metrics.py rewritten around tags;
  test_eval_gate.py stub gains a mixed prose turn. Size M.

PR9 -- v7 prompt: selective citation + REASONING frames, flag-staged.
- prompts.py version "7" per research 3.2: SOURCE_FACT cite-required with
  the AIS test sentence, REASONING framed-uncited, CONVERSATION plain,
  one-question discipline, never-blend rule, history-not-a-source rule;
  flag REGWATCH_SELECTIVE_CITATION (default off); per-mode exemplars.
- turn_gate policy shifts activate: DROP_NO_CITES -> classify-then-
  correct via PR3 machinery WITH the framed-materiality reclassification
  live (finding F3: a materiality hit inside a model-authored frame goes
  to the unsupported-SOURCE_FACT correct-or-drop path, and eval rows
  cover it); DROP_MARKUP relaxed to sanitize-keep (URL drops stay);
  renderer emits uncited REASONING/CONVERSATION -> RENDERER_VERSION 2.
- PARTIAL_EVIDENCE_PREFIX disposition: survives v6 untouched; in v7 the
  unsupported tail becomes a conversational what-I-could-not-find
  sentence -- the byte-pin (turn_gate.py:132 / prompt_eval.py:44 / the
  citations test) moves in THIS PR, both copies at once.
- Tests: no-uncited-prose pin rewritten to the tagged contract; AIS
  compliance (uncited declarative FDA-fact caught); FRAMED material
  sentence caught; one-question cap; test_invariants.py INV-1 branch
  rewritten as no-uncited-SOURCE_FACT. Dark eval re-run with both flags.
  Size L.

> OWNER CHECKPOINT 2 -- selective-citation flip. Evidence: dark eval
> under v7, sample transcripts, refusal-phrasing A/B for touched copy.
> Flip/rollback via secret.

PR10 -- v7 default-on + pin move.
- Default True; version literals "7" at both pin sites + hash test;
  contract env flag-on; qa.jsonl expected_turn_type/expect_partial
  vocabulary per the prose gate; new eval rows: AIS-compliance,
  clarify-candidate-quality, and the re-homed must_clarify decision rows
  (finding F8, see PR14); gold-set integrity floors updated. Follow-up
  deletes the v6-policy branch + flag after soak. Size S/M.

### Phase C -- route call (shadow-first; can interleave after PR1)

PR11 -- route module + shadow logging, flag off.
- New src/regwatch/generate/route.py mirroring guidance.py:34-57:
  RouteDecision(extra="forbid") { standalone_question, mode:
  converse|lookup|lookup_clarify, product_hint? }; ROUTE_SCHEMA_MESSAGE;
  ROUTE_PROMPT = identify_prompt("regwatch.route", "1", ...) INCLUDING
  the schema message text; sentinel [REGWATCH_ROUTE_V1]; parse with
  guidance's ValueError vocabulary.
- llm.py echo: route branch (sentinel-keyed) returning valid route JSON.
- config/settings.py: route_call_mode off|shadow|live (env
  REGWATCH_ROUTE_CALL, default off).
- grounded_qa.py shadow: ONE bounded role="router" call after filter
  canonicalization: response_format="json", D1ResidencyError re-raised
  FIRST before any degrade, other failures logged-and-ignored BUT
  COUNTED: a route-call failure-rate metric with an alert, not just a
  log line (finding F17). MAX_TOKENS SIZED BY PROBE, NOT ASSUMPTION
  (finding F17): before enabling shadow, probe the live one-model
  endpoint's effective reasoning floor (qwen35-122b measured ~761
  reasoning tokens; reasoning_effort is sent on every role,
  llm.py:742-750) and set the cap comfortably above floor + JSON body;
  a mis-budgeted cap yields a quietly all-failure shadow week.
- Result written to route_json["route_call"] = {prompt, mode,
  standalone_question, agrees_with_gates, latency_ms} -- a nested new
  top-level key; the single route_json["prompt"] identity per row is
  untouched. Contract env keeps shadow off; the key-set pin update ships
  in the PR that turns it on there (PR12b).
- Eval: prompt_sets/route.jsonl + _run_route runner + closed-set update.
- Tests: new tests/test_route_call.py -- parse vocabulary, allowlist
  rejection, sentinel round-trip, D1 re-raise, shadow-never-overrides,
  failure-counted. Size M.
- Ops (no PR): enable shadow via Fly secret on a traffic sample;
  collect mode-vs-gate agreement + QPS/latency >= 1 week. ALSO logged
  during shadow (finding F5): the would-be converse-guard materiality
  trigger rate on converse-shaped turns, so PR15's guard is designed
  against measured data, not the deliberately-broad lexicon's economics.

> OWNER CHECKPOINT 3 -- route promotion. Evidence: shadow agreement
> confusion matrix, added latency p95, QPS headroom, route failure rate.

PR12 -- route standalone_question live (mode still advisory-logged).
- Flag live: search_query from RouteDecision.standalone_question
  (capped/stripped like _retrieval_query); route failure fails OPEN to
  the existing heuristics (_retrieval_query + _looks_like_follow_up stay
  as fallback until PR16); retrieval_query_rewritten semantics preserved;
  the two session carry-over sites stay the source of
  context_applied/resolved_by_name with identical audit semantics.
- Eval: recall_at_k 0.80 is the proof that the model rewrite >= the
  heuristic. Tests: follow-up cases parametrized over route mode;
  context_applied/resolved_by_name parity; contract S6 re-verified.
- Rollback: REGWATCH_ROUTE_CALL=shadow. Size M.

PR12b -- route default flip (finding F16; mirrors PR6/PR10).
- settings default live; contract env route-on; route_json key-set pin
  gains "route_call" (tests_contract/conftest.py:150-167); S-row
  expectations updated; blocking CI eval now measures the route-on
  pipeline -- closing the window where prod runs a pipeline CI does not
  measure. Size S.

### Phase D -- gate softening (first user-visible philosophy change;
requires v7 default-on + route default-on)

PR13 -- retrieval-group gates + candidate-naming clarifies, WITHOUT
losing catalog completeness.
- SEPARATE TRIGGERING FROM NAMING (finding F2, P0). Retrieved candidates
  are used for clarify COPY (CLARINET/ECLAIR pattern); the catalog and
  resolver remain the TRIGGER authority:
  - form-silent question (_combo_from_question returns None) while
    current_dosage_form_routes shows > 1 combo -> clarify fires even if
    top-k collapsed to one form (retrieval visibility is not
    completeness; the estradiol gel-vs-tablet case at :1728-1735 is the
    motivating bug and it reappears under homogeneous top-k);
  - resolver status ambiguous -> the answered product must be explicitly
    disclosed in the reply, or the clarify fires, even if top-k is
    single-product.
  - post-retrieval mixed_products/multi_form tripwires (:1842-1879) stay
    as defense-in-depth; incomplete-metadata skip preserved.
- Clarify copy names retrieved candidates via build_form_options/
  _options_from_names + passages= audit plumbing. The model's
  lookup_clarify mode stays a hint; deterministic logic decides.
- Tests: test_multiform_clarify.py rewritten (catalog-trigger +
  candidate-naming, homogeneous-top-k case included); S11 copy re-pins;
  clarify-names-only-retrieved-candidates test; reason strings UNCHANGED
  (frontend REASON_COPY untouched). VALIDATION (finding F12): whitepaper
  populator round-trip -- populator.py branches on qa.status
  clarify/refused and embeds interpretation/option labels
  (populator.py:1991-2036, :1909-1922); run its filter-pinned ask,
  multi-form clarify, and refused cases; confirm INV-5 form-selection
  semantics. Size L.

PR14 -- decline humanization + resolver-advisory + guidance shrink +
refusal_accuracy removal.
- low_top_score becomes conversational what-I-could-not-find + nearest
  candidates (status stays refused, reason stays low_top_score -- copy
  only). vague_input/no_product copy softened; no_product collapses
  toward lookup-broad -> weak-retrieval copy.
- Resolver gates advisory: ambiguous (:1601), did_you_mean (:1647),
  brand_lookup (:1656) stop terminal-blocking; resolver output becomes a
  filter hint + clarify-copy context, under the PR13 disclosure rule.
- model_refusal (:2009) rendered conversationally; status unchanged.
- scope_warning gate (:1566) dies HERE: its replacement (cited
  requirements + framed reasoning on the v7 lookup path) is already
  default-on. The META gate (:1586) and _meta_answer_text SURVIVE until
  PR16 (finding F11): its replacement is converse, which cannot serve
  traffic before Checkpoint 4; deleting it here would send "what can you
  do" into weak-retrieval refusals for a whole phase.
  tests/test_meta_questions.py survives with it (dispositioned in PR16).
- DELETION LIST RE-SCOPED (finding F9): PR14 deletes ONLY
  _SCOPE_WARNING_PHRASES + _is_scope_warning_request. _FILLER,
  _FOLLOW_UP_*, _DRILL_DOWN_WORDS, _carries_own_topic,
  _combo_from_question, _looks_vague, _META_PHRASES all SURVIVE to PR16:
  they are still referenced by live paths (_retrieval_query fallback,
  _looks_like_follow_up, the PR13 form-silent trigger, vague_input's
  only producer, the meta gate).
- Guidance allowlist drops absorbed pre-retrieval rows, keeps the
  post-retrieval family; guidance.jsonl rows for dead reasons removed.
- refusal_accuracy + TARGETS entry + the 16 must_refuse gold rows
  removed per the sanctioned note; THE 3 must_clarify ROWS ARE RE-HOMED,
  NOT ORPHANED (finding F8): they move into the clarify-candidate-
  quality eval rows (PR10 machinery) with a decision-accuracy check
  (clarified_correctly plumbing retargeted), since PR13 makes clarify
  the primary gate the same phase.
- Byte-pin moves in-PR: NO_PRODUCT_GUIDANCE_TEXT/LOW_SCORE_GUIDANCE_TEXT
  (tests_contract/conftest.py:91-107), guidance next_step asserts,
  scenario rows S8, S9, S10, S12 rewritten (S9 byte-pins the
  low_top_score answer copy -- finding F13; S13/meta stays until PR16).
  Tests for the dead scope gate deleted; test_helpful_refusal_related.py
  rewritten around humanized weak-retrieval; hard-gate tests survive.
- New decline copy A/B-checked BEFORE merge (section 6). VALIDATION:
  whitepaper populator round-trip re-run (finding F12).
- Size L; split resolver-advisory + model_refusal into PR14b if review
  load demands.

### Phase E -- true converse mode ("hello" gets a greeting)

PR15 -- converse path behind REGWATCH_CONVERSE_MODE, default off.
- Flag-on: route mode="converse" -> respond call with v7 system +
  conversation block, NO documents block; converse discipline: zero
  SOURCE_FACTs allowed.
- CONVERSE GUARD, redesigned per finding F5: reroute-to-lookup triggers
  are (a) any resolvable-shaped [n] marker, (b) any pair-shaped marker,
  (c) materiality-lexicon hit AND corpus-anchored content (a resolvable
  product/regulatory term or lexical overlap with corpus vocabulary) --
  NOT the bare materiality lexicon, which is deliberately broad and
  would fire on benign chat ("I'm not sure", "you may ask") and reroute
  most converse turns into weak-retrieval copy, defeating the feature.
  The threshold is designed against the trigger-rate data logged during
  Phase C shadow (PR11 ops step). Reroute is deterministic, never
  served-as-is.
- AUDIT CONTINUITY (finding F7): a rerouted turn persists
  route_json["route_call"]["converse_guard"] = {tripped, trigger:
  marker|pair|materiality, trigger_token, draft_prefix (capped)} and the
  discarded call's usage is folded into the audit row token totals
  (mirroring the guidance-call merge at grounded_qa.py:1531-1534) -- the
  Checkpoint 4 reroute rate must be queryable from the DB, and INV-6
  demands the turn's first decision be reconstructable.
- Wire: status answer, refused=False, citations=[] (uncited served
  prose has wire precedent: meta status -- still live, per PR14).
  docs/DECISIONS.md records the INV-2 redefinition ("refuse over guess"
  -> "never present an unsupported statement as an FDA fact") + the
  converse-guard invariant.
- Tests: "hello" -> greeting/no-citations/one audit row; converse-guard
  reroute table (marker, pair, materiality+anchor, benign-chat
  NON-trigger); INV-6 single-row incl. rerouted turns; flag-off suite
  untouched. Eval: converse rows in route.jsonl; AIS rows extended.
- Size M/L.

> OWNER CHECKPOINT 4 -- converse flip. Evidence: manual smoke,
> converse-guard reroute rate from route_json (DB-queryable), AIS-guard
> review. Explicit go-ahead to expose uncited turns to users.

PR15b -- converse default flip (finding F16; mirrors PR12b).
- settings default on; contract env converse-on; converse S-row added;
  key-set pin updated; blocking CI eval measures the full final
  pipeline. Size S.

PR16 -- cleanup + docs (pure deletion, after >= 1 week converse soak).
- Delete: meta gate + _META_PHRASES + _is_meta_request +
  _meta_answer_text (converse now owns capability questions;
  tests/test_meta_questions.py replaced by converse-mode equivalents;
  contract S13 rewritten); _retrieval_query/_looks_like_follow_up
  fallback (route-failure fallback becomes: one bounded retry, else
  broad retrieval on the raw question with carried filters -- defined,
  not silent); _FILLER/_FOLLOW_UP_*/_DRILL_DOWN_WORDS/
  _carries_own_topic/_combo_from_question/_looks_vague (their PR13/PR14
  survivors re-homed or retired with vague_input's replacement trigger);
  remaining flags (REGWATCH_ROUTE_CALL collapses, REGWATCH_CONVERSE_MODE
  deleted); route_json["route_call"] collapsed into the stable ledger
  shape. docs/ARCHITECTURE.md + README refresh. Rollback: git revert.
  Size M/L (deletion-heavy).

## 4. Compatibility and migration notes

- No DB migration in any PR. Epistemic/corrector fields ride
  route_json["turn"] claim dicts (sa.JSON; Go passes verbatim). No new
  query_log column, no sqlc regen, no Go deploy coupling.
- Go: untouched through PR16 (byte-parity forks -- error copy
  query.go:17-23, synthesized-turn key list :246-258 -- outside the
  changed surface). compute_turn's {response, persist} shape frozen.
- Frontend: untouched (D5); REASON_COPY never needs a new code.
- route_json top-level keys are set-equality-pinned; "route_call"
  appears in the pin only in PR12b (the PR that turns it on in the
  contract env). The single route_json["prompt"] identity per row is
  preserved throughout (route identity nests under route_call).
- Prompt-version pins move as a unit in PR6/PR10 (test_prompt_eval_sets
  :60, test_query_outcomes :88, hash-input test) -- never piecemeal.
- Deploy ordering for flips: set the Fly secret in the release
  containing the default change; both the relayed-stream (Python) and
  native (Go -> /internal/query/compute) paths run the same
  compute_turn, so no mixed-version window exists.
- Databricks json seam: prose mode makes _ensure_user_json_token inert
  for synthesis; the route call (json) satisfies it via its own tail
  directive.
- CI: exactly ONE live eval at a time via the dedicated
  "databricks-eval" non-canceling concurrency group (PR5); serialize
  merges; the flag-ON arm is workflow_dispatch until each default-flip
  PR. Lint scope: ruff/black/mypy over src tests tests_contract
  (+migrations for format); run black after the LAST edit before push.
- Audit continuity: every audit row stamps the identity of the path
  that produced it through every dual-path window;
  correction_method/original_cites/downgraded/converse_guard make every
  corrector and guard action reconstructable (INV-6).

## 5. Risk register (top 8, post-verification)

| # | Risk | Mitigation |
|---|---|---|
| 1 | Live citation_precision under prose < 0.74 on the one-model endpoint | PR5 dark eval gates Checkpoint 1; levers: quote-first, exemplars, corrector redirection; fallback NL-to-Format second call. v6 format-only isolates format from policy. |
| 2 | Marker-scope laundering or quoted/user-typed [n] misattribution | PR2 position rule (trailing-only), one-marker-one-sentence binding, leftover-bracket kill, framed-materiality reclassification, exhaustive unit table (finding F4). |
| 3 | Corrector re-stamps a materially-worded or cross-form claim | Materiality exemption (never correct/downgrade material claims -- F1) + metadata-uniformity precondition (F6) + floor + margin + full ledgering; material-and-uncorrectable still rejects the whole answer. |
| 4 | Converse serves an uncited corpus fact / converse guard fires on benign chat | Deterministic guard with corpus-anchored materiality trigger, designed against Phase C shadow measurements (F5); reroute audited in route_json (F7); converse lands last behind Checkpoint 4. |
| 5 | Gemma think-channel leak once prose re-enables allow_thinking | GEMMA_THINKING_ENABLED unset through rollout + boot warning + answer-path scrub test (PR4). |
| 6 | Missed byte-pin or fixture breaks a flip PR late | Pin-move checklist per PR; stub consolidation in PR4 (F10); synth_turn_json untouched until PR7 (F14); S9 and meta-row dispositions explicit (F13, F11). |
| 7 | +1 Databricks call/turn against shared QPS; eval collisions | Shadow on a sample, max_tokens sized above the probed reasoning floor with a failure-rate alert (F17); promotion gated on measured headroom; one live eval at a time via the databricks-eval group (F15); converse turns skip retrieval+synthesis, offsetting load. |
| 8 | CI measures a pipeline prod is not running during Phases D-E | Dedicated default-flip PRs PR12b/PR15b close each window; PR16 is pure deletion (F16). |

Watch-list: D1 residency -- every new call re-raises D1ResidencyError
before any degrade (route call copies the guidance pattern, tested in
PR11). malformed_structure semantics shift to "no sentences parsed" --
reason string kept so ops greps and the 12% prod baseline stay
comparable. Truncated final prose sentence -- unterminated-sentence drop
+ tail materiality + retained 2x retry + forced-truncation echo test.
Route rewrite degrading retrieval -- fail-open fallback + recall gate at
PR12b. Whitepaper populator embeds ask() copy -- round-trip in PR13/PR14
validation (F12).

## 6. Validation plan

- Manual smoke at each flip (runbook style of docs/GO_NATIVE_QUERY
  rollout, recorded in the PR): cited factual answer (canonical markers
  render, evidence drawer works); follow-up pronoun rewrite; ambiguous
  multi-form question (clarify fires from the CATALOG and names
  RETRIEVED candidates); homogeneous-top-k form-silent question (clarify
  still fires -- the F2 case); off-corpus question (conversational
  not-found + nearest candidates); cross-product bait (INV-9 drop +
  disclosure); NO_EVIDENCE; "hello" at Phase E (greeting, zero
  citations, one audit row); SSE replay (no torn markers, declines
  replay zero); audit row inspection (prompt version, kind /
  correction_method / converse_guard ledger, one row per turn);
  whitepaper populator round-trip; /metrics rollup sane.
- Dark eval gates: the PR5 workflow_dispatch harness produces a recorded
  scorecard before Checkpoints 1, 2 and the PR12 recall proof --
  serialized via the databricks-eval concurrency group.
- Refusal-phrasing A/B before PR14: each new decline/not-found/clarify
  copy variant run over the gold decision rows + a paraphrase set
  (abstention is a prompt artifact); winner pinned in tests_contract
  bytes; recorded in DECISIONS.md.
- Reasoning-floor probe before PR11 shadow: measure the live endpoint's
  effective floor; size route max_tokens above it; alert on route
  failure rate.
- Converse-guard trigger-rate measurement during Phase C shadow: log
  would-be materiality trips on converse-shaped turns; design the PR15
  guard threshold from this data.
- Threshold re-sweep after PR14: re-run the sweep harness over
  post-change audit rows (the false-low cost changed from "user blocked"
  to "agent says it could not find something it had"); decision packet
  to owner; 0.30 stays provisional until swept.
- Soak gates: each flip (PR6, PR10, PR12b, PR15b) requires >= 1 week
  prod soak monitoring cite-prec proxy, correction_method rates (incl.
  material_exempt), converse-guard reroute rate, route latency p95,
  malformed-successor rate.
- Repetition check (armed, unused): dormant eval metric from PR9 (max
  n-gram repetition over rendered prose); becomes blocking only inside
  the temperature experiment if the owner opens D3.

## 7. Adversarial verification log (19 findings, all dispositioned)

| F# | Sev | Verdict | Finding (short) | Resolution |
|---|---|---|---|---|
| F1 | P0 | CONFIRMED | Corrector token-overlap is negation-blind; material claims could be re-stamped onto the passage they invert | PR3 materiality exemption: material claims are never corrected or downgraded; drop -> VERDICT_MATERIAL_DROP; exemption ledgered |
| F2 | P0 | CONFIRMED | Deleting catalog/resolver triggers trades completeness for retrieval visibility; homogeneous top-k answers silently for one form | PR13 separates triggering (catalog + resolver stay authoritative) from naming (retrieved candidates fill the copy) |
| F3 | P1 | CONFIRMED | Model-authored REASONING frame launders uncited material FDA facts past every check | PR2/PR9: materiality lexicon runs inside framed sentences; hit -> unsupported SOURCE_FACT; only gate-authored frames exempt; eval rows added |
| F4 | P1 | PLAUSIBLE | In-range numeric bracket quoted from source/user text resolves as a valid-but-wrong citation | PR2 position rule: trailing markers only; mid-sentence/intra-quote brackets fall to the leftover-bracket kill |
| F5 | P1 | PLAUSIBLE | Bare materiality lexicon as converse-reroute trigger fires on benign chat and defeats Phase E | PR15 guard requires materiality AND corpus-anchored content; threshold designed from Phase C shadow measurements |
| F6 | P2 | CONFIRMED | Corrector's single-product/form premise is metadata-conditional (empty-metadata passages skip the upstream guards) | PR3 metadata-uniformity precondition on correct=True |
| F7 | P2 | PLAUSIBLE | Rerouted converse turn's first decision and token usage vanish from the audit row | PR15 converse_guard subfield + usage folding (INV-6) |
| F8 | P1 | CONFIRMED | Removing refusal_accuracy orphans the 3 must_clarify gold rows exactly when clarify becomes the primary gate | PR14 re-homes them into clarify-candidate-quality rows with a decision-accuracy check |
| F9 | P1 | CONFIRMED | PR14's deletion list removes vocabulary still referenced by surviving paths (_looks_vague is vague_input's only producer) | Deletions re-scoped: only scope-warning vocabulary dies in PR14; the rest moves to PR16 |
| F10 | P1 | CONFIRMED | Four test files carry private claims-JSON stub builders the shared fixture cannot reach; they break at the PR6 flip | Consolidated onto synth_turn_json in PR4 while the flag is dark |
| F11 | P1 | CONFIRMED | Deleting the meta gate one PR before converse can serve traffic creates an unflagged regression window; test_meta_questions.py never dispositioned | Meta gate + tests survive to PR16 (post-converse); scope_warning dies in PR14 because its v7 replacement is already default-on |
| F12 | P2 | CONFIRMED | Whitepaper populator consumes ask() statuses/copy and appears in no PR's blast radius | Populator round-trips added to PR13/PR14 validation; INV-5 semantics checked |
| F13 | P2 | CONFIRMED | S9 byte-pins the low_top_score copy PR14 changes but is missing from the rewrite list | S9 added to PR14's scenario list |
| F14 | P1 | CONFIRMED | PR6 rewrites synth_turn_json while legacy-gate tests still consume it -> PR6 not independently green | PR6 adds a parallel prose fixture; synth_turn_json deleted in PR7 with its consumers retargeted same-PR |
| F15 | P1 | CONFIRMED | "Serialized" dark eval has no mechanism; the obvious one cancels PR CI (ci-${ref} cancel-in-progress) | PR5 splits eval into its own job; dedicated databricks-eval non-canceling concurrency group shared with the dispatch workflow |
| F16 | P1 | CONFIRMED | Secret-only promotions leave CI measuring a pipeline prod is not running through Phases D-E; PR16 silently absorbs deferred pin moves | New PR12b/PR15b default-flip PRs; PR16 becomes pure deletion |
| F17 | P2 | PLAUSIBLE | route max_tokens ~400 may sit under the live endpoint's ~761-token reasoning floor; silent failures waste the shadow week | Probe the floor first; size the cap above it; failure-rate alert |
| F18 | P2 | CONFIRMED | Plan patched a nonexistent settings path and misread conftest:568 (flavor env var, not payload) | Paths corrected: config/settings.py, get_settings.cache_clear(), echo payload re-keyed in llm.py |
| F19 | P2 | PLAUSIBLE | Echo prose-branch discrimination by marker-shape + response_format misfires on the deficiency chat_completion seam | Echo prose branch keyed on the [REGWATCH_GROUNDED_QA_V6] system sentinel |
