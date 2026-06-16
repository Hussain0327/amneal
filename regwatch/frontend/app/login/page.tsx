"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { broadcastAuthChange, QuietShell, useAuth } from "@/components/AuthProvider";
import { Wordmark } from "@/components/Wordmark";
import { ApiError, login } from "@/lib/api";

export default function LoginPage() {
  const { user, loading, refresh } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Already signed in (or just signed in): leave for the app. The gate never
  // redirects TO /login while a user is set, so this cannot loop.
  useEffect(() => {
    if (!loading && user) router.replace("/");
  }, [loading, user, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
      await refresh();
      broadcastAuthChange(); // other tabs re-validate — no stale prior-user UI
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Invalid email or password.");
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Too many attempts — wait a minute, then try again.");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  // Don't paint the sign-in form for a visitor who is already (or just became)
  // authenticated — the redirect effect above is about to leave for the app.
  if (loading || user) return <QuietShell />;

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: "2rem 1.5rem" }}>
      <div style={{ width: "100%", maxWidth: "26rem" }}>
        <div className="rise d1" style={{ display: "flex", justifyContent: "center" }}>
          <Wordmark size="sm" />
        </div>
        <div className="rise d2" style={{ textAlign: "center", marginTop: "0.9rem" }}>
          <span className="kicker" style={{ color: "var(--ink-soft)" }}>
            REGWATCH · Access
          </span>
          <h1 className="display" style={{ fontSize: "2.2rem", margin: "0.5rem 0 0" }}>
            Sign in.
          </h1>
          <hr className="rule-gold draw d2" style={{ margin: "0.9rem auto 0", maxWidth: "7rem" }} />
        </div>

        <form onSubmit={onSubmit} className="doc doc--seal doc--pad rise d3" style={{ marginTop: "1.8rem" }}>
          <label className="kicker" htmlFor="email" style={{ color: "var(--ink-faint)" }}>
            Email
          </label>
          <input
            id="email"
            type="email"
            className="field mt-1"
            autoComplete="email"
            placeholder="you@amneal.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <label
            className="kicker"
            htmlFor="password"
            style={{ color: "var(--ink-faint)", display: "block", marginTop: "1.1rem" }}
          >
            Password
          </label>
          <input
            id="password"
            type="password"
            className="field mt-1"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {error && (
            <p
              className="code"
              role="alert"
              style={{ margin: "0.9rem 0 0", fontSize: "0.78rem", color: "var(--oxblood)" }}
            >
              {error}
            </p>
          )}
          <button className="btn" type="submit" disabled={submitting} style={{ marginTop: "1.3rem", width: "100%" }}>
            {submitting ? "Verifying…" : "Sign in"}
          </button>
        </form>

        <p
          className="rise d4"
          style={{ marginTop: "1.1rem", textAlign: "center", fontSize: "0.78rem", color: "var(--ink-soft)" }}
        >
          Pilot access is provisioned by the REGWATCH team.
        </p>
      </div>
    </main>
  );
}
