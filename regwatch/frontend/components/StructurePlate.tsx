"use client";

import { useEffect, useRef, useState } from "react";

import { fetchStructures, type ChemistryStructure } from "@/lib/api";
import { safeHref } from "@/lib/url";

// A USP-monograph-style plate: draws a citation's active ingredient(s) from
// PubChem so the reader can confirm what the guidance is actually about
// without leaving the reply. Pure decoration over an already-cited answer --
// it owns its own fetch (mount + ingredient change) and NEVER delays or
// blocks the answer: nothing renders until data lands, and any non-200,
// network, or draw failure collapses to nothing rather than a broken card
// (see fetchStructures' contract in lib/api.ts). Nothing here uses gold: gold
// means "sourced guidance" on this surface, and a structure is chemistry, not
// a citation.

const MAX_STRUCTURES = 2;

// Every element key the library's default 'light' theme defines, forced to
// the app's ink so bonds/atoms never wear the library's per-element palette
// (red O, blue N, ...) -- the plate introduces no color scheme of its own.
const INK = "#16213a";
const PLATE_THEME = {
  FOREGROUND: INK,
  BACKGROUND: "transparent",
  C: INK,
  O: INK,
  N: INK,
  F: INK,
  CL: INK,
  BR: INK,
  I: INK,
  P: INK,
  S: INK,
  B: INK,
  SI: INK,
  H: INK,
};

/** "C13H21NO3 . 239.31 g/mol" (middle dot), or whichever half PubChem recorded; "" if neither. */
function metaLine(structure: ChemistryStructure): string {
  const parts: string[] = [];
  if (structure.molecular_formula) parts.push(structure.molecular_formula);
  if (structure.molecular_weight !== null) parts.push(`${structure.molecular_weight} g/mol`);
  return parts.join(" \u00b7 ");
}

/** One drawn structure: the SVG art plus its USP-style caption. */
function StructureCard({
  structure,
  compact,
}: {
  readonly structure: ChemistryStructure;
  readonly compact: boolean;
}): React.JSX.Element {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    let cancelled = false;
    // Clear a prior render before the new one lands (the structure changed
    // while a plate was already drawn).
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    // Dynamic import: the module touches `document` at draw time, so it must
    // never load at build/SSR time (next build --webpack must not break).
    import("smiles-drawer")
      .then((mod) => {
        if (cancelled) return;
        const SmilesDrawer = mod.default;
        const drawer = new SmilesDrawer.SvgDrawer({ themes: { light: PLATE_THEME } });
        SmilesDrawer.parse(
          structure.smiles,
          (tree) => {
            if (cancelled) return;
            try {
              drawer.draw(tree, svg, "light");
            } catch {
              // A draw failure renders nothing (the art box just stays
              // empty) -- silent in production, a breadcrumb in dev only.
              if (process.env.NODE_ENV !== "production") {
                console.warn("StructurePlate: failed to draw structure");
              }
            }
          },
          () => {
            if (process.env.NODE_ENV !== "production") {
              console.warn("StructurePlate: failed to parse SMILES");
            }
          },
        );
      })
      .catch(() => {
        // The module failed to load -- render nothing, same contract as a
        // fetch failure.
      });
    return () => {
      cancelled = true;
    };
  }, [structure.smiles]);

  return (
    <figure
      className={`plate${compact ? " plate--compact" : ""}`}
      aria-label={`Chemical structure of ${structure.name}`}
    >
      <div className="plate__art" aria-hidden="true">
        <svg ref={svgRef} />
      </div>
      <figcaption className="plate__cap">
        <span className="plate__name">{structure.name}</span>
        {metaLine(structure) && <span className="plate__meta code">{metaLine(structure)}</span>}
        <a
          className="plate__src code"
          href={safeHref(structure.source_url)}
          target="_blank"
          rel="noreferrer"
        >
          {`PubChem CID ${structure.pubchem_cid} `}
          <span aria-hidden="true">{"\u2197"}</span>
        </a>
        {!compact && (
          <span className="plate__note">
            {"Structure from PubChem; not part of the cited guidance."}
            {structure.match === "parent" ? " Parent compound shown." : ""}
          </span>
        )}
      </figcaption>
    </figure>
  );
}

/**
 * A USP-monograph-style plate for a citation's active ingredient(s), drawn
 * from PubChem. Fetches on mount and again whenever `ingredient` changes,
 * keyed on an AbortController so a fast unmount or re-key never races a stale
 * response into state. Renders null while loading, on error, and on an empty
 * result -- the card is decoration for a cited answer, never a load-bearing
 * part of it. Multi-structure responses render one plate per structure, up
 * to `MAX_STRUCTURES`.
 */
// Result state tagged with the ingredient it answers, so a still-mounted
// plate can tell a fresh key change from a settled fetch WITHOUT a
// synchronous setState reset at the top of the effect (react-hooks/
// set-state-in-effect): render derives "no data yet for this ingredient" by
// comparing tags instead.
interface FetchedFor {
  readonly ingredient: string;
  readonly structures: readonly ChemistryStructure[];
}

export function StructurePlate({
  ingredient,
  compact = false,
}: {
  readonly ingredient: string;
  readonly compact?: boolean;
}): React.JSX.Element | null {
  const [fetched, setFetched] = useState<FetchedFor | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchStructures(ingredient, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setFetched({ ingredient, structures: result });
      })
      .catch(() => {
        // Non-200 / network / abort: the card simply does not render.
        if (controller.signal.aborted) return;
        setFetched({ ingredient, structures: [] });
      });
    return () => controller.abort();
  }, [ingredient]);

  const structures = fetched && fetched.ingredient === ingredient ? fetched.structures : null;
  if (!structures || structures.length === 0) return null;

  return (
    <>
      {structures.slice(0, MAX_STRUCTURES).map((s) => (
        <StructureCard key={s.pubchem_cid} structure={s} compact={compact} />
      ))}
    </>
  );
}
