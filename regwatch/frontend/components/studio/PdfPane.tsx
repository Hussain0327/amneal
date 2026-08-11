"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { psgPdfPath } from "@/lib/api";
import type { LibraryDoc } from "@/lib/studio-library";
import { safeHref } from "@/lib/url";

/**
 * The probe's own timeout. An <iframe> gives no reliable signal for an HTTP
 * error -- a 404 JSON body just renders as text inside the frame -- so the
 * pane HEADs the PDF path first and only mounts the frame on a 2xx. The
 * backend answers HEAD from the DB row alone, so this is fast even when the
 * PDF itself would need an fda.gov fetch.
 */
const PROBE_TIMEOUT_MS = 15_000;

interface PdfPaneProps {
  /** The open reference PSG. The page mounts this component with
   * key={doc.id}, so a doc change always remounts -- no reset effects. */
  doc: LibraryDoc;
}

type PaneStatus = "loading" | "ready" | "error";

export function PdfPane({ doc }: PdfPaneProps) {
  const [status, setStatus] = useState<PaneStatus>("loading");
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Bumped by Retry so the probe effect re-runs.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // Status is already "loading" on mount (initial state) and on retry (the
    // retry handler resets it), so the effect never sets state synchronously.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
    let cancelled = false;
    fetch(psgPdfPath(doc.psgId), {
      method: "HEAD",
      credentials: "include",
      signal: controller.signal,
    })
      // 405 is an error on purpose: the backend ships an explicit HEAD
      // handler, so a 405 means the probe is hitting a build without it.
      .then((res) => {
        if (!cancelled) setStatus(res.ok ? "ready" : "error");
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      })
      .finally(() => clearTimeout(timer));
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [doc.psgId, attempt]);

  // Opening a PSG is always user-initiated: land keyboard focus on the new
  // content's heading so the switch is perceivable without a pointer.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const retry = useCallback(() => {
    setStatus("loading");
    setAttempt((n) => n + 1);
  }, []);

  const href = safeHref(doc.sourceUrl);
  const linkOut = href ? (
    <a
      className="st-pdf__link st-btn st-btn--outline"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
    >
      Open on fda.gov
    </a>
  ) : (
    // Keep the slot so the header never reflows; an unsafe or missing URL
    // renders inert rather than binding an unguarded href. Borrows the
    // .st-btn disabled treatment (opacity, not-allowed cursor) without being
    // a real button.
    <span className="st-pdf__link--inert st-btn st-btn--outline">No source link</span>
  );

  return (
    <section className="st-pdf" aria-label={`Reference PSG: ${doc.ingredient}`}>
      <div className="st-pdf__head">
        <h2 className="st-pdf__title" tabIndex={-1} ref={headingRef}>
          PSG: {doc.ingredient}
        </h2>
        <span className="st-pdf__meta">
          <span className="st-chip">{doc.label}</span>
          {doc.recommendedDate ? <span className="st-chip">{doc.recommendedDate}</span> : null}
          <span className="st-chip">{doc.psgType === "final" ? "Final" : "Draft"}</span>
        </span>
        {linkOut}
      </div>

      {status === "loading" && <div className="st-pdf__note">Loading PDF...</div>}
      {status === "ready" && (
        <iframe
          className="st-pdf__frame"
          src={psgPdfPath(doc.psgId)}
          title={`PSG PDF: ${doc.ingredient} - ${doc.label}`}
        />
      )}
      {status === "error" && (
        <div className="st-pdf__fallback" role="alert">
          <span>Couldn&apos;t load this PDF in the studio.</span>
          <button type="button" className="st-btn st-btn--quiet st-tree__retry" onClick={retry}>
            Retry
          </button>
          {href ? (
            <a
              className="st-pdf__link st-btn st-btn--outline"
              href={href}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open on fda.gov
            </a>
          ) : null}
        </div>
      )}
    </section>
  );
}
