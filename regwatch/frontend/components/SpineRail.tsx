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

// The register's spine: the shell reduced to a slim bound edge. Top to bottom --
// the wax seal (home), the two studios, the title lettered down the spine, then
// the analyst's own things (new chat, history, account). Every stop shows its
// name in a flyout on hover/focus and carries a full aria-label, so the letters
// stay glyphs, not guesses.
//
// WHY THE 01-05 NUMERALS ARE GONE
// A numeral is a claim about order, and that claim was only ever true while Ask,
// Assemble, Watch and White Paper read as chapters of one sequence. They are now
// four artifact kinds inside the Research Studio, and a studio is not chapter two
// of the other studio: two rooms in one building have no first and second. What
// is left on the rail is the choice actually being made -- whose material am I
// working on. R is the public record, C is our own drafts.

interface Stop {
  /** Where the stop navigates. */
  readonly href: string;
  /** The letter struck on the stop's mark. */
  readonly mark: string;
  readonly label: string;
  readonly note: string;
  /** Paths this studio owns; any of them marks the stop active. */
  readonly paths: readonly string[];
  /** Whether the href carries the scoped product (Compliance reads none). */
  readonly scoped: boolean;
}

const STOPS: readonly Stop[] = [
  {
    href: "/research",
    mark: "R",
    label: "Research Studio",
    note: "Ask, assemble, watch, publish",
    // Ask has not moved off the root yet, so the root is still Research's;
    // drop "/" here the day it does.
    paths: ["/research", "/"],
    scoped: true,
  },
  {
    href: "/studio",
    mark: "C",
    label: "Compliance Studio",
    note: "Review and check our drafts",
    paths: ["/studio"],
    scoped: false,
  },
];

// Prefix match, not equality: /research?kind=dossier reaches usePathname as
// "/research", but a single artifact reaches it as "/research/<id>" and is still
// the Research Studio. The root is the one path that can never be treated as a
// prefix -- every path starts with "/" -- so it matches only itself, which is
// also what keeps the two stops disjoint and at most one of them active.
function owns(pathname: string, paths: readonly string[]): boolean {
  return paths.some((p) =>
    p === "/" ? pathname === "/" : pathname === p || pathname.startsWith(`${p}/`),
  );
}

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
          {STOPS.map((s) => {
            const active = owns(pathname, s.paths);
            return (
              <Link
                key={s.href}
                href={s.scoped ? withScope(s.href, productParams) : s.href}
                className={`rail__stop${active ? " rail__stop--active" : ""}`}
                aria-label={`${s.label} - ${s.note}`}
                aria-current={active ? "page" : undefined}
              >
                {/* Both studios are places, so both wear a lettered mark. The
                    mark is the glyph; the flyout and the aria-label say which. */}
                <span className="rail__mark" aria-hidden>
                  {s.mark}
                </span>
                <Tip label={s.label} note={s.note} />
              </Link>
            );
          })}
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
