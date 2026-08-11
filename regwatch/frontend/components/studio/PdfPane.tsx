"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { psgPdfPath } from "@/lib/api";
import type { LibraryDoc } from "@/lib/studio-library";

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

  return (
    <section className="st-pdf" aria-label={`Reference PSG: ${doc.ingredient}`}>
      {/* The chips, the download and the fda.gov link live on the reference
          bar above this pane, which stays put whether the analyst is reading
          the extracted text or the original PDF. The heading remains: it is
          the focus anchor on open and the element Escape-to-draft works from
          (COMPLIANCE_STUDIO.md section 10). */}
      <div className="st-pdf__head">
        <h2 className="st-pdf__title" tabIndex={-1} ref={headingRef}>
          PSG: {doc.ingredient}
        </h2>
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
        </div>
      )}
    </section>
  );
}
