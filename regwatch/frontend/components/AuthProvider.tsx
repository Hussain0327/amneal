"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { logout as apiLogout, me, setUnauthorizedHandler, type User } from "@/lib/api";
import { Wordmark } from "./Wordmark";

interface AuthState {
  user: User | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

// Cross-tab auth sync: the HttpOnly cookie is invisible to JS, so an idle tab
// only learns about a logout/login elsewhere if we tell it. Login and logout
// ping this channel; every AuthProvider re-validates /auth/me on the ping.
const AUTH_CHANNEL = "regwatch-auth";

export function broadcastAuthChange(): void {
  if (typeof BroadcastChannel === "undefined") return;
  const channel = new BroadcastChannel(AUTH_CHANNEL);
  channel.postMessage("changed");
  channel.close();
}

// Paths that render bare — no auth, no sidebar shell. /login is the gate
// itself; /fixtures is the static design gallery (fake data only, and it
// 404s in production builds).
const BARE_PATHS = new Set(["/login", "/fixtures"]);

// Session gate for the whole app. Bare paths render as-is; protected routes
// render their children — the sidebar shell (see app/(shell)/layout.tsx) — only
// once /auth/me has confirmed a user; until then a quiet shell, so protected UI
// never flashes for signed-out visitors.
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const pathname = usePathname();
  const router = useRouter();

  const refresh = useCallback(async () => {
    try {
      setUser(await me());
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Central 401 hook: any protected call that comes back unauthorized drops
  // the user here; the redirect effect below then routes to /login.
  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Re-validate when another tab changes auth, and on focus (covers a cookie
  // that changed while this tab sat idle, even without a broadcast).
  useEffect(() => {
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    let channel: BroadcastChannel | null = null;
    if (typeof BroadcastChannel !== "undefined") {
      channel = new BroadcastChannel(AUTH_CHANNEL);
      channel.onmessage = () => void refresh();
    }
    return () => {
      window.removeEventListener("focus", onFocus);
      channel?.close();
    };
  }, [refresh]);

  useEffect(() => {
    if (!loading && !user && !BARE_PATHS.has(pathname)) router.replace("/login");
  }, [loading, user, pathname, router]);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } catch {
      // The cookie may already be dead; we are leaving regardless.
    }
    setUser(null);
    broadcastAuthChange();
    router.replace("/login");
  }, [router]);

  return (
    <AuthContext.Provider value={{ user, loading, refresh, logout }}>
      {BARE_PATHS.has(pathname) ? (
        children
      ) : user ? (
        // The authed shell (sidebar + canvas + scoped-product context) is the
        // (shell) route-group layout; it remounts per identity via its own
        // key={user.id}, so a stale tab never keeps a prior user's state.
        children
      ) : (
        <QuietShell />
      )}
    </AuthContext.Provider>
  );
}

function QuietShell() {
  return (
    <div className="shell" style={{ alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.9rem", opacity: 0.7 }}>
        <Wordmark size="sm" />
        <span className="kicker" style={{ color: "var(--ink-faint)" }}>
          verifying session
        </span>
      </div>
    </div>
  );
}
