"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { listSessions, type SessionSummary } from "@/lib/api";

interface SessionsState {
  sessions: SessionSummary[];
  loaded: boolean;
  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;
  refresh: () => Promise<void>;
}

const SessionsContext = createContext<SessionsState | null>(null);

export function useSessions(): SessionsState {
  const ctx = useContext(SessionsContext);
  if (!ctx) throw new Error("useSessions must be used inside <SessionsProvider>");
  return ctx;
}

// Holds the signed-in user's chat-session list so the Sidebar (history list,
// delete) and the Ask page (post-query refresh, active highlight) stay in step.
// Mounted only inside the auth gate, so listing never fires signed-out.
export function SessionsProvider({ children }: { children: React.ReactNode }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await listSessions();
      setSessions(data.sessions);
    } catch {
      // 401 is handled centrally; on other failures keep the last good list.
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ sessions, loaded, activeSessionId, setActiveSessionId, refresh }),
    [sessions, loaded, activeSessionId, refresh],
  );

  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>;
}
