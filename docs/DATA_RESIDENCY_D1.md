# Data Residency Decision (D1): Getting Amneal User-Query Text Off Public OpenAI

Status: OPEN decision. Owner: Amneal IT/Legal + regwatch eng. Date drafted: 2026-06-26.

This is a decision doc, not a code change. It pins exactly where Amneal user-query
text leaves to OpenAI today, lays out four options with tradeoffs, recommends one,
and gives legal/IT a concrete checklist to close the blocker.

Blocker history: D1 is tracked in `docs/ROADMAP.md` (line ~29, "LLM / data-handling
decision", flagged as "the longest pole -- start it first") and `docs/PROD_READINESS.md`
(item #5). Both currently frame the fix as "BAA / zero-retention vendor agreement, or
an in-house OpenAI-compatible model" and explicitly call it a "business/compliance call,
not code." `docs/DECISIONS.md` records the original provider choice
(default `openai`, embeddings switched to OpenAI `text-embedding-3-small` for prod on
Jun 12 2026) and notes "the production model and the on-prem question are the IT team's
call." This doc is the structured version of that decision.

---

## 1. Where user-query text leaves to OpenAI today (exact exfil points)

regwatch sends the **user's raw query string** to OpenAI at TWO points on the live
Ask/Q&A path. Both are live in prod: `fly.toml` sets `EMBEDDING_PROVIDER = "openai"`
and `config/settings.py:36` defaults `llm_provider = "openai"`.

### Exfil point A -- query EMBEDDING (high volume, every query, cheap)

- Call site: `src/regwatch/retrieve/retriever.py:159-160`
  ```python
  embedder = get_embedding_provider()
  qv = embedder.embed([query])[0]
  ```
  `query` is the verbatim user question. `retrieve()` is invoked on the Ask path before
  synthesis.
- Network egress: `src/regwatch/process/embedder.py:179`
  ```python
  return client.embeddings.create(model=self.model, input=batch)
  ```
  where `self.model = "text-embedding-3-small"` (embedder.py:136) and the client is a
  real `openai.OpenAI` (embedder.py:151-159, via `regwatch.common.llm_clients.shared_openai_client`).
- **What leaves:** the full raw query text, verbatim, as the embedding `input`, to
  `api.openai.com`. Nothing is redacted or hashed before the call (the SHA-256 in
  embedder.py:70 is only the *local* bge cache key path; the OpenAI path sends plaintext).
- **Volume:** one embedding call per user query. This is the high-frequency, low-cost
  exfil channel.

### Exfil point B -- SYNTHESIS / answer generation (lower volume, richer payload)

- Prompt assembly: `src/regwatch/generate/grounded_qa.py:1331-1334`
  ```python
  user_prompt = GROUNDED_QA_USER.format(
      question=question,
      passages=_format_passages(passages),
  )
  ```
  `GROUNDED_QA_USER` (`src/regwatch/generate/prompts.py:43-50`) embeds the user's
  `question` literally as `Question: {question}` and appends the retrieved FDA passages.
- Network egress: `src/regwatch/generate/grounded_qa.py:1338-1347`
  ```python
  provider = get_llm_provider(role="synthesizer")
  response = provider.complete([
      LLMMessage(role="system", content=system_prompt),
      LLMMessage(role="user", content=user_prompt),
  ], temperature=0.0, max_tokens=900)
  ```
  which lands in `OpenAIProvider._complete_responses` -> `client.responses.create(...)`
  (`src/regwatch/generate/llm.py:193`) or the legacy `client.chat.completions.create(...)`
  (llm.py:226), depending on `OPENAI_API_MODE` (default `"responses"`).
- **What leaves:** the user's question (verbatim, inside `user_prompt`) PLUS the retrieved
  FDA guidance passages, to `api.openai.com`. The passages are public FDA text, so they are
  not the residency concern; the **query is**. Note that the same `OpenAIProvider` is also
  used for the router and the BE extractor (`_model_for_role`, llm.py:289-299), and the
  whitepaper populator -- any path that sends the user's text shares this channel.
- **Volume:** one synthesis call per answered query (router/extractor calls add more, but
  the synthesis call is the one that always carries the user's free-text question).

### Models in play

- Embeddings: `text-embedding-3-small` (1536-dim), hard-coded in embedder.py:136 and locked
  to the `vector(1536)` chunk column (asserted at startup; see DECISIONS.md Jun 12 entry).
- Synthesis/router/extractor default to the `gpt-5-nano` family (`config/settings.py:43-46`:
  `llm_model`/`synthesizer_model`/`extractor_model = "gpt-5.4-nano"`, `router_model = "gpt-5-nano"`).
  **UNVERIFIED:** the model actually running in prod may be overridden by a Fly secret
  (`SYNTHESIZER_MODEL` / `LLM_MODEL`); the repo default is the floor, not a guarantee. The
  price table in `config/settings.py:67-71` ($0.05 in / $0.40 out per 1M tokens) is a
  placeholder default and is itself flagged env-overridable -- **treat all cost figures below
  as estimates to confirm against the actual contract and live model.**

### What is NOT the concern

The daily Watch pipeline embeds **public FDA source documents** (PSG PDFs). That text is
public, so sending it to OpenAI is not a residency violation. Only the **user-query path**
(points A and B) is in scope for D1. Fixing D1 does not require changing how FDA docs are
embedded -- though note the chunk table is 1536-dim, so changing the *query* embedder away
from `text-embedding-3-small` forces re-embedding the entire corpus with the new model (the
query and corpus embedders must match; see Section 4C).

---

## 2. The requirement

Amneal states Amneal user-query text **must not leave to OpenAI** (public OpenAI). The
ambiguity legal/IT must resolve (Section 5) is *what "must not leave" means*:

- (i) "must not be **retained or trained on** by OpenAI" -> a contractual zero-retention
  term satisfies it (Option A), cheapest path, no infra change.
- (ii) "must stay in **Amneal's cloud tenant / chosen region**, never touching the public
  OpenAI multi-tenant service" -> needs Azure-in-tenant (Option B) or self-host (Option C).
- (iii) "must never leave Amneal-controlled **infrastructure** at all" (true on-prem /
  air-gap) -> forces self-host (Option C).

The right option depends entirely on which of these three it is. Get that answer first.

---

## 3. Provider data-handling facts (verify against the live contract)

These reflect publicly documented offerings as of mid-2026. **Verify each against the
signed agreement before relying on it** -- terms change and are account-specific.

**Public OpenAI API (today's prod path):**
- Default: OpenAI may retain API inputs/outputs for **up to 30 days** for abuse/misuse
  monitoring, then deletes them (unless legally compelled). API data is **not** used to
  train models by default.
- **Zero Data Retention (ZDR):** available to **eligible enterprise customers** on
  **eligible endpoints**, enabled by the OpenAI account team -- not a self-serve toggle.
  ZDR removes the 30-day retention and excludes content from abuse-monitoring logs.
  **VERIFY:** that BOTH endpoints regwatch uses (`/v1/embeddings` and the Responses API /
  Chat Completions) are ZDR-eligible for Amneal's account, and that ZDR is contractually
  bound, not best-effort.

**Azure OpenAI Service (Microsoft tenant):**
- Same underlying models (incl. `text-embedding-3-small`-class embeddings and GPT-class
  synthesis), served from Microsoft Azure under the customer's Azure subscription.
- Prompts, completions, and embeddings are **not** shared with OpenAI and **not** used to
  train foundation models without explicit customer instruction.
- Deployment residency tiers: **Standard (single-region)** pins processing to the chosen
  Azure region; DataZone pins data-at-rest to a named geo; Global pins only data-at-rest.
- Covered by the **Microsoft BAA/DPA** (HIPAA, SOC 2 Type 2, ISO 27001, FedRAMP available).
- **Abuse-monitoring caveat -- VERIFY:** by default Azure OpenAI may **store prompts/responses
  up to 30 days** for abuse monitoring, with Microsoft staff able to review flagged content.
  Customers can apply for **Limited Access "modified abuse monitoring" / no-content-logging**
  (the Azure ZDR-equivalent) to disable that logging. Without that approval, "in-tenant"
  still has a 30-day Microsoft-side store. This is the Azure analog of OpenAI ZDR and must be
  requested the same way.

Net: Azure-in-tenant gives a stronger residency story (data stays in Amneal's Azure
subscription/region, under an existing Microsoft enterprise + BAA relationship) **but** has
the same abuse-logging footnote as public OpenAI unless the modified-monitoring exception is
granted.

---

## 4. Options

Scored on six axes: residency guarantee / eng-effort / latency / answer-quality / cost /
ongoing-ops. Quality and cost figures are directional; confirm against the live contract.

### Option A -- OpenAI enterprise zero-retention DPA (contractual only)

Stay on `api.openai.com`; sign an enterprise agreement with ZDR enabled on the embeddings
and synthesis endpoints. **No code change** (provider stays `openai`).

- Residency: data still **transits and is processed on** public OpenAI multi-tenant infra;
  ZDR means it is not **retained** or used for monitoring/training. Satisfies requirement
  (i). Does **NOT** satisfy (ii) "stays in our tenant/region" or (iii) on-prem.
- Eng effort: ~zero. Possibly point the base URL at a ZDR-scoped org; no provider swap.
- Latency: unchanged (current prod baseline).
- Quality: unchanged (same models regwatch is tuned and eval-gated on today).
- Cost: unchanged API usage cost; enterprise commit/minimums may apply. No infra to run.
- Ongoing ops: none beyond today. OpenAI owns uptime, scaling, model updates.
- Risk: depends on a contract term, not a network boundary; if "must not leave our
  tenant/infra" is the real requirement, A does not clear it. Eligibility and endpoint
  coverage are account-specific (verify).

### Option B -- Azure OpenAI in Amneal's own tenant + region

Provision Azure OpenAI in Amneal's Azure subscription, region-pinned (Standard/single-region),
under the Microsoft BAA. Same model classes. Add an Azure-OpenAI client behind the existing
`LLMProvider` / `EmbeddingProvider` interfaces.

- Residency: data stays in **Amneal's Azure subscription and chosen region**; not shared
  with OpenAI; not used for training. Satisfies (i) and (ii). Closest practical answer to
  (iii) without running models yourself (Microsoft still operates the hardware, but inside
  Amneal's tenant boundary under BAA). Apply for modified abuse monitoring to also kill the
  30-day Azure-side log (Section 3).
- Eng effort: **low-moderate.** The provider seam already exists
  (`src/regwatch/generate/llm.py:302` `get_llm_provider`, `src/regwatch/process/embedder.py:242`
  `get_embedding_provider`; CLAUDE.md hard-rule #5 keeps these sacred). Add an
  `AzureOpenAI`-backed branch to each factory + config for endpoint/deployment names/API
  version. The `openai` SDK supports `AzureOpenAI` with the same `embeddings.create` /
  `responses.create` surface, so the call-site code is nearly unchanged. Effort is config,
  a thin client class, deployment provisioning, and re-running the eval gate.
- Latency: comparable to public OpenAI, often better if the Azure region is near Amneal/Fly
  (`primary_region = "iad"`). Confirm a region pairing close to the app.
- Quality: same model classes -> expect parity. **Re-run the offline eval gate**
  (`tests/test_eval_gate.py`, `refusal_accuracy >= 0.95`) and the threshold sweep against
  Azure, because the exact model snapshot/version can differ from public OpenAI and the 0.30
  refusal threshold is already flagged unvalidated in the prod vector space
  (`docs/THRESHOLD_VALIDATION_2026-06-25.md`).
- Cost: Azure OpenAI token pricing is broadly comparable to public OpenAI (verify; can run
  10-20% different by model/region and commitment). No GPU fleet to own.
- Ongoing ops: low. Microsoft owns uptime/scaling/patching; Amneal owns the Azure
  subscription, quota, and key rotation. Adds an Azure dependency to a stack currently on
  Fly + Supabase + Vercel.
- Embedding caveat: if the Azure embedding deployment is `text-embedding-3-small` (1536-dim),
  the existing `vector(1536)` corpus is reusable as-is. If a different embedding model is
  used, the whole corpus must be re-embedded (Section 4C).

### Option C -- Self-host embeddings (BGE/E5) + self-host / in-tenant open LLM for synthesis

Run an open embedding model (e.g. BGE / E5; the code already ships a local BGE provider,
`LocalBgeSmallProvider`, embedder.py:33) and an open-weights LLM (Llama/Mistral-class) for
synthesis on Amneal-controlled infra (in-tenant GPU or on-prem).

- Residency: **strongest.** Query text never leaves Amneal infra. Satisfies (i), (ii), and
  (iii) including true on-prem/air-gap. This is the only option that clears a hard
  "never leaves our infrastructure" requirement.
- Eng effort: **high.** Embeddings are the easy half -- a local BGE provider already exists
  (used as the dev default). The hard half is **production synthesis**: stand up a served
  open LLM (vLLM/TGI + GPU), wire a provider behind `LLMProvider`, then **re-validate
  grounding quality**. regwatch's whole value rests on INV-1/INV-2 (cited, no-fabrication)
  answers; an open model must hold the citation discipline the GPT-class synthesizer was
  tuned against. Expect prompt-tuning and a full eval re-run, not a drop-in.
- Latency: depends entirely on hosted hardware. Risk of being **worse** than managed unless
  well-provisioned; cold models and queueing hurt p95.
- Quality: **highest risk axis.** Open embeddings (BGE/E5) are competitive for retrieval.
  Open synthesis LLMs *can* match GPT-class on grounded summarization but typically need more
  prompt engineering and may regress on strict citation faithfulness. Must clear the same
  eval gate; budget for it to initially fall short.
- Cost: **highest fixed cost.** A GPU good enough for low-latency synthesis runs ~thousands
  of USD/month reserved (or large on-demand), dwarfing this app's token spend at current
  volume. You trade a small variable bill for a large fixed one. **VERIFY** against actual
  query volume -- at low volume, self-hosting is the most expensive option, not the cheapest.
- Ongoing ops: **highest.** Amneal now owns model serving, GPU capacity, scaling, security
  patching, model upgrades, and incident response for the LLM -- a standing platform burden
  on a small team. This is a permanent operational commitment, not a one-time build.

### Option D -- Hybrid: self-host embeddings + contractual/Azure synthesis

Kill the high-volume cheap exfil (point A) by self-hosting embeddings, and cover the
lower-volume richer synthesis (point B) via Option A (ZDR) or Option B (Azure).

- Residency: removes the **most frequent** egress (one embed call per query) from public
  OpenAI entirely. Synthesis residency is then whatever A or B gives. **Partial** unless the
  synthesis half is also Azure/ZDR -- and note point B still sends the **full query text** to
  the synthesizer, so if the requirement is "the query string must never reach OpenAI at all,"
  D alone does NOT clear it (the query is in `user_prompt`). D shrinks the exfil **surface
  and frequency**, it does not by itself remove the query from the synthesis call.
- Eng effort: **moderate.** Embedding self-host reuses the existing local BGE provider (low
  effort), but switching the **query** embedder off `text-embedding-3-small` forces
  re-embedding the corpus (Section 4C). Plus the A-or-B work for synthesis.
- Latency: local embeddings on CPU/MPS are fine at this corpus size; removes one network
  round-trip per query (can be a small win). Synthesis latency follows A/B.
- Quality: embedding retrieval quality shifts (1536 OpenAI -> 384/1024 open model);
  re-validate retrieval recall on the gold set. Synthesis quality follows A/B.
- Cost: drops embedding token spend to ~zero (compute already paid for); synthesis cost
  follows A/B. Cheaper than C (no GPU needed for CPU embeddings), but only worth it if the
  embedding exfil specifically is the thing that must stop.
- Ongoing ops: low-moderate -- one self-hosted model (embeddings, CPU-friendly) instead of
  two. Avoids the GPU burden of C while removing the highest-frequency egress.

---

## 5. Recommendation

**Decide the requirement first (Section 2), then:**

- If "must not be **retained/trained on**" (interpretation i): **Option A** -- OpenAI
  enterprise ZDR DPA. Zero code change, zero quality/latency/ops cost, no infra. Cheapest
  path that honors a contractual-retention requirement.
- If "must stay in **Amneal's tenant/region**" (interpretation ii -- the most likely reading
  of "must not leave to OpenAI" for a regulated pharma): **Option B** -- Azure OpenAI
  in-tenant, region-pinned, under the Microsoft BAA, with modified abuse monitoring requested.
  Same model classes -> preserves quality and latency; low-moderate eng effort because the
  provider seam already exists; Microsoft owns the ops. **This is the recommended default**
  for a regulatory team that wants data inside its own cloud boundary without taking on a
  model-serving platform.
- If "must **never leave Amneal infrastructure**" (interpretation iii, true on-prem/air-gap):
  **Option C** -- self-host. It is the only option that clears that bar, but it is the most
  expensive on cost, latency-risk, quality-risk, and ongoing ops. Choose it only if a hard
  infra boundary genuinely forces it.

**General principle:** managed/contractual (A) or in-tenant managed (B) beats DIY (C) on
cost, quality, latency, and ops at this app's volume. **Only a hard data-residency
requirement justifies self-hosting.** Do not self-host to save money -- at current query
volume, self-hosting synthesis is the *most* expensive option, not the cheapest.

**Option D** is the right move only if the embedding exfil *specifically* (the high-volume
point A) is the binding concern while synthesis can stay contractual/in-tenant -- a narrow
case. For most readings of the requirement, B is simpler and stronger than D.

This recommendation does not change behavior today; it is the input to the legal/IT
decision below. No provider should be switched until that decision is logged in
`docs/DECISIONS.md` (per ROADMAP D1 "done when").

---

## 6. Decision checklist for Legal / IT

Close D1 by answering these in order. Log the outcome in `docs/DECISIONS.md`.

**Define the requirement (blocks everything else):**
- [ ] Which interpretation binds: (i) no-retention/no-training, (ii) stay-in-our-tenant/region,
      or (iii) never-leave-our-infrastructure? Get this in writing.
- [ ] Is the user **query string** itself classified (confidential/CBI), or only the answer?
      (Both A and B still transmit the query; only C/full-on-prem keeps it entirely in-house.)
- [ ] Any jurisdiction/region constraint on where data may be processed (US-only, EU, etc.)?

**If Option A (OpenAI ZDR):**
- [ ] Confirm Amneal qualifies for an enterprise agreement with ZDR.
- [ ] Confirm BOTH endpoints regwatch uses are ZDR-eligible: `/v1/embeddings` AND the
      Responses API (or Chat Completions if `OPENAI_API_MODE=chat`).
- [ ] Get ZDR + "no training" + retention terms **in the signed contract**, not a sales email.
- [ ] Confirm sub-processor list and data-flow are acceptable to compliance.

**If Option B (Azure OpenAI in-tenant) -- recommended default:**
- [ ] Confirm an Azure subscription/tenant Amneal controls, with an approved region close to
      the app (`primary_region = iad`).
- [ ] Confirm the needed model deployments exist in that region: an embedding model
      (ideally `text-embedding-3-small`, 1536-dim, so the corpus is reusable) and a
      GPT-class synthesizer comparable to today's `gpt-5.x-nano`.
- [ ] Confirm the **Microsoft BAA/DPA** covers Azure OpenAI for Amneal's licensing.
- [ ] Apply for **Limited Access / modified abuse monitoring** (no content logging) if the
      default 30-day Azure-side abuse log is unacceptable.
- [ ] Confirm token pricing in-region vs current OpenAI spend (verify the placeholder
      price table in `config/settings.py:67-71` against reality).
- [ ] Eng: add Azure branch to `get_llm_provider` + `get_embedding_provider`, then re-run the
      eval gate (`tests/test_eval_gate.py`) and threshold sweep
      (`docs/THRESHOLD_VALIDATION_2026-06-25.md`) before cutover.

**If Option C (self-host) or Option D (hybrid):**
- [ ] Confirm GPU budget/capacity for production synthesis (Option C) -- this is a standing
      fixed cost; size it against actual query volume.
- [ ] Pick embedding model (BGE/E5) and accept the corpus re-embed (Section 4C); confirm
      dimension and update the `vector(N)` chunk column / migration accordingly.
- [ ] Pick the open synthesis LLM and budget time to re-validate INV-1/INV-2 citation
      faithfulness on the gold set; do not assume drop-in parity.
- [ ] Define ownership for model serving uptime, scaling, patching, and upgrades.

**Cross-cutting verifications flagged in this doc (do not skip):**
- [ ] Confirm the **actual synthesizer model** running in prod (Fly secret may override the
      `gpt-5.4-nano` repo default) before pricing/quality assumptions.
- [ ] Re-validate the 0.30 refusal threshold in whatever new vector space is chosen -- it is
      already unvalidated in the current prod OpenAI-1536 space.
- [ ] Treat every cost figure in Section 4 as an estimate; confirm against the signed contract
      and live token volume.

### 4C note -- corpus re-embed coupling (applies to B-with-different-embedder, C, D)

The query embedder and the corpus embedder MUST produce vectors in the same space and
dimension (the chunk table is `vector(1536)` and startup asserts provider-dim == table-dim;
DECISIONS.md Jun 12). So any option that changes the **query** embedding model away from
`text-embedding-3-small` requires **re-embedding the entire FDA corpus** with the new model
(and a migration to the new `vector(N)` dimension). Re-embedding public FDA docs is not a
residency problem -- it is an eng cost (compute + a migration + re-validating retrieval
recall on the gold set). Budget for it. Option A (and Option B if it uses the same
1536-dim embedder) avoids this entirely.
