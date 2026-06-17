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

// Fail the BUILD loudly on Vercel if API_PROXY_TARGET is missing/still the local
// loopback default — otherwise every /api/* rewrite would 502 against the
// function's own 127.0.0.1 at request time, a green-but-broken frontend. This
// mirrors the backend's fail-loud posture (REQUIRE_DATABASE_URL). A correctly
// configured deploy (the var set to the https API origin) is unaffected.
if (process.env.VERCEL === "1" && API_PROXY_TARGET === "http://127.0.0.1:8000") {
  throw new Error(
    "API_PROXY_TARGET must be set to the https API origin on Vercel (e.g. https://amneal.fly.dev); " +
      "it is unset, so /api/* would proxy to the function loopback and 502. " +
      "Set it via `vercel env add API_PROXY_TARGET production` (see docs/DEPLOY.md §4.3).",
  );
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  // instrumentation.ts (where Sentry's server/edge configs load) is on by
  // default from Next 15+, so the old experimental.instrumentationHook flag is
  // gone — no config needed here.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_PROXY_TARGET}/:path*` }];
  },
  // Security response headers. The app is a cookie-authed origin reachable over
  // a public tunnel, so the simple anti-clickjacking / sniffing / referrer
  // headers are enforced immediately. The CSP ships Report-Only first: Next's
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
      // Next App Router injects inline bootstrap scripts without a nonce.
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

// withSentryConfig wires instrumentation-client.ts into the client bundle (and
// instrumentation.ts loads the server/edge configs). The SDK itself initializes
// only when NEXT_PUBLIC_SENTRY_DSN is set (see those files) — without it the
// wrapper adds no behavior. Source map upload is explicitly disabled: no
// org/project/auth-token plumbing here.
export default withSentryConfig(nextConfig, {
  silent: true,
  telemetry: false,
  sourcemaps: { disable: true },
  // Tree-shake the SDK's debug logging out of the production bundle. Bundler-
  // agnostic, so it still applies under Next 16's default Turbopack build (the
  // old webpack.treeshake option was silently ignored once Turbopack took over).
  bundleSizeOptimizations: { excludeDebugStatements: true },
  // Replay and tunneling intentionally not configured.
});
