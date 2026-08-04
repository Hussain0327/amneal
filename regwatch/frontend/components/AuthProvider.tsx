"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { ApiError, logout as apiLogout, me, setUnauthorizedHandler, type User } from "@/lib/api";
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
// itself. /fixtures (the static design gallery) deliberately stays OUT of this
// set so it sits behind the auth gate: it already 404s in production builds,
// and gating it keeps its sample feedback controls from being reachable by a
// signed-out visitor in any non-prod build.
const BARE_PATHS = new Set(["/login"]);

// Session gate for the whole app. Bare paths render as-is; protected routes
// render their children — the sidebar shell (see app/(shell)/layout.tsx) — only
// once /auth/me has confirmed a user; until then a quiet shell, so protected UI
// never flashes for signed-out visitors.
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const pathname = usePathname();
  const router = useRouter();

  // Coalesce concurrent re-validations (mount + focus + cross-tab can overlap):
  // a last-issued-wins token drops stale resolves so an out-of-order /auth/me
  // can't clobber a newer result. lastValidated gates the focus re-check.
  const refreshSeq = useRef(0);
  const lastValidated = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++refreshSeq.current;
    try {
      const u = await me();
      if (seq === refreshSeq.current) setUser(u);
    } catch (e) {
      // Only a real 401 means the session is gone -- and handle() has already
      // cleared it via the 401 hook below, so this branch is belt-and-braces.
      // Everything else (offline, an edge 502, the 504 timeout ApiError) says
      // nothing about the cookie: clearing on those signed a working analyst out
      // mid-task on one bad hop, unmounting their composer text with a live
      // session. Same discrimination as Sidebar.tsx:60.
      if (seq === refreshSeq.current && e instanceof ApiError && e.status === 401) setUser(null);
    } finally {
      if (seq === refreshSeq.current) {
        setLoading(false);
        lastValidated.current = Date.now();
      }
    }
  }, []);

  // Central 401 hook: any protected call that comes back unauthorized drops
  // the user here; the redirect effect below then routes to /login.
  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    // refresh() sets state only after the awaited /auth/me resolves (behind a
    // sequence guard) — not synchronously in this effect body.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  // Re-validate when another tab changes auth, and on focus (covers a cookie
  // that changed while this tab sat idle, even without a broadcast). The focus
  // path is throttled to once a minute so tab-switching doesn't spray /auth/me;
  // a cross-tab broadcast (an actual auth change) always re-validates at once.
  useEffect(() => {
    const onFocus = () => {
      if (Date.now() - lastValidated.current > 60_000) void refresh();
    };
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

export function QuietShell() {
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
