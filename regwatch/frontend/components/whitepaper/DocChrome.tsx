"use client";

import type { FormTally } from "@/lib/whitepaper-form";

/**
 * How full the paper is, in one glance: cited ink, verified-absent hatching,
 * and the blanks still waiting. The proportions are the document's, so the bar
 * reads the same as the page under it.
 */
export function FillMeter({ tally, filled }: { tally: FormTally; filled: number }) {
  const total = Math.max(1, tally.total);
  const pct = (n: number) => `${(n / total) * 100}%`;
  return (
    <div className="wp-meter" title={`${tally.total} cells`}>
      <div className="wp-meter__bar" aria-hidden>
        <span className="wp-meter__seg wp-meter__seg--pop" style={{ width: pct(tally.populated) }} />
        <span className="wp-meter__seg wp-meter__seg--abs" style={{ width: pct(tally.absent) }} />
        <span
          className="wp-meter__seg wp-meter__seg--fill"
          style={{ width: pct(Math.min(filled, tally.pending)) }}
        />
        <span
          className="wp-meter__seg wp-meter__seg--gap"
          style={{ width: pct(Math.max(0, tally.pending - filled)) }}
        />
      </div>
      <span className="wp-meter__text">
        {tally.populated} cited
        <i />
        {tally.absent} absent
        <i />
        {Math.max(0, tally.pending - filled)} blank
      </span>
    </div>
  );
}

const STEPS = [0.5, 0.65, 0.8, 1, 1.25];

/** Fit-to-width by default, with the same discrete steps a word processor offers. */
export function ZoomControl({
  zoom,
  scale,
  onZoom,
}: {
  /** null means "fit to width". */
  zoom: number | null;
  scale: number;
  onZoom: (z: number | null) => void;
}) {
  const step = (dir: -1 | 1) => {
    const from = zoom ?? scale;
    const next = dir < 0 ? [...STEPS].reverse().find((s) => s < from - 0.01) : STEPS.find((s) => s > from + 0.01);
    onZoom(next ?? (dir < 0 ? STEPS[0] : STEPS[STEPS.length - 1]));
  };
  return (
    <div className="wp-zoom">
      <button className="wp-btn wp-btn--icon" type="button" aria-label="Zoom out" onClick={() => step(-1)}>
        -
      </button>
      <button
        className="wp-btn wp-btn--zoomlabel"
        type="button"
        onClick={() => onZoom(null)}
        aria-label={zoom === null ? "Zoom, fit to width" : "Fit to width"}
      >
        {zoom === null ? "Fit" : `${Math.round(zoom * 100)}%`}
      </button>
      <button className="wp-btn wp-btn--icon" type="button" aria-label="Zoom in" onClick={() => step(1)}>
        +
      </button>
    </div>
  );
}

export function StatusChip({ status }: { status: string }) {
  return <span className={`wp-status wp-status--${status}`}>{status}</span>;
}
