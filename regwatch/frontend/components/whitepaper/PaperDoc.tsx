"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

// The CRA template's own page setup, read off the .docx: landscape US Letter
// (w:pgSz 15840x12240 twips) with 720-twip margins. At 96dpi that is exactly
// the geometry below, so the page on screen and the page that prints are the
// same page.
export const PAGE_W = 1056; // 11in
export const PAGE_H = 816; // 8.5in
export const MARGIN = 48; // 0.5in
export const CONTENT_W = PAGE_W - MARGIN * 2; // 960
const HEAD_H = 30; // running head strip, inside the top margin box
const FOOT_H = 26; // "Page n of m" strip
const GUTTER = 26; // space between sheets on screen
const FLOW_H = PAGE_H - MARGIN * 2 - HEAD_H - FOOT_H;

export interface FlowItem {
  key: string;
  node: React.ReactNode;
  /** A section band must never print as the last thing on a page. */
  keepWithNext?: boolean;
  /** Which table this item belongs to, for the continued-on-next-sheet head. */
  section?: string;
}

interface Layout {
  /** Absolute y of each page's top edge, within the document stack. */
  pageTops: number[];
  pageHeights: number[];
  /** Index of the item each page opens with. */
  firstItems: number[];
  /** Extra margin-top that pushes the first item of each page onto that page. */
  offsets: Map<number, number>;
  docHeight: number;
  key: string;
}

// One flow, N sheets drawn behind it. Splitting the items into per-page
// containers would remount every row whenever pagination shifted -- and a
// remount mid-edit would close an open note editor and drop its text. Pushing
// the first item of each page down with a margin keeps the DOM order, and every
// component instance, stable no matter how the breaks move.
function layOut(heights: number[], keepWithNext: boolean[]): Layout {
  const pages: number[][] = [];
  let current: number[] = [];
  let used = 0;

  for (let i = 0; i < heights.length; i += 1) {
    const h = heights[i] ?? 0;
    // A band is packed together with the row under it, so it can only start a
    // page it can actually hold something on.
    const withNext = keepWithNext[i] ? (heights[i + 1] ?? 0) : 0;
    if (current.length > 0 && used + h + withNext > FLOW_H) {
      pages.push(current);
      current = [];
      used = 0;
    }
    current.push(i);
    used += h;
  }
  if (current.length > 0 || pages.length === 0) pages.push(current);

  const pageHeights: number[] = [];
  const pageTops: number[] = [];
  const firstItems: number[] = [];
  const offsets = new Map<number, number>();
  let top = 0;
  let flowBottom = 0;

  for (const items of pages) {
    const contentH = items.reduce((sum, i) => sum + (heights[i] ?? 0), 0);
    // A single row taller than the printable area grows its own sheet rather
    // than spilling past the edge: an oversized cell stays visible, never clipped.
    const pageH = Math.max(PAGE_H, MARGIN * 2 + HEAD_H + FOOT_H + contentH);
    pageTops.push(top);
    pageHeights.push(pageH);
    const contentTop = top + MARGIN + HEAD_H;
    if (items.length > 0) {
      offsets.set(items[0], contentTop - flowBottom);
      firstItems.push(items[0]);
      flowBottom = contentTop + contentH;
    }
    top += pageH + GUTTER;
  }

  const key = `${pageHeights.join(",")};${Array.from(offsets, ([i, v]) => `${i}:${Math.round(v)}`).join(",")}`;
  return { pageTops, pageHeights, firstItems, offsets, docHeight: top - GUTTER, key };
}

/**
 * A paginated facsimile of the printed white paper.
 *
 * Heights are measured off the real DOM -- there is no hidden duplicate copy,
 * because duplicating the rows would duplicate their form fields too -- and the
 * breaks are recomputed from those heights. Until a measurement succeeds, and
 * anywhere it cannot (server render, jsdom, a viewport too narrow for a
 * landscape sheet), the document renders as one continuous sheet with every
 * cell present.
 */
export function PaperDoc({
  items,
  runningHead,
  scale,
  paged = true,
  stamp,
  onLayout,
}: {
  items: FlowItem[];
  runningHead: string;
  scale: number;
  /** false on narrow viewports: the page metaphor gives way to a single column. */
  paged?: boolean;
  /** Rendered once, over the first sheet: the DRAFT / FINAL mark. */
  stamp?: React.ReactNode;
  onLayout?: (pageCount: number) => void;
}) {
  const flowRef = useRef<HTMLDivElement>(null);
  const [measured, setLayout] = useState<Layout | null>(null);
  const signature = items.map((i) => i.key).join("|");

  const measure = useCallback(() => {
    const flow = flowRef.current;
    if (!flow) return;
    const kids = Array.from(flow.children) as HTMLElement[];
    const heights = kids.map((el) => el.offsetHeight);
    // Nothing has laid out yet: stay unpaginated rather than packing the whole
    // document onto a page-1-of-46 that measures zero.
    if (heights.length === 0 || heights.every((h) => h === 0)) {
      setLayout((prev) => (prev === null ? prev : null));
      return;
    }
    const keep = kids.map((el) => el.dataset.keepWithNext === "1");
    const next = layOut(heights, keep);
    setLayout((prev) => (prev !== null && prev.key === next.key ? prev : next));
  }, []);

  useLayoutEffect(() => {
    if (!paged) return;
    measure();
  }, [measure, paged, signature]);

  useEffect(() => {
    const flow = flowRef.current;
    if (!paged || !flow || typeof ResizeObserver === "undefined") return;
    // Re-measure whenever anything inside changes height: a webfont landing, a
    // note editor opening, a saved value wrapping onto a second line.
    const ro = new ResizeObserver(() => measure());
    ro.observe(flow);
    for (const kid of Array.from(flow.children)) ro.observe(kid);
    return () => ro.disconnect();
  }, [measure, paged, signature]);

  // Derived, not stored: a narrow viewport unpaginates by reading the flag at
  // render time, so the measured layout survives a resize back to full width.
  const layout = paged ? measured : null;
  const pageCount = layout ? layout.pageHeights.length : 1;
  useEffect(() => {
    onLayout?.(pageCount);
  }, [onLayout, pageCount]);

  const width = paged ? PAGE_W : undefined;

  return (
    <div
      className={`wp-stage${layout ? "" : " wp-stage--flow"}`}
      style={layout ? { width: PAGE_W * scale, height: layout.docHeight * scale } : undefined}
    >
      <div
        className={`wp-doc${layout ? "" : " wp-doc--flow"}`}
        style={
          layout
            ? { width, height: layout.docHeight, transform: `scale(${scale})` }
            : undefined
        }
      >
        {layout && (
          <div className="wp-doc__sheets" aria-hidden>
            {layout.pageHeights.map((h, i) => (
              <div
                key={i}
                className="wp-sheet"
                style={{ top: layout.pageTops[i], height: h, width: PAGE_W }}
              >
                <div className="wp-sheet__head">
                  {runningHead}
                  {continuedOn(items, layout.firstItems[i]) && (
                    <span className="wp-sheet__cont">
                      {items[layout.firstItems[i]].section} (continued)
                    </span>
                  )}
                </div>
                <div className="wp-sheet__foot">
                  Page {i + 1} of {pageCount}
                </div>
              </div>
            ))}
          </div>
        )}
        {stamp}

        <div
          className="wp-doc__flow"
          ref={flowRef}
          style={layout ? { width: CONTENT_W } : undefined}
        >
          {items.map((item, i) => (
            <div
              key={item.key}
              data-keep-with-next={item.keepWithNext ? "1" : undefined}
              data-page-start={layout?.offsets.has(i) ? "1" : undefined}
              style={layout?.offsets.has(i) ? { marginTop: layout.offsets.get(i) } : undefined}
            >
              {item.node}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// A sheet that opens partway through a table repeats which table it is, the way
// a word processor repeats a table's header row across a page break.
function continuedOn(items: FlowItem[], first: number | undefined): boolean {
  if (first === undefined || first <= 0) return false;
  const item = items[first];
  if (!item?.section || item.keepWithNext) return false;
  return items[first - 1]?.section === item.section;
}

/**
 * Fit the page to the space it has, the way a word processor does: never wider
 * than 100%, never so small it stops being readable. Falls back to 1 wherever
 * the container cannot be measured.
 */
export function useFitScale(ref: React.RefObject<HTMLElement | null>, zoom: number | null): number {
  const [fit, setFit] = useState(1);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const read = () => {
      const w = el.clientWidth;
      if (w > 0) setFit(Math.min(1, Math.max(0.42, w / PAGE_W)));
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);

  return zoom ?? fit;
}

/** True when the viewport is too narrow to hold a landscape sheet. */
export function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(`(max-width: ${PAGE_W - 40}px)`);
    const read = () => setNarrow(mq.matches);
    read();
    mq.addEventListener("change", read);
    return () => mq.removeEventListener("change", read);
  }, []);
  return narrow;
}
