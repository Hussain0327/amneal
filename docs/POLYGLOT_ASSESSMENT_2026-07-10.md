# Polyglot Assessment - 2026-07-10 (follow-up to the 2026-06-17 review)

**Question:** "I want to make this a polyglot system - what can we do?"

**Method:** 21-agent fleet against the live tree (branch `fix/assemble-audit`):
6 read-only mappers (prior review, hot paths, ingest/crawl, deploy topology,
contracts, D1 residency) -> 5 proposal lenses (perf-native, ops-binary,
residency, product-edge, contrarian) -> merge to an 8-candidate slate -> one
adversarial verifier per candidate -> completeness critic. Every verdict below
cites code the verifier read itself.

---

## 1. What changed since the 2026-06-17 review

The old review said "no new language; do these in-language items first."
Those items are now mostly **done**, which makes the question fresh again:

| Jun 17 prescription | Status Jul 10 |
|---|---|
| Durable alerts in Postgres | SHIPPED (`migrations/0009_durable_alerts.py`) |
| Daily watch running in prod | SHIPPED (GH Actions `watch-daily.yml`, cron 07:17 UTC) |
| api.ts phantom fields / contract codegen | SHIPPED for `/query` (generated OpenAPI types + CI drift check) |
| Real token streaming in Python | SHIPPED (Jul 1) |
| ask() decomposition | NOT done (`grounded_qa.py` ask() now 1,604 lines) |
| Async crawl (`crawl_concurrency`) | NOT done; Jul-9 backlog says delete the dead knob |

New facts the old review did not have:

- **D1 went hard** (Jun 19): Amneal query text cannot leave to OpenAI. Two live
  exfil points: query embedding (`retriever.py:196-197` -> `embedder.py:179`)
  and synthesis (`grounded_qa.py:1429-1455` -> `llm.py:284/337`). This is the
  only *active requirement* that could ever justify new runtimes.
- **Whitepaper runs shipped Jul 7**, which *fired* the old review's own gate for
  extending contract codegen ("next model-field change or api.ts > 25-30
  interfaces").
- Ask already runs on a dedicated 16-token anyio limiter; nothing blocks the
  event loop. Assemble/whitepaper ride the shared ~40-token pool as sync
  handlers with **no request-level deadline**.
- The pgvector chunk table is hard-locked to `vector(1536)` (OpenAI-only);
  any embedder change forces a full corpus re-embed + migration.
- No `base_url` override exists anywhere in the client layer
  (`common/llm_clients.py:19-27` caches on api_key/timeout/max_retries only).

## 2. Slate verdicts (8 candidates, adversarially verified)

**STRONG (do now - both are "deepen the seams", not new languages):**

1. **Extend the contract codegen across the whole API surface** (TS+Python seam).
   `POST /whitepaper` returns `dict[str, Any]` with no `response_model` while
   `api.ts` hand-mirrors its largest payload; `AssembleResponse` is duplicated
   (api.ts:42-46 shadows an identical generated shape in api-types.ts:584-593).
   The critic widened the scope: ~10 more hand-written mirror interfaces
   (AlertRecord, WatchRunSummary, WatchLatest, ProductRecord, ProductsResponse,
   PublicSettings, User, SessionSummary, ChatMessage, SessionDetail) and
   `response_model` missing on /alerts, /watch, /products, /settings,
   /sessions. Toolchain (openapi-typescript, gen:types, CI drift job) already
   exists - zero new surface, deletes a documented manual chore.
   First slice: frontend-only, `export type AssembleResponse =
   Schemas["AssembleResponse"]`. Risk to test: run-persist stores the payload
   pre-serialization (main.py:1168), so a stripping Pydantic model must not
   diverge stored run vs POST response.

2. **Finish the 4th PDF-parse guard: page-count bound** (Python, ~20 lines).
   Byte cap, %PDF magic, and the 60s spawn-child timeout exist; the page bound
   does not (`pdf_parser.py` has none; backlog 2.11). Implementation gotcha the
   verifier caught: the check runs inside the spawn child, where `_child_main`
   (pdf_parser.py:137-139) serializes exceptions to strings - the new error
   must survive that boundary, not rely on exception identity.

**CONDITIONAL (pre-decided, build only on the gate):**

3. **llama.cpp in-boundary synthesis** (C++ server, operated not written).
   Driver is real (D1 active) but gated: managed-first ordering says Azure
   Option B (pure-Python provider branch, keeps the 1536 corpus, closes BOTH
   exfil points) beats self-host for D1 readings (i) and (ii).
   GATE: legal rejects Options A and B in writing (D1 = "never leaves our
   infrastructure"), with a paired embedding plan (local embed + corpus
   re-embed + threshold re-sweep + eval gate re-run).
   Useful NOW regardless: a **per-provider base_url override seam** - small,
   pure Python, makes Azure/vLLM/llama.cpp a config swap later. Must be
   per-provider: a global override on `shared_openai_client` would mispoint
   the 1536-dim embedder too.

4. **Vercel Edge middleware session gate** (TS edge tier).
   Mechanically real (no middleware.ts; signed-out visitors download the full
   bundle then bounce after a /auth/me round trip) but tiny today: internal
   single-tenant pilot, cookie max_age = 72h session TTL. The proposed slice
   had a redirect-loop bug (present-but-invalid HttpOnly cookie the client
   cannot clear). Cheaper now: client-side deep-link preservation -
   `AuthProvider.tsx:109` -> `router.replace('/login?next=...')`.
   GATE (unchanged from Jun 17): broadly public/multi-tenant AND stateless
   signed tokens the edge can verify without a DB hit.

**REJECT (with the evidence):**

5. **ONNX Runtime in-boundary embeddings** - fails "the driver moves with the
   language": self-hosting embeddings alone still sends the verbatim question
   to OpenAI at synthesis (D1 doc says so itself), while uniquely incurring
   corpus re-embed (1536->384 migration), recall re-validation, and a
   re-sweep of the already-unvalidated 0.30 threshold. Revisit only under D1
   (iii) with self-host synthesis committed - and even then torch-CPU via the
   existing `INSTALL_LOCAL_EMBEDDINGS=true` path competes; ONNX is a packaging
   detail, not the decision.
6. **SQL prefilter for /assemble's 1,795-row scan** - proposed slice is
   *unsound*: `names_match`'s stripped-name branch fires when strip==canon, so
   an indexed `normalized_name == "albuterol"` prefilter would silently drop
   every "Albuterol Sulfate" PSG (the repo's documented ALBUTEROL shape); the
   populator survives only via appl_no predicates dossier.py lacks. Also no
   measured driver. Critic correction: the verifier's "5 sequential fetches"
   profile belongs to /whitepaper, not /assemble (dossier.py has ONE fetch,
   `_fetch_rld_label` at :404, plus nested ask() at :451) - the REJECT stands
   because the nested ask() dominates, but the recorded basis is now correct.
7. **Client-side docx preview** - the render endpoint does zero fetches/LLM
   calls; the preview would re-fetch the same blob per iteration; the proposed
   cache fingerprint excludes analyst inputs (stale previews after edits); and
   docx-preview's fidelity is worst exactly where a regulated dossier needs it
   (pagination/tables). RunView already shows every generated cell + input.
   Measure first: audit rows (`docx_rendered`) can show real iteration pain.
8. **pypdfium2 as primary extraction engine** - driver absent: first-party FDA
   PDFs only, no measured crash rate anywhere, the pypdf CVE is patched via
   the >=6.14.2 pin. It would also ship a Chromium-lineage C++ binary into the
   prod API image (which never parses a PDF) under the currently-clean Trivy
   gate. The old review's "try pypdfium2 first" was conditional on a trigger
   that has not fired. Do #2 (page bound) instead.

## 3. What the fleet missed and the critic caught

**The real user-facing latency win nobody proposed:** `populator.py:358-365`
runs 5 strictly sequential external fetch stages (each 30s timeout, DailyMed up
to 10 pages, some 3x retry) with zero concurrency in `whitepaper/` or
`assemble/`, and no request-level deadline on POST /whitepaper or /assemble -
a slow run stacks 5x30s + retries + a nested ask() while holding a shared
default-pool thread. `_fetch_orange_book` and `_fetch_drugsfda` are mutually
independent; dailymed/ndc/psg depend only on `_establish_identity`. Bounded
parallelism + an overall request budget is pure Python and matches the repo
standard ("every external call gets a timeout" - the *request* needs one too).

Also caught: the contracts-seams mapper silently returned a placeholder (its
ground was re-verified by the critic's own greps, which is where the wider
codegen scope in #1 comes from).

## 4. Recommendation

regwatch is already a three-language system (Python, TypeScript, SQL) plus an
operated C-extension tier (pgvector). The honest polyglot roadmap:

**Now (in-stack, PR-sized each, no go-ahead assumed):**
- S1: contract codegen across the full API surface (verdict #1, widened scope).
- S2: populator bounded parallelism + request deadline (critic find).
- S3: PDF page-count bound in the parse child (verdict #2).
- S4: per-provider base_url override seam (from verdict #3) - the
  polyglot-*enabling* socket that makes any future in-tenant or self-hosted
  endpoint a config swap.

**The one real 4th-runtime door: D1.** Decide the requirement interpretation
(the D1 doc's Section 6 checklist). (i)/(ii) -> Azure branch, pure Python,
corpus intact. (iii) -> the genuine polyglot program: llama.cpp/vLLM synthesis
+ local embeddings + re-embed + threshold re-sweep + eval-gate re-run.

**Still parked, gates restated:** Rust PDF worker (user uploads AND >1% crash
rate), Go crawler (>50k items/run), TS edge auth (public/multi-tenant +
stateless tokens), realtime eventing (>1k concurrent subscribers).

The 2026-06-17 four-gate framework (clean boundary / measured driver / driver
moves with the language / worth the cost) survives this re-run unchanged; what
changed is that the in-language runway it prescribed is now mostly consumed,
D1 is the single live driver pointing at new runtimes, and the cheapest way to
be ready for it is S4's seam, not an early engine bet.
