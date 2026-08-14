# slm-layer execution plan: the prompt-layer redesign

Written 2026-08-07. Last updated: 2026-08-11. Status: execution in progress;
v7 selective synthesis and PR11b route/scope observation landed on 2026-08-10.
Route execution remains unauthorized while PR11c corrects the measured v1
classifier reliability gap.

**Where this stands.** The answer layer shipped. v6 prose synthesis and v7
selective citation are both live in production, flipped by Fly secret on
2026-08-10. The route call is built but can only observe, and its flag defaults
to off. Everything after that, gate softening and converse mode, is still open.

Headline rule in prod today: **cite the facts, talk like a person.** A sentence
that says what FDA guidance requires carries the passage numbers it came from.
Our own reading and ordinary conversation carry none. INV-1 has not moved: an
uncited source fact is still dropped by `generate/turn_gate.py`, not by the
prompt.

Companion doc: `docs/PROMPT_LAYER_RESEARCH_2026-08-07.md` is the why, this one
is the how.

## 1. Status, PR by PR

| PR | What it does | State |
|---|---|---|
| PR1 | `ask_core` split into stages around a `TurnState` dataclass | done |
| PR2 | `generate/prose_turn.py`: prose parser, marker grammar, sentence kinds | done |
| PR3 | `turn_gate` corrector + kind/correction ledger fields | done |
| PR4 | v6 prose prompt behind `REGWATCH_PROSE_SYNTHESIS` (#175) | done |
| PR5 | dark eval harness + serialized `databricks-eval` CI lane | done |
| Checkpoint 1 | v6 prod flip | done, secret is set |
| PR6 | v6 default-on in code + prompt-version pin move | **open** |
| PR7 | delete the v5 claims-JSON path | **open** |
| PR8 | faithfulness redefined over ledger kind tags (#178) | done |
| PR9 | v7 prompt + `REGWATCH_SELECTIVE_CITATION` (#182) | done |
| Checkpoint 2 | v7 prod flip | done, secret is set |
| PR10 | v7 default-on in code + pin move | **open** |
| PR11a | dark route and corpus-scope contracts (#177) | done |
| PR11b | route shadow logging and metrics (#181) | done |
| PR11c | route v2 reliability correction, still shadow-only (#184) | **in review** |
| Checkpoint 3 | route promotion decision | **open**, needs shadow data |
| PR12 | route `live` + deterministic scope compiler executing | **open** |
| PR12b | route default flip | **open** |
| PR13 | retrieval-group gates + clarifies that name candidates | **open** |
| PR14 | decline humanization, guidance shrink, refusal_accuracy removal | **open** |
| PR15 | converse mode behind `REGWATCH_CONVERSE_MODE` | **open** |
| PR15b | converse default flip | **open** |
| PR16 | cleanup and deletion | **open** |

Two things to know before you read the rest:

- Live draft streaming (#179) landed outside this plan but shares the same code
  path. It streams the provisional answer token by token over SSE behind
  `REGWATCH_LIVE_DRAFT`. That flag is on in prod too, and it only does anything
  when prose synthesis is on.
- `REGWATCH_ROUTE_CALL` defaults to `off`, and it is not one of the three flags
  confirmed set in prod on 2026-08-11, so assume the route call is not running
  until you check `fly secrets list -a amneal`. Both `shadow` and the reserved
  `live` value execute as shadow either way. PR12 is the first change allowed to
  give `live` a meaning.

### What "done" actually looks like in the tree

- `src/regwatch/generate/prose_turn.py` parses prose into claims and tags each
  sentence `source_fact`, `reasoning`, or `conversation`.
- `src/regwatch/generate/turn_gate.py` holds the enforcement:
  `REASONING_FRAME_PREFIXES` (the four allowed opener phrases, pinned by a
  test), `correct_unknown_citation`, `downgrade_to_reasoning`, and the verdicts
  `answer`, `partial`, `material_drop`, `no_valid_citations`, `no_evidence`,
  `conversational_decline`.
- `src/regwatch/generate/prompts.py` carries v5, v6 and v7 side by side.
  `active_grounded_qa_prompt()` picks one from the flags.
- `src/regwatch/generate/route.py`, `route_shadow.py`, `retrieve/scope.py` and
  `retrieve/scope_catalog.py` are the route and scope machinery, all observation
  only.
- `/metrics` exposes `regwatch_route_shadow_calls_total`,
  `regwatch_route_shadow_failures_total` and
  `regwatch_route_shadow_compilations_total`.
- Live eval runs in its own non-canceling `databricks-eval` concurrency group
  (`.github/workflows/databricks-eval.yml`), so only one live eval touches the
  shared Databricks workspace at a time.

Battery results recorded at the v7 merge: malformed structure 0, citation
precision 0.779, refusal accuracy 0.903, uncited source facts 0.

## 2. Decisions that are settled

| # | Decision | Where it landed |
|---|---|---|
| D1 | Format change and policy change ship separately: v6 = prose format only, v7 = selective citation | Both flipped, one day apart |
| D2 | Two calls per turn (route + respond); route ships shadow first | Shadow is built, promotion is Checkpoint 3 |
| D3 | Temperature 0.0 everywhere | Unchanged. `_SYNTH_TEMPERATURE = 0.0` in `grounded_qa.py` |
| D4 | No SFT in this plan | Still out of scope |
| D5 | Rendered answers keep `[SHORT_NAME, p.N]` markers; `[n]` is model-facing only | Unchanged. A numeric-citation UI is deliberately foreclosed |
| D6 | The guidance layer shrinks, it does not die | Post-retrieval declines still need it |
| D7 | `refusal_accuracy` removal waits for PR14 | Metric is already un-gated |
| D8 | Every default flip is its own PR, proved by a green blocking eval | Pattern for PR6/PR10/PR12b/PR15b |
| D9 | `RENDERER_VERSION` goes 1 -> 2 under v7 | Shipped as `RENDERER_VERSION_SELECTIVE = 2` |
| D10 | The model proposes scope, application code compiles it | `retrieve/scope.py` produces PRODUCT, CORPUS, CLARIFY or CONVERSE and nothing else |
| D11 | Product scope and corpus scope are separate; a corpus turn never overwrites the session's product | Encoded in the scope contract |

## 3. Rules that still bind every remaining PR

**The wire does not change.** The model-facing format changed; the wire format
did not. The server still renders canonical `[SHORT_NAME, p.N]` markers plus the
`Sources:` trailer, so the frontend, the Go proxy, the OpenAPI codegen, the SSE
grammar, the `QueryResponse` keys, the 7-status enum and the DB schema are all
untouched until PR13/PR14 change copy only.

**No database migration anywhere in this plan.** Sentence kinds and corrector
fields ride `route_json["turn"]` as JSON and pass through Go verbatim. No new
`query_log` column, no sqlc regen, no Go deploy coupling.

**Prompt-version pins move as a unit.** `tests/test_prompt_eval_sets.py`,
`tests_contract/test_query_outcomes.py` and the hash-composition test move
together in PR6 and PR10, never piecemeal.

**One live eval at a time.** Serialize merges. The flag-on arm stays
`workflow_dispatch` until its default-flip PR.

**Flips are secrets, not deploys.** The relayed-stream Python path and the
Go-native path both run the same `compute_turn`, so a secret flip has no
mixed-version window. Rollback is unsetting the secret.

**Settings live at `config/settings.py`, not `src/regwatch/config/`,** and
`get_settings` is `lru_cache`d. Flag-parametrized tests must call
`get_settings.cache_clear()`.

**Lint scope.** ruff, black and mypy run over `src tests tests_contract`, plus
migrations for formatting. Run black after the last edit, before pushing.

Non-goals, unchanged: SFT, moving off temperature 0.0, status-enum or SSE or
schema changes, a numeric-`[n]` frontend, a new provider role, server-side
`json_schema` enforcement on the route call, a streaming redesign, removing the
0.30 threshold, and any unbounded corpus search.

## 4. The open work

### PR6, PR7: retire v5

PR6 sets `prose_synthesis_enabled` default True, moves the version pins to "6",
turns the flag on in the contract environment, and re-keys the echo provider's
forced-refusal payload in `llm.py`. It adds a parallel prose fixture and leaves
`synth_turn_json` alone, because 17 test files import it (counted 2026-08-11)
and `test_turn_gate.py` feeds it straight into the legacy JSON gate.

PR7 then deletes the v5 path in one go: the v5 `GROUNDED_QA` copy,
`TURN_SCHEMA_MESSAGE`, the echo JSON-synthesizer branch, the
`REGWATCH_PROSE_SYNTHESIS` flag, and `synth_turn_json` with its consumers
retargeted at prose-gate equivalents in the same PR. The `GroundedTurn` and
`Claim` pydantic models survive as the internal representation.

### PR10: v7 default-on

Same shape: default True, version literals "7" at both pin sites and the hash
test, contract environment on, `qa.jsonl` vocabulary updated for the prose gate,
plus new eval rows for AIS compliance, clarify-candidate quality, and the three
re-homed `must_clarify` decision rows. A follow-up deletes the v6 policy branch
and its flag after a soak.

### PR11c: make the route shadow reliable

- The first bounded production shadow battery exposed a classifier defect, not
  an execution defect: 3/24 route calls were structurally invalid (12.5%, above
  the <2% gate), and valid outputs inconsistently confused a named product with
  the corpus, a greeting with lookup clarification, and audited inheritance
  with freshly asserted scope. No unsafe corpus result executed because PR11b
  remained shadow-only.
- Bump the route prompt identity to v2 and its sentinel to
  `[REGWATCH_ROUTE_V2]`. Keep the strict `RouteDecision` schema and parser
  unchanged. Make the decision precedence explicit: genuine audited follow-up,
  single named product, explicit guidance family, ambiguity, and social turns
  are distinct; plural "guidances" does not make a named-product question
  corpus-wide. Enumerate the five valid mode/scope/hint shapes so unused hints
  remain null.
- The standalone rewrite must retain every named study, metric, requested
  subpart, and qualifier. Inherited rewrites carry the audited product or corpus
  family into the standalone text while the advisory scope remains `inherit`;
  identifying a cross-referenced guidance inside an explicit family is a corpus
  lookup, not missing scope.
- Expand the committed route set with plural-guidance product controls,
  additional ambiguous/capability turns, and product/corpus inheritance under
  conflicting trusted context. Echo accepts both sentinels only for backwards
  compatible local tests; runtime emits v2.
- Re-run a paced route-only Databricks repetition battery and then a short
  production shadow window after merge/deploy. Acceptance is parse failure
  <2%, every committed semantic control correct, zero unsafe corpus proposals,
  and no retrieval/response/session delta. PR12 still receives no authority in
  this PR.
- Pre-merge v2 evidence (2026-08-11): two complete paced passes over the 16-row
  route set produced 32/32 semantic matches, 0 invalid responses, 0 provider
  errors, and 0 unsafe corpus proposals on the served `gpt-oss-120b-080525`
  endpoint. Segment median latency ranged from 751-1217 ms; maximum observed
  route latency was 2670 ms. This clears the direct model battery only; the
  post-deploy production shadow window remains required.

> OWNER CHECKPOINT 3 -- route promotion. Evidence: joint mode/scope confusion
> matrix, added latency p95, QPS headroom, route failure rate, and zero unsafe
> corpus authorizations in reviewed shadow traces.

### PR12, PR12b: make the route real

`REGWATCH_ROUTE_CALL=live` may supply the rewritten standalone question. The
application, never the model, compiles execution scope.

- Existing product resolution produces an exact product scope.
- A corpus hint produces a corpus scope only when explicit corpus intent is
  positively detected AND an application-allowlisted policy expands from the
  catalog to a non-empty bounded set of current `version_id`s. Every corpus
  retrieval carries that allowlist.
- `REGWATCH_CORPUS_SCOPE` is a separate flag, defaulting off, so route rewriting
  can be promoted on its own. With it off a would-be corpus turn stays audited
  and dark and follows today's clarify path.
- Route failure, or an empty or conflicting scope, falls back to deterministic
  product resolution or clarification. Never to broad retrieval.
- A corpus turn does not update the session's active product filters. `inherit`
  compiles to corpus only from a prior audited corpus turn, and the compiler
  re-expands and re-validates the version set every turn.

Gates: `recall_at_k >= 0.80` for rewrite parity, plus scope-decision accuracy,
bounded-set membership, and zero leakage outside the allowed set. Rollback is
`REGWATCH_CORPUS_SCOPE=false` and `REGWATCH_ROUTE_CALL=shadow`.

PR12b flips the default, turns the route on in the contract environment, adds
`route_call` to the `route_json` top-level key-set pin, and makes the blocking
CI eval measure the route-on pipeline.

### PR13: clarify from the catalog, name candidates from retrieval

Keep triggering and naming separate. The catalog and the resolver stay the
authority on *whether* to clarify; retrieved candidates only fill in the copy.

- Form-silent question while the catalog shows more than one form/route combo:
  clarify fires even if top-k collapsed to a single form. Retrieval visibility
  is not completeness, and the estradiol gel-versus-tablet case is the bug that
  proves it.
- Ambiguous resolver status: either the answered product is explicitly disclosed
  in the reply, or the clarify fires, even when top-k is single-product.
- The post-retrieval mixed-products and multi-form tripwires stay as
  defense-in-depth for product scope.

Also make the mixed-product guard scope-aware. Multiple products stay forbidden
under a product scope. They are expected under a validated corpus scope, which
instead rejects any passage whose `(doc_id, version_id)` falls outside the
compiler's allowed set.

The reason strings do not change, so the frontend copy map is untouched.
Validate with a whitepaper-populator round trip: `whitepaper/populator.py`
branches on `qa.status` for clarify and refused and embeds the interpretation
and option labels, so it has to be exercised.

### PR14: declines that sound like a person

- `low_top_score` becomes a plain "here is what I could not find, here is what I
  do have nearby" plus candidates. Status stays `refused` and the reason stays
  `low_top_score`. Copy only.
- `vague_input` and `no_product` copy softens. `no_product` is bypassed only
  when the compiler has positively validated a bounded corpus scope. Every other
  product-less or ambiguous lookup clarifies.
- Resolver gates become conversational instead of dead ends. `did_you_mean` and
  `brand_lookup` supply clarify context, but an unresolved or ambiguous product
  still cannot execute a product-scoped retrieval.
- `model_refusal` renders conversationally. Status unchanged.
- `material_drop` copy reframes from an evidence-failure register to what it
  actually is: a rationale question meeting a guidance that only states
  requirements, not why. Status stays `refused` and the reason stays
  `material_drop`. Copy only.
- The scope-warning gate dies here, because its replacement (cited requirements
  plus framed reasoning on the v7 path) is already live. The META gate and
  `_meta_answer_text` survive to PR16: their replacement is converse, which
  cannot serve traffic until Checkpoint 4.
- Deletion is narrow. PR14 removes only `_SCOPE_WARNING_PHRASES` and
  `_is_scope_warning_request`. `_FILLER`, `_FOLLOW_UP_*`, `_DRILL_DOWN_WORDS`,
  `_carries_own_topic`, `_combo_from_question`, `_looks_vague` and
  `_META_PHRASES` all survive to PR16, because live paths still reference them.
- `refusal_accuracy`, its TARGETS entry and the 16 `must_refuse` gold rows come
  out. The 3 `must_clarify` rows are re-homed into the clarify-candidate-quality
  eval rows, not orphaned.
- Byte-pins that move in this PR: `NO_PRODUCT_GUIDANCE_TEXT`,
  `LOW_SCORE_GUIDANCE_TEXT`, the guidance next-step asserts, and contract
  scenario rows S8, S9, S10 and S12. S9 pins the `low_top_score` copy this PR
  changes. S13 (meta) stays until PR16. `MATERIAL_DROP_TEXT`
  (`turn_gate.py`) and its `turns.ts` `REASON_COPY.material_drop` pair move
  together, plus any contract row asserting either string.
- A/B the new decline copy before merge. Abstention is a prompt artifact, so
  phrasing has to be checked before the eval numbers mean anything.

### PR15, PR15b: converse mode

Behind `REGWATCH_CONVERSE_MODE`, default off. Route mode `converse` goes to a
respond call with the v7 system prompt and a conversation block, no documents
block, and zero source facts allowed.

The converse guard reroutes to lookup on three triggers: any resolvable-shaped
`[n]` marker, any pair-shaped marker, or a materiality-lexicon hit **together
with** corpus-anchored content. The bare materiality lexicon is deliberately
broad and would fire on ordinary chat like "I'm not sure", which would reroute
most converse turns into weak-retrieval copy and defeat the feature. Design the
threshold from the trigger-rate data logged during the shadow phase.

A rerouted turn must stay auditable: persist
`route_json["route_call"]["converse_guard"]` with `tripped`, `trigger`,
`trigger_token` and a capped `draft_prefix`, and fold the discarded call's token
usage into the audit row totals. INV-6 requires the turn's first decision to be
reconstructable from the database.

Wire shape: status `answer`, `refused=False`, `citations=[]`. Uncited served
prose already has precedent in the meta status. `docs/DECISIONS.md` records the
INV-2 redefinition, from "refuse over guess" to "never present an unsupported
statement as an FDA fact", plus the converse-guard invariant.

Checkpoint 4 stays a manual owner flip. It is the point where uncited turns get
exposed to users.

### PR16: cleanup

Pure deletion, after a converse soak. Out go the meta gate and its vocabulary,
the `_retrieval_query` / `_looks_like_follow_up` heuristic fallback, the
remaining survivors from PR14's deferred list, and the remaining flags. Route
failure gets one bounded retry, then returns to deterministic product resolution
or clarification. Raw-question corpus search stays forbidden.
`route_json["route_call"]` collapses into its stable ledger shape.
`docs/ARCHITECTURE.md` and the README get refreshed.

## 5. Risks that still apply

| # | Risk | Mitigation |
|---|---|---|
| 1 | Deleting catalog and resolver triggers trades completeness for retrieval visibility, so a homogeneous top-k answers silently for one form | PR13 keeps triggering with the catalog and resolver, and uses retrieved candidates only for the copy |
| 2 | The converse guard fires on benign chat, or converse serves an uncited corpus fact | Guard needs materiality AND corpus anchoring; threshold designed from shadow data; reroute audited; converse lands last behind Checkpoint 4 |
| 3 | A rerouted converse turn's first decision and token usage vanish from the audit row | `converse_guard` subfield plus usage folding (INV-6) |
| 4 | Removing `refusal_accuracy` orphans the 3 `must_clarify` gold rows exactly when clarify becomes the primary gate | PR14 re-homes them with a decision-accuracy check |
| 5 | One extra Databricks call per turn against shared QPS | Shadow on a sample; `REGWATCH_ROUTE_MAX_TOKENS` default 1200 sits above the measured ~761-token reasoning floor; alert when the 15-minute failure ratio passes 2% after 20+ calls; converse turns skip retrieval and synthesis, which offsets some load |
| 6 | CI measures a pipeline prod is not running | Each default flip is its own PR (PR6, PR10, PR12b, PR15b) whose green blocking eval is the proof; PR16 is deletion only |
| 7 | Product-less or follow-up turns widen to the whole corpus | Separate corpus flag, explicit-intent plus allowlisted-policy compiler, bounded current-version IDs on every query, zero-leakage gate, no broad fallback |
| 8 | The whitepaper populator embeds Ask copy and statuses | Round-trip the populator in PR13 and PR14 validation |
| 9 | A missed byte-pin breaks a flip PR late | Pin-move checklist per PR; `synth_turn_json` untouched until PR7; S9 and the meta rows have explicit dispositions |

Also on the watch list: every new model call must re-raise `D1ResidencyError`
before any degrade path, the way the guidance call does.
`malformed_structure` now means "no sentences parsed", but the reason string is
unchanged so ops greps and the old baseline stay comparable.

## 6. Validation

Manual smoke at each flip, recorded in the PR: a cited factual answer with
working evidence drawer, a follow-up pronoun rewrite, an ambiguous multi-form
question that clarifies from the catalog and names retrieved candidates, a
homogeneous-top-k form-silent question that still clarifies, an off-corpus
question that gets a plain not-found plus nearest candidates, cross-product bait
that gets dropped and disclosed (INV-9), "hello" at Phase E, an SSE replay with
no torn markers, one audit row per turn with the prompt version and the kind /
correction / guard ledger, a whitepaper populator round trip, and a sane
`/metrics` rollup.

Dark eval gates: the `workflow_dispatch` harness produces a recorded scorecard
before each checkpoint, serialized through the `databricks-eval` group.

The #163 corpus acceptance battery runs once corpus execution is enabled: all
five corpus rows route as explicit corpus queries, execute against a bounded
current-version set, retrieve nothing outside it, and keep every document and
application association. The beclomethasone control stays product-scoped, and
"What are the bioequivalence requirements?" clarifies.

Open validation items:

- Probe the live endpoint's reasoning floor again before enabling shadow at
  scale, and keep the route token cap above it.
- Measure converse-guard trigger rates during shadow, then set the PR15
  threshold from that data.
- Re-sweep the 0.30 refusal threshold after PR14. The cost of a false low
  changed from "user is blocked" to "agent says it could not find something it
  had". Note that 0.30 was tuned on the old OpenAI vector space and has never
  been validated against the Qwen3 1024-dim space now in production, so this
  sweep is overdue on two counts.
- The repetition metric added in PR9 stays dormant. It only becomes blocking if
  the owner reopens the temperature decision.

Each remaining flip wants at least a week of prod soak, watching the citation
precision proxy, correction rates including `material_exempt`, converse-guard
reroute rate, route latency p95, and the malformed-successor rate.

## 7. Where the adversarial findings went

The plan was reviewed by three adversarial critics who produced 19 verified
findings, 2 of them P0. All of them were folded into the PR descriptions above,
so they no longer need their own table. The two P0s and the ones that still
shape unshipped work are called out inline: corrector negation-blindness (fixed
in PR3 by never correcting or downgrading a material claim), catalog-versus-
retrieval triggering (PR13), the converse-guard trigger design (PR15), audit
continuity on reroute (PR15), the orphaned `must_clarify` rows (PR14), the
re-scoped deletion list (PR14), the meta gate's survival to PR16 (PR16), the
whitepaper populator blast radius, and the S9 byte-pin.
