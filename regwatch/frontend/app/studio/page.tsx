"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ActivityRail } from "@/components/studio/ActivityRail";
import { AssistantPanel } from "@/components/studio/AssistantPanel";
import { ComplianceSpine } from "@/components/studio/ComplianceSpine";
import { DocumentCanvas } from "@/components/studio/DocumentCanvas";
import { FindingsPanel } from "@/components/studio/FindingsPanel";
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
import { addHighlight, applyEdit, applyFindings, clearHighlights, quote, sliceBlock } from "@/lib/studio-marks";
import type { AssistantMessage, PanelId, SelectionAction, StudioDoc } from "@/lib/studio-types";

// How long a stand-in compliance run takes. The real pipeline will be slower and
// streamed; the surface only needs the states (idle -> checking -> checked).
const CHECK_MS = 1500;
const TYPE_MS = 14;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
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

  const scrollRef = useRef<HTMLDivElement>(null);
  // Every pending timer, so an unmount mid-check or mid-stream leaves nothing
  // running against a dead component.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const seq = useRef(0);

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
    () => (doc ? doc.findings.filter((f) => !f.stale && f.severity !== "info").length : 0),
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

      if (prefersReducedMotion()) {
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
    [doc, later],
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
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      if (selection) setSelection(null);
      else if (treeOpen) setTreeOpen(false);
      else if (panel) setPanel(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panel, selection, treeOpen]);

  if (!doc) return null;

  return (
    <div className="studio">
      <TopBar doc={doc} onToggleTree={() => setTreeOpen((v) => !v)} />

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
                onSelect={focusFinding}
                onAsk={(f) => send(`Explain the rule behind "${f.title}"`)}
                onClose={() => setPanel(null)}
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
