"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { ActivityRail } from "@/components/studio/ActivityRail";
import { AssistantPanel } from "@/components/studio/AssistantPanel";
import { ComplianceSpine } from "@/components/studio/ComplianceSpine";
import { DocumentCanvas } from "@/components/studio/DocumentCanvas";
import { FindingsPanel, type PendingDisposition } from "@/components/studio/FindingsPanel";
import { FormatBar } from "@/components/studio/FormatBar";
import type { LibraryState } from "@/components/studio/LibrarySection";
import { PdfPane } from "@/components/studio/PdfPane";
import { ReferenceBar } from "@/components/studio/ReferenceBar";
import { RepositoryTree } from "@/components/studio/RepositoryTree";
import { SelectionToolbar, type StudioSelection } from "@/components/studio/SelectionToolbar";
import { TopBar } from "@/components/studio/TopBar";
import { fetchPsgContent, fetchPsgLibrary } from "@/lib/api";
import { buildLibraryTree, type LibraryDoc } from "@/lib/studio-library";
import { toReferenceDoc } from "@/lib/studio-reference";
import {
  ASSISTANT_INTRO,
  CHECK_RESULTS,
  DOCS,
  INITIAL_DOC_ID,
  TREE,
  assistantReply,
} from "@/lib/studio-fixtures";
import {
  addHighlight,
  applyEdit,
  applyFindings,
  applySuggestion,
  clearHighlights,
  disposeFinding,
  formatRecords,
  isDisposed,
  nextOpenFinding,
  quote,
  revertBlock,
  sliceBlock,
  verdictFor,
} from "@/lib/studio-marks";
import type {
  AssistantMessage,
  Disposition,
  DispositionError,
  Finding,
  PanelId,
  SelectionAction,
  StudioDoc,
} from "@/lib/studio-types";

// How long a stand-in compliance run takes. The real pipeline will be slower and
// streamed; the surface only needs the states (idle -> checking -> checked).
const CHECK_MS = 1500;
const TYPE_MS = 14;

/**
 * Who a disposition is attributed to.
 *
 * Deliberately not a person's name. There is no server timestamp and no
 * signature behind anything recorded here, so putting an analyst's name on it
 * would dress a working note up as an attributable record. The panel says the
 * same thing in words, and the exported record repeats it.
 */
const RECORDED_BY = "studio session";

/**
 * How long to wait on the clipboard before showing the text instead.
 *
 * navigator.clipboard.writeText does not always settle: Chrome queues the write
 * until the document regains focus, so a reviewer who clicks Copy and then
 * switches window can leave the promise pending indefinitely and watch the
 * button do nothing. The clipboard is an external call like any other and gets
 * a timeout and a defined outcome.
 */
const COPY_TIMEOUT_MS = 2000;

async function writeToClipboard(text: string): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      navigator.clipboard.writeText(text),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("clipboard write timed out")), COPY_TIMEOUT_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

/** Documents that arrive already checked, so the spine carries its findings on load. */
const PRECHECKED = [INITIAL_DOC_ID];

function initialDocs(): Record<string, StudioDoc> {
  const out: Record<string, StudioDoc> = {};
  for (const doc of DOCS) {
    out[doc.id] = PRECHECKED.includes(doc.id) ? applyFindings(doc, CHECK_RESULTS[doc.id] ?? []) : doc;
  }
  return out;
}

const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function subscribeToMotion(onChange: () => void): () => void {
  const query = window.matchMedia?.(MOTION_QUERY);
  query?.addEventListener?.("change", onChange);
  return () => query?.removeEventListener?.("change", onChange);
}

/**
 * The OS motion setting, live: a visitor can change it without reloading, and
 * this surface animates a scan, a stream and a scroll.
 *
 * useSyncExternalStore rather than an effect, because that is exactly what this
 * is -- a subscription to something outside React. Reading it in an effect and
 * calling setState would cascade a render on every mount for a value that was
 * already knowable.
 */
function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeToMotion,
    () => window.matchMedia?.(MOTION_QUERY).matches === true,
    // Server render: assume motion is acceptable and let hydration correct it.
    // The alternative would suppress the opening animation for everyone.
    () => false,
  );
}

/**
 * Loading state for the open PSG's text. Deliberately separate from
 * `LibraryState` (the catalog): the catalog can be listed while one
 * document's text fails, and an error must never render as an empty document.
 */
type ReferenceState =
  | { phase: "loading" }
  | { phase: "ready"; truncated: boolean }
  | { phase: "error"; message: string };

export default function StudioPage() {
  const [docs, setDocs] = useState<Record<string, StudioDoc>>(initialDocs);
  const [activeId, setActiveId] = useState<string>(INITIAL_DOC_ID);
  const [panel, setPanel] = useState<PanelId | null>(null);
  const [activeFindingId, setActiveFindingId] = useState<string | null>(null);
  const [selection, setSelection] = useState<StudioSelection | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [tracked, setTracked] = useState(true);
  const [treeOpen, setTreeOpen] = useState(false);
  const [thinking, setThinking] = useState(false);

  // The reference library (real FDA PSGs from the database) and the one open
  // on the canvas. `activeId` stays a valid DRAFT id throughout: a library doc
  // is an overlay, so closing it returns to exactly the draft that was open,
  // and none of the disposition-loop code paths change shape.
  const [libraryDoc, setLibraryDoc] = useState<LibraryDoc | null>(null);
  const [libraryState, setLibraryState] = useState<LibraryState>({ phase: "loading" });
  // The open PSG's own text, rebuilt server-side into the same StudioDoc shape
  // a working document has. Held beside `docs` rather than inside it: the
  // fixture repository is the analyst's working set, and a reference document
  // must never be swept into a repository-wide check or a disposition record.
  //
  // ONE slot, not a map. The library is ~1,800 documents, and keeping every
  // one an analyst opened would grow without bound for the sake of remembering
  // highlights on a document they left. The cost is that highlighting a PSG
  // lasts as long as it is open, which is the same lifetime the rest of this
  // surface gives anything (nothing here survives a refresh).
  const [referenceDoc, setReferenceDoc] = useState<StudioDoc | null>(null);
  const [referenceState, setReferenceState] = useState<ReferenceState>({ phase: "loading" });
  // The PDF is the artifact FDA published; the canvas shows its extracted
  // text. This switches between them for the document that is already open.
  const [showPdf, setShowPdf] = useState(false);
  // The psgId of the most recent text request; see openLibraryDoc.
  const referenceRequest = useRef<number | null>(null);
  const libraryLoading = useRef(false);
  // Mirrored for delayed callbacks: a check that completes while a PSG is on
  // the canvas must not resurrect the findings panel (its scrim would sit
  // over the PDF), and the timer closure would otherwise read a stale value.
  const libraryDocRef = useRef<LibraryDoc | null>(null);
  useEffect(() => {
    libraryDocRef.current = libraryDoc;
  }, [libraryDoc]);

  // The disposition the analyst has picked but not recorded, the words they are
  // part-way through typing, and the last refusal. Drafts are keyed by finding
  // and live here rather than in the panel so closing the panel or switching
  // documents does not throw away a half-written justification.
  const [pending, setPending] = useState<PendingDisposition | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [dispositionError, setDispositionError] = useState<{
    findingId: string;
    reason: DispositionError;
  } | null>(null);
  const [live, setLive] = useState("");
  const [copyFallback, setCopyFallback] = useState<string | null>(null);
  // Bumped only by explicit finding-to-finding navigation, so focus moves when
  // the analyst asked to move and stays put when something else changed.
  const [focusToken, setFocusToken] = useState(0);

  const scrollRef = useRef<HTMLDivElement>(null);
  // Every pending timer, so an unmount mid-check or mid-stream leaves nothing
  // running against a dead component.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const seq = useRef(0);
  const reduceMotion = useReducedMotion();

  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach(clearTimeout);
      pending.length = 0;
    };
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    const id = setTimeout(fn, ms);
    timers.current.push(id);
    return id;
  }, []);

  const doc = docs[activeId];
  const checking = doc?.checkState === "checking";
  const openFindings = useMemo(
    () => (doc ? doc.findings.filter((f) => !isDisposed(f) && f.severity !== "info").length : 0),
    [doc],
  );

  // One writer for both stores: handlers name the document they are acting on
  // and never learn which of the two it lives in. A reference document accepts
  // exactly the same operations, which is what keeps highlighting working on
  // it without a second code path.
  const patch = useCallback((id: string, fn: (d: StudioDoc) => StudioDoc) => {
    setReferenceDoc((prev) => (prev && prev.id === id ? fn(prev) : prev));
    setDocs((prev) => (prev[id] ? { ...prev, [id]: fn(prev[id]) } : prev));
  }, []);

  // --- documents -----------------------------------------------------------

  const loadLibrary = useCallback(() => {
    if (libraryLoading.current) return;
    libraryLoading.current = true;
    setLibraryState({ phase: "loading" });
    fetchPsgLibrary()
      .then((rows) => setLibraryState({ phase: "ready", buckets: buildLibraryTree(rows) }))
      // State stays out of "ready" on failure: an error must never render as
      // loaded-but-empty (house convention, same as the watch page).
      .catch((e) =>
        setLibraryState({ phase: "error", message: e instanceof Error ? e.message : String(e) }),
      )
      .finally(() => {
        libraryLoading.current = false;
      });
  }, []);

  useEffect(() => {
    loadLibrary();
  }, [loadLibrary]);

  const openDoc = useCallback((id: string) => {
    setLibraryDoc(null);
    setReferenceDoc(null);
    setShowPdf(false);
    // Abandon any in-flight PSG text. Without this its reply still matches the
    // request token and re-populates referenceDoc under the draft now on the
    // canvas -- which would silently reroute this document's highlights to a
    // document that is not open and hide the assistant actions on it.
    referenceRequest.current = null;
    setActiveId(id);
    setActiveFindingId(null);
    setSelection(null);
    setTreeOpen(false);
    setDispositionError(null);
    // Optional call: jsdom elements have no scrollTo, and a missing scroll reset
    // must never take the document switch down with it.
    scrollRef.current?.scrollTo?.({ top: 0 });
  }, []);

  const openLibraryDoc = useCallback((d: LibraryDoc) => {
    setLibraryDoc(d);
    setReferenceDoc(null);
    setReferenceState({ phase: "loading" });
    setShowPdf(false);
    // Last-request-wins. Two quick clicks in the rail would otherwise land the
    // first document's text under the second document's header, and the slower
    // reply is the one that arrives last.
    referenceRequest.current = d.psgId;
    fetchPsgContent(d.psgId)
      .then((content) => {
        if (referenceRequest.current !== d.psgId) return;
        setReferenceDoc(toReferenceDoc(content));
        setReferenceState({ phase: "ready", truncated: content.truncated });
      })
      .catch((e: unknown) => {
        if (referenceRequest.current !== d.psgId) return;
        // Stays out of "ready" on failure: an error must never render as a
        // loaded-but-empty document (house convention, same as the catalog).
        setReferenceState({
          phase: "error",
          message: e instanceof Error ? e.message : String(e),
        });
      });
    setActiveFindingId(null);
    setSelection(null);
    setTreeOpen(false);
    setDispositionError(null);
    // Same reset openDoc does: the canvas element is shared, so a switch from
    // halfway down a long draft would otherwise open the PSG mid-document.
    scrollRef.current?.scrollTo?.({ top: 0 });
    // The panel shows the DRAFT's findings; leaving it open beside a PDF would
    // mislabel them. `docs`, `drafts` and `pending` are deliberately untouched
    // so a half-typed justification survives draft -> library -> draft.
    setPanel(null);
    setLive(`Opened PSG for ${d.ingredient}. Read-only FDA reference.`);
  }, []);

  // A read-only canvas raises neither of these. They exist because the props
  // are required, and they are stable so the canvas is not remounted.
  const noEdit = useCallback(() => {}, []);
  const noSelectFinding = useCallback(() => {}, []);

  const editBlock = useCallback(
    (blockId: string, text: string) => {
      patch(activeId, (d) => applyEdit(d, blockId, text));
      setActiveFindingId(null);
    },
    [activeId, patch],
  );

  // --- compliance ----------------------------------------------------------

  const runCheck = useCallback(
    (id: string) => {
      if (docs[id]?.checkState === "checking") return;
      patch(id, (d) => ({ ...d, checkState: "checking" }));
      later(() => {
        patch(id, (d) => applyFindings(d, CHECK_RESULTS[id] ?? []));
        if (id === activeId && !libraryDocRef.current) setPanel("findings");
        // Announced from the page's single live region rather than from the
        // format bar that displays the same count: two polite regions updating
        // on one state change double-announce, and applying a fix already
        // writes here while flipping the bar to "edited since last check".
        //
        // Counted from CHECK_RESULTS rather than from the patched document:
        // this callback fires CHECK_MS after runCheck was created, so any doc
        // read out of the closure is stale by then. A freshly returned finding
        // is never disposed, so severity is the whole filter.
        const found = CHECK_RESULTS[id] ?? [];
        const open = found.filter((f) => f.severity !== "info").length;
        setLive(
          open === 0
            ? "Check complete. No open findings."
            : `Check complete. ${open} open ${open === 1 ? "finding" : "findings"}.`,
        );
      }, CHECK_MS);
    },
    [activeId, docs, later, patch],
  );

  // The full-repository run. Staggered rather than simultaneous so the tree
  // reads as a queue working through, which is what the real pipeline does.
  const runFullCheck = useCallback(() => {
    setPanel("findings");
    DOCS.forEach((d, i) => {
      later(() => runCheck(d.id), i * 320);
    });
  }, [later, runCheck]);

  const focusFinding = useCallback((id: string | null) => {
    setActiveFindingId(id);
    if (id) setPanel("findings");
  }, []);

  // --- the disposition loop ------------------------------------------------

  const applyFix = useCallback(
    (findingId: string) => {
      // Leave the block before rewriting it. The canvas can land a foreign write
      // under a live caret, but the caret would collapse to the block start; a
      // blur first means there is nothing to disturb.
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();

      const current = docs[activeId];
      if (!current) return;
      const result = applySuggestion(current, findingId);
      if (result.error) {
        setDispositionError({ findingId, reason: "unknown_finding" });
        setLive("The text under this finding has changed since the check. Run the check again before you apply the fix.");
        return;
      }
      setDocs((prev) => ({ ...prev, [activeId]: result.doc }));
      setActiveFindingId(findingId);
      setDispositionError(null);
      const f = result.doc.findings.find((x) => x.id === findingId);
      setLive(`Suggested fix applied. ${f?.title ?? ""}. Record the disposition to close it.`);
    },
    [activeId, docs],
  );

  const revert = useCallback(
    (blockId: string) => {
      patch(activeId, (d) => revertBlock(d, blockId));
      setLive("Checked text restored.");
    },
    [activeId, patch],
  );

  const record = useCallback(
    (findingId: string, disposition: Disposition, justification: string) => {
      const current = docs[activeId];
      if (!current) return false;
      const at = new Date().toISOString();
      const result = disposeFinding(current, findingId, disposition, justification, RECORDED_BY, at);
      if (result.error) {
        setDispositionError({ findingId, reason: result.error });
        return false;
      }
      setDocs((prev) => ({ ...prev, [activeId]: result.doc }));
      setDispositionError(null);
      setPending(null);
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[findingId];
        return next;
      });
      const f = result.doc.findings.find((x) => x.id === findingId);
      const v = verdictFor(result.doc);
      setLive(
        `Recorded. ${f?.title ?? ""}. ${v.blocking} blocking, ${v.toResolve} to resolve, ${v.disposed} recorded.`,
      );
      return true;
    },
    [activeId, docs],
  );

  const pickDisposition = useCallback(
    (f: Finding, disposition: Disposition) => {
      setActiveFindingId(f.id);
      // Fixed needs no words, so picking it IS recording it -- unless the gate
      // refuses, in which case the refusal explains itself in place.
      if (disposition === "fixed") {
        record(f.id, "fixed", "");
        return;
      }
      setDispositionError(null);
      setPending({ findingId: f.id, disposition });
    },
    [record],
  );

  const commitPending = useCallback(
    (f: Finding) => {
      if (!pending || pending.findingId !== f.id) return;
      record(f.id, pending.disposition, drafts[f.id] ?? "");
    },
    [drafts, pending, record],
  );

  const cancelPending = useCallback(() => {
    // The draft survives on purpose: cancelling the editor is not the same as
    // throwing away words somebody just wrote.
    setPending(null);
    setDispositionError(null);
  }, []);

  const step = useCallback(
    (direction: 1 | -1) => {
      if (libraryDoc) {
        setLive("Reference documents have no findings to step through.");
        return;
      }
      if (!doc) return;
      const next = nextOpenFinding(doc, activeFindingId, direction);
      if (!next) {
        setLive("No open findings.");
        return;
      }
      setActiveFindingId(next);
      setPanel("findings");
      setFocusToken((n) => n + 1);
    },
    [activeFindingId, doc, libraryDoc],
  );

  const copyRecord = useCallback(async () => {
    if (!doc) return;
    const text = formatRecords(doc);
    try {
      await writeToClipboard(text);
      setCopyFallback(null);
      setLive("Record copied.");
    } catch {
      // An insecure origin, a denied permission and a write that never settles
      // all land here. Showing the text is the difference between a dead button
      // and a slower path that still gets the reviewer their record.
      setCopyFallback(text);
      setLive("Could not copy to the clipboard. The record is shown below; select it and copy it yourself.");
    }
  }, [doc]);

  // Move focus onto the card the analyst navigated to. Layout effect so it runs
  // before paint and the card is never briefly focused-but-offscreen.
  useLayoutEffect(() => {
    if (focusToken === 0 || !activeFindingId) return;
    const card = document.querySelector<HTMLElement>(`[data-finding-card="${CSS.escape(activeFindingId)}"]`);
    card?.querySelector<HTMLElement>("button")?.focus();
  }, [focusToken, activeFindingId]);

  // --- assistant -----------------------------------------------------------

  const send = useCallback(
    (prompt: string) => {
      const text = prompt.trim();
      if (!text || !doc) return;
      setDraft("");
      setPanel("assistant");
      seq.current += 1;
      const turn = seq.current;

      setMessages((prev) => [...prev, { id: `u${turn}`, role: "user", text }]);

      const reply = assistantReply(text, doc.name);
      const id = `a${turn}`;

      if (reduceMotion) {
        setMessages((prev) => [...prev, { id, role: "assistant", text: reply.text, sources: reply.sources }]);
        return;
      }

      setThinking(true);
      later(() => {
        setThinking(false);
        setMessages((prev) => [...prev, { id, role: "assistant", text: "", streaming: true }]);
        // Reveal by word, not character: it reads as composition rather than a
        // teletype, and it is far cheaper in renders.
        const words = reply.text.split(" ");
        words.forEach((_, i) => {
          later(
            () => {
              const partial = words.slice(0, i + 1).join(" ");
              const done = i === words.length - 1;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === id
                    ? { ...m, text: partial, streaming: !done, sources: done ? reply.sources : undefined }
                    : m,
                ),
              );
            },
            i * TYPE_MS + 40,
          );
        });
      }, 420);
    },
    [doc, later, reduceMotion],
  );

  // --- selection -----------------------------------------------------------

  const onSelectionAction = useCallback(
    (action: SelectionAction) => {
      // Whichever document is under the selection -- the working draft, or the
      // reference PSG that has taken its place on the canvas.
      const open = referenceDoc ?? doc;
      if (!open || !selection) return;
      const block = open.blocks.find((b) => b.id === selection.blockId);
      const text = block ? quote(sliceBlock(block, selection.start, selection.end)) : "";

      if (action === "highlight") {
        patch(open.id, (d) => addHighlight(d, selection.blockId, selection.start, selection.end));
        setSelection(null);
        window.getSelection()?.removeAllRanges();
        return;
      }

      // The toolbar hides the assistant actions over a reference document; this
      // is the same refusal at the model, so a keyboard or test path cannot
      // reach around the missing buttons and ask the working-repository
      // assistant about FDA's guidance.
      if (referenceDoc) {
        setSelection(null);
        window.getSelection()?.removeAllRanges();
        setLive("The assistant answers about your working documents, not about a reference PSG.");
        return;
      }

      const prompt =
        action === "summarize"
          ? `Summarize this passage: "${text}"`
          : action === "explain"
            ? `Explain this passage: "${text}"`
            : action === "check"
              ? `Check this passage against the guidelines: "${text}"`
              : "";

      setSelection(null);
      window.getSelection()?.removeAllRanges();

      if (action === "ask") {
        setPanel("assistant");
        setDraft(`About "${text}" - `);
        return;
      }
      send(prompt);
    },
    [doc, patch, referenceDoc, selection, send],
  );

  // --- keyboard ------------------------------------------------------------

  useEffect(() => {
    function inTextField(target: EventTarget | null): boolean {
      return (
        target instanceof HTMLElement &&
        (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.isContentEditable)
      );
    }

    function onKey(e: KeyboardEvent) {
      // F8 rather than a letter or Alt+Arrow: the document is contentEditable
      // and a printable key would type into it, while Option+Up/Down is
      // paragraph navigation on macOS. Function keys are exempt from the
      // single-character-shortcut rule, so nothing is owed a remap affordance.
      if (e.key === "F8") {
        if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.isComposing) return;
        // A justification field owns its own keystrokes.
        if (e.target instanceof HTMLTextAreaElement) return;
        e.preventDefault();
        step(e.shiftKey ? -1 : 1);
        return;
      }

      if (e.key !== "Escape") return;
      if (selection) setSelection(null);
      else if (libraryDoc) {
        // The PDF is the topmost surface, so Escape peels it (or the mobile
        // drawer above it) FIRST -- reaching past it to cancel the retained
        // draft's pending disposition would mutate state the analyst cannot
        // see. The search box keeps its own Escape.
        if (inTextField(e.target)) return;
        if (treeOpen) setTreeOpen(false);
        else {
          setLibraryDoc(null);
          setReferenceDoc(null);
          setShowPdf(false);
          referenceRequest.current = null;
          setLive(`Returned to ${doc.name}.`);
        }
      }
      // Before the panel: closing the editor must not also close the panel, or a
      // half-typed justification would vanish along with two surfaces.
      else if (pending) cancelPending();
      else if (treeOpen) setTreeOpen(false);
      else if (panel && !inTextField(e.target)) setPanel(null);
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cancelPending, doc, libraryDoc, panel, pending, selection, step, treeOpen]);

  if (!doc) return null;

  return (
    <div className="studio">
      <TopBar
        doc={doc}
        library={
          libraryDoc ? { ingredient: libraryDoc.ingredient, drugLabel: libraryDoc.drugLabel } : null
        }
        onToggleTree={() => setTreeOpen((v) => !v)}
      />

      {/* One live region for the whole surface, mounted unconditionally: a region
          that appears at the same moment as its first message is not announced. */}
      <div className="studio__sr" role="status" aria-live="polite" aria-atomic="true">
        {live}
      </div>

      <div className="st-body">
        <RepositoryTree
          tree={TREE}
          docs={docs}
          activeId={libraryDoc ? null : activeId}
          library={libraryState}
          activeLibraryId={libraryDoc?.id ?? null}
          open={treeOpen}
          checking={checking}
          onOpenDoc={openDoc}
          onOpenLibraryDoc={openLibraryDoc}
          onRetryLibrary={loadLibrary}
          onCheck={() => runCheck(activeId)}
          // The repository-wide run lives with the repository. It used to sit
          // rotated 90 degrees at the foot of the activity rail, which put the
          // widest-scope action in the least readable place in the studio.
          onRunFullCheck={runFullCheck}
        />

        {libraryDoc ? (
          // The compliance chrome is HIDDEN, not disabled, while a reference
          // PSG is open: the spine, format bar, panels and rail all read or
          // write the DRAFT's blocks and findings, and rendering them next to
          // a document they do not describe would be a lie ("2 open findings"
          // beside a document that has none). The reference bar takes their
          // place with the three things a PSG actually supports.
          <div className="st-main">
            <ReferenceBar
              doc={libraryDoc}
              showingPdf={showPdf}
              truncated={referenceState.phase === "ready" && referenceState.truncated}
              onTogglePdf={() => setShowPdf((v) => !v)}
            />

            {showPdf ? (
              <PdfPane key={libraryDoc.id} doc={libraryDoc} />
            ) : referenceState.phase === "error" ? (
              <div className="st-ref__fallback" role="alert">
                <span>Couldn&apos;t load the text of this PSG.</span>
                <button
                  type="button"
                  className="st-btn st-btn--quiet st-tree__retry"
                  onClick={() => openLibraryDoc(libraryDoc)}
                >
                  Retry
                </button>
                <button
                  type="button"
                  className="st-btn st-btn--outline"
                  onClick={() => setShowPdf(true)}
                >
                  View original PDF
                </button>
              </div>
            ) : referenceDoc ? (
              // The same canvas the working documents use, reading the same
              // block model -- which is the whole point of rebuilding a PSG
              // into one. Read-only: FDA's published text is not ours to edit.
              <DocumentCanvas
                key={referenceDoc.id}
                doc={referenceDoc}
                scrollRef={scrollRef}
                activeFindingId={null}
                tracked={false}
                reduceMotion={reduceMotion}
                readOnly
                onEditBlock={noEdit}
                onSelectFinding={noSelectFinding}
                onSelectionChange={setSelection}
              />
            ) : (
              <div className="st-ref__note">Loading document...</div>
            )}
          </div>
        ) : (
          <>
            <ComplianceSpine
              doc={doc}
              scrollRef={scrollRef}
              activeFindingId={activeFindingId}
              onSelect={focusFinding}
            />

            <div className="st-main">
              <FormatBar
                tracked={tracked}
                onTrackedChange={setTracked}
                canClear={doc.blocks.some((b) => b.marks.some((m) => m.kind === "highlight"))}
                onClearHighlights={() => patch(activeId, clearHighlights)}
                // The bar used to spend its width on a sentence teaching the
                // selection gesture. It now reports the state the analyst is
                // actually tracking while they read.
                checkState={doc.checkState}
                openCount={openFindings}
                totalCount={doc.findings.length}
              />
              <DocumentCanvas
                doc={doc}
                scrollRef={scrollRef}
                activeFindingId={activeFindingId}
                tracked={tracked}
                reduceMotion={reduceMotion}
                onEditBlock={editBlock}
                onSelectFinding={focusFinding}
                onSelectionChange={setSelection}
              />
            </div>

            <div className={`st-panel${panel ? " is-open" : ""}`} aria-hidden={panel === null}>
              <div className="st-panel__inner">
                {panel === "findings" && (
                  <FindingsPanel
                    doc={doc}
                    activeFindingId={activeFindingId}
                    pending={pending}
                    drafts={drafts}
                    error={dispositionError}
                    copyFallback={copyFallback}
                    onSelect={focusFinding}
                    onAsk={(f) => send(`Explain the rule behind "${f.title}"`)}
                    onClose={() => setPanel(null)}
                    onApplySuggestion={applyFix}
                    onRevert={revert}
                    onPickDisposition={pickDisposition}
                    onDraftChange={(id, text) => setDrafts((prev) => ({ ...prev, [id]: text }))}
                    onRecord={commitPending}
                    onCancelPending={cancelPending}
                    onCopyRecord={copyRecord}
                    onStep={step}
                  />
                )}
                {panel === "assistant" && (
                  <AssistantPanel
                    doc={doc}
                    messages={messages}
                    draft={draft}
                    thinking={thinking}
                    intro={ASSISTANT_INTRO}
                    onDraftChange={setDraft}
                    onSend={send}
                    onClose={() => setPanel(null)}
                  />
                )}
              </div>
            </div>

            <ActivityRail
              panel={panel}
              findingCount={openFindings}
              checking={checking}
              onTogglePanel={(id) => setPanel((prev) => (prev === id ? null : id))}
            />
          </>
        )}

        {(treeOpen || panel) && (
          <button
            type="button"
            className="st-scrim"
            aria-label="Close panel"
            onClick={() => {
              setTreeOpen(false);
              setPanel(null);
            }}
          />
        )}
      </div>

      <SelectionToolbar
        selection={selection}
        assistant={referenceDoc === null}
        onAction={onSelectionAction}
      />
    </div>
  );
}
