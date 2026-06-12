// Node-runtime Sentry for the Next server (loaded via instrumentation.ts).
// Same single switch as the browser: NEXT_PUBLIC_SENTRY_DSN — unset means
// no-op. The Next server is a thin proxy/renderer here (all data lives behind
// the FastAPI backend, which has its own Sentry wiring), so the defaults are
// deliberately conservative: no PII, no request bodies, light tracing.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "dev",
    tracesSampleRate: 0.1,
    profilesSampleRate: 0,
    sendDefaultPii: false,
  });
}
