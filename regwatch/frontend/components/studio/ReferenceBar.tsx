"use client";

import { psgDocxPath } from "@/lib/api";
import type { LibraryDoc } from "@/lib/studio-library";
import { safeHref } from "@/lib/url";

/**
 * The header strip above an open reference PSG. It takes the place of the
 * format bar, which belongs to a document the analyst is editing: a PSG is
 * read, taken away, or checked against its original, and those are the three
 * things offered here.
 *
 * The PDF stays one click away on purpose. What the canvas renders is text
 * extracted from that PDF -- faithful in words, not in layout, and without its
 * tables -- so the artifact FDA published has to remain reachable rather than
 * replaced.
 */
interface ReferenceBarProps {
  doc: LibraryDoc;
  /** True while the original PDF is showing instead of the extracted text. */
  showingPdf: boolean;
  /** True when the rebuild hit its cap and the text is incomplete. */
  truncated: boolean;
  onTogglePdf: () => void;
}

export function ReferenceBar({ doc, showingPdf, truncated, onTogglePdf }: ReferenceBarProps) {
  const href = safeHref(doc.sourceUrl);

  return (
    <div className="st-refbar">
      <span className="st-refbar__chips">
        <span className="st-chip">{doc.label}</span>
        {doc.recommendedDate ? <span className="st-chip">{doc.recommendedDate}</span> : null}
        <span className="st-chip">{doc.psgType === "final" ? "Final" : "Draft"}</span>
      </span>

      {truncated ? (
        <span className="st-refbar__warn" role="status">
          Extract incomplete - open the PDF for the full guidance.
        </span>
      ) : null}

      <span className="st-refbar__actions">
        {/* A plain link, not a fetch: the browser owns the download, and the
            same-origin path carries the session cookie a fetch+blob dance
            would have to re-create. `download` keeps the name the server
            already chose in Content-Disposition. */}
        <a className="st-btn st-btn--outline" href={psgDocxPath(doc.psgId)} download>
          Download .docx
        </a>
        <button type="button" className="st-btn st-btn--quiet" onClick={onTogglePdf}>
          {showingPdf ? "View text" : "View original PDF"}
        </button>
        {href ? (
          <a
            className="st-btn st-btn--quiet"
            href={href}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open on fda.gov
          </a>
        ) : null}
      </span>
    </div>
  );
}
