"use client";

// STAGING, NOT A DESIGN. Read this before you read the code.
//
// The Research Studio absorbs four surfaces as four artifact kinds. Exactly ONE
// of them -- the thread -- has been rebuilt as a sheet on the desk. The other
// three still render as they always did: this component mounts today's
// Assemble, Watch and White Paper views inside the desk, with the old shell's
// chrome (the .measure reading column and the PageHeader masthead) neutralised
// in research.css so they do not stack a second header under the top bar or a
// second measure inside the sheet's.
//
// Each of the three becomes a real sheet in its own later change, and until
// then this is scaffolding: it makes the new shell shippable without a
// four-surface rewrite in one diff. Nothing here is worth polishing. What IS
// worth knowing before you touch it:
//
//   - These are route modules imported as components. Legal, and the cheapest
//     honest way to mount a whole surface twice; it also means the old routes
//     keep working unchanged while this exists, which is the point of staging.
//   - They read the URL. White Paper writes ?run= onto whatever pathname it is
//     mounted at, so it will write onto /research and can drop ?kind=. The page
//     holds the active kind in state for exactly that reason (see page.tsx).
//   - They read CurrentProductProvider, which page.tsx mounts.
//
// Surface 05 Deficiency is deliberately absent. It leaves the UI here; its
// Python backend stays intact and reachable.

import AssemblePage from "@/app/(shell)/assemble/page";
import WatchPage from "@/app/(shell)/watch/page";
import WhitepaperPage from "@/app/(shell)/whitepaper/page";
import type { ArtifactKind } from "@/lib/research-types";

/** Every kind except the one that has a real sheet. */
export type LegacyKind = Exclude<ArtifactKind, "thread">;

const LEGACY_VIEW: Record<LegacyKind, React.ComponentType> = {
  dossier: AssemblePage,
  bulletin: WatchPage,
  paper: WhitepaperPage,
};

interface LegacyCenterProps {
  readonly kind: LegacyKind;
}

export function LegacyCenter({ kind }: LegacyCenterProps): React.ReactElement {
  const View = LEGACY_VIEW[kind];
  // Keyed by kind so switching kinds REMOUNTS rather than re-renders. These
  // views hold a surface's worth of local state (a built dossier, a loaded run,
  // a filter facet) and none of them was written to be reused for another
  // artifact; a remount is the cheap way to guarantee kind B never opens
  // showing kind A's result.
  return (
    <div className="rw-legacy" key={kind}>
      <View />
    </div>
  );
}
