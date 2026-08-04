# S1 — In-app Evidence Drawer (Ask citations)

> **ARCHIVED** — **SHIPPED**, merged as PR #16 (`ff3f6a2`). The component lives at
> `regwatch/frontend/components/EvidenceDrawer.tsx`. One item was carried into
> `docs/ROADMAP.md` rather than closed: the full `inert` background for assistive
> tech (aria-modal + scrim shipped; the shell behind the drawer is not `inert`).

Part of the grounded UX 10x plan (slices S1–S9). This is S1: the first, smallest,
zero-backend slice. North-star metric it moves: **time-to-defensible-deliverable** —
keep the regulatory analyst on the cited passage instead of bouncing them out to a
remote 50-page FDA PDF to verify the one thing the product exists to prove.

## Problem (grounded, current `main`)

A citation today is a gold chip (`CiteChip`, `components/Turns.tsx:43-52`) rendered as
a plain `<a target="_blank">` to the FDA PDF's `source_url`. Clicking it **leaves
RegWatch** for a remote PDF. The cited quote and page already exist client-side — they
are rendered in a *collapsed* `<details className="sources">` disclosure below the
answer (`Turns.tsx:160-174`): `short_name`, `p.{page}`, the `snippet` in a
`.ref__quote` blockquote, and the raw `source_url` link. So the evidence is present but
**buried and out-of-context**, not beside the answer where verification happens.

The wire `Citation` (`QueryCitation`, `lib/api-types.ts:451-467`) carries exactly:
`short_name, page, chunk_id, doc_id, version_id, source_url, snippet`.

### Two honesty constraints (verified, do not violate)

1. **No exact-span highlight is possible.** `snippet` is a blind 200-char *prefix* of
   the retrieved chunk (`grounded_qa.py:430`), not the exact cited span, and **no char
   offsets exist anywhere** in the pipeline. The source is a remote PDF URL, not a
   positional document. We therefore render the snippet as a *styled quote*, and we do
   **not** call it a "highlight" of the exact passage in analyst-facing copy.
2. **No date reaches the client.** `Citation` carries no date field; a true "retrieval
   date" exists nowhere. A dated provenance badge is a separate, **backend-gated** slice
   (S7) — and note prod pgvector drops `recommended_date` entirely, so S7 is `needs-new`,
   not a copy-through. S1 ships **no date**.

These two constraints are the whole point of doing S1 first: it sets a
*trust-not-theater* bar (show exactly what we can defend, label nothing we can't) for
every subsequent slice.

## What S1 does

Replace the citation chip's new-tab jump with an **in-app slide-in evidence drawer**
showing, beside the answer: the source name, the page, the cited `snippet` as a styled
quote, and an explicit **"Open source PDF ↗"** link (the old affordance, preserved
inside the drawer). The collapsed `<details className="sources">` list stays untouched
as a **no-JS fallback**.

Backend dependency: **none.** All three fields are already on the wire.

## Architecture

```
 page.tsx  (app/(shell)/page.tsx)            ── owns drawer state
   │  const [activeCitation, setActiveCitation] = useState<Citation|null>(null)
   │  renders <EvidenceDrawer citation={activeCitation} onClose={…} />  (root-level)
   │  passes onCite={setActiveCitation}  ▼
 AssistantTurn (components/Turns.tsx)         ── threads onCite into the chip map
   │  passes onSelect={onCite}  ▼
 CiteChip                                      ── <a target=_blank>  →  <button onClick>
   └─ onSelect(c)  ───────────────────────────▶ setActiveCitation(c)  ▶ drawer opens
 EvidenceDrawer (components/EvidenceDrawer.tsx) ── pure presentation over one Citation
```

**Component design / modularity (high cohesion, low coupling):**
- `EvidenceDrawer` is a self-contained presentational component. Its entire input is
  `{ citation: Citation | null, onClose: () => void }`. It owns *no* business logic and
  *no* I/O — it renders the fields of one citation and manages its own focus/keyboard
  affordances. `citation === null` ⇒ renders `null` (closed).
- `CiteChip` gains one prop, `onSelect: (c: Citation) => void`. It changes from an anchor
  to a `<button type="button">`. This keeps the open-trigger where the citation already
  is, rather than lifting citation identity into shared state.
- `page.tsx` is the single owner of "which citation is open" — one `useState`, mounted
  once at the shell root so the drawer overlays the whole canvas.

**Data flow:** unidirectional. Citation data flows down (`turn.citations` → chip →
drawer); the open/close event flows up (`onSelect` → `setActiveCitation`). No new wire
fields, no fetch, no effect that touches the network.

### Sequence (open → verify → close)

1. Analyst reads a cited answer; clicks a `CiteChip` button.
2. `onSelect(c)` → `setActiveCitation(c)` → drawer slides in from the right with the
   snippet, page, source name, and "Open source PDF ↗".
3. Focus moves into the drawer (close button); `Esc`, backdrop click, or the close
   button calls `onClose()` → `setActiveCitation(null)`; focus returns to the chip that
   opened it.

## Invariants honored

- **INV-1 (grounding):** drawer is pure presentation over an *already-validated*
  citation; it adds no claim and cannot fabricate grounding.
- **INV-2 (refuse-over-guess):** the drawer is, *by construction*, unreachable for
  `refused` / `clarify` / `scope_warning` turns. `CiteChip` is rendered **only** inside
  the `answer`/`summary` branch of `AssistantTurn` (`Turns.tsx:119-125`); the refused and
  clarify branches return early and render no chips. There is no code path where a
  declined turn produces a drawer trigger. This is enforced structurally **and** covered
  by a regression test (see below).
- **No-JS fallback:** the `<details className="sources">` Reference list is retained, so
  with JS disabled (or if the drawer regresses) the quote + page + source link are still
  reachable.

## Accessibility (must not regress the existing baseline)

The app already ships skip-links, `:focus-visible` rings (`globals.css:128`), and
`aria-live` status regions. The drawer adds, in kind:
- `role="dialog"`, `aria-modal="true"`, `aria-label` naming the source.
- **Focus management:** focus moves to the close button on open; `Esc` closes; focus
  returns to the invoking chip on close (captured as `document.activeElement` on open).
- A **focus trap** keeping Tab within the drawer while open.
- **Background scroll lock** (`document.body.style.overflow = "hidden"` while open,
  restored on close) so the page can't scroll under the full-height scrim.
- Motion: the slide-in is disabled under `@media (prefers-reduced-motion: reduce)`,
  matching the existing `.rise`/`.draw` handling (`globals.css:1374`).

Note on mid-open citation switching: the full-viewport scrim (z 200) sits above the
chips and the focus trap keeps Tab inside the panel, so a second chip can't be
activated while the drawer is open — every open→verify→close is a clean cycle. If a
later slice makes the drawer non-modal, the focus effect must be re-keyed on the
citation identity to follow content changes.

## Testing — harness deferred (see below)

S1 originally shipped a **vitest + Testing Library** harness (11 tests, incl. a
mutation-verified INV-2 gate). It was **removed before merge**: vitest/vite pull a
transitive toolchain (vite, esbuild) that carries **HIGH/CRITICAL advisories**
(vite path-traversal, vitest UI/browser-mode file-read/RCE), and the dev-mode web
Docker image does `npm ci` (all deps), so the CI **trivy image scan** failed on them.
The only patched versions are `vite@8` / `vitest@4`, whose upgrade cascaded into peer
conflicts (`@types/node`) and lockfile churn — not worth blocking the feature on.

So S1 ships gated by the **existing** frontend CI (`eslint` + `tsc --noEmit` +
`next build` + OpenAPI contract-drift), plus the **structural** INV-2 guarantee below.
A proper frontend test harness — on a CVE-clean runner, with the Docker image excluding
test tooling — is a **dedicated follow-up** (the SWE audit's P1 "frontend tests" item).

INV-2 is enforced **by construction, not just by a test**: `CiteChip` is rendered only
inside the `answer`/`summary` branch of `AssistantTurn`; the `refused` / `clarify` /
`scope_warning` branches return early and never map citations, and `status="error"`
turns reach the cited branch only with empty citations (`_refuse()`), hitting the
no-citations path. There is no code path where a declined turn produces a drawer trigger.

## Reviewed (3-lens adversarial pass) — deferred follow-ups

A 3-lens review (a11y/interaction, React lifecycle, standards/test-integrity) cleared
the slice. Verified false alarms: SSR/portal safety, StrictMode double-invoke, the
chip→button new-tab trade. Deferred, non-blocking (no baseline regression — there was no
modal before S1):
- **Frontend test harness** on a CVE-clean runner, with the Docker image excluding test
  tooling (the SWE audit's P1 item). Re-add the INV-2 / drawer tests there.
- **Full `inert` background for assistive tech.** `aria-modal` + the scrim cover most
  AT/pointer cases; making the shell behind the drawer `inert` is additive hardening.
These are tracked for a later hardening pass, not required for S1 to be correct.

## Explicitly out of scope (smallest change)

- Exact-span highlight (no offsets exist — would need a backend span-mapping change).
- Any date / "retrieval date" badge (no date on the wire; dated provenance is S7,
  backend-gated).
- Answer-span → citation linking, multi-citation compare, PDF embedding.
- Touching the streamed `result` frame, the audit path, or any retrieval/generate code.
