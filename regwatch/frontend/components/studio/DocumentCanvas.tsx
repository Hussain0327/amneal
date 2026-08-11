"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type HTMLAttributes,
  type MouseEventHandler,
  type RefObject,
} from "react";

import type { StudioSelection } from "@/components/studio/SelectionToolbar";
import { NoteIcon } from "@/components/studio/icons";
import { changeMarks, dominantMark, segmentBlock, selectionOffsets } from "@/lib/studio-marks";
import type { Block, Finding, StudioDoc } from "@/lib/studio-types";

// ---------------------------------------------------------------------------
// Marked-up HTML for one block
// ---------------------------------------------------------------------------

function escapeText(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** A finding id lands in an attribute value, so the quote has to go too. */
function escapeAttr(value: string): string {
  return escapeText(value).replace(/"/g, "&quot;");
}

/**
 * The block's text with its marks wrapped. Built as a string rather than as
 * React children because the element it lands in is contentEditable and must
 * stay outside React's reconciler (see EditableBlock).
 */
function blockHtml(
  block: Block,
  findings: Finding[],
  activeFindingId: string | null,
  tracked: boolean,
): string {
  const byId = new Map(findings.map((f) => [f.id, f]));
  // Tracked changes are derived from the block, not stored on it, so turning the
  // switch off removes the marks without touching the analyst's text.
  const marks = tracked ? [...block.marks, ...changeMarks(block)] : block.marks;
  let html = "";

  for (const segment of segmentBlock(block.text, marks)) {
    const text = escapeText(segment.text);
    const mark = dominantMark(segment.marks, findings);

    if (!mark) {
      html += text;
      continue;
    }
    if (mark.kind === "highlight") {
      html += `<mark class="st-mark st-mark--highlight">${text}</mark>`;
      continue;
    }
    if (mark.kind === "insert") {
      html += `<mark class="st-mark st-mark--insert">${text}</mark>`;
      continue;
    }

    const finding = mark.findingId ? byId.get(mark.findingId) : undefined;
    if (!finding) {
      html += text;
      continue;
    }
    // A stale finding describes text that no longer exists: it keeps its span so
    // the analyst can see what was claimed, but drops its severity weight.
    const tone = finding.stale ? "stale" : finding.severity;
    const active = finding.id === activeFindingId ? " is-active" : "";
    html += `<mark class="st-mark st-mark--${tone}${active}" data-finding-id="${escapeAttr(finding.id)}">${text}</mark>`;
  }

  // An empty contentEditable with no children cannot take a caret, so an emptied
  // block would become permanently unclickable without this.
  return html || "<br>";
}

// ---------------------------------------------------------------------------
// Blocks
// ---------------------------------------------------------------------------

interface DocBlockProps {
  block: Block;
  findings: Finding[];
  activeFindingId: string | null;
  tracked: boolean;
  /** 1-based position and total, for a name that does not change as you type. */
  position: number;
  total: number;
  onEditBlock: (blockId: string, text: string) => void;
  onSelectFinding: (id: string | null) => void;
}

/** What each block type is called when a screen reader announces the textbox. */
const BLOCK_ROLE: Record<Block["type"], string> = {
  title: "Title",
  meta: "Header line",
  h2: "Heading",
  p: "Paragraph",
  table: "Table",
};

/** Props shared by the three editable tags; only the tag itself differs. */
type EditableProps = HTMLAttributes<HTMLElement> & {
  ref: (node: HTMLElement | null) => void;
  "data-block-id": string;
};

function EditableBlock({
  block,
  findings,
  activeFindingId,
  tracked,
  position,
  total,
  onEditBlock,
  onSelectFinding,
}: DocBlockProps) {
  const node = useRef<HTMLElement | null>(null);
  const written = useRef<string | null>(null);
  // The last text this block reported upward. Anything else arriving in the
  // model came from somewhere other than these keystrokes.
  const emitted = useRef<string | null>(null);
  const [focused, setFocused] = useState(false);

  const html = useMemo(
    () => blockHtml(block, findings, activeFindingId, tracked),
    [block, findings, activeFindingId, tracked],
  );

  // A stable ref callback: an inline one would be re-invoked with null and then
  // the node again after every commit.
  const attach = useCallback((el: HTMLElement | null) => {
    node.current = el;
  }, []);

  // The element is written imperatively and never given React children. Setting
  // innerHTML under a live caret collapses it to the start of the block, so the
  // block's own typing is echoed back silently; blurring is what re-applies the
  // marks around whatever they typed.
  //
  // The exception is text that arrived from somewhere else -- a suggested fix
  // being applied, a block being restored. That has to land even under a live
  // caret, or Apply would appear to do nothing whenever the analyst happened to
  // be standing in the block it edits.
  useEffect(() => {
    const el = node.current;
    if (!el) return;
    const ownEcho = focused && emitted.current === block.text;
    if (ownEcho || written.current === html) return;
    el.innerHTML = html;
    written.current = html;
  }, [html, focused, block.text]);

  function handleBlur() {
    // contentEditable leaves markup of its own behind (a split <mark>, an
    // inserted <div>), and an edit that nets out to the same text produces the
    // same html. Dropping the record forces one resync from the model.
    written.current = null;
    emitted.current = null;
    setFocused(false);
  }

  const handleClick: MouseEventHandler<HTMLElement> = (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const mark = target.closest("[data-finding-id]");
    const id = mark?.getAttribute("data-finding-id");
    if (id) onSelectFinding(id);
  };

  const props: EditableProps = {
    ref: attach,
    className: `st-blk st-blk--${block.type}`,
    "data-block-id": block.id,
    contentEditable: true,
    role: "textbox",
    "aria-multiline": true,
    // Positional, not derived from the text. A name built from the content
    // renames the focused textbox on every keystroke, and a screen reader
    // announces the rename over what the analyst is trying to hear.
    "aria-label": `${BLOCK_ROLE[block.type]} ${position} of ${total}`,
    onInput: (event) => {
      const text = event.currentTarget.textContent ?? "";
      emitted.current = text;
      onEditBlock(block.id, text);
    },
    onKeyDown: (event) => {
      // onInput reads textContent, which yields no separator for the <div> or
      // <br> that contentEditable inserts on Enter -- so an accepted Enter
      // silently welds two paragraphs together in a GMP-controlled document.
      // There is no block-splitting model to do this properly, so it is refused
      // rather than allowed to corrupt the text.
      if (event.key === "Enter") event.preventDefault();
    },
    onFocus: () => setFocused(true),
    onBlur: handleBlur,
    onClick: handleClick,
  };

  if (block.type === "title") return <h1 {...props} />;
  if (block.type === "h2") return <h2 {...props} />;
  return <p {...props} />;
}

/** Tables are read-only in this cut: cell-level offsets need an editor engine. */
function TableBlock({ block }: { block: Block }) {
  const rows = block.rows ?? [];
  const head = rows.find((r) => r.head);
  const body = rows.filter((r) => r !== head);

  return (
    <div className="st-blk st-blk--table" data-block-id={block.id}>
      <table>
        {head && (
          <thead>
            <tr>
              {head.cells.map((cell, i) => (
                <th key={i} scope="col">
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {body.map((row, i) => (
            <tr key={i}>
              {row.cells.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DocBlock(props: DocBlockProps) {
  if (props.block.type === "table") return <TableBlock block={props.block} />;
  return <EditableBlock {...props} />;
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

/**
 * The block element a selection endpoint sits in. closest() matches the node
 * itself, so an endpoint that landed on the block element rather than inside a
 * text node still resolves to its own block.
 */
function blockElementOf(node: Node | null): HTMLElement | null {
  const base = node instanceof Element ? node : (node?.parentElement ?? null);
  return base ? base.closest<HTMLElement>("[data-block-id]") : null;
}

function sameSelection(a: StudioSelection | null, b: StudioSelection | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.blockId === b.blockId &&
    a.start === b.start &&
    a.end === b.end &&
    a.rect.top === b.rect.top &&
    a.rect.left === b.rect.left &&
    a.rect.width === b.rect.width
  );
}

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------

interface Props {
  doc: StudioDoc;
  scrollRef: RefObject<HTMLDivElement>;
  activeFindingId: string | null;
  /** Show tracked-change spans. On unless a caller turns the switch off. */
  tracked?: boolean;
  /** Honour prefers-reduced-motion when scrolling a finding into view. */
  reduceMotion?: boolean;
  onEditBlock: (blockId: string, text: string) => void;
  onSelectFinding: (id: string | null) => void;
  onSelectionChange: (s: StudioSelection | null) => void;
}

export function DocumentCanvas({
  doc,
  scrollRef,
  activeFindingId,
  tracked = true,
  reduceMotion = false,
  onEditBlock,
  onSelectFinding,
  onSelectionChange,
}: Props) {
  const emitted = useRef<StudioSelection | null>(null);

  useEffect(() => {
    // selectionchange fires on every caret move, so the same value would
    // otherwise be pushed at the parent dozens of times per drag.
    function emit(next: StudioSelection | null) {
      if (sameSelection(emitted.current, next)) return;
      emitted.current = next;
      onSelectionChange(next);
    }

    function read() {
      const root = scrollRef.current;
      const selection = window.getSelection();
      if (!root || !selection || selection.rangeCount === 0 || selection.isCollapsed) {
        emit(null);
        return;
      }

      const range = selection.getRangeAt(0);
      const block = blockElementOf(range.startContainer);
      // A selection crossing two blocks spans two offset spaces and cannot be
      // anchored; so does one that starts outside the page.
      if (!block || block !== blockElementOf(range.endContainer) || !root.contains(block)) {
        emit(null);
        return;
      }
      // Read-only blocks carry no offset model to map a span into.
      if (!block.matches('[contenteditable="true"]')) {
        emit(null);
        return;
      }

      const offsets = selectionOffsets(block, selection);
      const blockId = block.getAttribute("data-block-id");
      if (!offsets || !blockId) {
        emit(null);
        return;
      }

      const rect = range.getBoundingClientRect();
      emit({
        blockId,
        start: offsets.start,
        end: offsets.end,
        rect: { top: rect.top, left: rect.left, width: rect.width },
      });
    }

    document.addEventListener("selectionchange", read);
    return () => document.removeEventListener("selectionchange", read);
  }, [onSelectionChange, scrollRef]);

  // Bring the marked span into view when a finding is picked on the spine or in
  // the panel. Child effects have already written their marks by this point.
  useEffect(() => {
    const root = scrollRef.current;
    if (!activeFindingId || !root) return;
    const finding = doc.findings.find((f) => f.id === activeFindingId);
    const target =
      Array.from(root.querySelectorAll("[data-finding-id]")).find(
        (el) => el.getAttribute("data-finding-id") === activeFindingId,
      ) ??
      // A recorded or replaced finding has no mark left in the text. Falling back
      // to its block is what stops a click on it scrolling silently nowhere.
      (finding ? root.querySelector(`[data-block-id="${CSS.escape(finding.blockId)}"]`) : null);

    // jsdom has no layout and so no scrollIntoView.
    if (target && typeof target.scrollIntoView === "function") {
      // An explicit "smooth" overrides the element's computed scroll-behavior, so
      // the reduced-motion rule in the stylesheet cannot help here on its own.
      target.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
    }
  }, [activeFindingId, doc.findings, reduceMotion, scrollRef]);

  return (
    <div className="st-scroll" ref={scrollRef}>
      {/* Keyed on the document: block ids are only unique within one, and a
          stale innerHTML would otherwise survive a switch. */}
      <article className="st-page" key={doc.id}>
        {/* The warning is about this document, so it belongs on it. Floated
            above the page it read as one more strip of chrome, and the analyst
            could scroll away from it while it still applied to what they read. */}
        {doc.checkState === "stale" && (
          <div className="st-page__stale" role="status">
            <NoteIcon className="st-icon" />
            <span>
              <b>Edited since the last check.</b> Findings below the edited text no longer describe
              this document. Run the check again.
            </span>
          </div>
        )}

        {doc.blocks.map((block, i) => (
          <DocBlock
            key={block.id}
            block={block}
            findings={doc.findings}
            activeFindingId={activeFindingId}
            tracked={tracked}
            position={i + 1}
            total={doc.blocks.length}
            onEditBlock={onEditBlock}
            onSelectFinding={onSelectFinding}
          />
        ))}

        {/* A controlled document repeats its identity at the foot of every page,
            because a page separated from its cover sheet is only traceable if it
            names itself. */}
        <footer className="st-foot">
          <span>{doc.name}</span>
          <span className="st-foot__v">v{doc.version}</span>
        </footer>
      </article>
    </div>
  );
}
