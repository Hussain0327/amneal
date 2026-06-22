"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CitationStamp } from "@/components/CitationStamp";
import type { Citation } from "@/lib/api";
import { citationIndex, citeKey, segmentCitations, type CitePair } from "@/lib/citations";
import { safeHref } from "@/lib/url";

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
function remarkCitationStamps(index: Map<string, number>) {
  return function transform(tree: MdNode): void {
    function walk(node: MdNode): void {
      if (!node.children) return;
      const next: MdNode[] = [];
      for (const child of node.children) {
        if (child.type === "text") {
          next.push(...splitTextNode(child.value ?? "", index));
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
function splitTextNode(value: string, index: Map<string, number>): MdNode[] {
  const segments = segmentCitations(value);
  // Fast path: no citation brackets — return one untouched text node.
  if (segments.length === 1 && segments[0].kind === "text") return [{ type: "text", value }];

  const out: MdNode[] = [];
  const pushText = (v: string) => {
    if (v) out.push({ type: "text", value: v });
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

// Renders model/dossier markdown as editorial prose (see .prose in globals.css).
// When `citations` + `onCite` are supplied (answer/summary turns only), inline
// citation tags become clickable stamps wired to the evidence drawer. Omit them
// (refused / clarify / scope / meta) and the markdown renders verbatim with no
// stamps and no drawer trigger (INV-2).
export function Markdown({
  children,
  citations,
  onCite,
}: {
  children: string;
  citations?: Citation[];
  onCite?: (c: Citation) => void;
}) {
  const stampable = citations !== undefined && onCite !== undefined && citations.length > 0;
  const index = stampable ? citationIndex(citations) : null;
  const plugins = stampable ? [remarkGfm, () => remarkCitationStamps(index!)] : [remarkGfm];

  // The Components map is typed for known HTML tags; our custom "cite-stamp"
  // element isn't in that type, so build the map loosely and cast once.
  const components = {
    // Guard model-authored link schemes (no javascript:/data: click-to-run).
    a: ({ href, children }: { href?: string; children?: React.ReactNode }) => (
      <a href={safeHref(href)} target="_blank" rel="noreferrer">
        {children}
      </a>
    ),
    // Custom hast element emitted by remarkCitationStamps. Reads the matched
    // citation back out of the data-* attributes and renders the stamp. Only
    // wired when `stampable`, so refused/meta turns never produce one.
    "cite-stamp": (props: Record<string, unknown>) => {
      if (!stampable) return null;
      const n = Number(props["data-n"]);
      const short = String(props["data-short"]);
      const page = Number(props["data-page"]);
      const citation = citations!.find((c) => c.short_name === short && c.page === page);
      // Defensive: the index guaranteed a match, but never render a stamp
      // without its backing citation (INV-1).
      if (!citation) return null;
      return <CitationStamp n={n} citation={citation} onCite={onCite!} />;
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
