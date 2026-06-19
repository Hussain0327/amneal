# Conversational refusal + meta-question split — plan

Status: APPROVED to build Slices 1-3 (not started). Verified against the real Ask
pipeline (grounding workflow w394n699g).
Goal: stop the Ask refusal being a dead-end (#1), and answer tool/meta questions
conversationally (#2) — WITHOUT weakening grounding invariants INV-1 (every answer
grounded in retrieved corpus) / INV-2 (refuse-over-guess, never dress a refusal as an
answer). CLAUDE.md: no LLM memory fills regulatory gaps.

## Principle
Reuse the existing deterministic refusal / clarify / scope_warning machinery. No LLM
judges intent. A regulatory claim is never emitted uncited.

## Decisions (locked)
- Build now: Slices 1-3 (all of #1 + #2 backend, INV-tested). #2 frontend = Slice 4.
- "What do you cover" = BOTH: corpus (~1,795 PSGs you can ask about) AND watchlist
  (products Watch monitors), clearly distinguished, never conflated.
- Meta wording: deterministic assembled string ships first; LLM-phrased over the same
  verified facts (non-grounding prompt) is the deferred slice.

## #1 Helpful refusal (no new retrieval, no LLM, refused=true kept)
Data already in hand at refusal time:
- low_top_score: the below-threshold passages are present -> surface distinct product
  names + source_url, labeled "related, not an answer".
- no_product: resolver candidates (suggest_products / resolve_brand) already computed.
Changes:
- backend: keep source_url etc. in the audited passage dict (grounded_qa.py:208-220);
  add `related: list[ClarifyOption]` to QAResult (refused=true, citations=[]); populate
  in _refuse from in-hand passages at low_top_score (grounded_qa.py:460-504, 920-940) and
  from resolver candidates at no_product (728-801); add `related` + `reason` to
  QueryResponse + map in _build_query_response (main.py:395-460); run `npm run gen:types`,
  commit api-types.ts.
- frontend: thread `related` through lib/turns.ts (assistantTurn; rehydration leaves []).
  APPEND a "Related, not an answer" section inside the declined branch
  (Turns.tsx:109-120) as inert `.pill` buttons wired to the existing `onPick`. NEVER
  `.cite` chips, never the evidence drawer. Keep the oxblood declined tag above it.

## #2 Meta vs regulatory (deterministic gate + hard veto)
- `_META_PHRASES` + `_is_meta_request()` beside `_is_scope_warning_request`
  (grounded_qa.py:174-205). Closed phrase set; false-negatives are safe (fall through to
  the grounded path), only false-positives are dangerous (handled by the veto).
- `_meta()` handler mirrors `_scope_warning` (grounded_qa.py:555-581): answer assembled
  from VERIFIED SYSTEM STATE only — distinct_metadata_values('normalized_name') /
  _doc_count (corpus), list_watchlist (watchlist), latest_digest_records ("what changed").
  Returns passages=[], citations=[], refused=false, status='meta', one audit row. No LLM
  in the first slice.
- Gate at top of ask() AFTER the scope-warning check (line 659) and BEFORE resolve_product
  (683). HARD VETO: fire meta only if `_is_meta_request(q)` AND
  `resolve_product(q).status != 'resolved'`. A named in-corpus drug => never meta. Order
  is load-bearing.
- 'meta' added to QueryStatusLiteral (main.py:392); `npm run gen:types`, commit api-types.
- frontend (Slice 4): 'meta' in STATUSES (turns.ts:25-32); new AssistantTurn branch =
  plain `<Markdown>`, no `.cites`, no `.msg__declined`, no "No citations" fallback; add a
  meta case to the SR announcement label (page.tsx:201-208).

## Safeguards (why it cannot leak an uncited regulatory claim)
1. Routing is deterministic phrase-match only — no LLM judges intent.
2. Named-drug veto: any resolved in-corpus drug skips meta -> grounded cite-or-refuse path.
3. `_meta` handler is citation-incapable and (first slice) calls no LLM — assembled from
   DB/corpus facts, never model memory.
4. #1 keeps refused=true, citations=[]; related items render as inert pills (re-runnable
   queries), never citation chips / evidence drawer.
5. related[] carries product NAMES + source links, never sub-threshold passage text.
6. Closed-enum lockstep: new status/fields go through Pydantic + gen:types (CI git-diff
   gate), or the boundary rejects them.

## Tests
- T1 routing-veto: regulatory Qs with meta words ("what BE study do you cover for
  atorvastatin?", "how do I scope the metformin dissolution?") => status != 'meta'.
- T2 meta-uncited: "what products do you cover?" => status='meta', refused=false,
  citations==[], monkeypatched LLM-call counter == 0, no passages retrieved.
- T3 no-drug-fact-leak: meta answer drawn only from system facts; no BE/dissolution prose.
- T4 meta-audited: exactly one QueryLog row status='meta' (extends INV-6).
- #1 low_top_score: weak (<0.30) chunks => refused=true, citations==[], related non-empty
  (name + source_url), LLM NOT called.
- #1 no_product: typo ('propranlol') => related populated from suggest_products; absent
  drug => related==[] graceful (no crash on empty resolver).
- contract: QueryResponse round-trips related/reason; api-types.ts matches gen:types.

## Sequencing (smallest-first, each shippable)
1. #1 low_top_score (backend + frontend) — smallest end-to-end, no new status, no LLM.
2. #1 no_product (backend) — rides slice 1's wire field.
3. #2 backend — gate + veto + `_meta` + 'meta' status + the 4 INV tests (invariant-covered
   before any UI).
4. #2 frontend — render the plain conversational meta turn.
Deferred: LLM-phrased meta answer behind a non-grounding prompt fed only verified facts,
with provider_error/timeout handling and a re-run of T3.

## Biggest risk
A false-positive in #2 routing — a regulatory question matching a meta phrase routed to
the uncited path (INV-1/INV-2 breach). Mitigated ONLY by ordering the gate BEFORE
resolve_product AND the named-drug veto AND the T1 test. Any LLM in the routing decision,
or omitting the veto, reopens the fabrication hole.

## Alternatives rejected
- Reuse status='clarify' for #1: it sets refused=false, flipping the INV-2 "shown as a
  refusal" contract. Keep refused=true + a separate related[] field.
- LLM-judged "is this meta?" classifier: a false 'meta' on a drug question = the exact
  fabrication breach. Routing must be deterministic + named-drug veto.
- Free LLM meta answer first: adds a model call + failure mode + hallucinated-coverage
  risk. Deterministic string ships first; LLM phrasing is deferred and gated.
- Client-side meta answer from GET /products: no audit row (INV-6), can't enforce the veto.
- Surface raw audited passages on the refusal: chunk text/score reads as quasi-evidence
  (INV-1). Curate a product-name list with source links instead.
