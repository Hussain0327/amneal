"use client";

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { useAuth } from "./AuthProvider";
import { useSettings } from "./SettingsProvider";

// Who is signed in, and what machinery answered — folded behind the rail's
// account button instead of standing in the shell all day. The colophon names
// the engines verbatim from /settings (never fabricated); reachability keeps
// its two honest states ("connecting…" before the first resolve, an oxblood
// notice when the backend can't be reached).

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function AccountPopover({ onClose }: { onClose: () => void }) {
  const { user, logout } = useAuth();
  const { settings, reachable } = useSettings();
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    openerRef.current = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const items = panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
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
      openerRef.current?.focus?.();
    };
  }, [onClose]);

  return createPortal(
    <div className="acct__root" role="presentation">
      <div className="acct__scrim" onClick={onClose} aria-hidden />
      <section
        ref={panelRef}
        className="acct"
        role="dialog"
        aria-modal="true"
        aria-label="Account and colophon"
      >
        <header className="acct__head">
          <div className="acct__who">
            {user && (
              <>
                <p className="acct__name">{user.display_name}</p>
                <p className="acct__email code">{user.email}</p>
              </>
            )}
          </div>
          <button
            ref={closeRef}
            type="button"
            className="acct__close"
            onClick={onClose}
            aria-label="Close account panel"
          >
            ×
          </button>
        </header>
        {user && (
          <button className="chip acct__signout" onClick={() => void logout()}>
            Sign out
          </button>
        )}
        <hr className="hair" />
        <p className="kicker acct__kicker">Colophon</p>
        {settings ? (
          <dl className="acct__colophon code">
            <div className="acct__row">
              <dt>Model</dt>
              <dd>
                {settings.llm_provider}/{settings.llm_model}
              </dd>
            </div>
            <div className="acct__row">
              <dt>Embeddings</dt>
              <dd>{settings.embedding_provider}</dd>
            </div>
          </dl>
        ) : reachable ? (
          <p className="acct__note code">connecting…</p>
        ) : (
          <p className="acct__note acct__note--down code" role="status">
            Can&rsquo;t reach RegWatch right now — retry in a moment.
          </p>
        )}
        {/* The product's standing contract, kept where the machinery is named. */}
        <p className="acct__creed">
          Public data only — RegWatch surfaces, organizes, and <em>cites</em>; it never authors
          submissions.
        </p>
      </section>
    </div>,
    document.body,
  );
}
