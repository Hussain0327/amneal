# Built but dormant

Code that exists in this repository but does not do what a reader would assume.

Two handoff mistakes are expensive. Rebuilding something that already works is
one. Building on something that never runs is the other. Both happened here,
because six documents each held one piece of this picture and none held all of
it.

Read this page before you plan work on retrieval, the Compliance Studio, the
knowledge graph, or anything you saw described as a residency control.

Every entry names the file and line that proves its state. Verify a claim
against the code before you act on it. If the code and this page disagree, the
code wins, and this page is the bug.

Related owners: `docs/PRODUCTION_TRUTH.md` for what serves a request today,
`docs/CONFIG_REFERENCE.md` for every flag and its default,
`docs/ROADMAP.md` for open work, `docs/DECISIONS.md` for why.

## 1. Built, wired, running, and documented as fixtures

The Compliance Studio is a three-way split, not a mockup. The boundary is
written down in the code:
`regwatch/frontend/lib/studio-fixtures.ts:1-21` is the definitive map, and
`regwatch/frontend/app/studio/page.tsx:62-64` repeats it.

### The PSG reference rail is real

`app/studio/page.tsx:25` imports `fetchPsgLibrary`, `fetchPsgContent` and
`fetchPsgRequirements` from `@/lib/api`. They are called at `page.tsx:275`,
`:320` and `:375`. They hit real, authenticated endpoints in
`src/regwatch/api/main.py`:

- `GET /psg/documents` (`main.py:2138`)
- `GET /psg/documents/{id}/content` (`main.py:2438`)
- `GET /psg/documents/{id}/requirements` (`main.py:2492`)
- `GET /psg/documents/{id}/pdf` and `HEAD` (`main.py:2303`, `:2276`)
- `GET /psg/documents/{id}/docx` (`main.py:2525`)

Open a reference PSG in Studio and you are reading the production corpus.

### The chat assistant is real, conditionally

`page.tsx:598` branches on `libraryDoc`. With a reference PSG open, the panel
calls `askQuery` (`page.tsx:600`) against `POST /query`, scoped to that PSG's
`normalized_name`, `dosage_form` and `route`, and renders the server-validated
citations. With no reference PSG open, it falls through to `assistantReply`
(`page.tsx:633`), a canned fixture. The same panel is a live retrieval client
in one state and a script in the other.

### The check endpoints are real, persisted, and not called

`POST /studio/check` (`main.py:2839`) returns 202 with a run id. It creates a
row through `deficiency_run_store.create_run` and queues
`_studio_check_background`, which runs `run_studio_check`
(`src/regwatch/deficiency/runner.py:52`). That is the same deficiency engine,
the same table and the same background machinery as the `/deficiency` upload
path; only the document source differs (`main.py:2753-2758`). The result is
polled from `GET /studio/check/{run_id}` (`main.py:2876`). Runs are private to
the analyst who made them, because the report quotes an unfinished draft.
`tests/test_studio_check_api.py` covers it.

No frontend code calls either endpoint. Grep `regwatch/frontend` for
`studio/check` and you get nothing. `page.tsx:65` defines `CHECK_MS = 1500` and
the page fakes the run with a timer.

### What is actually a fixture

From `lib/studio-fixtures.ts:1-21`, and nothing beyond it:

- the eleven working draft documents (`DOCS`) and the repository tree (`TREE`);
- the findings (`CHECK_RESULTS`), applied by the page after the fake delay;
- the canned assistant replies (`assistantReply`) about the working
  repository.

There is no drafts service and no document store behind the working set. If you
add a Studio feature, decide which side of that line it belongs on first.

### What it would take

Wiring the canvas to `POST /studio/check` and the poll is a frontend change
against an endpoint that already exists and is tested. Do not rebuild the
engine.

## 2. Built and write-only

Migration `0018_knowledge_graph` created `graph_node`, `graph_edge` and
`graph_node_chunk`. The derivation lives in
`src/regwatch/store/graph_store.py` (`derive_document_graph`, line 176), and
the tables are registered in `SQLModel.metadata` so a fresh Postgres bootstrap
creates them (`src/regwatch/store/db.py:855-862`).

Nothing reads them at runtime. Grep for `graph_node`, `graph_edge` and
`graph_store` across `src/` and `go/`: the only hits outside `graph_store.py`
itself are `cli.py` (the backfill command) and `db.py` (the bootstrap import).
There is no reader in `api/`, `retrieve/` or `generate/`.
`tests/test_retrieval_no_graph.py` enforces this by running the real
`retrieve()` with every SQL statement captured at the engine and failing on any
statement that touches a graph table, across both corpus arms.

### How they get populated today

Only one way: the `regwatch graph-backfill` CLI command
(`src/regwatch/cli.py:1043-1055`), run by hand. It derives rows from chunks that
already exist and is idempotent. It needs `chunk.ordinal` (migration 0017)
populated.

Ingest-time derivation was removed on 2026-08-18 because nothing read the
output (`graph_store.py:9-15`). In a clean database these tables are empty and
stay empty.

Caveat on a stale comment: `tests/test_retrieval_no_graph.py:3-4` still says the
ingest pipeline is a writer. It is not; `graph_store.py:9-15` and
`cli.py:1049-1052` both record the retirement. Trust the module docstring and
the CLI.

### What it would take

The tables, the derivation and the backfill are the revival path. When a
traversal consumer actually ships, backfill through the CLI first, then decide
whether to re-wire derivation into ingest. Design notes are in
`docs/GRAPH_ASSISTED_RETRIEVAL.md`.

## 3. Built and disabled code-wide

`RetrievalMode.ANN_RERANKED` is declared at
`src/regwatch/retrieve/mode.py:51`. The full SQL for it (a bounded HNSW
candidate pool, then an exact full-precision rerank) is written out in
`src/regwatch/store/embedding_profiles.py:876-901`.

It cannot execute. `assert_mode_permitted` (`mode.py:101`) raises
`ApproximateSearchNotPermitted` for that mode unconditionally, with no flag and
no environment check (`mode.py:108-112`). Every search goes through it:
`similarity_search_profile` calls it on line 925 of
`embedding_profiles.py`, after `default_mode_for_scope` (`mode.py:91-98`) has
already picked one of the two exact modes for any caller that did not choose.

So retrieval is always an exact pgvector scan. `EXACT_SCOPED` and
`EXACT_CORPUS` emit byte-identical SQL; the difference is a policy label
recorded on the audit row saying whether a product resolved
(`mode.py:22-24`). The exact path also sets `enable_indexscan = off` so an
index cannot satisfy a query that claims to be exact
(`embedding_profiles.py:871-874`).

### Why

Two reasons, and only one of them is capacity.

The correctness reason is in `mode.py:16-19`: measured against exact ground
truth on this corpus, HNSW recall@8 averaged 0.984 but bottomed out at 0.125 on
one query. Usually perfect, occasionally catastrophic, and silent either way.
On a path that decides which document gets cited to a regulator, that is not an
acceptable failure mode.

The capacity reason is the Lakebase branch cap. The branch is limited to
512 MiB, tier-fixed and not raisable through the API
(`scripts/reclaim_lakebase_space.py:1-13,32-36`;
`migrations/versions/0027_chunk_filter_indexes.py:20`). Measured headroom has
been as low as ~21 MiB. The legacy HNSW index was dropped to reclaim space, and
`fly.toml:69-71` pins `PROFILE_HNSW_INDEX_REQUIRED = "false"` so a profile
serves without one. `config/settings.py:132-133` carries the same rule: exact
retrieval needs full embedding coverage, not an index.

### What it would take

More storage than the current branch tier allows, plus a recall study that
explains the 0.125 outlier. Do not flip this to buy latency.

## 4. Built and flag-off by default

Four switches exist, default to off in code, and are not pinned on in
`fly.toml`. For each one, the evidence you should have before flipping it.

### MMR diversity

- Flag: `REGWATCH_MMR_DIVERSITY`, `config/settings.py:540`, default false.
- Code: `src/regwatch/retrieve/diversity.py`; call site
  `src/regwatch/generate/grounded_qa.py:2841`.
- Behavior: keeps the same NUMBER of passages but charges a candidate for
  repeating what is already selected. Similarity is token-set Jaccard over
  chunk text, so the flip costs no extra embedding call and no extra query.
  Only passages already above the INV-2 floor are eligible, so the flip cannot
  shrink the evidence set and confound a count with a quality change
  (`grounded_qa.py:2841-2851`).
- Tests: `tests/test_mmr_diversity.py`.
- Evidence to require: a same-corpus, same-profile eval A/B on `recall_at_k`
  and `citation_precision`. Note the trap first: `mmr_diversity_enabled` is NOT
  in the eval run fingerprint (`src/regwatch/eval/run_fingerprint.py:144-150`
  records `reranker_enabled` but not this), so two scorecards taken with the
  flag in different states look identical in their recorded provenance. Add it
  to the fingerprint before you run the A/B, or the result is unreproducible.

### Cross-encoder reranker

- Flag: `RERANKER_ENABLED`, `config/settings.py:531`, default false.
- Code: `src/regwatch/retrieve/reranker.py`.
- Behavior: with the flag off, stage 2 of retrieval is the identity, the first
  `rerank_top_k` of the wide net. This is a hook point, not a chosen model. The
  module hardcodes `BAAI/bge-reranker-base` as a placeholder
  (`reranker.py:36`).
- The trap: turning the flag on is a silent no-op in the API image. The model
  needs `sentence-transformers`, which ships only in the `local-embeddings`
  extra (`pyproject.toml:46-50`), and the API image installs `--extra llm`
  only (`Dockerfile:74,85`). `reranker.py:28-31` catches the failed import and
  returns the passages unchanged. You would see a flag set to true, no error,
  and no reranking.
- Evidence to require: proof the extra is installed in whatever image runs it,
  a chosen model with a license review, a cold-start measurement, and an eval
  A/B. Also read `diversity.py:18-22`: the reranker reorders on a score whose
  scale differs from the cosine value it leaves on `.score`, so MMR after it
  re-ranks on stale cosines. Do not turn both on at once.

### Live provisional draft streaming

- Flag: `REGWATCH_LIVE_DRAFT`, `config/settings.py:178`, default false.
- Code: `src/regwatch/api/main.py:1097`. It is a triple gate:
  `live_draft_enabled AND prose_synthesis_enabled AND req.live_draft`. The
  per-request opt-in is `QueryRequest.live_draft` (`main.py:748-750`), which
  the blocking `/query` route ignores; only `POST /query/stream` honors it.
  The client already sends it (`regwatch/frontend/lib/api.ts:631,665-666`).
- Behavior: un-gated prose from the worker thread, streamed as a `draft` SSE
  event, provisional by contract, rendered as a draft and replaced by the gated
  answer (`main.py:1085-1092`). Owner-amended INV-1, 2026-08-10.
- Not pinned in `fly.toml`. Whether a Fly secret sets it in production is not
  provable from this checkout; read `GET /settings` or `fly secrets list`.
- Evidence to require: that a reader never mistakes draft text for the cited
  answer, that a `draft_reset` clears it, and that a MATERIAL_DROP rejection
  cannot leave provisional text on screen.

### The authoritative FDA corpus arm

- Flags: `REGWATCH_RETRIEVAL_CORPUS` (`config/settings.py:505-506`, default
  `legacy`) and `REGWATCH_SERVING_MANIFEST_SHA`
  (`config/settings.py:515-516`, default unset). Neither is in `fly.toml`.
- Behavior with the arm on: `src/regwatch/retrieve/retriever.py:264-272`
  adds a `source_family IN (approved list)` filter and `:235` drops the
  current-version scoping. Legacy PSG rows carry NULL `source_family` and
  authoritative rows carry a reviewed value, so one shared `chunk` table holds
  both arms and rollback is a one-variable change with no data rewrite. The
  boot-time embedding-coverage guard flips its predicate the same way
  (`src/regwatch/store/embedding_profiles.py:603,623-626`). Boot refuses the
  arm outright unless the corpus passes an activation check
  (`src/regwatch/api/main.py:208-211` ->
  `corpus.status.assert_authoritative_corpus_ready_for_activation`).
- The 2026-08-18 scoped-activation amendment: the complete-universe target
  became permanently unreachable under the 512 MiB cap, so activation now
  counts against a curated manifest the operator names explicitly by sha
  (`config/settings.py:508-516`, `src/regwatch/corpus/status.py:126-131` and
  `:222-229`). With the sha unset, the original complete-universe requirement
  still applies and will not pass.
- Evidence to require: a successful sync of the named manifest with 100%
  embedding coverage, zero errors, every approved source family present, and a
  retrieval eval on the new arm. The activation assert enumerates its own
  blockers (`_activation_blockers`, `src/regwatch/corpus/status.py:203-256`);
  read them rather than guessing.
- Detail lives in `docs/AUTHORITATIVE_FDA_CORPUS.md`.

## 5. Built, then taken back off the critical path

`REGWATCH_ROUTE_CALL` has three modes: `off`, `shadow`, `live`
(`config/settings.py:190-192`, default `off`).

`live` is implemented. `_compile_route_live_scope`
(`src/regwatch/generate/grounded_qa.py:2339`) compiles a live route decision
into an executable scope, and the call site is
`grounded_qa.py:2484-2498`. It runs only in the no-product branch, only after
did-you-mean suggestions and brand matches are known empty, and only for a
PRODUCT-scope decision. A CORPUS, CLARIFY or CONVERSE decision is compiled,
audited and then discarded (`grounded_qa.py:2492-2498`); execution for those is
a later change. Any route failure falls open to the deterministic heuristic.
`tests/test_route_live.py` covers it.

It does not run in production. `fly.toml:72-74` pins
`REGWATCH_ROUTE_CALL = "off"` with the reason on the line above it: the shadow
router was a blocking pre-retrieval model call whose result was discarded, and
it came off the critical Ask path on 2026-08-20.

The cost is latency, not correctness: shadow and live both add a model call
before retrieval. Evidence to require before re-enabling: a latency budget that
accepts the extra call, and shadow-window data showing the route decision beats
the word-list heuristic on the elliptical follow-ups it was built for.

## 6. Never built, despite being documented as shipped

**There is no D1 residency guard.** Several documents in this set said the
guard shipped and was tested. It did not ship, and no test covers it. For a
project handling FDA data, that is the worst class of documentation error, so
it gets stated plainly.

Verified in this checkout at `0a13c4a`:

- `D1_ENFORCED` appears nowhere in `src/`, `config/`, `go/`, `.github/` or
  `fly.toml`.
- `D1_ALLOWED_LLM_MODELS` appears nowhere in the same set.
- `tests/test_d1_guards.py` does not exist.
- `D1ResidencyError` is defined at `src/regwatch/generate/llm.py:429-436` and
  is never raised. `grep -rn "raise D1ResidencyError" src/` returns nothing.
  No provider constructs it.

Reproduce it yourself:

```bash
grep -rn "D1_ENFORCED\|D1_ALLOWED_LLM_MODELS" src/ config/ go/ .github/ fly.toml
grep -rn "raise D1ResidencyError" src/
ls tests/test_d1_guards.py
```

All three should come back empty or not-found.

What this means operationally: nothing in the code checks which model served a
reply, and nothing rejects a reply from a model outside a perimeter. Today
generation and embeddings both go to OpenAI, an external vendor, on every normal
question. Only the database stays in the company tenant.
`docs/PRODUCTION_TRUTH.md` owns the current provider facts.

### D1ResidencyError is not dead code

The class is live plumbing, so do not delete it as unused. It is the type that
every degradation path is required to let through:

- `src/regwatch/deficiency/structured.py:118-119`, `:172`, `:236` re-raise it
  instead of converting it to the `ParseFailed` sentinel, so a residency
  violation fails the run loudly rather than degrading into a
  needs-human-review card (`structured.py:18-19`).
- `src/regwatch/deficiency/runner.py:141-142` lets it reach the generic handler,
  which fails the run with the residency message and a Sentry capture.
- `src/regwatch/generate/grounded_qa.py:1361` and `:1484` re-raise it FIRST, so
  the stream path's SSE fallback cannot re-send the request as a buffered
  completion to the very endpoint a guard would be fencing off
  (`llm.py:430-436`).
- `src/regwatch/generate/route_shadow.py:118` does the same.

Tests inject it with fake providers to prove those paths propagate rather than
swallow it (`tests/test_deficiency_structured.py:151-152`,
`tests/test_route_shadow.py:181-182`, `tests/test_synthesis_budget.py:372-377`).

So the plumbing for a residency guard is in place and tested. The guard itself
was never written. If you build it, the raise site is a provider in `llm.py`,
and every catch site above already does the right thing.

## 7. Dead-but-kept seams

Code with no caller that is deliberately not deleted. Each one has a reason.
One of those reasons has expired.

### `resolve_brand`

`src/regwatch/retrieve/resolver.py:413` is a narrow wrapper over
`lookup_external_drug` with no in-repo caller. Its docstring
(`resolver.py:418-425`) says it is kept because the Ask outcomes and provenance
design names it, and that removing it needs that spec updated in the same
change.

Both halves of that justification are now stale. The spec it names lives under
`docs/superpowers/`, which this documentation change deletes; the work is
recoverable from git history. And the docstring's own line reference is off:
`grounded_qa` calls `lookup_external_drug` directly at
`grounded_qa.py:2474`, not the `1814` the comment cites.

So it is now removable, and the docstring is the thing to fix or delete along
with it. Do that as its own change.

### `trust_proxy_headers`

`config/settings.py:861` declares it and nothing Python-side reads it
(`settings.py:852-860` says so explicitly). The consumer is the Go proxy:
`go/internal/api/config.go:103` reads `TRUST_PROXY_HEADERS` from the
environment itself, and the login-spray limiter keys its per-IP bucket on the
platform-attested `Fly-Client-IP` because of it.

It stays declared so the environment contract is one documented list and so
`.env` files carrying the variable keep validating. `fly.toml:85` pins it
true in production, and `tests/test_trust_proxy_fly_toml.py` fails if that pin
is dropped, because nothing on the Go side would notice.

Keep it. Deleting the Python field would break `.env` validation for a variable
production depends on.

### `regwatch/backend/`

The directory holds one README and no code. It exists to say that the Python
package is not there and must not be moved there: the canonical backend source
is `src/regwatch/`, and a second package root under `regwatch/backend/` would
give the repository two competing backend roots.

That README is deleted in this change. The rule it carried survives here: do
not create `regwatch/backend/__init__.py`, and do not move `src/regwatch/`. The
directory itself carries no build meaning.

## Maintaining this page

One rule. An entry leaves this page only when the code changes, never because
another document says otherwise.

Concretely:

- A flag flipped on in `fly.toml` or by a Fly secret does not move an entry out
  of section 4. The flag state is a `docs/PRODUCTION_TRUTH.md` fact. What
  belongs here is whether the code path can run at all and what still gates it.
- An entry moves out of section 1 when a caller exists in committed code, not
  when a plan says one is coming.
- An entry moves out of section 6 when the guard is written and a test fails
  without it. Not when a runbook mentions it.
- Add an entry the moment you merge something behind a flag, a stub, or an
  unconditional raise. That is the cheapest moment to write it down, and this
  page exists because nobody did.

Every entry needs a file and a line. An entry with no citation is a rumor. A
rumor is how a residency guard that exists in no line of code ended up
described as shipped and tested in six documents.
