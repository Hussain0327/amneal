"use client";

// The compliance spine: a 22px column carrying one tick per finding, each at
// that finding's measured position in the document.
//
// It answers two questions without opening a panel. How weak is this document,
// and how much of that is still yours to answer. An open finding is a solid
// mark at full width; a recorded one drops to a hairline across the left half
// only. Working a document closes the spine down, and when every actionable
// finding was genuinely FIXED the column resolves to a single gold thread.
//
// Disposition is carried by geometry, not by fading. A faded tick on parchment
// lands under the 3:1 contrast floor a non-text indicator owes, and half-width
// versus full-width survives greyscale, 8px, and both common colour-blindnesses.
//
// Positions are measured from the live DOM against the PAGE box rather than the
// scroll container: the container carries 5rem of bottom padding, so measuring
// against it would squash every tick upward by a fixed percentage and quietly
// misstate where the document actually ends.

import { useCallback, useEffect, useMemo, useState } from "react";

import { SEVERITY_RANK, currentRecord, isDisposed, isSealed, isStale, sortFindings } from "@/lib/studio-marks";
import type { Block, Disposition, Finding, Severity, StudioDoc } from "@/lib/studio-types";

/** How each severity is named in a tick's accessible label. */
const SEVERITY_WORD: Record<Severity, string> = {
  critical: "Critical",
  major: "Major",
  minor: "Minor",
  info: "Info",
};

const DISPOSITION_WORD: Record<Disposition, string> = {
  fixed: "Fixed",
  fixed_elsewhere: "Fixed elsewhere",
  not_applicable: "Not applicable",
  disputed: "Disputed",
};

/** A judgement the reviewer argued rather than one the text now answers. */
function isArgued(d: Disposition): boolean {
  return d === "not_applicable" || d === "disputed";
}

function clamp01(n: number): number {
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0;
}

/**
 * Where a finding sits before the document has been laid out: server render,
 * jsdom, or the first paint after switching documents. Block order is the only
 * ordering available without boxes, and using it means the spine is never empty
 * and never has to appear from nothing once measurement lands.
 */
function fallbackPct(blocks: Block[], blockId: string): number {
  if (blocks.length === 0) return 0;
  const index = blocks.findIndex((b) => b.id === blockId);
  return clamp01((index < 0 ? 0 : index) / blocks.length);
}

/**
 * The block element DocumentCanvas stamped for this finding. Block ids will come
 * from the API eventually, so a selector-hostile id must not take the spine down
 * with a SyntaxError.
 */
function blockElement(container: HTMLElement, blockId: string): HTMLElement | null {
  try {
    return container.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`);
  } catch {
    return null;
  }
}

/** Re-measuring on every resize is cheap; re-rendering when nothing moved is not. */
function samePositions(a: Record<string, number>, b: Record<string, number>): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((k) => a[k] === b[k]);
}

/**
 * The tick's whole meaning in words. Disposition leads, because a recorded tick
 * is a hairline carrying no severity weight at all -- a screen reader user would
 * otherwise hear an open finding where the eye sees a closed one.
 */
function tickLabel(f: Finding, block: Block | undefined): string {
  const severity = SEVERITY_WORD[f.severity];
  const where = `${severity} finding at ${f.location}: ${f.title}`;
  const record = currentRecord(f);
  if (record && isDisposed(f)) return `${DISPOSITION_WORD[record.disposition]}. ${where}`;
  if (f.contested) return `Contested, recorded fixed and still reported. ${where}`;
  if (isStale(block, f)) return `Text edited, no disposition recorded. ${where}`;
  return where;
}

interface ComplianceSpineProps {
  doc: StudioDoc;
  /** The document's scroll container. Ticks are measured against the page inside it. */
  scrollRef: React.RefObject<HTMLDivElement>;
  activeFindingId: string | null;
  onSelect: (id: string) => void;
}

export function ComplianceSpine({ doc, scrollRef, activeFindingId, onSelect }: ComplianceSpineProps) {
  const [positions, setPositions] = useState<Record<string, number>>({});

  const { blocks, findings } = doc;

  const measure = useCallback(() => {
    const container = scrollRef.current;
    const page = container?.querySelector<HTMLElement>(".st-page") ?? null;
    const next: Record<string, number> = {};

    // Both rects are viewport-relative, so their difference is a layout offset
    // and needs no scrollTop correction.
    const frame = page?.getBoundingClientRect();

    for (const f of findings) {
      const el = container ? blockElement(container, f.blockId) : null;
      if (frame && frame.height > 0 && el) {
        const box = el.getBoundingClientRect();
        next[f.id] = clamp01((box.top - frame.top + box.height / 2) / frame.height);
      } else {
        next[f.id] = fallbackPct(blocks, f.blockId);
      }
    }

    setPositions((prev) => (samePositions(prev, next) ? prev : next));
  }, [blocks, findings, scrollRef]);

  // Switching documents, running a check and editing a block all hand down new
  // blocks/findings arrays, so measure's identity is what "the document changed"
  // reduces to here: the observer is torn down and re-subscribed.
  useEffect(() => {
    const container = scrollRef.current;
    // jsdom has no ResizeObserver; the fallback positions already stand in there.
    if (!container || typeof ResizeObserver === "undefined") return;

    // observe() delivers an initial observation, so the subscription is also
    // what takes the first measurement. Measuring in the effect body instead
    // would read boxes and set state on every commit, cascading renders.
    const observer = new ResizeObserver(() => measure());
    observer.observe(container);
    return () => observer.disconnect();
  }, [measure, scrollRef]);

  // DOM order is document order, which is also tab order. Paint order is handled
  // by z-index instead, so the most serious tick still wins an overlap without
  // sending a keyboard user through the column backwards.
  const ordered = useMemo(() => {
    const at = new Map(doc.blocks.map((b, i) => [b.id, i]));
    return [...doc.findings].sort(
      (a, b) => (at.get(a.blockId) ?? 0) - (at.get(b.blockId) ?? 0) || a.start - b.start,
    );
  }, [doc.blocks, doc.findings]);

  const byBlock = useMemo(() => new Map(doc.blocks.map((b) => [b.id, b])), [doc.blocks]);

  const checking = doc.checkState === "checking";
  const sealed = isSealed(doc);
  const openCount = sortFindings(doc).filter((f) => !isDisposed(f) && f.severity !== "info").length;

  const status = checking
    ? "Checking this document."
    : doc.checkState === "unchecked"
      ? "Not checked yet. Run a check to place findings here."
      : doc.findings.length === 0
        ? "No findings in this document."
        : `${openCount} of ${doc.findings.length} findings still open.`;

  return (
    <div className={`st-spine${sealed ? " st-spine--clear" : ""}`} role="group" aria-label="Compliance spine">
      {checking && <div className="st-spine__scan" aria-hidden="true" />}
      <p className="studio__sr">{status}</p>

      {ordered.map((f) => {
        const pct = positions[f.id] ?? fallbackPct(blocks, f.blockId);
        const block = byBlock.get(f.blockId);
        const record = currentRecord(f);
        const disposed = isDisposed(f);
        const state = disposed
          ? ` is-recorded${record && isArgued(record.disposition) ? " is-argued" : ""}`
          : f.contested
            ? " is-contested"
            : isStale(block, f)
              ? " is-stale"
              : "";
        return (
          <button
            key={f.id}
            type="button"
            className={`st-spine__tick st-spine__tick--${f.severity}${state}${
              f.id === activeFindingId ? " is-active" : ""
            }`}
            // Recorded ticks sit under every open one, and the most serious open
            // tick wins any overlap. Paint order only; DOM order stays document order.
            style={{ top: `${pct * 100}%`, zIndex: disposed ? 1 : 10 - SEVERITY_RANK[f.severity] }}
            title={f.title}
            aria-label={tickLabel(f, block)}
            aria-current={f.id === activeFindingId ? "true" : undefined}
            onClick={() => onSelect(f.id)}
          />
        );
      })}
    </div>
  );
}
