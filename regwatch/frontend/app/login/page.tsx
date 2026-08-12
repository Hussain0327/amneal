"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { broadcastAuthChange, QuietShell, useAuth } from "@/components/AuthProvider";
import { ApiError, login } from "@/lib/api";

interface FieldErrors {
  email: string | null;
  password: string | null;
}

const NO_FIELD_ERRORS: FieldErrors = { email: null, password: null };

// One authored line for every way sign-in can fail. The catch-all deliberately
// never surfaces err.message: the generic branch used to print the transport's
// own words into the form (an observed "Internal Server Error" from the proxy),
// which tells an analyst nothing they can act on.
function signInError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "Invalid email or password.";
    if (err.status === 429) return "Too many attempts. Wait a minute, then try again.";
    // 502/503, plus the client-side timeout api.ts reports as a 504.
    if (err.status >= 502) return "Can't reach RegWatch right now. Try again in a moment.";
  }
  // A dead network rejects with a bare TypeError, never an ApiError. Kept to one
  // line: the colophon below already names who to ask for help.
  return "Sign-in failed. Try again in a moment.";
}

// The maker's seal at full size. The shared `.seal` is a CSS disc built for
// 2rem; rim lettering needs real type on a path, so this is an inline SVG.
// Decorative -- the masthead beside it already names the product to a reader.
function SealEmblem() {
  return (
    <svg className="signin__emblem" viewBox="0 0 200 200" aria-hidden focusable="false">
      <defs>
        {/* Struck, not spherical: a flat raking gradient reads as a stamped
            impression, where a centred highlight read as a glossy coin. */}
        <linearGradient id="signinSealFace" x1="0" y1="0" x2="0.85" y2="1">
          <stop offset="0%" stopColor="var(--gold)" />
          <stop offset="52%" stopColor="var(--gold-deep)" />
          <stop offset="100%" stopColor="var(--gold)" />
        </linearGradient>
        {/* r=76 keeps the rim lettering clear of both rings. The legend is
            sized to occupy the top arc only -- longer copy wraps past the
            path's start offset and silently clips its first characters. */}
        <path id="signinSealRim" fill="none" d="M100 100 m -76 0 a 76 76 0 1 1 152 0 a 76 76 0 1 1 -152 0" />
      </defs>
      <circle cx="100" cy="100" r="97" fill="url(#signinSealFace)" />
      <circle cx="100" cy="100" r="92" fill="none" stroke="#7a5200" strokeWidth="1.6" opacity="0.55" />
      <circle cx="100" cy="100" r="62" fill="none" stroke="#7a5200" strokeWidth="1.2" opacity="0.45" />
      <text className="signin__emblem-rim" fill="#4a3200">
        <textPath href="#signinSealRim" startOffset="25%" textAnchor="middle">
          PRODUCT-SPECIFIC GUIDANCE
        </textPath>
      </text>
      <text
        className="signin__emblem-mark"
        x="100"
        y="100"
        fill="#4a3200"
        textAnchor="middle"
        dominantBaseline="central"
      >
        RW
      </text>
    </svg>
  );
}

function SignInForm() {
  const { user, loading, refresh } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>(NO_FIELD_ERRORS);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  // AuthProvider stamps this only when the gate dropped an analyst who HAD a
  // session; a cold visit carries no reason and gets no notice.
  const sessionEnded = searchParams?.get("reason") === "expired";

  // Already signed in (or just signed in): leave for the app. The gate never
  // redirects TO /login while a user is set, so this cannot loop.
  useEffect(() => {
    if (!loading && user) router.replace("/");
  }, [loading, user, router]);

  async function onSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    if (submitting) return;
    const trimmedEmail = email.trim();
    const errors: FieldErrors = {
      email: trimmedEmail ? null : "Enter your email.",
      password: password ? null : "Enter your password.",
    };
    setFieldErrors(errors);
    setFormError(null);
    // An empty field used to return silently from here: the button did nothing
    // at all, with no message and no moved cursor. Name what is missing and put
    // the caret in the first field that needs it.
    if (errors.email || errors.password) {
      (errors.email ? emailRef : passwordRef).current?.focus();
      return;
    }
    setSubmitting(true);
    try {
      await login(trimmedEmail, password);
      await refresh();
      broadcastAuthChange(); // other tabs re-validate -- no stale prior-user UI
      router.replace("/");
    } catch (err) {
      setFormError(signInError(err));
    } finally {
      setSubmitting(false);
    }
  }

  // Don't paint the sign-in form for a visitor who is already (or just became)
  // authenticated -- the redirect effect above is about to leave for the app.
  if (loading || user) return <QuietShell />;

  return (
    <main className="signin">
      <div className="signin__sheet">
        <header className="signin__masthead rise d1">
          <span className="signin__nameplate">
            <span className="wordmark" style={{ fontSize: "2.1rem" }}>
              Amneal
            </span>
            <span className="signin__series">Regwatch</span>
          </span>
          <span className="signin__standing">FDA guidance intelligence</span>
        </header>
        <hr className="signin__rule draw d1" />

        <div className="signin__body">
          <SealEmblem />

          <div className="signin__slip rise d3">
            <h1 className="display signin__heading">Sign in.</h1>

            {sessionEnded && (
              <p className="signin__notice">Your session ended. Sign in to pick up where you left off.</p>
            )}

            <form onSubmit={onSubmit} noValidate>
              <div className="signin__group">
                <label className="kicker signin__label" htmlFor="email">
                  Email
                </label>
                <input
                  id="email"
                  name="email"
                  ref={emailRef}
                  type="email"
                  className="field"
                  autoComplete="email"
                  autoFocus
                  placeholder="you@amneal.com"
                  aria-invalid={fieldErrors.email !== null}
                  aria-describedby={fieldErrors.email ? "email-error" : undefined}
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    if (fieldErrors.email) setFieldErrors((prev) => ({ ...prev, email: null }));
                  }}
                />
                {fieldErrors.email && (
                  <p id="email-error" className="signin__note">
                    {fieldErrors.email}
                  </p>
                )}
              </div>

              <div className="signin__group">
                <label className="kicker signin__label" htmlFor="password">
                  Password
                </label>
                <input
                  id="password"
                  name="password"
                  ref={passwordRef}
                  type="password"
                  className="field"
                  autoComplete="current-password"
                  aria-invalid={fieldErrors.password !== null}
                  aria-describedby={fieldErrors.password ? "password-error" : undefined}
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    if (fieldErrors.password) setFieldErrors((prev) => ({ ...prev, password: null }));
                  }}
                />
                {fieldErrors.password && (
                  <p id="password-error" className="signin__note">
                    {fieldErrors.password}
                  </p>
                )}
              </div>

              <p className="signin__alert" role="alert">
                {formError}
              </p>

              <button className="btn btn--ink signin__submit" type="submit" disabled={submitting}>
                {submitting ? "Signing in..." : "Sign in"}
              </button>
            </form>
          </div>
        </div>

        <hr className="hair" />
        <div className="signin__colophon rise d4">
          <p>Public FDA data only.</p>
          <p>Access is provisioned by the REGWATCH team. Locked out? Ask them to reset your password.</p>
        </div>
      </div>
    </main>
  );
}

// useSearchParams() forces a Suspense boundary in the App Router; the fallback
// is the same quiet shell the form itself shows while /auth/me is in flight.
export default function LoginPage() {
  return (
    <Suspense fallback={<QuietShell />}>
      <SignInForm />
    </Suspense>
  );
}
