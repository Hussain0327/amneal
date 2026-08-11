# Ask turn outcomes and citation provenance

Written 2026-08-11.
Status: design approved in outline; awaiting spec review before an
implementation plan is written.

Two production defects on the 01 Ask surface, diagnosed 2026-08-11 from audit
rows #1715 and #1716.
They share a surface and nothing else, so this document defines three
independently shippable units.

- Unit 1 fixes citation provenance (#1716). Ships on its own, now.
- Unit 2 replaces the single `no_product` outcome with a five-outcome
  taxonomy (#1715, immediate correction).
- Unit 3 is the eventual conversational routing (#1715, deferred), scoped here
  only so Unit 2 does not have to be unwound to reach it.

Companion: `docs/SLM_LAYER_IMPLEMENTATION_PLAN_2026-08-07.md` is the governing
plan for this area.
This design touches two of its rules; section 6 records where and why.

## 1. Problem statement

### 1.1 Audit #1715

A first turn of exactly `Hello`, no product filter, no history, rendered a red
`Evidence gap` card carrying two independently authored restatements of the
same fact and no clickable next step.

Root cause: `_resolve_and_carry_over` has exactly one terminal exit for a turn
that resolves no product (`src/regwatch/generate/grounded_qa.py:1715`), and it
asks only one question -- did this text name an in-corpus drug?
A greeting, a topic-less request, and a genuinely absent drug are structurally
indistinguishable at that line, so all three return
`status="refused", reason="no_product"`.

Contributing facts, all verified in the tree:

- The greeting vocabulary already exists. `_FILLER` contains `hi`, `hello`,
  `hey`, `thanks` (`grounded_qa.py:234-237`), but is consumed only by
  `_looks_vague` and `_carries_own_topic`, which run *after* a product is
  pinned.
- The resolver disagrees with it. `resolver._NON_DRUG_WORDS` contains no
  greetings, so `hello` is emitted as a drug-name candidate token and fuzzy
  scored against the catalog.
- The card states the reason twice: once from `guidance.py:164` and once from
  `regwatch/frontend/lib/turns.ts:112`, the second in a monospace `.code` class
  that reads as machine diagnostics.
- `related` is `[]` on this branch (`grounded_qa.py:1723`), and the refused
  branch never reads `turn.clarify`, so the card offers nothing to click.
- The feedback prompt is selected by outcome kind, not reason
  (`Turns.tsx:337`), so a greeting is asked "Should this have been answered?".
- Every greeting pays a router LLM round trip. The guide block fires
  (`grounded_qa.py:2681`) and `render_guidance_message` then returns hard-coded
  copy on the `no_product` branch before it ever reads `plan`
  (`guidance.py:164`).

### 1.2 Audit #1716

A correct, well-cited answer rendered its provenance as `PSG_020911 . p.1`.

Root cause: `short_name` is literally `"PSG_" + appl_no`
(`src/regwatch/retrieve/retriever.py:53-57`), an FDA application number, and it
is the only human-facing identity a citation ever carries.
Product identity is dropped at two distinct points:

- `ingest/pipeline.py:587,591` denormalizes `active_ingredient` and
  `recommended_date` onto every chunk, and `pgvector_store.add_chunks` discards
  both at the upsert whitelist (`pgvector_store.py:423-441`).
- `turn_gate._citation_for` (`turn_gate.py:439`) drops `normalized_name` while
  holding it, along with the whole metadata dict carrying `dosage_form`,
  `route` and `psg_type`.

Separately, citations are persisted as `asdict()` of the domain `Citation`
(`grounded_qa.py:473`), which has no `recommended_date`.
The recency join runs only on the response path (`api/main.py:820`), so every
reopened conversation degrades to "Revision date not recorded" permanently.

## 2. Unit 1: citation provenance

### 2.1 Approved label

Chip, always visible under the answer:

```text
Beclomethasone Dipropionate - Inhalation Aerosol PSG, revised Mar 2021 . p.1
```

Reference row and evidence drawer:

```text
[1] Beclomethasone Dipropionate - Inhalation Aerosol
    FDA product-specific guidance (final) . revised Mar 2021 . p.1
    PSG_020911 . v4137 . score 0.61
    "...four in-vitro bioequivalence studies..."
    Open source PDF
```

`short_name` is not removed.
It remains the key linking a rendered `[n]` stamp to its citation, it stays on
the wire, and it stays visible in the reference row and drawer as the internal
identifier support conversations need.
Only the *default* label changes.

Rendering rules, all deterministic and client side:

- Product: title-case `product_name`. Omit the whole label and fall back to
  `short_name` when it is null.
- Form phrase: title-case `route` then `dosage_form`, collapsing to one term
  when either string contains the other, omitting each when null.
  These strings come from the FDA listing and are upper case with qualifiers
  (`AEROSOL, METERED`). The rendered examples above are illustrative; the
  normalizer's exact output must be checked against real catalog rows during
  implementation, and it must never drop a qualifier to make a label shorter.
- Document kind: the chip says `PSG`. The reference row spells it
  `FDA product-specific guidance (<psg_type>)`, omitting the parenthetical when
  `psg_type` is null.
- Date: `revised <Mon YYYY>` from `recommended_date`. Omitted from the chip when
  null; the reference row keeps today's explicit
  "Revision date not recorded" via `RecencyBadge explicitEmpty`.
- Collision: when two citations in one answer produce an identical chip label,
  both gain a trailing ` . #<appl_no>`. Computed over the citation list, not per
  citation, so a single citation never carries the number.

### 2.2 Data flow

No additional query, no migration, no re-ingest.
Every field except the revision date is already on the chunk row
(`pgvector_store.py:72-81`), and the revision date already has a batched,
failure-safe join.

```text
chunk row (normalized_name, dosage_form, route, psg_type, appl_no)
  -> RetrievedPassage                        (already carries all of it)
  -> turn_gate._citation_for                 (STOPS DROPPING IT)
  -> domain Citation                          (gains 6 optional fields)
  -> fetch_citation_recency, moved earlier    (fills recommended_date,
                                               diff_summary BEFORE persist)
  -> _build_patch asdict -> citations_json    (provenance now survives reload)
  -> QueryCitation                            (gains 4 identity fields)
  -> citationLabel() in the frontend
```

The one behavioral move is calling `store.queries.fetch_citation_recency` in
the orchestration layer before `_build_patch` persists, instead of in
`api.main._wire_citations` on the response path only.
`_wire_citations` then becomes a pass-through, because the values already ride
the citation.

A client-side join against the already-shipped `GET /psg/documents` was
considered and rejected: that endpoint returns the document's *current*
recommended date, so a citation to an older indexed version would be labelled
with a revision it did not come from.
Provenance that is subtly wrong is worse than provenance that is opaque.

### 2.3 Contract changes

`rag_contract.Citation` gains six fields, every one optional with a `None`
default:

```python
product_name: str | None = None
dosage_form: str | None = None
route: str | None = None
psg_type: str | None = None
recommended_date: str | None = None
diff_summary: str | None = None
```

`QueryCitation` gains the four identity fields; it already carries
`recommended_date` and `diff_summary`.
`api-types.ts` is regenerated from OpenAPI.

Backward compatibility, which is the load-bearing constraint here:

- `chat_message.citations_json` is `sa.JSON()`
  (`migrations/versions/0001_initial_schema.py:87`), so additive keys need no
  migration and no schema change.
- Every new field defaults to `None`, so a citation dict persisted before this
  change deserializes unchanged.
- The frontend falls back to `short_name` whenever `product_name` is null,
  which is exactly the legacy case.
- The Go proxy replays `citations_json` as `json.RawMessage`
  (`go/internal/api/sessions.go:44`) and never enumerates citation keys, so it
  needs no change and no coupled deploy.

### 2.4 Failure paths

- `fetch_citation_recency` already swallows every exception to an empty index
  and logs (`store/queries.py:59-72`), and the surrounding `session_scope`
  carries the per-connection `statement_timeout`. Moving the call earlier
  preserves that: a failed recency lookup yields citations with a null date and
  a turn that still answers.
- A citation whose passage metadata lacks a field yields `None` for it, never a
  placeholder string, so the UI can distinguish "not recorded" from "not
  loaded".
- No path may raise out of label construction. `citationLabel` returns
  `short_name` for any input it cannot fully render.

## 3. Unit 2: the outcome taxonomy

### 3.1 The model

Five outcomes, each a distinct `(status, reason)` pair with its own UI register.
These are terminal outcomes, not a routing layer.

| status | reason | Means | Renders as |
| --- | --- | --- | --- |
| `meta` | `greeting` | social turn | plain prose, no tag, no reason line, no feedback prompt |
| `clarify` | `need_product` | no credible product supplied | conversational clarify plus option pills |
| `clarify` | `product_not_covered` | credible product, corpus has no coverage | conversational, no red register |
| `answer` | none | resolved and grounded | unchanged |
| `refused` | `low_top_score`, `model_refusal`, `no_valid_citations`, `material_drop` | product resolved, retrieval or verification failed | Evidence gap, unchanged |

`no_product` retires from the live vocabulary and becomes read-only: its
frontend copy entry stays so persisted turns predating this change still
render.

`did_you_mean` (a typo clearing the 82 fuzzy threshold) and `brand_lookup` are
unchanged and sit *above* this fork.
`no_matching_psg` and `spine_unresolved` have frontend copy but are emitted
nowhere in `src/regwatch`; they are dead vocabulary and must not be allowed to
become aliases of the new states.

### 3.2 Why `meta` and not a new status

The `QueryStatusLiteral` seven-value enum
(`rag_contract.py:20-22`) is load-bearing across the Go proxy, OpenAPI codegen,
the SSE grammar, the frontend `STATUSES` allowlist and persisted rows, and the
governing plan lists status-enum changes as a non-goal.

`meta` already means exactly what a social turn needs: answered from trusted
state, citation-incapable by construction, no retrieval.
Its render branch (`Turns.tsx:252-266`) already emits plain prose with no
declined register, no reason line and no feedback prompt.
The new `reason` distinguishes greeting from capability question in audit.

This also means Unit 3 needs no enum change either: a live `mode=converse` turn
lands on the same status.

### 3.3 Classification

A new domain-pure module, `src/regwatch/generate/unresolved.py`, with no I/O:

```python
UnresolvedOutcome = Literal["converse", "need_product", "product_not_covered"]

def classify_unresolved(question: str, *, external_drug_known: bool)
        -> UnresolvedOutcome:
    ...
```

Rules, in order:

1. `converse` when the question's token set is non-empty and is a subset of a
   new `_SOCIAL` vocabulary.
2. `product_not_covered` when `external_drug_known` is true.
3. `need_product` otherwise.

`_SOCIAL` is a new, narrow frozenset (`hi`, `hello`, `hey`, `hiya`, `howdy`,
`yo`, `greetings`, `good`, `morning`, `afternoon`, `evening`, `thanks`,
`thank`, `you`, `please`, `ok`, `okay`).
It deliberately is *not* `_FILLER`: `_FILLER` also contains `more`, `something`,
`else`, `question` and `regarding`, so "something else" would misclassify as
social.
`_SOCIAL` is a subset of `_FILLER` and a test pins that relationship.

The caller supplies `external_drug_known`, so the classifier stays pure and
table-testable.

### 3.4 Earning `product_not_covered`

The corpus catalog is the only drug vocabulary in this repository, so a real
drug we do not cover and a nonsense token are locally indistinguishable.
An INN stem test does not rescue this; romidepsin matches no standard stem.

The decision is to widen the OpenFDA call this branch already makes.
`resolver.resolve_brand` (`resolver.py:305-330`) already fires a Drugs@FDA
request on exactly this branch, already gates on `openfda_api_key`, already
runs through an `httpx.Client(timeout=s.http_timeout_s)`
(`sources/_utils.py:114`), and already degrades to `[]` on any exception.

Change: one function, `lookup_external_drug(question) -> ExternalDrugMatch`,
issuing a single request that searches `openfda.brand_name` *and*
`openfda.generic_name`, and returning both

- `corpus_products: list[str]` -- today's `resolve_brand` result, feeding the
  unchanged `brand_lookup` clarify, and
- `known_absent: bool` -- a real FDA drug matched and none of its ingredients
  are in the corpus.

`resolve_brand` becomes a thin wrapper over it so no caller changes.
Net effect on latency and dependencies: zero. It is the same request.

Consequence to accept explicitly: where `OPENFDA_API_KEY` is unset,
`known_absent` is always false and every absent drug falls to `need_product`.
That is the safe direction and matches the rule that weak or ambiguous
drug-like tokens must not claim `product_not_covered`.
The key defaults to `None` (`config/settings.py:506`); whether prod has it set
is a Fly secret question for the owner, and this design does not depend on the
answer for correctness.

### 3.5 Where it hooks in

Two edits, both at sites that already exist.
No new gate ordering.

1. In `_pre_retrieval_route`, immediately after the meta gate
   (`grounded_qa.py:1785`), a social check guarded on there being no pinned or
   session product. This runs before `resolve_product`, so a greeting costs no
   resolver work, no network call and no LLM call.
   The guard preserves today's behavior for `Hello` *with* an active-ingredient
   filter, which correctly reaches the `vague_input` clarify and its product
   options (`grounded_qa.py:1802-1828`).
2. At the single `else` branch that produces `no_product` today
   (`grounded_qa.py:1715`), the fork between `need_product` and
   `product_not_covered`. `did_you_mean` and `brand_lookup` still fire first and
   are untouched.

A new `_converse` maker joins `_refuse`, `_clarify` and `_meta`: it sets
`status="meta"`, `reason="greeting"`, `refused=False`, `citations=[]` and
`guide=False`.

### 3.6 Killing the wasted model call

The guide block currently fires on every decline.
Its only surviving effect on `no_product` is prioritizing options, and the
options list is empty there, so the call is pure cost.

The rule cannot simply be "skip the planner when there are no options".
`render_guidance_message` branches on `plan.next_step` for `low_top_score`,
`model_refusal`, `no_valid_citations` and `material_drop`
(`guidance.py:170-180`), choosing between two different sentences, so skipping
the call on those reasons would silently change their copy.

Rule, stated narrowly: the planner is skipped only where the rendered copy is
plan-independent *and* there are no options to order -- that is `greeting`
(always, via `guide=False` on `_converse`) and `need_product` /
`product_not_covered` when their option lists are empty.
Every other decline reason keeps today's behavior exactly.
A test asserts the provider call count is zero for a greeting turn, and a
separate test pins that a `low_top_score` turn still makes its planner call.

### 3.7 Copy

Server-owned and deterministic, as today.
The model never writes display prose on these paths.

- `greeting`: "Hey! What can I help you with today?"
- `need_product`: "Sure -- which product are you asking about?"
- `product_not_covered`: "I don't currently have FDA product-specific guidance
  for <product> in this corpus." followed by covered-product options where the
  application can construct them.

### 3.8 Frontend

- The `clarify` branch suppresses its monospace `why` line for reasons in a new
  `SELF_EXPLANATORY_REASONS` set (`need_product`, `product_not_covered`),
  mirroring the suppression already applied to `scope_warning`
  (`Turns.tsx:284`). The backend copy already states the why in plain language;
  restating it in a diagnostic register is the duplication being removed.
- `REASON_COPY` keeps `no_product` for legacy history and gains nothing new.
- `nonAnswerLabel` needs no logic change -- clarify and meta never reach it --
  but the aria-live composition at `app/(shell)/page.tsx:586-604` independently
  rebuilds the same verdict for screen readers and must follow, or assistive
  technology keeps announcing "Evidence gap -- see the reply" for a greeting.
- The `meta` branch needs no change. It already renders what a greeting wants.

## 4. Unit 3: conversational routing (deferred)

Not built here. Scoped so Unit 2 is not something to unwind.

When `REGWATCH_ROUTE_CALL=live` lands (plan PR12, behind Checkpoint 3), the
route model's `mode=converse` becomes an *additional trigger* for the same
CONVERSE outcome, and `lookup_clarify` for the same `need_product` outcome.
The deterministic classifier from section 3.3 becomes the fallback that PR12
already requires: "Route failure, or an empty or conflicting scope, falls back
to deterministic product resolution or clarification."

There is no second routing architecture in this design, because the five
outcomes are a destination and the classifier is one of two eventual ways to
reach it.

Coordination: the resolver, `no_product` path and route seam are the issue #163
lane (PRs #177, #181, #184).
Unit 2 must be shown to that owner before it is written.
Unit 1 has no overlap with that lane.

## 5. Testing

Every test below must fail if the behavior it covers regresses.

Unit 1:

- `citationLabel` table tests: full data, missing date, missing form, missing
  product name (legacy fallback to `short_name`), and the two-citations-collide
  case that appends `#<appl_no>`.
- A backend test that `_citation_for` carries `product_name`, `dosage_form`,
  `route` and `psg_type` off a `RetrievedPassage`.
- A round-trip test that a persisted citation rehydrates with a non-null
  `recommended_date` -- the test that would have caught today's degradation.
- A test that a citation dict lacking every new key still deserializes.
- A test that a raising `fetch_citation_recency` still yields an answered turn.

Unit 2:

- `classify_unresolved` table tests: `Hello`, `hi there`, `thanks!`,
  `Can you tell me something about a drug?`, `Tell me about romidepsin` with
  and without `external_drug_known`, `asdfgh`, and a bare in-corpus drug name.
- A test pinning `_SOCIAL` as a strict subset of `_FILLER`.
- A test that a greeting turn issues zero provider calls.
- A test that a greeting and an absent drug produce different
  `(status, reason)` -- the explicit "romidepsin is not Hello" invariant.
- A test that `Hello` *with* a pinned product still reaches the `vague_input`
  clarify.
- Frontend: a greeting turn renders no `.msg__declined`, no
  `.msg__declined-tag` and no `AnswerFeedback`; a `need_product` turn renders no
  `.msg__reason`; the aria-live announcement for both contains no
  "Evidence gap".
- Contract scenarios replacing the `NO_PRODUCT_GUIDANCE_TEXT` pins.

## 6. Blast radius and plan rules

Byte-pins and assertions this renegotiates:

- `NO_PRODUCT_GUIDANCE_TEXT` (`tests_contract/conftest.py:96`), asserted in
  `test_query_failure_audit.py`, `test_query_stream.py` and
  `test_query_outcomes.py`.
- `regwatch/frontend/test/turns.test.tsx:73` and `:132`.
- `regwatch/frontend/test/askPage.test.tsx:262`.
- `src/regwatch/eval/metrics.py:519`, which classifies `refused` plus
  `no_product` as not testable.

Two governing-plan rules are touched, both already superseded in the tree:

- "The wire does not change ... the `QueryResponse` keys ... untouched until
  PR13/PR14" (plan section 3). Already broken by `draft_withdrawn`, added to
  `QueryResponse` in #179 after the plan landed. Unit 1 adds citation keys
  additively with no enum, schema or Go change.
- D5, "a numeric-citation UI is deliberately foreclosed" (plan line 94).
  Already contradicted by shipped code: `CitationStamp.tsx:31` renders `[{n}]`
  and `Turns.tsx:501` renders numbered reference rows. Unit 1 does not change
  the inline marker grammar, which D5's operative half pins.

Both should be corrected in the plan doc as part of Unit 1, not silently
ignored.

## 7. Risks

- A product whose name collides with the social vocabulary would be swallowed
  by the greeting gate. Mitigated by requiring the *whole* question to be
  social and by the no-pinned-product guard; a bare drug name never matches.
- Non-English greetings are out of scope and fall to `need_product`, which is
  the safe direction.
- `product_not_covered` is inert wherever `OPENFDA_API_KEY` is unset. Stated in
  3.4; it degrades to `need_product`, never to a refusal.
- Moving the recency join earlier puts one more query on the answer path before
  persistence. It is the same batched query that runs today, relocated, not
  duplicated -- `_wire_citations` stops issuing it.
- Unit 2 edits `grounded_qa.py` and `resolver.py`, which are in the #163 lane.
  Coordination is a prerequisite, not a courtesy.
