"use client";

// The compliance spine: an 18px column of ticks, one per finding, each sitting
// at that finding's measured position in the document. It answers "where is this
// document weak, and how badly" without opening a panel.
//
// Positions are measured from the live DOM rather than derived from block index,
// because a two-line heading and a six-row table are not the same distance apart
// on the page, and a tick that points at the wrong place is worse than no tick.

import { useCallback, useEffect, useMemo, useState } from "react";

import { docGlyph, sortFindings } from "@/lib/studio-marks";
import type { Block, Finding, Severity, StudioDoc } from "@/lib/studio-types";

/** How each severity is named in a tick's accessible label. */
const SEVERITY_WORD: Record<Severity, string> = {
  critical: "Critical",
  major: "Major",
  minor: "Minor",
  info: "Info",
};

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

function tickLabel(f: Finding): string {
  const word = SEVERITY_WORD[f.severity];
  // Staleness is carried visually by opacity alone, so it has to be said here.
  return f.stale
    ? `Stale ${word.toLowerCase()} finding at ${f.location}: ${f.title}`
    : `${word} finding at ${f.location}: ${f.title}`;
}

interface ComplianceSpineProps {
  doc: StudioDoc;
  /** The document's scroll container. Ticks are measured against its content. */
  scrollRef: React.RefObject<HTMLDivElement>;
  activeFindingId: string | null;
  onSelect: (id: string) => void;
}

export function ComplianceSpine({ doc, scrollRef, activeFindingId, onSelect }: ComplianceSpineProps) {
  const [positions, setPositions] = useState<Record<string, number>>({});

  const { blocks, findings } = doc;

  const measure = useCallback(() => {
    const container = scrollRef.current;
    const next: Record<string, number> = {};

    for (const f of findings) {
      const el = container ? blockElement(container, f.blockId) : null;
      // scrollHeight is 0 until layout; dividing by it would stack every tick at
      // the top, which is a claim about the document that is not true.
      if (container && el && container.scrollHeight > 0) {
        const box = el.getBoundingClientRect();
        const frame = container.getBoundingClientRect();
        const centre = box.top - frame.top + container.scrollTop + box.height / 2;
        next[f.id] = clamp01(centre / container.scrollHeight);
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

  // Paint order, not reading order: sortFindings leads with the most serious for
  // the panel, so the spine walks it backwards and the worst tick lands on top
  // when two findings resolve to the same height.
  const painted = useMemo(() => [...sortFindings(doc)].reverse(), [doc]);

  const checking = doc.checkState === "checking";
  // Same rule as the tree glyph, deliberately: checked with nothing open. The
  // quiet gold thread and a solid file dot must never disagree.
  const isClear = docGlyph(doc) === "clean";

  const status = checking
    ? "Checking this document."
    : doc.checkState === "unchecked"
      ? "Not checked yet. Run a check to place findings here."
      : painted.length === 0
        ? "No findings in this document."
        : `${painted.length} finding${painted.length === 1 ? "" : "s"} on this document.`;

  return (
    <div className={`st-spine${isClear ? " st-spine--clear" : ""}`} role="group" aria-label="Compliance spine">
      {checking && <div className="st-spine__scan" aria-hidden="true" />}
      <p className="studio__sr">{status}</p>

      {painted.map((f) => {
        const pct = positions[f.id] ?? fallbackPct(blocks, f.blockId);
        const active = f.id === activeFindingId;
        return (
          <button
            key={f.id}
            type="button"
            className={`st-spine__tick st-spine__tick--${f.severity}${f.stale ? " is-stale" : ""}${
              active ? " is-active" : ""
            }`}
            style={{ top: `${pct * 100}%` }}
            title={f.title}
            aria-label={tickLabel(f)}
            aria-current={active ? "true" : undefined}
            onClick={() => onSelect(f.id)}
          />
        );
      })}
    </div>
  );
}
