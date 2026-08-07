"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { deleteSession, type SessionSummary } from "@/lib/api";
import { withScope } from "@/lib/scope";
import { historyBucket, parseApiDate } from "@/lib/time";
import { useCurrentProduct } from "./CurrentProductProvider";
import { useSessions } from "./SessionsProvider";

// The conversation docket: the analyst's prior sessions, grouped by day in the
// viewer's local calendar (Today / Yesterday / This week / month labels) and
// filterable by title. Slides out from the spine rail; selecting a row routes
// the Ask page to /?session=<id> and closes the panel. Deletion asks for an
// inline confirm before committing, and a failed delete says so in place.

const FOCUSABLE = 'a[href], button:not([disabled]), input, [tabindex]:not([tabindex="-1"])';

// Compact relative time for a history row. parseApiDate applies the shared
// naive-UTC rule (a missing offset means UTC) so this can never disagree with
// the docket provenance stamps.
function relTime(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return "";
  const mins = Math.floor(Math.max(0, Date.now() - t) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d`;
  return new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// Absolute date for a row's hover title -- "Jan 5, 2026". en-US pinned so the
// rendering is deterministic under vitest regardless of host locale (same rule
// as lib/time.ts). Returns "" when the timestamp does not parse.
function absTime(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return "";
  return new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

interface Group {
  label: string;
  items: SessionSummary[];
}

export function HistoryPanel({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const { productParams } = useCurrentProduct();
  const { sessions, loaded, activeSessionId, setActiveSessionId, refresh } = useSessions();
  const [confirming, setConfirming] = useState<string | null>(null);
  // Session id whose delete failed -- drives the inline "couldn't delete" row
  // so a failure is never silent (the row would otherwise just reappear).
  const [deleteFailed, setDeleteFailed] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  // "Now" pinned at mount: the panel remounts on every open, and the buckets
  // are day-granular, so a static reference clock is both pure (the lint
  // contract for memo callbacks) and accurate for its lifetime.
  const [now] = useState(() => Date.now());
  const panelRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  // Same open-state contract as the evidence drawer: focus moves in on open
  // (to the filter, where a keyboard user goes next), Escape closes, Tab is
  // trapped, and focus returns to whichever control opened the panel.
  useEffect(() => {
    openerRef.current = document.activeElement as HTMLElement | null;
    searchRef.current?.focus();
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

  async function remove(id: string) {
    setConfirming(null);
    // A retry starts clean: the failure row clears on entry and comes back
    // only if this attempt also fails.
    setDeleteFailed(null);
    let ok = true;
    try {
      await deleteSession(id);
    } catch {
      // A 401 already routes to /login via the central handler; any other
      // failure means the row is still there — don't clear the active session
      // or navigate away as if the delete had succeeded.
      ok = false;
      setDeleteFailed(id);
    }
    if (ok && id === activeSessionId) {
      setActiveSessionId(null);
      // Only reset the Ask page if we're on it; don't yank other pages. Keep
      // the scoped product on the reset URL.
      if (pathname === "/") router.replace(withScope("/", productParams));
    }
    await refresh();
  }

  // Newest first regardless of server ordering, filtered by title, then
  // bucketed into contiguous day groups.
  const groups = useMemo<Group[]>(() => {
    const needle = query.trim().toLowerCase();
    const filtered = sessions
      .filter((s) => !needle || s.title.toLowerCase().includes(needle))
      .slice()
      .sort((a, b) => (parseApiDate(b.updated_at) ?? 0) - (parseApiDate(a.updated_at) ?? 0));
    const out: Group[] = [];
    for (const s of filtered) {
      const label = historyBucket(s.updated_at, now);
      const tail = out[out.length - 1];
      if (tail && tail.label === label) tail.items.push(s);
      else out.push({ label, items: [s] });
    }
    return out;
  }, [sessions, query, now]);

  return createPortal(
    <div className="histpanel__root" role="presentation">
      {/* Transparent click-catcher: the panel is a quick switcher, so the page
          behind stays legible -- but any outside click closes it. */}
      <div className="histpanel__scrim" onClick={onClose} aria-hidden />
      <section
        ref={panelRef}
        className="histpanel"
        role="dialog"
        aria-modal="true"
        aria-label="Conversation history"
      >
        <header className="histpanel__head">
          <span className="kicker">History{loaded ? ` · ${sessions.length}` : ""}</span>
          <Link
            href={withScope("/", productParams)}
            className="code link histpanel__new"
            onClick={() => {
              setActiveSessionId(null);
              onClose();
            }}
          >
            + New chat
          </Link>
          <button type="button" className="histpanel__close" onClick={onClose} aria-label="Close history">
            ×
          </button>
        </header>
        <input
          ref={searchRef}
          className="field histpanel__search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter conversations…"
          aria-label="Filter conversations"
        />
        <div className="histpanel__scroll">
          {!loaded ? (
            <p className="histpanel__note code">…</p>
          ) : sessions.length === 0 ? (
            <p className="histpanel__note code">No conversations yet — ask something and it files here.</p>
          ) : groups.length === 0 ? (
            <p className="histpanel__note code">No conversations match.</p>
          ) : (
            groups.map((g) => (
              <div key={g.label} className="histpanel__group">
                <p className="kicker histpanel__day">{g.label}</p>
                {g.items.map((s) => (
                  <HistoryRow
                    key={s.id}
                    s={s}
                    active={s.id === activeSessionId}
                    confirming={confirming === s.id}
                    failed={deleteFailed === s.id}
                    productParams={productParams}
                    onOpen={() => {
                      setActiveSessionId(s.id);
                      onClose();
                    }}
                    onAskDelete={() => setConfirming(s.id)}
                    onCancelDelete={() => setConfirming(null)}
                    onDelete={() => void remove(s.id)}
                    onDismissFailure={() => setDeleteFailed(null)}
                  />
                ))}
              </div>
            ))
          )}
        </div>
      </section>
    </div>,
    document.body,
  );
}

function HistoryRow({
  s,
  active,
  confirming,
  failed,
  productParams,
  onOpen,
  onAskDelete,
  onCancelDelete,
  onDelete,
  onDismissFailure,
}: {
  s: SessionSummary;
  active: boolean;
  confirming: boolean;
  failed: boolean;
  productParams: string;
  onOpen: () => void;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onDelete: () => void;
  onDismissFailure: () => void;
}) {
  const rel = relTime(s.updated_at);
  return (
    <div className={`hist${active ? " hist--active" : ""}`}>
      {confirming ? (
        <div className="hist__confirm">
          <span>delete?</span>
          <button onClick={onDelete}>yes</button>
          <button onClick={onCancelDelete}>no</button>
        </div>
      ) : failed ? (
        // The delete did NOT happen: say so where it failed, with a way to
        // retry or stand down. role=alert so a screen reader hears the
        // failure without hunting for the row.
        <div className="hist__confirm" role="alert">
          <span>{"couldn't delete"}</span>
          <button onClick={onDelete}>retry</button>
          <button onClick={onDismissFailure}>dismiss</button>
        </div>
      ) : (
        <>
          <Link
            href={withScope(`/?session=${encodeURIComponent(s.id)}`, productParams)}
            className="hist__main"
            title={`${s.title} · ${absTime(s.updated_at)} · ${s.message_count} messages`}
            onClick={onOpen}
          >
            <span className="hist__title">{s.title}</span>
            {/* Visible: just the compact time -- the row reads as a title, not
                a data dump. The message count still reaches screen readers
                (sr-only tail) and sighted users via the hover title. */}
            <span className="hist__time">
              {rel}
              <span className="sr-only">{` — ${s.message_count} messages`}</span>
            </span>
          </Link>
          <button
            className="hist__del"
            aria-label={`Delete conversation "${s.title}"`}
            onClick={onAskDelete}
          >
            ×
          </button>
        </>
      )}
    </div>
  );
}
