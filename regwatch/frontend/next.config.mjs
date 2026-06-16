// The browser only ever talks to the Next origin: API calls go to /api/* and
// are rewritten server-side to the FastAPI backend. This is what lets a single
// cloudflared tunnel (over :3000) expose the whole app — no second tunnel, no
// CORS, no public API URL to configure. Override the backend with
// API_PROXY_TARGET (server-side env, e.g. if the API runs on another host).
// 127.0.0.1, not "localhost" — Node resolves localhost to IPv6 ::1 first, but
// uvicorn binds IPv4 by default, so "localhost" wastes a failed ::1 attempt per
// request. Pinning IPv4 avoids it.
import { withSentryConfig } from "@sentry/nextjs";

const API_PROXY_TARGET = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Next 14 gates instrumentation.ts (where Sentry's server/edge configs load)
  // behind this flag; it is on by default from Next 15.
  experimental: {
    instrumentationHook: true,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_PROXY_TARGET}/:path*` }];
  },
  // Security response headers. The app is a cookie-authed origin reachable over
  // a public tunnel, so the simple anti-clickjacking / sniffing / referrer
  // headers are enforced immediately. The CSP ships Report-Only first: Next 14's
  // App Router emits unnonced inline bootstrap/hydration scripts and the UI uses
  // inline style attributes, so an enforced default-src would need 'unsafe-inline'
  // anyway — observe violations in Report-Only, then promote to enforced once the
  // policy is confirmed against the running app (and a real report-uri is wired).
  async headers() {
    const csp = [
      "default-src 'self'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "img-src 'self' data:",
      "font-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      // Next 14 App Router injects inline bootstrap scripts without a nonce.
      "script-src 'self' 'unsafe-inline'",
      // Same-origin /api proxy covers the backend; Sentry ingest is allowed for
      // when NEXT_PUBLIC_SENTRY_DSN is set (no-op otherwise).
      "connect-src 'self' https://*.sentry.io https://*.ingest.sentry.io",
    ].join("; ");
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
          { key: "Content-Security-Policy-Report-Only", value: csp },
        ],
      },
    ];
  },
};

// withSentryConfig wires sentry.client.config.ts into the client bundle. The
// SDK itself initializes only when NEXT_PUBLIC_SENTRY_DSN is set (see the
// sentry.*.config.ts files) — without it the wrapper adds no behavior. Source
// map upload is explicitly disabled: no org/project/auth-token plumbing here.
export default withSentryConfig(nextConfig, {
  silent: true,
  telemetry: false,
  sourcemaps: { disable: true },
  // Tree-shake the SDK's debug logging out of the production bundle.
  webpack: { treeshake: { removeDebugLogging: true } },
  // Replay and tunneling intentionally not configured.
});
