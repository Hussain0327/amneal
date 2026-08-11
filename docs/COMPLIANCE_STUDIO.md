# Compliance Studio (`/studio`)

> A workbench for reviewing the company's **own** CMC documents against ICH, USP,
> 21 CFR and internal SOPs. Every other RegWatch surface reads *public FDA*
> material. This one reads *our* drafts, and that difference shapes everything
> below.
>
> Status: **the working documents are UI and domain model only.** The document
> service, the compliance pipeline and the assistant are fixtures behind typed
> seams, and nothing recorded here survives a page refresh. One seam is real:
> the left rail's **reference library** lists the FDA PSG corpus from the
> database (`GET /psg/documents`), opens each PSG as a document on the same
> canvas the working files use (`GET /psg/documents/{id}/content`), offers it
> as a Word download (`GET /psg/documents/{id}/docx`), and keeps the original
> PDF one click away (`GET /psg/documents/{id}/pdf`). See section 10.
>
> Last updated: 2026-08-11 (re-checked against the frontend on that date).
>
> Related: [ARCHITECTURE.md](ARCHITECTURE.md) section 3 (product surfaces),
> [ROADMAP.md](ROADMAP.md) (what is queued next).

---

## 1. What we are building towards

A reviewer opens the studio and can, in one place:

- **Find the document.** The left rail is the document repository for a product:
  the CTD modules, the specifications, the method SOPs.
- **Read it, or have it summarized.** The middle is the document itself. Select
  any passage and ask the assistant to summarize or explain it, with citations.
- **Check it for compliance.** Run the document against the guidelines and get
  findings anchored to the exact text that triggered them.
- **See whether it has gone out of date.** Two senses: stale against the
  analyst's own edits, and stale against the guidance it was written to. Only
  the first is built. See section 6.

**Where this is heading (owner direction, 2026-08-05).** The product should end
up as two surfaces: Ask for conversation, and Studio as the document workspace,
with Assemble, Watch, White Paper and Deficiency folded in as generators that
produce documents into the tree and checks that run against documents already
there. Nothing has moved yet. Studio has no backend, no persistence and no
product scope, so it cannot receive a working surface until those exist.
Sequencing lives in [`ROADMAP.md`](ROADMAP.md).

## 2. What exists today

| Piece | State |
|---|---|
| Repository tree, live filter, per-document check glyphs | Built, fixture-backed |
| Reference library: DB PSGs grouped A-Z by drug | Built, **API-backed** (section 10) |
| Reference PSG as a read-only document on the canvas, .docx download, PDF toggle | Built, **API-backed** (section 10) |
| Document canvas: contentEditable blocks, span-anchored marks, tracked changes | Built |
| Compliance findings anchored to `(blockId, start, end)` | Built, fixture-backed |
| Suggested fix, apply, restore | Built (3 of 12 fixture findings carry one) |
| Disposition record: fixed / fixed elsewhere / not applicable / disputed | Built |
| Compliance spine (the closure gauge) | Built |
| Cited assistant panel | Built, canned replies |
| Upload, new folder | Disabled, needs a document service |
| Persistence of any kind | **Not built.** Refresh destroys everything. |

## 3. The anchoring idea

**A finding is not a report line, it is a span of the document.**

Every finding carries `(blockId, start, end)` plus the `excerpt` those offsets
resolved to. That is what lets it highlight in place, tick the spine at its
measured position, and be invalidated when the analyst edits the text under it.
Anything that cannot be anchored is not a finding, it is a note about the
document as a whole.

`Finding.start/end` is the immutable **as-checked** anchor and is never remapped.
`Mark.start/end` is the current render position and *is* remapped on every edit.
Keeping those separate stops an edit from silently relocating a claim.

## 4. The disposition loop

Read a finding, apply its suggested fix, mark it fixed.

A reviewer is not clearing a checklist, they are building a record. Every
finding ends in exactly one of four dispositions, or stays open:

| Disposition | Evidence required |
|---|---|
| **Fixed** | The diff. No words needed. |
| **Fixed elsewhere** | Written justification: the remedy landed in a block, document or change record this finding does not point at. |
| **Not applicable** | Written justification: the condition that puts it out of scope, and where that is documented. |
| **Disputed** | Written justification: what the document says, and the requirement it was read against. |

Records are **append-only**. Changing your mind writes a second entry and the
first stays readable.

### 4.1 The evidence gate

**"Fixed" is refused until the analyst has changed the text the finding points
at.** A reviewer must not be able to assert a fix that did not happen, or the
record is worthless.

The obvious predicate is a "this block was edited" flag, and it does not work.
The original `staleFindings` set such a flag on any edit anywhere in the block
and never cleared it, so:

- typing one character and deleting it left the flag set with the text
  byte-identical, and the gate was defeatable in two keystrokes;
- editing an assay limit unlocked a finding about a word two sentences away.

So staleness is **derived, not stored**:

- `Block.checkedText` is the text as the checker last read it.
- `changedRegion(block)` localises what moved, in `checkedText` offsets.
- `isStale(block, finding)` is true only when that region touches
  `[finding.start, finding.end]`, inclusive at both ends.

Reverting an edit re-locks Fixed. Editing elsewhere in the block never unlocks
it. An insertion flush to either end of the span counts, because appending an
approver right after `"Version: 5.0"` is exactly how that finding gets fixed.

The gate is deliberately strict, and **"fixed elsewhere" is the escape hatch**.
It demands words instead of a diff rather than loosening the rule. Two shipped
fixture findings name a remedy that lands outside their own span ("or add a
definitions table", "or the cross-reference"), and without that option the only
way to close them would be a false "not applicable".

### 4.2 Two baselines, and why one field cannot do both

| Field | Means | Serves |
|---|---|---|
| `Block.original` | text when the analyst **opened** the document | tracked changes |
| `Block.checkedText` | text when the checker last **read** the block | the evidence rule |

They diverge the moment anyone edits before running a check.

### 4.3 Suggested fixes

A finding may carry a `suggestion`, which is replacement text for its own span.
Applying it splices the text, which makes the finding stale, which unlocks
Fixed.

Only 3 of the 12 fixture findings carry one, and **that ratio is the point.** A
suggestion is offered when the remedy is carried entirely in the words. It is
withheld whenever the fix needs a fact only the analyst holds: an approver, an
effective date, a validation report number, a sampling interval. A fabricated
suggestion in a GMP-controlled document is worse than no suggestion.

`applySuggestion` refuses unless the block is unchanged since the check and the
span still holds the quoted excerpt, so the same fix can never be written twice.
`revertBlock` restores `checkedText` and is the whole of undo.

### 4.4 Re-checking never loses a judgement

`applyFindings` carries records forward by finding id. A finding that comes back
still reported after being recorded **fixed** is marked `contested` and re-opens
with its record kept: silently dropping either side of that contradiction would
be the audit lie. A recorded finding the checker no longer reports moves to
`StudioDoc.closed`, because the defect is gone but the judgement that closed it
still has to be printable.

### 4.5 This is not a controlled record

`FindingRecord.at` is the client clock and `by` is `"studio session"`. There is
no server timestamp, no authenticated attribution and no electronic signature,
so nothing recorded here is a 21 CFR Part 11 record. The panel says exactly that
where the record is made, and the exported TSV repeats it in a provenance
trailer. **Do not** put a real analyst name on it until there is a server behind
it.

## 5. The spine

A 22px column carrying one tick per finding at its measured position.

- **Severity is weight.** Critical is solid clay, info is a slate hairline.
- **Disposition is width.** An open finding runs the full column; a recorded one
  drops to a hairline across the left half only. Working a document visibly
  closes the spine down.
- **Not encoded by fading.** The `0.3` opacity previously used on stale ticks
  measures about 1.2:1 against the panel, well under the 3:1 a non-text
  indicator owes (WCAG 1.4.11). Width survives greyscale, 8px, protanopia and
  deuteranopia. A fade survives none of them.
- The **gold thread** is granted by `isSealed()`: every actionable finding
  closed by an actual fix. A document argued down to zero shows "All findings
  recorded" and never the seal.

## 6. "Old or stale" means two different things

Only the first is built, and conflating them in the UI would be a lie.

1. **Stale against the analyst's edits.** Built. The check ran, then the text
   under a finding changed, so the claim no longer describes the document.
   Carried by `isStale` and `checkState: "stale"`.
2. **Out of date against current FDA guidance.** *Not built.* "This
   specification was written against ICH Q1A(R2); a revision has since
   published." That is the Watch layer pointed at our own documents instead of
   the pipeline, and it needs the document corpus, a guidance-version link per
   document, and a real pipeline before it can mean anything.

## 7. Deliberately not built

- **Persistence.** `docs` is `useState`, so a refresh destroys every
  disposition. `localStorage` for GMP dispositions would be worse than nothing:
  a record with no server, no audit trail and no way to reconcile two tabs. The
  honest fix is the backend.
- **Editable tables.** Cell-level offsets need an editor engine. A finding
  anchored to a table block can therefore never be marked Fixed, only fixed
  elsewhere, not applicable, or disputed. No fixture finding does this; an API
  might.
- **Enter inside a block.** `onInput` reads `textContent`, which yields no
  separator for the `<div>` contentEditable inserts, so an accepted Enter would
  silently weld two paragraphs together in a controlled document. Refused until
  there is a block-splitting model.
- **Risk acceptance** (`accepted` / `rejected` dispositions) needs an
  authenticated QA role and a countersignature. Recording it here would let one
  analyst clear a critical finding.
- **A compliance score.** No 0-100 scale exists in CMC review, and inventing one
  presents a guess as a measurement. The panel reports a verdict, counts, and
  the standards checked against.

## 8. The contract a real backend has to meet

The fixture shapes in `lib/studio-fixtures.ts` are the contract, not a sketch.
An endpoint replacing `CHECK_RESULTS` must return findings that:

- anchor to `(blockId, start, end)` in the **same block text the service handed
  back**, with `excerpt` matching that slice (`applyFindings` recomputes it and
  will overwrite a disagreeing one);
- carry a stable `id` across re-checks, since records merge by id;
- omit `suggestion` rather than invent one when the fix needs an external fact.

## 9. Where the code is

| Path | What |
|---|---|
| `app/studio/page.tsx` | container: state, F8 traversal, live region, clipboard |
| `app/studio/studio.css` | every colour a token from `globals.css`, used verbatim |
| `lib/studio-types.ts` | domain types plus `JUSTIFICATION_MAX` |
| `lib/studio-marks.ts` | **all** pure logic: splice, staleness, dispositions, verdict |
| `lib/studio-fixtures.ts` | stand-in repository plus `CHECK_RESULTS` plus canned assistant |
| `lib/studio-library.ts` | pure grouping: wire PSG rows to a letter/drug/doc tree |
| `lib/studio-reference.ts` | pure: one PSG's wire content to the same `StudioDoc` shape |
| `components/studio/*.tsx` | tree, library section, reference bar, PDF pane, canvas, spine, panels, rails |
| `src/regwatch/process/psg_document.py` | pure: stored chunks back to typed blocks |
| `src/regwatch/process/psg_docx.py` | pure: those blocks to .docx bytes |
| `test/studioMarks.test.ts` | 109 tests on the pure layer |
| `test/studioLibrary.test.ts` | 13 tests on the library grouping and filter layer |
| `test/studioReference.test.ts` | 6 tests on the reference-document mapping |
| `test/StudioPage.test.tsx` | 42 page tests, including the whole loop and the library |

Keyboard: `F8` and `Shift+F8` move between open findings, behind visible
Previous/Next buttons. A letter or `Alt+Arrow` would collide with typing in the
contentEditable document. `Escape` degrades one layer at a time, and its last
rung closes an open reference PDF back to the retained draft.

## 10. The reference library (the first real seam)

The rail's second section lists the **FDA PSG corpus actually in the database**.
That is public reference material, not the company's drafts, which is why it
sits beside the fixture working set rather than replacing it.

- `GET /psg/documents` (FastAPI, relayed by the Go edge) returns the catalog.
  `lib/studio-library.ts` buckets it A-Z, then by drug (salt-collapsed on the
  server-computed `stripped_name`, so "Albuterol Sulfate" files under
  "Albuterol"), then by PSG labelled `{dosage_form} ({route})` with a
  Draft/Final badge. Letter buckets start closed. The shared search box filters
  both sections and force-opens surviving branches.
- **Opening a PSG opens a document, not a viewer.**
  `GET /psg/documents/{id}/content` rebuilds the PSG from the chunk rows ingest
  already stored for its current version and returns the studio's own block
  vocabulary (`title` / `meta` / `h2` / `p`, each carrying its PDF page), so
  `DocumentCanvas` renders a reference PSG exactly as it renders a working
  document. Nothing is fetched, parsed or persisted to serve it: a PSG averages
  three chunks, and the rebuild (`process/psg_document.py`) is a pure function
  over rows already in Postgres. Because those are the same rows retrieval
  quotes, a passage read here and a passage cited by an answer cannot disagree.
  A document whose row exists but whose text never landed answers `409`, not
  `404` - it is a real document, and the client offers the PDF instead.
- **The reference bar** replaces the format bar while a PSG is open: chips
  (form/route, date, Draft/Final), **Download .docx**, a **View original PDF**
  toggle and the fda.gov link-out. The `.docx` is generated per request by
  `process/psg_docx.py` (python-docx) from the same blocks and stored nowhere,
  so a revised PSG has no stale file behind it. It carries a provenance
  paragraph naming the FDA source and stating that the PDF is authoritative and
  the file is not an FDA-issued document - the words are FDA's, the layout is
  not. The drug name reaches `Content-Disposition`, so it is sanitised to
  `[A-Za-z0-9._-]` first.
- **The PDF is still one click away**, unchanged: an `<iframe>` over
  `GET /psg/documents/{id}/pdf`, which serves the local cache or fetches from
  fda.gov using the crawler's own hardened `download_pdf` (polite pause, byte
  cap, `%PDF` check, best-effort write-through cache). Local hits are
  hash-verified before serving, so the content ETag never vouches for a
  truncated file. A `HEAD` probe runs first (the DB row plus at most two stat()
  calls, never the network) because an iframe renders an error body as page text
  instead of signalling; it refuses rows the GET is guaranteed to fail.
  `next.config.mjs` relaxes `X-Frame-Options` to `SAMEORIGIN` for `/api/psg/*`
  only; every other route keeps `DENY`. Opening focuses the pane heading so
  Escape-to-draft works immediately; once the reader clicks into the PDF the
  iframe owns the keyboard (frames swallow keystrokes) and the exit is the tree
  or the heading.
- **Reference docs are read-only and carry no findings.** The evidence gate
  (section 4.1) anchors to editable block text, and a PSG is FDA's document, not
  ours, so checks, dispositions, the spine and the panels are hidden rather than
  disabled while one is open. Read-only means no editing surface at all rather
  than a textbox that refuses every keystroke: no block is `contentEditable` and
  none announces as a textbox. Highlighting still works - it marks text without
  changing it. The selection toolbar's three assistant actions are withheld,
  because that assistant answers about the working repository and pointing it at
  FDA's own guidance would produce confident answers about a document it was
  never given; the page refuses them at the model too, not only in the toolbar.
  The tree-footer check button is the one disabled control, with a note
  explaining why. `activeId` keeps pointing at the retained draft the whole
  time, so a half-typed justification survives draft, library, draft untouched.
  The document's footer shows FDA's recommended date, never a `v`-prefixed
  internal revision number the agency never issued.
