# Prompt engineering research for the conversational AI layer (slm-layer)

Date: 2026-08-07
Status: research deliverable, no code changed. Grounded in a full audit of the
current prompt surface on this branch plus three deep literature/product
research passes (selective citation, clarification dialogue, SLM prompting).

## 0. The philosophy change this research serves

Old contract (current code): "Prove that this turn is answerable from FDA
evidence, otherwise block it." Every sentence the user sees is either a cited
claim or deterministic refusal/clarify copy. Product resolution is a
prerequisite for most turns; "hello" dead-ends in a product picker.

New contract (owner direction, 2026-08-07):

- Be conversational by default.
- Use FDA retrieval whenever the question may depend on the corpus.
- Never present unsupported information as an FDA fact.
- Cite evidence when making a factual FDA claim; talk normally otherwise.
- When reasoning beyond the source, mark the frame naturally.
- Ask one concise clarifying question only when missing information actually
  matters for a reliable factual answer; use retrieval to form intelligent
  clarifications instead of blocking.
- Never silently choose between materially different products, dosage forms,
  routes, strengths, or guidance documents.

Three epistemic categories replace the single "claim" category:

1. SOURCE FACT  -- "FDA recommends X." -> citation required.
2. REASONING    -- "That suggests Y may matter here." -> framed as analysis.
3. CONVERSATION -- "Want me to break that down?" -> no citation.

## 1. Ground truth: where the old philosophy lives in this codebase

Full map produced 2026-08-07 (all refs on branch slm-layer at 718cd34).

### 1.1 Three prompt strings own the philosophy

- `src/regwatch/generate/prompts.py:19-82` GROUNDED_QA_SYSTEM (version "5").
  Rules 2 ("Every claim MUST carry at least one cite; a statement you cannot
  cite is not a claim and must be left out"), 6 (NO_EVIDENCE), and 7 (no
  recommendations) are the refuse-or-cite core. Rule 10 restricts history to
  pronoun resolution.
- `src/regwatch/generate/prompts.py:105-131` QUERY_GUIDANCE_SYSTEM: the
  router is forbidden to answer, state facts, or write prose; it only picks a
  next step from a closed vocabulary.
- `src/regwatch/generate/turn_schema.py:96-103` TURN_SCHEMA_MESSAGE: the
  model must author the entire visible answer inside a strict JSON schema
  (claims[] with per-claim text <= 400 chars, cites[], extra="forbid").

### 1.2 But most of the blocking is deterministic Python: 20 gates

All in `src/regwatch/generate/grounded_qa.py` `ask_core`, funneled through the
`_decline` ceremony (:1439-1564). Under the new philosophy they sort into:

DIE (philosophy gates -- removed or absorbed into conversation):
- :1566 scope-warning phrase gate (fires before anything else)
- :1667 no_product terminal refusal ("hello" lands here)
- :1715 vague_input clarify (filler-word heuristic)
- :1586 meta phrase gate (becomes ordinary conversation)

BECOME ADVISORY (signal feeds the agent instead of blocking):
- :1601 ambiguous product, :1647 did_you_mean, :1656 brand_lookup
  (resolver output becomes context; the agent asks naturally when it matters)
- :1761 pre-retrieval multi_form (ask naturally, or answer across forms)
- :1818 low_top_score refusal at 0.30 (becomes an evidence-strength signal:
  weak retrieval -> say what was not found, offer candidates, or clarify)
- :1843 mixed_products, :1867 post-retrieval multi_form (KEEP the invariant
  "never silently blend"; the response becomes a natural question that names
  the retrieved candidates instead of canned clarify copy)

STAY HARD (correctness/ops, not philosophy):
- :1736 catalog_error, :1833 per-passage evidence filter, :1932
  provider_error, :1955 empty_completion, :1968 malformed_structure,
  :2047 no_valid_citations, :2061 material_drop, :2118 audit_error
  (2009 model_refusal becomes "the model said the corpus does not answer
  this" -- rendered conversationally, not as a refusal ceremony)

### 1.3 Constraints any redesign must respect

- Prompt identity: GROUNDED_QA_PROMPT sha256 covers system + user + schema
  text, is stamped on every audit row, and is pinned by
  `tests/test_prompt_eval_sets.py`. The redesign ships as version 6+.
- Eval gates that remain blocking: recall_at_k >= 0.80, citation_precision
  >= 0.74 (`src/regwatch/eval/run_eval.py:74-77`). refusal_accuracy was
  un-gated 2026-08-06 with an owner note anticipating exactly this change;
  its 16 gold rows are slated for removal.
- `faithfulness` in `src/regwatch/eval/metrics.py:261-272` is defined as
  "fraction of sentences carrying >= 1 citation". Under selective citation
  this metric is WRONG BY DESIGN (conversational sentences legitimately carry
  none) and must be redefined before it is read again (see 4.6).
- Serving reality: Databricks path is one-model-all-roles (single
  DATABRICKS_LLM_MODEL endpoint, `llm.py:1063-1077`); Gemma has no system
  role (content folded into user turn 1); JSON mode needs the word "json" in
  a user turn on Databricks (`llm.py:686-719`); synthesis runs at
  temperature 0.0 with a 3000-token cap; reasoning effort pinned "low".
- INV-1 (grounding), INV-7..9 (never blend forms/products silently) stay.
  INV-2 changes meaning: from "refuse over guess" to "never present an
  unsupported statement as an FDA fact" (the invariant the owner's new
  contract actually states). INV-3 was already amended 2026-07-30
  (docs/DECISIONS.md:376-389) to allow cited recommendations.

## 2. Research findings

Three research passes, sources inline in section 6. Confidence flags:
[strong] = replicated/production evidence; [medium] = single good study or
converging secondary evidence; [weak] = directional, verify before relying.

### 2.1 Selective citation: how everyone else does it

- [strong] Every major production system -- Anthropic Citations API, Cohere
  Command R grounded RAG, Gemini grounding, Perplexity -- generates natural
  prose and attaches citations only to spans that use a source. Nobody ships
  per-sentence mandatory citation. Cohere's trained template literally asks
  the model to decide "which of the retrieved documents contain facts that
  should be cited in a good answer".
- [strong] Citation is structural, not textual: interleaved text blocks with
  citation metadata (Anthropic), span tags `<co: 3>...</co: 3>` (Cohere),
  segment-to-chunk support maps (Gemini). The cite/no-cite decision happens
  at the span level; conversational scaffolding is structurally uncited.
- [strong] Anthropic's API refuses to combine citations with strict
  structured output (400 error): interleaved text + citation metadata is
  architecturally incompatible with rigid JSON schemas. Independent evidence
  for taking the schema off the synthesis prose.
- [strong] Quote-first (extract supporting quotes, then answer from them) is
  the best-evidenced prompt-level lever for 7-9B citation quality: FRONT
  (ACL 2024) +14.21% citation quality on LLaMA-2-7B; Anthropic's own
  `<quotes>` pattern is the production version.
- [strong] Post-hoc citation verification/correction is the highest-leverage,
  lowest-cost mitigation: CiteFix (Amazon, ACL 2025 Industry) +15.46%
  relative citation accuracy, and enabled a ~12x cheaper model. Post-hoc
  beats in-prompt schemes for small models.
- [medium] The boundary rule for "does this sentence need a cite" is the AIS
  test (Rashkin et al., the field standard): a sentence needs a citation iff
  "According to [source], [sentence]" is a sensible claim about the corpus.
  AIS explicitly scopes attribution to statements about the external world;
  conversation and framed reasoning are out of scope by definition.
- [medium] Epistemic labeling must be templated, not trusted: models mimic
  hedging distributions rather than expressing calibrated confidence, and
  humans over-trust unhedged prose. REASONING must open with a fixed
  discourse frame ("The guidance does not state this directly; reading the
  two sections together, ..."), enforced by pattern, not by asking the model
  to be honest about uncertainty.
- [medium] Abstention is a prompt artifact: refusal rates swing widely with
  phrasing alone. Any new decline wording must be A/B checked across
  phrasings before eval numbers are trusted.
- [medium] If SFT is ever on the table: Trust-Align (ICLR 2025) jointly
  trains grounded refusal + citation quality on exactly our model sizes
  (Qwen 0.5-7B class), public code/data; LongCite shows 8-9B models beating
  GPT-4o citation quality with targeted SFT. Relevant to the existing
  `data/finetune/` machinery, which currently trains escalation behavior.

### 2.2 Is JSON good for the model? Split verdict

- [strong] Schema constraints on REASONING/PROSE hurt the reasoning itself,
  not just parsing: "Let Me Speak Freely?" (tested on Gemma-2-9B and
  LLaMA-3-8B among others) measured drops up to 38-63 points on reasoning
  tasks under JSON+schema with ~0.1% parse failures. One failure mode:
  schema key order forced the answer before the reasoning.
- [strong] Schema constraints on CLASSIFICATION-shaped outputs help: they
  restrict the answer space. Mode decisions, citation-ID lists, extraction
  fields are the good quadrant. Databricks json_schema enforces shape.
- [medium] ~8B-class models manage only ~82% average JSON-format compliance
  with high variance (StructuredRAG). Consistent with our live ~12% prod
  malformed_structure rate under the full claims[] contract.
- [strong] Mitigations that recovered quality: NL-to-Format two-step (answer
  free-form, then a cheap structuring call) scored "nearly identical" to
  unrestricted; looser format instruction without rigid schema; bounded
  reformat-on-parse-error repair call.

Verdict for RegWatch: keep JSON for the mode envelope and extraction
surfaces (BE extraction stays as-is); take it off the synthesis prose. The
model writes natural text with inline `[n]` markers; the server parses into
claims[] and validates. Downstream consumers (audit, frontend, turn_gate)
keep their structure; only the model-facing format changes.

### 2.3 Clarification: the decision must be externalized

- [strong] Models almost never ask spontaneously: <5% clarification rate in
  normal QA mode across 10 models including Qwen2.5-7B/14B, even though the
  same models detect ambiguity at 60-80% when asked directly ("Knowing but
  Not Showing", 2026; CLAM, 2022). A system-prompt sentence "ask if unclear"
  is the most replicated NEGATIVE result in this literature -- it barely
  moves behavior.
- [strong] Adding retrieved context SUPPRESSES clarification further: the
  model reads chunks and commits. So the ask/answer gate cannot be "let the
  synthesizer decide after reading passages"; it needs its own signal.
- [strong] Retrieval-informed clarification is a validated pattern, not just
  our idea: corpus-informed clarifying questions (Krasakis et al. 2024 --
  facets must come from the collection or the question hallucinates options
  that do not exist), CLARINET (inject top-k candidates + scores into the
  question generator; SOTA on Flan-T5-base, proving the value is the
  injected retrieval state, not model scale), Adobe ECLAIR in production
  (entity candidates from the index, rendered as one question + clickable
  options), Bing MIMICS (question + up to 5 candidate answers, never
  open-ended "what do you mean").
- [strong] The gate that works at our scale is structural, not judgmental:
  group top-k passages by a discrete key (ingredient / dosage form / route);
  1 group + adequate scores -> answer; few groups whose answers differ ->
  one narrow question naming the top 2-3 retrieved candidates; middling
  confidence -> answer the most likely interpretation and flag the
  alternative at the end (ECLAIR's softer variant, also Tree of
  Clarifications: answer across facets when they are few). Score margin
  across groups is a free information-gain signal.
- [strong] One-question discipline, production wording (Claude's published
  system prompt): "doesn't always ask questions, but when it does, avoids
  more than one per response, and tries to address even an ambiguous query
  before asking for clarification." OpenAI Model Spec sets the bar at
  "markedly unclear" and gives the uncertainty ladder: confident right >
  hedged right > no answer > hedged wrong > confident wrong.
- [strong] UX evidence: high-quality clarifying questions improve
  satisfaction; low-quality ones are worse than not asking, and one bad
  question poisons trust in later good ones. The bar is not "is it
  ambiguous" but "can I ask a good, corpus-answerable question".
- [medium] CoT/few-shot ambiguity judgment makes 7-14B models WORSE
  (overconfidence, CLAMBER). Do not delegate when-to-ask to the model's
  free-form reasoning. A CLAM-style logprob classifier ("This question is
  ambiguous: True/False", threshold on P(True)) is within our model's
  reach if a model-side signal is ever wanted; retrieval structure is
  cheaper and stronger.

### 2.4 Conversation memory

- [strong] The published architecture is rewrite-then-retrieve: history is
  consumed by a query-rewriting step that produces a standalone question
  ("rewrite the message to be a standalone question that captures all
  relevant context"); the answer prompt then gets fresh retrieval. History
  is structurally incapable of being cited because it never enters the
  documents block. Our existing `_retrieval_query` re-anchoring is a
  primitive version of this; the research supports promoting it to a real
  rewrite step (explicit noun phrases, preserve domain terms, resolve
  pronouns from the last turns).
- [strong] Multi-turn degradation is real and large: -39% average vs
  single-turn across 15 models (Microsoft/Salesforce 2025); models commit to
  early assumptions and do not recover. Mitigations: restate the resolved
  state each turn (the rewrite does this), re-retrieve every factual turn,
  never let prior synthesized answers into the documents block (knowledge-
  conflict anchoring bias), cap raw history and carry a running summary of
  user goal + resolved referents (not facts).
- Current rule 10 ("context, NOT a source") is validated by the literature
  and stays -- but its scope widens from pronoun resolution to goal
  tracking, per the owner contract.

### 2.5 Prompting the actual models we serve (Gemma 3 / Qwen 3.x)

- [strong] Gemma 3 has NO system role: "the system role or a system turn is
  not supported" (official). The HF chat template silently folds system
  content into user turn 1. Consequence: our "system prompt" has zero
  architectural privilege on Gemma; it is a user-turn prefix. The contract
  must be short, re-rendered every turn, with the critical rule repeated at
  the tail (primacy + recency both documented; SysBench shows multi-turn
  adherence decay).
- [strong] Negative instructions are weak at open-model scale (documented
  direction; exact 77%/100% violation figures are secondary-sourced). Write
  positive if-then routing: "If the reply contains a factual claim about the
  corpus: cite it. Otherwise: no citation." Pair any prohibition with the
  replacement behavior.
- [strong] Few-shot has outsized benefit at small scale (gaps >10% for
  Qwen3-8B-class where frontier models gain 1-3%). Include one worked
  example per mode (conversational turn, cited answer, clarify turn) -- and
  positive exemplars only; showing labeled bad outputs risks activating
  them.
- [strong] Small models should not free-decide retrieval as open-ended tool
  calling (BFCL irrelevance failures; budget-aware active-RAG shows
  retrieval-decision policies are poorly calibrated and thresholds do not
  transfer). The convergent pattern: single agent whose per-turn decision is
  a schema-constrained mode field with deterministic downstream handling
  (LangGraph's generate_query_or_respond is the canonical shape). This is
  the midpoint between our old gated pipeline and a free agentic loop.
- [medium] Official sampling guidance conflicts with our temperature 0.0:
  Gemma 3 team recommends temp 1.0 / top_k 64 / top_p 0.95 for quality;
  Qwen3 says "DO NOT use greedy decoding" (repetition risk). Our
  `_SYNTH_TEMPERATURE = 0.0` is currently declared an invariant for
  determinism. Under natural-prose output this becomes a real decision:
  deterministic-but-stilted vs sampled-but-natural. Recommend: low nonzero
  (0.2-0.4) for conversational prose, keep 0.0 for the mode envelope and
  extraction calls; validate against the repetition failure mode.
- [strong] Qwen3.5 removed the /no_think soft switch; thinking is
  serving-layer only (chat_template_kwargs). Explains why the qwen35-122b
  reasoning floor could not be prompted away (Aug 4 probe). If that model
  ever serves synthesis, the floor is a cost floor per call.
- [strong] Guardrail placement: the only layer with strong published numbers
  is deterministic post-hoc validation (Llama Guard line of work: a small
  dedicated checker beats prompting a generalist). System prompts are UX
  steering, not an invariant boundary, on models without instruction-
  hierarchy training -- and Gemma has no privileged channel at all. This
  independently validates keeping turn_gate-style checks while relaxing the
  conversational gates: INV-1 lives in the validator, not the prompt.

## 3. Proposed prompt architecture (synthesis)

One conversational agent, two model calls maximum per turn, deterministic
validators around it. Retrieval becomes a tool available to the
conversation, not permission to have one.

### 3.1 Turn flow

```
user turn
  |
  v
[rewrite + route call]  (small, schema-constrained -- the good JSON quadrant)
  in:  contract header + running goal summary + last N turns + question
  out: { standalone_question, mode: converse | lookup | lookup_clarify }
       (mode is advisory; deterministic checks below can override)
  |
  +-- converse: no retrieval needed and no factual FDA claim implied
  |     -> [respond call] natural prose, no documents block, no citations
  |
  +-- lookup (default whenever the turn may depend on the corpus):
  |     retrieve BROADLY on standalone_question (resolver output becomes a
  |     filter hint, never a gate)
  |     group passages by (ingredient, dosage form, route)
  |       1 group, scores adequate -> [respond call] with documents block
  |       multiple groups, answers would differ -> either answer across
  |         facets (few) or ask ONE question naming the retrieved
  |         candidates (this is lookup_clarify, decided deterministically
  |         from group structure -- NOT by asking the model if it is
  |         ambiguous, per 2.3)
  |       all scores weak -> conversational "what I could not find" +
  |         nearest candidates (the old low_top_score refusal, humanized)
  |
  v
[respond call] natural prose; SOURCE FACTS carry inline [n] markers;
  REASONING opens with a templated frame; CONVERSATION is plain text
  |
  v
server-side parse -> claims[] derived by parsing -> citation validator
  (turn_gate as CORRECTOR: verify each [n] span against its chunk;
   fix wrong cite if a match exists, else downgrade the sentence to framed
   reasoning or drop it; whole-answer rejection reserved for material
   claims, as today)
```

### 3.2 Draft system contract (GROUNDED_QA v6 direction, not final copy)

Design rules applied: short (low tens of rules), positive if-then routing,
one exemplar per mode, tail repetition, no reliance on system-role
privilege, ASCII.

```
You are RegWatch, a research colleague for a generic-drug regulatory team.
You have an FDA guidance corpus available through retrieval. You converse
naturally; retrieval is a tool you use whenever a factual claim about FDA
guidance is needed.

How to write your reply:
1. If a sentence states what FDA guidance says or requires, it is a SOURCE
   FACT: support it with the provided passages and mark it with [n] right
   after the sentence, citing the smallest sufficient set. Test: if
   "According to the guidance, <sentence>" makes sense, it needs [n].
2. If a sentence is your analysis beyond the passages, open it with a frame
   such as "The guidance does not state this directly; my reading is ..."
   and carry no [n].
3. Everything else -- greetings, offers, transitions, questions back to the
   user -- is conversation: plain text, never [n].
4. If the passages do not support a fact the user asked for, say plainly
   what you could not find and what you did find nearby. Do not guess, and
   do not pretend the corpus answered.
5. Ask at most one short clarifying question per reply, only when the
   missing detail changes the factual answer, and name the concrete
   candidates you were given ("I found guidance for X and Y -- which one?").
   Try to address even an ambiguous question before asking.
6. Passages can span different products or dosage forms. Never blend them
   into one answer silently: either separate them explicitly or ask.
7. The recent-conversation block tells you what the user is referring to
   and what their goal is. It is not a source: facts come only from the
   passages, including facts you stated in earlier turns.

Before you reply, decide sentence by sentence: FDA fact -> cite [n];
analysis -> framed, no cite; conversation -> plain. One question maximum.
```

Plus (per 2.5): one worked exemplar per mode appended in few-shot position,
and the final two lines re-rendered at the tail of every turn's user
message, since Gemma folds all of this into the user turn anyway.

### 3.3 Output format decision

- Respond call: free natural prose with inline [n] markers. No JSON.
- Route call: strict json_schema, three fields (standalone_question, mode,
  optional product_hint). Classification-shaped, the good quadrant.
- Server derives claims[] by sentence-splitting + marker parsing (the
  existing `common/sentences.py` splitter already makes one claim = one
  rendered sentence a shared definition). Audit rows, frontend contract and
  turn_gate keep consuming claims[]; the model never sees the schema.
- Fallback if parse quality disappoints: NL-to-Format second call (2.2),
  which is still cheaper than today's malformed_structure retry loop.

### 3.4 What each old gate becomes

See 1.2. Summary: 4 die, 8 become advisory signals into the flow above, 8
stay hard. The 0.30 threshold survives as the evidence-strength signal that
routes between "answer", "answer with gaps named", and "clarify with
candidates" -- it stops being a refusal trigger. Its value remains
provisional (docs/DECISIONS.md:207) and should be re-swept once the response
policy changes, because the cost of a false-low changes from "user blocked"
to "agent says it could not find something it had".

### 3.5 Safety model after the change

INV-1 moves fully into the deterministic layer (where the research says it
belongs): every [n] span verified against its chunk post-hoc; unsupported
SOURCE FACTS get corrected, downgraded to framed reasoning, or dropped;
material-language drops still reject the turn. The prompt's job is voice
and epistemic framing; the validator's job is truth-to-source. The
scope-warning gate dies, but INV-3-as-amended survives as prompt rule 2 +
the existing cited-recommendation discipline: strategy talk is allowed,
clearly framed as reasoning, with the FDA-fact substrate cited.

## 4. Consequences for tests and eval (work list, not yet done)

1. GROUNDED_QA_PROMPT version bump to 6; new sha256; `tests/
   test_prompt_eval_sets.py` and audit-row assertions update.
2. `faithfulness` metric redefinition: from "fraction of sentences cited"
   to "fraction of SOURCE-FACT sentences cited" -- requires the parser's
   epistemic tags. citation_precision (blocking, 0.74) is unaffected in
   definition but must be re-measured under prose+markers output.
3. refusal_accuracy: complete the already-sanctioned removal (16 gold rows,
   run_eval.py note of 2026-08-06).
4. prompt_eval sets: qa.jsonl expected_turn_type vocabulary changes;
   guidance.jsonl next-step vocabulary shrinks as gates die; the
   PARTIAL_EVIDENCE_PREFIX byte-pin moves or dies with turn_gate's new
   corrector role.
5. The ~15 test files pinned to the gate table (1.2) partition the same
   way the gates do: tests for dying gates are deleted with their gates,
   tests for advisory signals are rewritten around the new flow, tests for
   hard gates survive mostly intact.
6. New tests the research says to write: AIS-test compliance (uncited
   FDA-fact sentence is caught by the validator), clarify-question quality
   (question names only retrieved candidates -- the hallucinated-facet
   failure), one-question cap, history-not-evidence (existing
   test_conversational_memory.py pattern survives), refusal-phrasing A/B
   stability (2.1), repetition check if temperature moves off 0.0.

## 5. Open decisions for the owner

1. Temperature for the respond call: stay 0.0 (deterministic, repetition
   risk on Qwen, stilted) vs 0.2-0.4 (natural voice, loses byte-for-byte
   replay). The audit row can store the sampled text either way.
2. Two calls per turn (route + respond) vs one call with a mode field:
   two calls match the evidence better (classification JSON separated from
   prose) and the route call is small; but it is +1 Databricks call per
   turn against shared QPS (CI eval collision memory applies).
3. How far "conversational" goes on day one: converse mode with no
   retrieval is the biggest INV-1 surface change (uncited text by design).
   Option A: ship lookup-always first (every turn retrieves, prose output,
   selective citation), add true converse mode second. Option B: ship the
   full flow at once. A is smaller and de-risks the parser/validator.
4. Whether the SFT path (Trust-Align recipe on the existing data/finetune/
   machinery) is worth queuing after the prompt layer stabilizes.

## 6. Primary sources

Production: Anthropic Citations API and reduce-hallucinations docs
(platform.claude.com); Cohere Command R model card + prompting guide
(docs.cohere.com, huggingface.co/CohereLabs); Gemini grounding
(ai.google.dev); Claude published system prompts (platform.claude.com/docs/
en/release-notes/system-prompts); OpenAI Model Spec (model-spec.openai.com);
Databricks FMAPI + structured outputs (docs.databricks.com); Gemma prompt
structure (ai.google.dev/gemma); Qwen3/3.5 model cards (huggingface.co/Qwen).

Papers (load-bearing): AIS (Rashkin et al., CL 2023); ALCE (2305.14627);
Liu et al. verifiability audit (2023.findings-emnlp.467); Self-RAG
(2310.11511); Sufficient Context (2411.06037); Trust-Align (2409.11242);
FRONT (2024.findings-acl.838); LongCite (2409.02897); CiteFix (ACL 2025
Industry); RARR (2210.08726); CoVe (2309.11495); "According to..."
(2024.eacl-long.140); Let Me Speak Freely (2408.02442); StructuredRAG
(2408.11061); IFScale (2507.11538); SysBench (2408.10943); CLAM
(2212.07769); Knowing but Not Showing (2605.25284); Clarify When Necessary
(2311.09469); ECLAIR (2503.15739); CLARINET (2405.15784); Corpus-informed
CQ (2409.18575); Tree of Clarifications (2023.emnlp-main.63); MIMICS
(2006.10174); STaR-GATE (2403.19154); ACT (2406.00222); Lost in Multi-Turn
(2505.06120); Knowledge Conflicts survey (2403.08319); Adaptive-RAG
(2403.14403); BFCL (openreview 2GmDdhBdDk); instruction hierarchy (OpenAI);
Llama Guard (2312.06674, 2411.17713).

Flagged as unverified in the underlying memos: Perplexity prompts (leaked,
unofficial); exact negation-violation percentages (secondary); Databricks
json_object "mention JSON" precondition absent from current public docs
(but enforced in practice per our own issue #162); qwen35 reasoning floor
is our in-house measurement only; several 2026 arXiv numbers were read at
abstract/summary level.
