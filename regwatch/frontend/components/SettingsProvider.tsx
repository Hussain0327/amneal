"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { ApiError, getPublicSettings, type PublicSettings } from "@/lib/api";

interface SettingsState {
  settings: PublicSettings | null;
  reachable: boolean;
}

const SettingsContext = createContext<SettingsState | null>(null);

/**
 * Read access to the backend's public settings (provider names, refusal
 * threshold). Throws outside <SettingsProvider> -- the same contract as
 * useSessions -- so a consumer can never silently read a stale default.
 */
export function useSettings(): SettingsState {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used inside <SettingsProvider>");
  return ctx;
}

/**
 * Holds the backend's public settings so the Sidebar colophon and the Ask
 * confidence legend share ONE GET /settings instead of fetching per consumer.
 * Mounted only inside the auth gate (shell layout), so the fetch never fires
 * signed-out. `reachable` is the transport verdict for the colophon: false
 * only on a non-auth failure -- a 401 is an auth expiry (AuthProvider handles
 * the redirect), not an unreachable API.
 */
export function SettingsProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    getPublicSettings()
      .then((s) => {
        setSettings(s);
        setReachable(true);
      })
      .catch((e: unknown) => {
        if (!(e instanceof ApiError) || e.status !== 401) setReachable(false);
      });
  }, []);

  const value = useMemo(() => ({ settings, reachable }), [settings, reachable]);

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}
