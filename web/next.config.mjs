// The browser only ever talks to the Next origin: API calls go to /api/* and
// are rewritten server-side to the FastAPI backend. This is what lets a single
// cloudflared tunnel (over :3000) expose the whole app — no second tunnel, no
// CORS, no public API URL to configure. Override the backend with
// API_PROXY_TARGET (server-side env, e.g. if the API runs on another host).
// 127.0.0.1, not "localhost" — Node resolves localhost to IPv6 ::1 first, but
// uvicorn binds IPv4 by default, so "localhost" wastes a failed ::1 attempt per
// request. Pinning IPv4 avoids it.
const API_PROXY_TARGET = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_PROXY_TARGET}/:path*` }];
  },
};

export default nextConfig;
