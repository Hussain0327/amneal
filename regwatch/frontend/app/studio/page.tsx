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
import { RepositoryTree } from "@/components/studio/RepositoryTree";
import { SelectionToolbar, type StudioSelection } from "@/components/studio/SelectionToolbar";
import { TopBar } from "@/components/studio/TopBar";
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

  const patch = useCallback((id: string, fn: (d: StudioDoc) => StudioDoc) => {
    setDocs((prev) => (prev[id] ? { ...prev, [id]: fn(prev[id]) } : prev));
  }, []);

  // --- documents -----------------------------------------------------------

  const openDoc = useCallback((id: string) => {
    setActiveId(id);
    setActiveFindingId(null);
    setSelection(null);
    setTreeOpen(false);
    setDispositionError(null);
    // Optional call: jsdom elements have no scrollTo, and a missing scroll reset
    // must never take the document switch down with it.
    scrollRef.current?.scrollTo?.({ top: 0 });
  }, []);

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
        if (id === activeId) setPanel("findings");
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
    [activeFindingId, doc],
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
      if (!doc || !selection) return;
      const block = doc.blocks.find((b) => b.id === selection.blockId);
      const text = block ? quote(sliceBlock(block, selection.start, selection.end)) : "";

      if (action === "highlight") {
        patch(activeId, (d) => addHighlight(d, selection.blockId, selection.start, selection.end));
        setSelection(null);
        window.getSelection()?.removeAllRanges();
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
    [activeId, doc, patch, selection, send],
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
      // Before the panel: closing the editor must not also close the panel, or a
      // half-typed justification would vanish along with two surfaces.
      else if (pending) cancelPending();
      else if (treeOpen) setTreeOpen(false);
      else if (panel && !inTextField(e.target)) setPanel(null);
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cancelPending, panel, pending, selection, step, treeOpen]);

  if (!doc) return null;

  return (
    <div className="studio">
      <TopBar doc={doc} onToggleTree={() => setTreeOpen((v) => !v)} />

      {/* One live region for the whole surface, mounted unconditionally: a region
          that appears at the same moment as its first message is not announced. */}
      <div className="studio__sr" role="status" aria-live="polite" aria-atomic="true">
        {live}
      </div>

      <div className="st-body">
        <RepositoryTree
          tree={TREE}
          docs={docs}
          activeId={activeId}
          open={treeOpen}
          checking={checking}
          onOpenDoc={openDoc}
          onCheck={() => runCheck(activeId)}
        />

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
          onRunFullCheck={runFullCheck}
        />

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

      <SelectionToolbar selection={selection} onAction={onSelectionAction} />
    </div>
  );
}
