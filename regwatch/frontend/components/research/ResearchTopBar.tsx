"use client";

import { MenuIcon } from "@/components/studio/icons";

interface ResearchTopBarProps {
  /** What is on the sheet, named the way the work rail names it: the kind
   * first, then the artifact. "Dossiers / Metformin ANDA gap analysis". */
  readonly crumb: { readonly kindLabel: string; readonly title: string };
  onToggleWork: () => void;
}

export function ResearchTopBar({ crumb, onToggleWork }: ResearchTopBarProps): React.ReactElement {
  return (
    <header className="rw-top">
      <button
        type="button"
        className="rw-icon-btn rw-top__menu"
        aria-label="Show your work"
        onClick={onToggleWork}
      >
        <MenuIcon />
      </button>

      <span className="rw-top__mark">RW</span>
      <span className="rw-top__name">Research Studio</span>

      {/* The kind is context and the title is the thing, so only the title gets
          weight -- same split the Compliance Studio makes between a CTD path
          and a filename. */}
      <span className="rw-top__crumb">
        {crumb.kindLabel} / <b>{crumb.title}</b>
      </span>

      {/* The spacer is a real element rather than margin-left:auto on a trailing
          control, because there is no trailing control: this bar deliberately
          carries no exit link. The spine rail is always on screen now, so
          "exit" has nowhere to go -- leaving the studio means clicking another
          room in the spine, not backing out of this one. */}
      <div className="rw-top__spacer" />

      {/* SEAM: the product-under-review scope belongs at this end of the bar,
          fed by CurrentProductProvider. Deliberately no markup yet -- a picker
          with nothing behind it would claim the artifact is scoped to a product
          when it is not. Whoever wires CurrentProductProvider renders it here
          and gives .rw-top__spacer back its margin. */}
    </header>
  );
}
