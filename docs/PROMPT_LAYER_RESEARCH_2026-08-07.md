# Prompt research behind the conversational answer layer

Written 2026-08-07. Last updated: 2026-08-11.

**What happened to this research.** The design it argued for shipped. v6 prose
synthesis and v7 selective citation are both live in production as of
2026-08-10. The three-category answer contract described below is what the
system runs today. The route call is built but observation only, and its flag
defaults to off. Converse mode is not built yet.

Read this doc for the reasoning. For the prompt text that actually ships, read
`src/regwatch/generate/prompts.py`. For the PR-by-PR status, read
`docs/SLM_LAYER_IMPLEMENTATION_PLAN_2026-08-07.md`.

The research came from an audit of the prompt surface as it stood on 2026-08-07,
plus three literature and product passes: selective citation, clarification
dialogue, and prompting small open models.

## 0. The philosophy change

The old contract, which is what v5 did: prove this turn is answerable from FDA
evidence, or block it. Every sentence the user saw was either a cited claim or
deterministic refusal copy. Product resolution was a prerequisite for most
turns, and "hello" dead-ended in a product picker.

The new contract, which is what runs now:

- Be conversational by default.
- Use FDA retrieval whenever the question may depend on the corpus.
- Never present unsupported information as an FDA fact.
- Cite when making a factual FDA claim. Talk normally otherwise.
- When reasoning past the source, say so in a fixed frame.
- Ask one short clarifying question only when the missing detail actually
  changes the factual answer, and use retrieval to make the question a good one.
- Never silently pick between materially different products, dosage forms,
  routes, strengths, or guidance documents.

The owner's short version: **cite the facts, talk like a person.**

Three sentence kinds replaced the single "claim" category:

1. SOURCE FACT: "FDA recommends X." Needs a citation.
2. REASONING: "That suggests Y may matter here." Framed as analysis, no citation.
3. CONVERSATION: "Want me to break that down?" No citation.

Scope note added 2026-08-10 by the owner: "use retrieval" means
scope-constrained retrieval, never an unbounded search when product resolution
fails. Explicit corpus-wide questions are supported through an
application-validated, current-version corpus policy. The model proposes intent
and scope; deterministic code alone authorizes filters and document membership.

## 1. Where the old philosophy lived

This map was produced 2026-08-07 against the v5 code. It is history now, but it
is the inventory the rewrite worked from, and the gates listed under DIE and
BECOME ADVISORY are still standing: PR13 through PR16 have not shipped.

### 1.1 Three prompt strings owned the philosophy

- `prompts.py` `GROUNDED_QA_SYSTEM` (version "5"). Rule 2 ("every claim MUST
  carry at least one cite"), rule 6 (the `NO_EVIDENCE` sentinel) and rule 7 (no
  recommendations) were the cite-or-refuse core. Rule 10 limited history to
  pronoun resolution. Still in the tree; PR7 deletes it.
- `prompts.py` `QUERY_GUIDANCE_SYSTEM`. The router is forbidden to answer, state
  facts, or write prose. It picks a next step from a closed vocabulary. Still
  live and still doing that job.
- `turn_schema.py` `TURN_SCHEMA_MESSAGE`. The model had to author the whole
  visible answer inside a strict JSON schema. v6 and v7 send no schema message
  at all. PR7 deletes it.

### 1.2 Most of the blocking was deterministic Python: 20 gates

All in `generate/grounded_qa.py` `ask_core`, funneled through the `_decline`
path. Under the new philosophy they sort three ways. None of these gates have
moved yet; PR13 through PR16 own the work, and the plan doc has the per-gate
detail.

- **Four die.** The scope-warning phrase gate, the `no_product` terminal refusal
  (where "hello" landed), the `vague_input` filler-word clarify, and the
  meta-phrase gate.
- **Eight become advisory.** Ambiguous product, `did_you_mean`, `brand_lookup`,
  pre-retrieval multi-form, the 0.30 `low_top_score` refusal, and the
  post-retrieval `mixed_products` / multi-form pair. The signal feeds the
  conversation instead of ending it. "Never silently blend" stays either way:
  the reply becomes a question naming the retrieved candidates instead of canned
  clarify copy.
- **Eight stay hard.** `catalog_error`, the per-passage evidence filter,
  `provider_error`, `empty_completion`, `malformed_structure`,
  `no_valid_citations`, `material_drop`, `audit_error`. Correctness and ops, not
  philosophy. `model_refusal` stays too, but renders conversationally.

### 1.3 Constraints the redesign had to respect

- Prompt identity: the `GROUNDED_QA_PROMPT` sha256 is stamped on every audit row
  and pinned by `tests/test_prompt_eval_sets.py`. The redesign had to ship as a
  new version number, which it did (6, then 7).
- Blocking eval gates: `recall_at_k >= 0.80` and `citation_precision >= 0.74` in
  `eval/run_eval.py`. Both still blocking. `refusal_accuracy` was un-gated
  2026-08-06 with an owner note anticipating this change; its 16 gold rows come
  out in PR14.
- `faithfulness` was defined as "fraction of sentences carrying a citation",
  which is wrong by design under selective citation. It was redefined in #178
  before any uncited sentence shipped, and now reads the gate's per-sentence kind
  tags. The old definition survives as `sentence_citation_rate`, reported
  alongside.
- Serving reality: the Databricks path is one model for every role. JSON mode
  needs the word "json" in a user turn on Databricks. Synthesis runs at
  temperature 0.0 with a 3000-token cap and reasoning effort pinned low. Some
  earlier candidate models had no system role, which shaped the layout;
  production serves `gpt-oss-120b-080525` today.
- INV-1 (grounding) and INV-7 through INV-9 (never blend forms or products
  silently) stay. INV-2 changes meaning, from "refuse over guess" to "never
  present an unsupported statement as an FDA fact". INV-3 was already amended
  2026-07-30 to allow cited recommendations.

## 2. What the research found

Three passes. Sources are listed in section 6. Confidence flags: [strong] means
replicated or production evidence, [medium] means a single good study or
converging secondary evidence, [weak] means directional.

### 2.1 Selective citation: how everyone else does it

- [strong] Every major production system generates natural prose and attaches
  citations only to spans that use a source: Anthropic's Citations API, Cohere
  Command R grounded RAG, Gemini grounding, Perplexity. Nobody ships per-sentence
  mandatory citation. Cohere's trained template literally asks the model which
  retrieved documents "contain facts that should be cited in a good answer".
- [strong] Citation is structural, not textual. Interleaved text blocks with
  citation metadata (Anthropic), span tags (Cohere), segment-to-chunk support
  maps (Gemini). The cite-or-not decision happens per span, and conversational
  scaffolding is uncited by construction.
- [strong] Anthropic's API returns a 400 if you combine citations with strict
  structured output. Interleaved text plus citation metadata does not fit a rigid
  JSON schema. Independent support for taking the schema off the prose.
- [strong] Quote-first, meaning extract supporting quotes then answer from them,
  is the best-evidenced prompt-level lever for 7-9B citation quality. FRONT
  measured +14.21% citation quality on LLaMA-2-7B. Anthropic's `<quotes>` pattern
  is the production version.
- [strong] Post-hoc citation verification and correction is the cheapest big win.
  CiteFix reports +15.46% relative citation accuracy and made a roughly 12x
  cheaper model viable. Post-hoc beats in-prompt schemes for small models. This
  is what `turn_gate.correct_unknown_citation` does.
- [medium] The boundary rule for "does this sentence need a cite" is the AIS
  test: it needs one if "According to [source], [sentence]" is a sensible claim
  about the corpus. AIS scopes attribution to statements about the external
  world, so conversation and framed reasoning are out of scope by definition.
  This test is quoted verbatim in the shipped v7 prompt.
- [medium] Epistemic labeling has to be templated, not trusted. Models mimic
  hedging distributions instead of expressing calibrated confidence, and people
  over-trust unhedged prose. REASONING must open with a fixed discourse frame,
  enforced by pattern match, not by asking the model to be honest. Shipped as
  `turn_gate.REASONING_FRAME_PREFIXES`, four exact phrases with a test pinning
  the prompt to the parser.
- [medium] Abstention is a prompt artifact. Refusal rates swing widely on
  phrasing alone, so new decline wording has to be A/B checked before its eval
  numbers mean anything.
- [medium] If SFT ever comes back: Trust-Align jointly trains grounded refusal
  and citation quality at exactly our model sizes, with public code and data.
  LongCite shows 8-9B models beating GPT-4o citation quality with targeted SFT.
  The existing `data/finetune/` machinery currently trains escalation behavior.

### 2.2 Is JSON good for the model? Split verdict

- [strong] Schema constraints hurt reasoning and prose, not just parsing. "Let Me
  Speak Freely?" measured drops of 38 to 63 points on reasoning tasks under
  JSON-plus-schema, with parse failures near 0.1%. One failure mode: key order
  forced the answer before the reasoning.
- [strong] Schema constraints help classification-shaped outputs, because they
  restrict the answer space. Mode decisions, citation-ID lists and extraction
  fields are the good quadrant.
- [medium] 8B-class models manage roughly 82% JSON-format compliance with high
  variance. That matches the roughly 12% `malformed_structure` rate prod was
  seeing under the full claims[] contract.
- [strong] What recovered quality: an NL-to-Format two-step (answer free-form,
  then a cheap structuring call) scored nearly identical to unrestricted; looser
  format instructions without a rigid schema; a bounded reformat-on-error repair
  call.

Verdict, and what shipped: keep JSON for the mode envelope and the extraction
surfaces, take it off the synthesis prose. The model writes natural text with
inline `[n]` markers and the server parses it into claims. Audit rows, the
frontend and the gate all keep their structure. Only the model-facing format
changed.

### 2.3 Clarification: the decision has to be externalized

- [strong] Models almost never ask spontaneously. Under 5% clarification rate in
  normal QA mode across 10 models including Qwen2.5-7B and 14B, even though the
  same models detect ambiguity at 60-80% when asked directly. A system-prompt
  line saying "ask if unclear" is the most replicated negative result in this
  literature.
- [strong] Adding retrieved context suppresses clarification further. The model
  reads the chunks and commits. So the ask-or-answer gate cannot be "let the
  synthesizer decide after reading passages". It needs its own signal.
- [strong] Retrieval-informed clarification is a validated pattern. Facets have
  to come from the collection or the question invents options that do not exist.
  CLARINET injects top-k candidates and scores into the question generator and
  hits SOTA on Flan-T5-base, which proves the value is the injected retrieval
  state, not model scale. Adobe's ECLAIR does this in production with entity
  candidates from the index rendered as one question plus clickable options. Bing
  MIMICS pairs the question with up to 5 candidate answers, never open-ended
  "what do you mean".
- [strong] The gate that works at our scale is structural, not judgmental. Group
  top-k passages by a discrete key (ingredient, dosage form, route). One group
  with adequate scores means answer. A few groups whose answers differ means one
  narrow question naming the top 2-3 retrieved candidates. Middling confidence
  means answer the most likely reading and flag the alternative at the end. Score
  margin across groups is a free information-gain signal.
- [strong] One-question discipline, in Claude's published system prompt wording:
  avoid more than one question per response, and try to address even an ambiguous
  query before asking. The OpenAI Model Spec sets the bar at "markedly unclear"
  and gives the ladder: confident right, hedged right, no answer, hedged wrong,
  confident wrong.
- [strong] A high-quality clarifying question improves satisfaction. A
  low-quality one is worse than not asking, and one bad question poisons trust in
  later good ones. The bar is not "is this ambiguous", it is "can I ask a good,
  corpus-answerable question".
- [medium] Chain-of-thought and few-shot ambiguity judgment make 7-14B models
  worse through overconfidence. Do not delegate when-to-ask to the model's
  free-form reasoning. Retrieval structure is cheaper and stronger.

### 2.4 Conversation memory

- [strong] The published architecture is rewrite-then-retrieve. History goes to a
  query-rewriting step that produces a standalone question, and the answer prompt
  gets fresh retrieval. History is structurally incapable of being cited because
  it never enters the documents block. Our `_retrieval_query` re-anchoring was a
  primitive version; the route call is the real one.
- [strong] Multi-turn degradation is real and large: -39% on average versus
  single-turn across 15 models. Models commit to early assumptions and do not
  recover. Mitigations: restate the resolved state each turn, re-retrieve every
  factual turn, never let prior synthesized answers into the documents block, cap
  raw history, and carry a running summary of the user's goal and resolved
  referents rather than facts.
- The old rule 10 ("context, NOT a source") is validated by the literature and
  survives into v7 as rule 10 there. Its scope widens from pronoun resolution to
  goal tracking.

### 2.5 Prompting small open models

- [strong] Some small open models have no system role: the chat template folds
  system content into user turn 1, so a "system prompt" has no architectural
  privilege there.
  The contract has to be short, re-rendered every turn, with the critical rule
  repeated at the tail. v6 and v7 both do this, and it costs nothing on the
  model prod actually serves.
- [strong] Negative instructions are weak at open-model scale. Write positive
  if-then routing instead: "If the sentence states what FDA guidance says, cite
  it. Otherwise, no citation." Pair every prohibition with the replacement
  behavior.
- [strong] Few-shot has outsized benefit at small scale, with gaps over 10% for
  Qwen3-8B-class models where frontier models gain 1-3%. Include one worked
  example per mode, and positive exemplars only. Showing labeled bad output risks
  activating it. v7 ships three exemplars: answered, clarify, and found-nothing.
- [strong] Small models should not free-decide retrieval as open-ended tool
  calling. Retrieval-decision policies are poorly calibrated and thresholds do
  not transfer. The convergent pattern is a single agent whose per-turn decision
  is a schema-constrained mode field with deterministic downstream handling.
  That is exactly the route call plus scope compiler.
- [medium] Official sampling guidance conflicts with our temperature 0.0.
  Small-open-model vendor guidance recommends temp 1.0, and Qwen3 says not to
  use greedy decoding
  because of repetition risk. We kept 0.0 for byte-replay determinism. If natural
  prose ever reads stilted, low nonzero (0.2 to 0.4) for the prose call while
  keeping 0.0 for the mode envelope and extraction is the experiment to run, with
  the repetition failure mode as the thing to measure.
- [strong] Qwen3.5 removed the `/no_think` soft switch. Thinking is
  serving-layer only. That explains why the qwen35-122b reasoning floor could not
  be prompted away in the 2026-08-04 probe. If that model ever serves synthesis,
  the floor is a per-call cost floor.
- [strong] Guardrail placement: the only layer with strong published numbers is
  deterministic post-hoc validation. A small dedicated checker beats prompting a
  generalist. System prompts are UX steering, not an invariant boundary. This is
  the independent argument for keeping INV-1 in `turn_gate` while relaxing the
  conversational gates, which is what shipped.

## 3. The architecture this produced

One conversational agent, at most two model calls per turn, deterministic
validators around it. Retrieval is a tool the conversation uses, not permission
to have a conversation.

### 3.1 Turn flow

The respond call and everything after it is live today. The route call and scope
compiler are built but observation only.

```
user turn
  |
  v
[rewrite + route call]  (small, schema-constrained: the good JSON quadrant)
  in:  contract header + running goal summary + last N turns + question
  out: { standalone_question, mode: converse | lookup | lookup_clarify,
         scope_hint: product | corpus | inherit | unknown,
         product_hint?, corpus_policy_hint? }
       (all advisory; no executable filters, no document IDs)
  |
  v
[deterministic scope compiler]
  caller-pinned product or validated resolver result -> EXACT_SCOPED
  explicit corpus cue + allowlisted policy + current version set
                                                -> EXACT_CORPUS
  inherit + prior audited validated scope       -> same bounded scope
  missing, conflicting, empty, or ambiguous     -> CLARIFY
  |
  +-- converse: no retrieval needed, no FDA claim implied
  |     -> [respond call] natural prose, no documents block, no citations
  |
  +-- lookup (only once a scope compiles):
  |     retrieve on standalone_question inside EXACT_SCOPED or EXACT_CORPUS
  |     EXACT_CORPUS always carries the compiler's allowed current version IDs
  |     group passages by (ingredient, dosage form, route)
  |       1 group, scores adequate -> [respond call] with documents block
  |       several groups under a product scope -> ask ONE question naming the
  |         candidates, decided from group structure, not by asking the model
  |       several groups under a corpus scope -> expected; reject any passage
  |         outside the allowed set, then answer across sources without
  |         collapsing their provenance
  |       all scores weak -> say plainly what could not be found, plus the
  |         nearest candidates
  |
  v
[respond call] natural prose; SOURCE FACTS carry inline [n]; REASONING opens
  with one of the four allowed frames; CONVERSATION is plain text
  |
  v
server-side parse -> claims[] -> citation validator
  (turn_gate as corrector: verify each [n] against its chunk; fix a wrong cite
   when a clear match exists, else downgrade to framed reasoning or drop it;
   whole-answer rejection reserved for material claims)
```

### 3.2 The system contract

The 2026-08-07 draft of the contract has been dropped from this doc. It has been
superseded verbatim by `GROUNDED_QA_SYSTEM_V7` in
`src/regwatch/generate/prompts.py`, and keeping a second, slightly different copy
of the same rules here was just a trap for the next reader. Read the code.

The design rules the draft applied, which the shipped prompt still follows:
short (low tens of rules), positive if-then routing, one worked exemplar per
mode, the critical rule repeated at the tail of the user message, no reliance on
system-role privilege, ASCII only.

What the build added on top of the draft: the four reasoning-frame openers
spelled out exactly and pinned to the parser, a "markers only at the end of a
sentence" rule, a "cite content, not metadata" rule, prompt-injection language
around the untrusted question and passage blocks, and a found-nothing rule with
no code word in it.

### 3.3 Output format decision

- Respond call: free natural prose with inline `[n]` markers. No JSON.
- Route call: strict schema with `standalone_question`, `mode`, `scope_hint`,
  optional `product_hint` and optional allowlisted `corpus_policy_hint`.
  Classification-shaped, the good quadrant. It cannot emit filters, document IDs,
  version IDs, or an executable retrieval mode.
- The server derives claims by sentence-splitting and marker parsing. The
  existing `common/sentences.py` splitter already makes "one claim = one rendered
  sentence" a shared definition. Audit rows, the frontend contract and the gate
  keep consuming claims. The model never sees the schema.
- Fallback if parse quality had disappointed: an NL-to-Format second call, still
  cheaper than the old malformed-structure retry loop. It was not needed;
  malformed structure measured 0 on the v7 battery.

### 3.4 What each old gate becomes

See 1.2. Four die, eight become advisory signals, eight stay hard. The 0.30
threshold survives as an evidence-strength signal that routes between "answer",
"answer with gaps named" and "clarify with candidates" instead of triggering a
refusal. Its value is still provisional. It was tuned on the old OpenAI vector
space and has never been validated against the Qwen3 1024-dim space now in
production, and the cost of a false low also changed, from "user is blocked" to
"agent says it could not find something it had". It needs a re-sweep on both
counts.

### 3.5 The safety model after the change

INV-1 moved fully into the deterministic layer, where the research says it
belongs. Every `[n]` is verified against its chunk after generation. An
unsupported source fact gets corrected, downgraded to framed reasoning, or
dropped, and a material-language drop still rejects the whole turn. The prompt's
job is voice and framing; the validator's job is truth-to-source.

The scope compiler is the second deterministic boundary. `no_product` is not
corpus authorization. A corpus scope requires explicit intent plus a non-empty
bounded current-version allowlist, and route failure returns to product
resolution or clarification rather than broad retrieval. Corpus turns do not
overwrite the session's active product, and corpus inheritance requires a prior
audited corpus turn plus a fresh catalog expansion.

The scope-warning gate dies, but INV-3 as amended survives as the framed-reasoning
rule (rule 2 in both the draft above and shipped v7) plus the existing
cited-recommendation discipline: strategy talk is allowed, clearly framed as
reasoning, with the FDA-fact substrate cited. v7 rule 11 draws the other edge of
the same line: say what the guidance states, do not say what the team should do.

## 4. Test and eval consequences

1. Prompt version bump and new sha256. **Done** for 6 and 7. The pin moves to
   "6" and "7" in `tests/test_prompt_eval_sets.py` and the contract tests are
   still open, in PR6 and PR10.
2. `faithfulness` redefined from "fraction of sentences cited" to "fraction of
   SOURCE_FACT sentences cited", reading the parser's kind tags. **Done** in
   #178, with `sentence_citation_rate` reported alongside. `citation_precision`
   keeps its definition and is re-measured under prose output.
3. `refusal_accuracy` removal, 16 gold rows. **Open**, lands in PR14.
4. `qa.jsonl` expected-turn-type vocabulary and the shrinking `guidance.jsonl`
   next-step vocabulary. **Open**, PR10 and PR14.
5. The roughly 15 test files pinned to the gate table partition the way the gates
   do. Tests for dying gates go with their gates, tests for advisory signals get
   rewritten, tests for hard gates survive.
6. New tests the research asked for: AIS compliance (an uncited FDA-fact sentence
   is caught), clarify-question quality (the question names only retrieved
   candidates), the one-question cap, history-not-evidence, refusal-phrasing A/B
   stability, and a repetition check if temperature ever moves off 0.0. The first
   set landed with v7; the clarify-quality rows land with PR13/PR14.
7. Route and scope eval separates classification from authorization: score `mode`
   and `scope_hint` jointly, then test the compiled scope separately. The #163
   battery requires five explicit corpus rows on a bounded corpus scope, the
   beclomethasone control on product scope, zero passages outside the allowed
   version set, preserved source and application associations, and an ambiguous
   no-product question landing on clarification. **Open**, gated on PR12.

## 5. The open decisions, and how they were called

1. **Temperature for the respond call.** Stayed 0.0. Determinism won, byte-replay
   is worth more than a slightly more natural voice, and repetition has not shown
   up as a problem. Reopenable as a dedicated experiment.
2. **Two calls per turn versus one call with a mode field.** Two calls. The
   route call is small, classification JSON is separated from prose, and it ships
   shadow-first so the extra Databricks QPS is measured before it is paid.
3. **How far conversational goes on day one.** Option A. Lookup-always shipped
   first, with prose output and selective citation. True converse mode is
   separate, behind its own flag and its own owner checkpoint, and is still
   unbuilt.
4. **Whether to queue the SFT path.** Still open. Not in scope for this plan.

## 6. Primary sources

Production: Anthropic Citations API and reduce-hallucinations docs
(platform.claude.com); Cohere Command R model card and prompting guide
(docs.cohere.com, huggingface.co/CohereLabs); Gemini grounding (ai.google.dev);
Claude published system prompts (platform.claude.com/docs/en/release-notes/
system-prompts); OpenAI Model Spec (model-spec.openai.com); Databricks FMAPI and
structured outputs (docs.databricks.com); Qwen3 and 3.5 model cards
(huggingface.co/Qwen).

Papers: AIS (Rashkin et al., CL 2023); ALCE (2305.14627); Liu et al.
verifiability audit (2023.findings-emnlp.467); Self-RAG (2310.11511); Sufficient
Context (2411.06037); Trust-Align (2409.11242); FRONT (2024.findings-acl.838);
LongCite (2409.02897); CiteFix (ACL 2025 Industry); RARR (2210.08726); CoVe
(2309.11495); "According to..." (2024.eacl-long.140); Let Me Speak Freely
(2408.02442); StructuredRAG (2408.11061); IFScale (2507.11538); SysBench
(2408.10943); CLAM (2212.07769); Knowing but Not Showing (2605.25284); Clarify
When Necessary (2311.09469); ECLAIR (2503.15739); CLARINET (2405.15784);
Corpus-informed CQ (2409.18575); Tree of Clarifications (2023.emnlp-main.63);
MIMICS (2006.10174); STaR-GATE (2403.19154); ACT (2406.00222); Lost in
Multi-Turn (2505.06120); Knowledge Conflicts survey (2403.08319); Adaptive-RAG
(2403.14403); BFCL (openreview 2GmDdhBdDk); instruction hierarchy (OpenAI);
Llama Guard (2312.06674, 2411.17713).

Flagged unverified in the underlying memos, and still unverified: Perplexity
prompts (leaked, unofficial); the exact negation-violation percentages
(secondary sources); the Databricks `json_object` "mention JSON" precondition,
which is absent from current public docs but enforced in practice per our own
issue #162; the qwen35 reasoning floor, which is our in-house measurement only;
and several 2026 arXiv numbers read at abstract level.
