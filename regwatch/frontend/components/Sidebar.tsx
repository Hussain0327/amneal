"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { getPublicSettings, type PublicSettings } from "@/lib/api";
import { Wordmark } from "./Wordmark";

const NAV = [
  { href: "/", no: "01", label: "Ask", note: "Cited Q&A" },
  { href: "/assemble", no: "02", label: "Assemble", note: "Build a dossier" },
  { href: "/watch", no: "03", label: "Watch", note: "Change feed" },
];

export function Sidebar() {
  const pathname = usePathname();
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    getPublicSettings()
      .then((s) => {
        setSettings(s);
        setReachable(true);
      })
      .catch(() => setReachable(false));
  }, []);

  return (
    <aside
      className="shrink-0"
      style={{
        width: "18.5rem",
        background: "linear-gradient(180deg, var(--paper-2), var(--paper-3))",
        borderRight: "1px solid var(--edge)",
        padding: "2.1rem 1.6rem",
        display: "flex",
        flexDirection: "column",
        position: "sticky",
        top: 0,
        height: "100vh",
      }}
    >
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
              href={n.href}
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: "0.7rem",
                padding: "0.7rem 0.65rem",
                borderRadius: "2px",
                textDecoration: "none",
                color: active ? "var(--ink)" : "var(--ink-2)",
                background: active ? "#fffdf8" : "transparent",
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

      {/* Colophon — the running provider info, set like a print imprint */}
      <div style={{ marginTop: "auto", paddingTop: "1.4rem" }}>
        <hr className="hair" style={{ marginBottom: "1rem" }} />
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
            API unreachable — start it &amp; set NEXT_PUBLIC_API_BASE
          </div>
        )}
      </div>
    </aside>
  );
}
