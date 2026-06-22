"use client";

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { RecencyBadge } from "@/components/RecencyBadge";
import type { Citation } from "@/lib/api";
import { safeHref } from "@/lib/url";

// Slide-in panel showing ONE already-validated citation beside the answer, so an
// analyst can verify the cited passage without leaving RegWatch for a remote
// 50-page FDA PDF. Pure presentation over data already on the wire: the snippet
// (rendered as a styled quote — NOT an exact-span highlight; no char offsets
// exist), the page, and the source link that used to be the chip's only
// affordance. Reachable only from answer/summary citations (see Turns.tsx), so it
// never surfaces on a refused / clarify / scope_warning turn (INV-2).

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function EvidenceDrawer({
  citation,
  onClose,
}: {
  citation: Citation | null;
  // Must be stable (useCallback) — the focus effect keys on it.
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const open = citation !== null;

  useEffect(() => {
    if (!open) return;
    // Remember who opened us so focus returns there on close — keyboard users
    // must not be dumped at the top of the document.
    openerRef.current = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    // Lock background scroll: the panel is 100vh fixed over a full-viewport scrim,
    // so the page behind it must not scroll underneath while open.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation(); // don't also trip page-level Escape handlers
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const items = panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      // Minimal focus trap: keep Tab within the open panel.
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = prevOverflow;
      openerRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!citation) return null;

  return createPortal(
    <div className="evidence" role="presentation">
      <div className="evidence__scrim" onClick={onClose} aria-hidden />
      <aside
        ref={panelRef}
        className="evidence__panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Source: ${citation.short_name}, page ${citation.page}`}
      >
        <header className="evidence__head">
          <p className="kicker" style={{ margin: 0 }}>
            Evidence
          </p>
          <button ref={closeRef} type="button" className="evidence__close" onClick={onClose} aria-label="Close evidence">
            ×
          </button>
        </header>
        <p className="evidence__src code">
          {citation.short_name} · p.{citation.page}
          {/* Numeric retrieval score lives here only — the main view shows a
              coarse band so a near-threshold answer reads hedged, not precise. */}
          {typeof citation.score === "number" && ` · score ${citation.score.toFixed(2)}`}
        </p>
        <RecencyBadge c={citation} />
        <blockquote className="ref__quote evidence__quote">{citation.snippet}</blockquote>
        <a className="link code evidence__link" href={safeHref(citation.source_url)} target="_blank" rel="noreferrer">
          Open source PDF ↗
        </a>
      </aside>
    </div>,
    document.body,
  );
}
