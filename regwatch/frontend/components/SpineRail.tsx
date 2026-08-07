"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { withScope } from "@/lib/scope";
import { AccountPopover } from "./AccountPopover";
import { useAuth } from "./AuthProvider";
import { useCurrentProduct } from "./CurrentProductProvider";
import { HistoryPanel } from "./HistoryPanel";
import { useSessions } from "./SessionsProvider";
import { useSettings } from "./SettingsProvider";

// The register's spine: the shell reduced to a slim bound edge. Top to bottom —
// the wax seal (home), the five numbered chapters, Studio below a hairline, the
// title lettered down the spine, then the analyst's own things (new chat,
// history, account). Every stop shows its name in a flyout on hover/focus and
// carries a full aria-label, so the numerals stay glyphs, not guesses.

const NAV = [
  { href: "/", no: "01", label: "Ask", note: "Cited Q&A" },
  { href: "/assemble", no: "02", label: "Assemble", note: "Build a dossier" },
  { href: "/watch", no: "03", label: "Watch", note: "Change feed" },
  { href: "/whitepaper", no: "04", label: "White Paper", note: "Populate & cite" },
  { href: "/deficiency", no: "05", label: "Deficiency", note: "Scan a draft" },
];

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "·";
  const first = parts[0][0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1][0] ?? "") : "";
  return (first + last).toUpperCase() || "·";
}

function HistoryGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" fill="none">
      <path
        d="M3 4.2h10M3 8h10M3 11.8h6.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Tip({ label, note }: { label: string; note?: string }) {
  return (
    <span className="rail__tip" aria-hidden>
      <span className="rail__tip-label">{label}</span>
      {note && <span className="rail__tip-note">{note}</span>}
    </span>
  );
}

export function SpineRail() {
  const pathname = usePathname();
  const { user } = useAuth();
  const { productParams } = useCurrentProduct();
  const { setActiveSessionId } = useSessions();
  // reachable drives the quiet status dot on the account button; the full
  // notice lives inside the popover's colophon.
  const { reachable } = useSettings();
  const [open, setOpen] = useState<"history" | "account" | null>(null);

  return (
    <>
      <aside className="rail">
        <Link
          href={withScope("/", productParams)}
          className="rail__seal"
          aria-label="Amneal RegWatch — home"
          onClick={() => setActiveSessionId(null)}
        >
          <span className="seal rail__seal-mark" aria-hidden />
        </Link>

        <nav className="rail__nav" aria-label="Surfaces">
          {NAV.map((n) => {
            const active = pathname === n.href;
            return (
              <Link
                key={n.href}
                href={withScope(n.href, productParams)}
                className={`rail__stop${active ? " rail__stop--active" : ""}`}
                aria-label={`${n.no} ${n.label} — ${n.note}`}
                aria-current={active ? "page" : undefined}
              >
                <span className="code rail__no">{n.no}</span>
                <Tip label={n.label} note={n.note} />
              </Link>
            );
          })}
          <hr className="hair rail__hair" aria-hidden />
          {/* Studio is its own surface (full-viewport, outside this shell);
              its stop wears Studio's own gold mark rather than a chapter
              numeral -- it is a place, not a chapter. */}
          <Link
            href="/studio"
            className={`rail__stop rail__stop--studio${pathname === "/studio" ? " rail__stop--active" : ""}`}
            aria-label="Studio — review and annotate documents"
          >
            <span className="rail__studio-mark" aria-hidden>
              S
            </span>
            <Tip label="Studio" note="Review & annotate" />
          </Link>
        </nav>

        {/* The volume's title, lettered down the spine. Decorative — the seal
            carries the accessible brand name. */}
        <span className="rail__wordmark code" aria-hidden>
          REGWATCH
        </span>

        <div className="rail__foot">
          <Link
            href={withScope("/", productParams)}
            className="rail__stop rail__stop--foot"
            aria-label="New chat"
            onClick={() => {
              setActiveSessionId(null);
              setOpen(null);
            }}
          >
            {/* Same dispatch as the sidebar's old "+ New chat": route to a
                clean Ask with scope kept, active session cleared. */}
            <span className="rail__plus" aria-hidden>
              +
            </span>
            <Tip label="New chat" />
          </Link>
          <button
            type="button"
            className={`rail__stop rail__stop--foot${open === "history" ? " rail__stop--open" : ""}`}
            aria-label="History"
            aria-haspopup="dialog"
            aria-expanded={open === "history"}
            onClick={() => setOpen(open === "history" ? null : "history")}
          >
            <HistoryGlyph />
            <Tip label="History" />
          </button>
          <button
            type="button"
            className={`rail__stop rail__stop--foot rail__account${open === "account" ? " rail__stop--open" : ""}`}
            aria-label={`Account — ${user?.display_name ?? "signed in"}${reachable ? "" : " (RegWatch unreachable)"}`}
            aria-haspopup="dialog"
            aria-expanded={open === "account"}
            onClick={() => setOpen(open === "account" ? null : "account")}
          >
            <span className="rail__avatar" aria-hidden>
              {initials(user?.display_name ?? "")}
            </span>
            {!reachable && <span className="rail__status" aria-hidden />}
            <Tip label={user?.display_name ?? "Account"} note={reachable ? undefined : "Unreachable"} />
          </button>
        </div>
      </aside>

      {open === "history" && (
        <HistoryPanel
          onClose={() => {
            // The Ask page's active-session routing happens via the row links;
            // closing only dismisses the panel (focus returns to the toggle).
            setOpen(null);
          }}
        />
      )}
      {open === "account" && <AccountPopover onClose={() => setOpen(null)} />}
    </>
  );
}
