// Browser-side Sentry. Gated on NEXT_PUBLIC_SENTRY_DSN at build time — when
// the variable is unset this file initializes nothing and every Sentry call
// elsewhere (captureException in global-error) is a safe no-op.
//
// Named instrumentation-client.ts: this is the @sentry/nextjs convention for the
// browser config and is injected by the Sentry webpack/Turbopack plugin on Next
// 14 too. (Next's NATIVE instrumentation-client support is 15.3+, but Sentry's
// own plugin loads this file regardless of Next version.) The old
// sentry.client.config.ts name is deprecated and stops working under Turbopack.
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

// App Router navigation instrumentation. Next 14 ignores this export; Sentry
// uses it to trace client-side route transitions on Next 15+/Turbopack.
// Available since @sentry/nextjs 9.12 (we're on ^10). Mirrors instrumentation.ts's
// onRequestError — harmless to export on 14, correct for the future.
export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
