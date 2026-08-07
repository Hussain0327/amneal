"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { deleteSession } from "@/lib/api";
import { parseApiDate } from "@/lib/time";
import { useAuth } from "./AuthProvider";
import { useCurrentProduct } from "./CurrentProductProvider";
import { useSessions } from "./SessionsProvider";
import { useSettings } from "./SettingsProvider";
import { Wordmark } from "./Wordmark";

// Append the scoped-product params to an in-app href so navigating between the
// four surfaces keeps the current product. `productParams` is "" when unset.
function withScope(href: string, productParams: string): string {
  if (!productParams) return href;
  return `${href}${href.includes("?") ? "&" : "?"}${productParams}`;
}

const NAV = [
  { href: "/", no: "01", label: "Ask", note: "Cited Q&A" },
  { href: "/assemble", no: "02", label: "Assemble", note: "Build a dossier" },
  { href: "/watch", no: "03", label: "Watch", note: "Change feed" },
  { href: "/whitepaper", no: "04", label: "White Paper", note: "Populate & cite" },
  { href: "/deficiency", no: "05", label: "Deficiency", note: "Scan a draft" },
];

// Compact relative time for the history list. parseApiDate applies the shared
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
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// Absolute date for a history row's hover title -- "Jan 5, 2026". en-US pinned
// so the rendering is deterministic under vitest regardless of host locale
// (same rule as lib/time.ts). Returns "" when the timestamp does not parse.
function absTime(iso: string): string {
  const t = parseApiDate(iso);
  if (t === null) return "";
  return new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

export function Sidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const { productParams } = useCurrentProduct();
  // Shared with the Ask page's confidence legend via SettingsProvider -- one
  // GET /settings for the whole shell instead of a private fetch here.
  const { settings, reachable } = useSettings();

  return (
    <aside className="sidebar">
      <Wordmark size="sm" />
      <div className="kicker" style={{ marginTop: "0.9rem", color: "var(--ink)" }}>
        REGWATCH
      </div>
      <p style={{ marginTop: "0.7rem", fontSize: "0.82rem", lineHeight: 1.5, color: "var(--ink-soft)" }}>
        FDA guidance intelligence. Public data only — it surfaces, organizes, and{" "}
        <em style={{ fontFamily: "var(--font-display), serif" }}>cites</em>; it never authors submissions.
      </p>

      <hr className="hair" style={{ margin: "1.5rem 0 1.1rem" }} />

      <nav style={{ display: "flex", flexDirection: "column", gap: "0.15rem" }}>
        {NAV.map((n) => {
          const active = pathname === n.href;
          return (
            <Link
              key={n.href}
              href={withScope(n.href, productParams)}
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: "0.7rem",
                padding: "0.7rem 0.65rem",
                borderRadius: "2px",
                textDecoration: "none",
                color: active ? "var(--ink)" : "var(--ink-2)",
                background: active ? "var(--paper-bright)" : "transparent",
                boxShadow: active ? "inset 3px 0 0 var(--gold)" : "inset 3px 0 0 transparent",
                transition: "background 0.15s ease, box-shadow 0.15s ease",
              }}
            >
              <span className="code" style={{ fontSize: "0.7rem", color: active ? "var(--gold-ink)" : "var(--ink-faint)" }}>
                {n.no}
              </span>
              <span>
                <span style={{ fontWeight: 600, fontSize: "0.98rem", display: "block" }}>{n.label}</span>
                <span style={{ fontSize: "0.74rem", color: "var(--ink-soft)" }}>{n.note}</span>
              </span>
            </Link>
          );
        })}
      </nav>

      <History />

      {/* User box + colophon, set like a print imprint */}
      <div style={{ marginTop: "auto", paddingTop: "1.4rem" }}>
        <hr className="hair" style={{ marginBottom: "0.9rem" }} />
        {user && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "0.6rem",
              marginBottom: "1rem",
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div className="kicker" style={{ fontSize: "0.6rem", color: "var(--ink-faint)" }}>
                Signed in
              </div>
              <div
                title={user.email}
                style={{
                  fontSize: "0.85rem",
                  fontWeight: 600,
                  color: "var(--ink)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {user.display_name}
              </div>
            </div>
            <button className="chip" onClick={() => void logout()}>
              Sign out
            </button>
          </div>
        )}
        <div className="kicker" style={{ fontSize: "0.6rem", color: "var(--ink-faint)", marginBottom: "0.5rem" }}>
          Colophon
        </div>
        {settings ? (
          <dl className="code" style={{ fontSize: "0.72rem", color: "var(--ink-soft)", lineHeight: 1.7, margin: 0 }}>
            <div>
              <span style={{ color: "var(--ink-faint)" }}>embed </span>
              {settings.embedding_provider}
            </div>
            <div>
              <span style={{ color: "var(--ink-faint)" }}>llm&nbsp;&nbsp; </span>
              {settings.llm_provider}/{settings.llm_model}
            </div>
          </dl>
        ) : reachable ? (
          <div className="code" style={{ fontSize: "0.72rem", color: "var(--ink-faint)" }}>
            connecting…
          </div>
        ) : (
          <div className="code" style={{ fontSize: "0.72rem", color: "var(--oxblood)" }}>
            Can&rsquo;t reach RegWatch right now — retry in a moment.
          </div>
        )}
      </div>
    </aside>
  );
}

// The user's prior conversations. Selecting one routes the Ask page to
// /?session=<id>; deletion asks for an inline confirm before committing. Links
// carry the scoped product so switching conversations keeps the current focus.
function History() {
  const router = useRouter();
  const pathname = usePathname();
  const { productParams } = useCurrentProduct();
  const { sessions, loaded, activeSessionId, setActiveSessionId, refresh } = useSessions();
  const [confirming, setConfirming] = useState<string | null>(null);
  // Session id whose delete failed -- drives the inline "couldn't delete" row
  // so a failure is never silent (the row would otherwise just reappear).
  const [deleteFailed, setDeleteFailed] = useState<string | null>(null);

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

  return (
    <div style={{ marginTop: "1.4rem", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <span className="kicker" style={{ fontSize: "0.6rem", color: "var(--ink-faint)" }}>
          History
        </span>
        <Link
          href={withScope("/", productParams)}
          className="code link"
          style={{ fontSize: "0.66rem", borderBottom: "none" }}
          onClick={() => setActiveSessionId(null)}
        >
          + New chat
        </Link>
      </div>
      <div className="sidebar__hist-scroll" style={{ marginTop: "0.5rem", overflowY: "auto", minHeight: 0 }}>
        {!loaded ? (
          <div className="code" style={{ fontSize: "0.7rem", color: "var(--ink-faint)", padding: "0.4rem 0.65rem" }}>
            …
          </div>
        ) : sessions.length === 0 ? (
          <div className="code" style={{ fontSize: "0.7rem", color: "var(--ink-faint)", padding: "0.4rem 0.65rem" }}>
            no conversations yet
          </div>
        ) : (
          sessions.map((s) => {
            const active = s.id === activeSessionId;
            const rel = relTime(s.updated_at);
            return (
              <div key={s.id} className={`hist${active ? " hist--active" : ""}`}>
                {confirming === s.id ? (
                  <div className="hist__confirm">
                    <span>delete?</span>
                    <button onClick={() => void remove(s.id)}>yes</button>
                    <button onClick={() => setConfirming(null)}>no</button>
                  </div>
                ) : deleteFailed === s.id ? (
                  // The delete did NOT happen: say so where it failed, with a
                  // way to retry or stand down. role=alert so a screen reader
                  // hears the failure without hunting for the row.
                  <div className="hist__confirm" role="alert">
                    <span>{"couldn't delete"}</span>
                    <button onClick={() => void remove(s.id)}>retry</button>
                    <button onClick={() => setDeleteFailed(null)}>dismiss</button>
                  </div>
                ) : (
                  <>
                    <Link
                      href={withScope(`/?session=${encodeURIComponent(s.id)}`, productParams)}
                      className="hist__main"
                      title={`${s.title} \u00b7 ${absTime(s.updated_at)} \u00b7 ${s.message_count} messages`}
                      onClick={() => setActiveSessionId(s.id)}
                    >
                      <span className="hist__title">{s.title}</span>
                      {/* Visible: compact "2h \u00b7 7". aria-label on a generic
                          span is prohibited ARIA, so the bare count is
                          aria-hidden and a sr-only tail spells it out as
                          "N messages" instead. When the timestamp does not
                          parse (rel === ""), the count renders alone -- no
                          orphaned separator. */}
                      <span className="hist__time">
                        {rel}
                        <span aria-hidden="true">
                          {rel ? ` \u00b7 ${s.message_count}` : s.message_count}
                        </span>
                        <span className="sr-only">{` \u2014 ${s.message_count} messages`}</span>
                      </span>
                    </Link>
                    <button
                      className="hist__del"
                      aria-label={`Delete conversation "${s.title}"`}
                      onClick={() => setConfirming(s.id)}
                    >
                      ×
                    </button>
                  </>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
