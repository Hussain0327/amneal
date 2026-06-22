"use client";

import type { Citation } from "@/lib/api";

// The signature grounding mark: an inline, claim-anchored citation stamp. Where a
// matched [short_name, p.N] tag sits in the answer prose, it becomes a small
// SQUARE gold "[n]" button that opens that exact citation in the evidence drawer.
// Square corners + IBM Plex Mono set it apart from the rounded .pill / .chip;
// gold is reserved to grounding, so a stamp reads as "this clause is sourced".
//
// INV-1: a stamp is only ever rendered for a tag whose (short_name, page) matched
// a real citation on the turn — see Markdown.tsx's remark plugin. This component
// never fabricates: it renders exactly the citation handed to it.
export function CitationStamp({
  n,
  citation,
  onCite,
}: {
  n: number;
  citation: Citation;
  onCite: (c: Citation) => void;
}) {
  return (
    <button
      type="button"
      className="cite-stamp"
      onClick={() => onCite(citation)}
      title={`${citation.short_name} · p.${citation.page}`}
      aria-label={`Source ${n}: ${citation.short_name}, page ${citation.page}`}
    >
      [{n}]
    </button>
  );
}
