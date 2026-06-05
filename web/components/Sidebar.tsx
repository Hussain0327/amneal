"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { getPublicSettings, type PublicSettings } from "@/lib/api";
import { Wordmark } from "./Wordmark";

const NAV = [
  { href: "/", label: "Ask" },
  { href: "/assemble", label: "Assemble" },
  { href: "/watch", label: "Watch" },
];

export function Sidebar() {
  const pathname = usePathname();
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [reachable, setReachable] = useState<boolean>(true);

  useEffect(() => {
    getPublicSettings()
      .then((s) => {
        setSettings(s);
        setReachable(true);
      })
      .catch(() => setReachable(false));
  }, []);

  return (
    <aside className="w-64 shrink-0 border-r border-gold bg-gold-soft p-5">
      <Wordmark size="sm" />
      <div className="mb-3 mt-1 text-sm font-bold tracking-[0.18em] text-ink">REGWATCH</div>
      <p className="mb-5 text-xs leading-relaxed text-ink-soft">
        Operational POC. Public FDA data only. Surfaces and cites; never authors submissions.
      </p>

      <nav className="flex flex-col gap-1">
        {NAV.map((n) => {
          const active = pathname === n.href;
          return (
            <Link
              key={n.href}
              href={n.href}
              className={`rounded-md px-3 py-2 text-sm font-medium transition ${
                active ? "bg-gold text-ink" : "text-ink hover:bg-white/60"
              }`}
            >
              {n.label}
            </Link>
          );
        })}
      </nav>

      <div className="mt-6 border-t border-gold/40 pt-4 text-xs text-ink-soft">
        {settings ? (
          <>
            <div>
              <span className="font-semibold">Embedding:</span> {settings.embedding_provider}
            </div>
            <div>
              <span className="font-semibold">LLM:</span> {settings.llm_provider} /{" "}
              {settings.llm_model}
            </div>
          </>
        ) : reachable ? (
          <div>Connecting to API…</div>
        ) : (
          <div className="text-red-700">
            API unreachable. Start it and set NEXT_PUBLIC_API_BASE.
          </div>
        )}
      </div>
    </aside>
  );
}
