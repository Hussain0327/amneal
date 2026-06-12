// Next.js instrumentation hook (enabled via experimental.instrumentationHook
// in next.config.mjs on Next 14). Loads the runtime-appropriate Sentry config;
// each config file is itself a no-op unless NEXT_PUBLIC_SENTRY_DSN is set.
import * as Sentry from "@sentry/nextjs";

export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }
  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}

// Next 15+ calls this for server request errors; Next 14 ignores the export.
// Harmless either way, and a no-op while Sentry is uninitialized.
export const onRequestError = Sentry.captureRequestError;
