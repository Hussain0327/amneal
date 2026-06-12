// Edge-runtime Sentry (loaded via instrumentation.ts). This app has no edge
// routes today, but the standard three-file layout keeps middleware/edge
// additions covered. Same gate as everywhere: NEXT_PUBLIC_SENTRY_DSN unset = no-op.
import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "dev",
    tracesSampleRate: 0.1,
    sendDefaultPii: false,
  });
}
