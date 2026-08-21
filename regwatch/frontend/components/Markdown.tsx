"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CitationStamp } from "@/components/CitationStamp";
import type { Citation } from "@/lib/api";
import { citationIndex, citeKey, dedupeCitations, segmentCitations, type CitePair } from "@/lib/citations";
import { safeHref } from "@/lib/url";

// A bare numeric marker bracket in prose: "[1]" or the compound "[1, 2]".
// Meaningless on its own -- it becomes a stamp ONLY when the caller supplies a
// trailer-derived marker map AND the mapped pair matches a validated citation.
const NUM_BRACKET = /\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]/g;

// Minimal local mdast shapes (we only touch type/value/children), so this file
// carries no hard dependency on @types/mdast — which is a transitive package and
// could de-hoist on a clean install.
interface MdNode {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: unknown;
}

// A stamp node carrying its payload through remark -> rehype. data.hName makes
// remark-rehype emit a hast element named "cite-stamp"; the citation index + the
// matched (short_name,page) ride in hProperties as data-* attributes (the only
// typed channel through hast custom elements). The `components` map below reads
// them back and renders <CitationStamp/>.
function stampNode(n: number, pair: CitePair): MdNode {
  return {
    type: "cite-stamp",
    data: {
      hName: "cite-stamp",
      hProperties: { "data-n": n, "data-short": pair.shortName, "data-page": pair.page },
    },
  };
}

// Remark plugin: replace LITERAL "[short_name, p.N]" tags in answer prose with
// stamp nodes, but ONLY for pairs that matched a real citation on the turn
// (INV-1). It visits mdast `text` nodes only — text inside code spans/blocks is
// an `inlineCode`/`code` node's `.value` string, never a `text` node, so code is
// untouched for free (the "tags inside code are not transformed" rule). Links,
// tables, lists keep working because their text children flow through unchanged.
function remarkCitationStamps(index: Map<string, number>, markers: Map<number, CitePair> | null) {
  return function transform(tree: MdNode): void {
    function walk(node: MdNode): void {
      if (!node.children) return;
      const next: MdNode[] = [];
      for (const child of node.children) {
        if (child.type === "text") {
          next.push(...splitTextNode(child.value ?? "", index, markers));
        } else {
          walk(child);
          next.push(child);
        }
      }
      node.children = next;
    }
    walk(tree);
  };
}

// Split one text node's value into text + stamp nodes. An unmatched pair (no real
// citation) keeps the bracket as literal text — never fabricated (INV-1).
function splitTextNode(
  value: string,
  index: Map<string, number>,
  markers: Map<number, CitePair> | null,
): MdNode[] {
  const segments = segmentCitations(value);
  const out: MdNode[] = [];
  const pushText = (v: string) => {
    if (!v) return;
    // Text between (or without) tag brackets may still carry bare numeric
    // markers -- resolve those through the trailer map where one exists.
    if (markers) {
      out.push(...splitMarkerText(v, index, markers));
    } else {
      out.push({ type: "text", value: v });
    }
  };
  for (const seg of segments) {
    if (seg.kind === "text") {
      pushText(seg.value);
      continue;
    }
    // A citation bracket: emit a stamp for each matched pair, keep the bracket
    // literal if NONE matched (so a hallucinated [PSG_999999, p.1] stays prose).
    const matched: { n: number; pair: CitePair }[] = [];
    for (const pair of seg.pairs) {
      const n = index.get(citeKey(pair.shortName, pair.page));
      if (n !== undefined) matched.push({ n, pair });
    }
    if (matched.length === 0) {
      pushText(seg.raw);
      continue;
    }
    // De-dupe within the bracket so a compound "[A, p.1; A, p.1]" stamps once.
    const seen = new Set<number>();
    for (const { n, pair } of matched) {
      if (seen.has(n)) continue;
      seen.add(n);
      out.push(stampNode(n, pair));
    }
  }
  return out;
}

// Resolve bare numeric markers ("[1]", "[1, 2]") against the trailer map. Each
// marker becomes a stamp only when its trailer pair matches a validated
// citation; the stamp DISPLAYS the canonical deduped index (the same [n] the
// reference list shows), not the model's marker, so numbering never forks. A
// bracket where nothing resolves stays literal prose (INV-1), mirroring the
// tag-bracket rule above.
function splitMarkerText(
  value: string,
  index: Map<string, number>,
  markers: Map<number, CitePair>,
): MdNode[] {
  const out: MdNode[] = [];
  let last = 0;
  NUM_BRACKET.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = NUM_BRACKET.exec(value)) !== null) {
    const matched: { n: number; pair: CitePair }[] = [];
    for (const raw of m[1].split(/[,;]/)) {
      const pair = markers.get(Number(raw.trim()));
      if (!pair) continue;
      const n = index.get(citeKey(pair.shortName, pair.page));
      if (n !== undefined) matched.push({ n, pair });
    }
    if (matched.length === 0) continue; // stays literal inside surrounding text
    if (m.index > last) out.push({ type: "text", value: value.slice(last, m.index) });
    const seen = new Set<number>();
    for (const { n, pair } of matched) {
      if (seen.has(n)) continue;
      seen.add(n);
      out.push(stampNode(n, pair));
    }
    last = m.index + m[0].length;
  }
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out;
}

/**
 * Renders model/dossier markdown as editorial prose (see .prose in globals.css).
 * When `citations` + `onCite` are supplied (answer/summary turns only), inline
 * citation tags become clickable stamps wired to the evidence drawer. Omit them
 * (refused / clarify / scope / meta) and the markdown renders verbatim with no
 * stamps and no drawer trigger (INV-2). `plainLinks` renders `a` elements as
 * inert <span> text (no href, no gold register) for UNVALIDATED surfaces --
 * the streaming draft must never carry a clickable affordance.
 */
export function Markdown({
  children,
  citations,
  onCite,
  markers,
  plainLinks = false,
}: {
  children: string;
  citations?: Citation[];
  onCite?: (c: Citation) => void;
  // Trailer-derived bare-marker map ([n] -> its bibliography pair), supplied
  // only alongside citations/onCite. Markers resolve through the validated
  // index like any tag; without this map a bare [n] always stays literal.
  markers?: Map<number, CitePair>;
  plainLinks?: boolean;
}): React.JSX.Element {
  const stampable = citations !== undefined && onCite !== undefined && citations.length > 0;
  // Index AND stamp resolution both come from the SAME deduped list: a
  // duplicated wire citation must not leave holes in [n] numbering, and [n]
  // must open the citation the index numbered (bijective, INV-1).
  const deduped = stampable ? dedupeCitations(citations) : null;
  const index = deduped ? citationIndex(deduped) : null;
  const plugins = index
    ? [remarkGfm, () => remarkCitationStamps(index, markers ?? null)]
    : [remarkGfm];

  // The Components map is typed for known HTML tags; our custom "cite-stamp"
  // element isn't in that type, so build the map loosely and cast once.
  const components = {
    // Guard model-authored link schemes (no javascript:/data: click-to-run).
    // In plainLinks mode the anchor collapses to inert text entirely: the
    // draft's muted register must not offer a clickable gold affordance.
    a: plainLinks
      ? ({ children }: { children?: React.ReactNode }) => <span>{children}</span>
      : ({ href, children }: { href?: string; children?: React.ReactNode }) => (
          <a href={safeHref(href)} target="_blank" rel="noreferrer">
            {children}
          </a>
        ),
    // A GFM table (the renderer's pathway matrix: options in columns, studies
    // in rows) can be wider than the chat column. Scroll it inside its own
    // box rather than letting it push the whole reply sideways; the body
    // must never scroll horizontally.
    table: ({ children }: { children?: React.ReactNode }) => (
      <div className="prose__scroll">
        <table>{children}</table>
      </div>
    ),
    // Custom hast element emitted by remarkCitationStamps. Reads the matched
    // citation back out of the data-* attributes and renders the stamp. Only
    // wired when `stampable`, so refused/meta turns never produce one.
    "cite-stamp": (props: Record<string, unknown>) => {
      // deduped is non-null exactly when stampable (narrowing TS can follow).
      if (!deduped || onCite === undefined) return null;
      const n = Number(props["data-n"]);
      const short = String(props["data-short"]);
      const page = Number(props["data-page"]);
      // Resolve by n: the 1-based position in the SAME deduped array the index
      // was built from -- resolving against the raw array would open the wrong
      // citation whenever a duplicate shifted the numbering. data-short carries
      // the MODEL-ECHOED casing (backend validation is case-insensitive), so a
      // strict name match can miss a valid stamp; the name+page find is only a
      // fallback for an out-of-range n.
      const citation =
        deduped[n - 1] ?? deduped.find((c) => c.short_name === short && c.page === page);
      // Defensive: never render a stamp without its backing citation (INV-1).
      if (!citation) return null;
      return <CitationStamp n={n} citation={citation} onCite={onCite} />;
    },
  } as Components;

  return (
    <div className="prose">
      <ReactMarkdown remarkPlugins={plugins} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
