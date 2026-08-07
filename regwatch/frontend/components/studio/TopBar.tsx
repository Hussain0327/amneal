"use client";

import Link from "next/link";

import { MenuIcon } from "@/components/studio/icons";
import type { StudioDoc } from "@/lib/studio-types";

interface TopBarProps {
  doc: StudioDoc;
  /** Set while a reference-library PSG is on the canvas; `doc` is then the
   * retained draft, and the bar must describe the PSG instead. */
  library: { ingredient: string; drugLabel: string } | null;
  onToggleTree: () => void;
}

export function TopBar({ doc, library, onToggleTree }: TopBarProps) {
  // "Saved" has to mean saved. Once the analyst edits a checked document the
  // findings stop describing the text, and the bar says so rather than claiming
  // a state the document is not in. A reference PSG has no version and nothing
  // savable, so it never claims either.
  const state =
    doc.checkState === "stale"
      ? `Edited since last check - v${doc.version}`
      : `All changes saved - v${doc.version}`;

  return (
    <header className="st-top">
      <button type="button" className="st-icon-btn st-top__menu" aria-label="Show repository" onClick={onToggleTree}>
        <MenuIcon />
      </button>

      <span className="st-top__mark">RW</span>
      <span className="st-top__name">Compliance Studio</span>

      {library ? (
        <span className="st-top__crumb">
          Reference library / {library.drugLabel} / <b>PSG: {library.ingredient}</b>
        </span>
      ) : (
        <span className="st-top__crumb">
          {doc.path} / <b>{doc.name}</b>
        </span>
      )}

      <div className="st-top__state">
        <span className="st-top__dot" />
        {library ? "Read-only - FDA reference" : state}
      </div>

      <Link href="/" className="st-top__exit">
        Exit studio
      </Link>
    </header>
  );
}
