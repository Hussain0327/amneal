# Work Log — June 8, 2026

Everything done today on `regwatch`, and the two commits that captured it.

---

## Commits today

| Time (ET) | Commit | Summary |
|---|---|---|
| 09:59 | [`566d124`](#566d124) | feat: chat sessions, current-version retrieval, resolution + eval hardening — 33 files, +1720/−775 |
| 10:23 | [`656e3d5`](#656e3d5) | chore: relocate UI to `regwatch/frontend/`, add frontend CI, bring all docs current — 36 files, +352/−143 |

Both authored by `Hussain0327` on `main`, no `Co-Authored-By` line. Not pushed (push is done manually).

### <a id="566d124"></a>`566d124` — feat: chat sessions, current-version retrieval, resolution + eval hardening

- **Conversational sessions** — `/query` accepts/returns `session_id` + `turn_id`; new `chat_session` / `chat_message` tables and conversational audit fields (migration `0002_chat_sessions`). Conversation memory is context, not evidence: a safe product filter carries across turns, but every answer still re-retrieves and validates citations. Broader response statuses: `answer` / `summary` / `clarify` / `scope_warning` / `refused`.
- **Current-version retrieval (fix-order #1)** — retrieval enforces current-PSG-version invariants so superseded/stale chunks are not served (retriever + vector_store + ingest); covered by `tests/test_current_version_retrieval.py`.
- **#2 entity-resolution hardening** — canonicalize the `normalized_name` filter so casing/salt-order variants from the API no longer silently miss the exact-match Chroma filter; explicit comparison queries ("compare X vs Y") clarify instead of collapsing to one product; mixed-product evidence clarifies (offers the products) instead of refusing.
- **#3 eval upgrade** — `fact_recall` scores a gold item's `expected_facts` (answer content, not just cited pages); `faithfulness` + `fact_recall` print on `run_eval`; deterministic seeded CI eval gate (`tests/test_eval_gate.py`) fires in CI, where `run_eval` previously no-opped on an empty corpus.
- **Retire Streamlit POC** — delete `src/regwatch/ui/`, drop the `streamlit` dependency (+ `uv.lock`) and the compose `ui` service; the Next.js app is the production UI.

### <a id="656e3d5"></a>`656e3d5` — chore: relocate UI + frontend CI + docs current

- **Restructure (workspace split)** — move the Next.js UI `web/` → `regwatch/frontend/` (clean renames, same `/api` proxy); `regwatch/backend/` workspace placeholder (Python source stays in `src/regwatch`); add a frontend CI job (`npm ci` / lint / build) in `.github/workflows/ci.yml`; update `compose.yaml`, `scripts/share-demo.sh`, `.dockerignore`, `.gitignore`, and the API CORS/proxy note for the new path.
- **Docs brought current** — README + `DOCKER`, `TECH_GUIDE_SIMPLE`, `NON_TECH_GUIDE`, `docs/README`, `PROD_READINESS`: UI = `regwatch/frontend/` (no Streamlit/8501), OpenAI **Responses API + role models** (not Chat Completions / gpt-4o-mini), conversational `/query`, current-version retrieval, INV-1..6 + INV-9, two-layer eval (live `run_eval` + deterministic `test_eval_gate.py`, 11 items, `fact_recall`), CORS allow-list. `PROJECT_SPEC` got a status banner marking it the original spec. `DECISIONS` appended: Streamlit-retired, entity-resolution hardening, eval upgrade.

---

## What we did, in order

### 1. Picked the work — correctness-first fix order
Reviewed `docs/PROD_READINESS.md` and adopted a correctness-first ordering. Chose to **build #2 (entity/source-resolution hardening) and #3 (eval upgrade)** — "the correctness narrative plus completeness, the differentiated work." Planned it in plan mode (validated the riskiest parts — the deterministic eval gate, the faithfulness gating risk — with a planning agent) before writing code.

### 2. Built #2 — resolution hardening (in place)
- **Product/document keys:** canonicalize the `normalized_name` filter in `grounded_qa.ask()` so a title-case / salt-order filter from the API stops silently missing Chroma's exact-match lookup.
- **AND validation:** comparison queries ("compare X and Y", "X vs Y") naming 2+ distinct products now **clarify** instead of collapsing to one — added comparison markers in `resolve_product()`, checked before the subset-collapse (markers deliberately exclude `and`/`with`).
- **Clarify over unclear rows:** mixed-product evidence now **clarifies** with the distinct products instead of bluntly refusing.
- New tests: `tests/test_resolution_hardening.py`.

### 3. Built #3 — eval upgrade
- **`fact_recall`** metric (tolerant substring over `expected_facts`) wired into `metrics.py` / `run_eval.py`.
- **Deterministic CI eval gate** (`tests/test_eval_gate.py`): seeds a fixed offline corpus + a faithful LLM stub, drives the real `ask()` pipeline, and hard-gates every metric — so the gate fires in CI.
- Ran the live eval once to calibrate (real-corpus `faithfulness` ≈ 0.59, so `faithfulness`/`fact_recall` stay observability-only on the live `run_eval` and are hard-gated only in the deterministic pytest gate). Live gate stays green (recall/precision/refusal = 1.0).

### 4. Retired Streamlit
Deleted `src/regwatch/ui/`, dropped the `streamlit` dependency + `uv.lock` entries, removed the compose `ui` service. Nothing imported it — a clean subtraction. The Next.js app is the only UI.

### 5. Coordinated with Codex (running in parallel)
Codex concurrently shipped the **conversational/session layer** (chat sessions, `session_id`/`turn_id`, `scope_warning`, conversational audit, `conversation.py`, `docs/CONVERSATIONAL_SESSIONS.md`) and **current-version retrieval (#1)**, then **restructured `web/` → `regwatch/frontend/`** with a frontend CI job. Reconciled the shared edits (notably `grounded_qa.py` and the docs), verified the merged tree was green, and committed.

### 6. Documentation overhaul
Read every doc and brought the current-truth set in line with reality (Streamlit→Next.js/`regwatch/frontend/`, conversational sessions, current-version retrieval, Responses API + role models, INV-9, eval upgrade, CORS). Appended the missing decisions to `DECISIONS.md`. Added a status banner to `PROJECT_SPEC.md`. Left the historical `.txt` artifacts and the original UI plan as-is.

### 7. Ran the MVP / demo debugging
- Confirmed prerequisites: frontend deps installed, `.env` has `OPENAI_API_KEY`, corpus already seeded (1,795 PSGs in DB + Chroma).
- `share-demo.sh` failed once because the production build was interrupted (`^C` during "Building the UI…") → `next start` had no `.next` to serve. Rebuilt cleanly (~30s, all 6 pages), verified the full stack boots end-to-end (API `/health` and the UI's `/api/health` proxy both `{"status":"ok"}`), then tore it down.
- Run paths: `./scripts/share-demo.sh` (public cloudflared link for managers) or two terminals (`uvicorn …:app --reload --port 8000` + `cd regwatch/frontend && npm run dev` → localhost:3000).

### 8. Reviewed a live demo answer
- **Strong:** "study on this" + amphetamine produced a multi-page, page-accurate, fully cited synthesis (study design, fasting, alcohol dose-dumping Tests 2/3/4, BE 80–125% CI for d-/l-amphetamine), with a provenance panel (model, audit id, session/turn, raw chunks). This is the differentiator — not a keyword search.
- **Bug found (not yet fixed):** typing a greeting ("Hello") with a drug pinned in the *Active ingredient* field returns a **cited greeting** ("Hello. [PSG_204326, p.1]"). Root cause: the vague/greeting "ask, don't answer" guard only fires when the drug is named in the *question*, not when the product is pinned via the filter field — so a filter-pinned greeting skips the guard and the model dutifully cites it. Fix direction: run the vague/greeting check whenever a product is pinned (question **or** filter), and never let a no-topic input reach the synthesizer.

---

## Open / next

- **Fix the greeting + filter-pinned bug** (#8) — small, contained change in the Q&A entry path; add a regression test.
- **Stray `package-lock.json` at the repo root** — an accidental root `npm install` artifact (the real lockfile is in `regwatch/frontend/`); safe to `rm`.
- **Top production blockers** still open (`docs/PROD_READINESS.md`): #1 API auth + rate limit, #5 LLM/data-handling decision, #2/#3 datastore + migration discipline.
- Today's two commits are **local on `main`, not pushed**.
