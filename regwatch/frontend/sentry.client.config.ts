// Browser-side Sentry. Gated on NEXT_PUBLIC_SENTRY_DSN at build time — when
// the variable is unset this file initializes nothing and every Sentry call
// elsewhere (captureException in global-error) is a safe no-op.
//
// NOTE: the SDK suggests renaming this to instrumentation-client.ts at build
// time; that convention is Next >= 15.3. On Next 14 this filename is the
// supported mechanism (injected by withSentryConfig) — rename only when the
// Next.js dependency moves past 15.3.
//
// Privacy posture mirrors the API: no PII, no session replay. Replay records
// DOM content, and analyst questions/answers are exactly what we keep out of
// Sentry — they live in our own audit log only.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "dev",
    tracesSampleRate: 0.1,
    profilesSampleRate: 0,
    sendDefaultPii: false,
    // Replay stays off: no replayIntegration, and the sample rates pinned to
    // zero so a future default can never switch it on silently.
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
  });
}
