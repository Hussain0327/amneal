# REGWATCH Frontend Workspace

Next.js (App Router, TypeScript) UI for RegWatch — replaces the Streamlit POC
feature-for-feature (Ask / Assemble / Watch) on the Amneal brand. It is a thin
client over the FastAPI backend; all logic and compliance live in the API.

## Run

```bash
# 1) Start the API (from the repo root)
uv run uvicorn regwatch.api.main:app --reload      # http://localhost:8000

# 2) Start the UI (from regwatch/frontend/)
cp .env.local.example .env.local                    # NEXT_PUBLIC_API_BASE
npm install
npm run dev                                          # http://localhost:3000
```

The API allows `http://localhost:3000` via CORS by default
(`cors_allow_origins_csv` in `config/settings.py`). If you serve the UI from a
different origin, add it there.

## Share with a manager (one public link)

The UI proxies `/api/*` to the backend (see `next.config.mjs`), so only the
UI's `:3000` origin needs to be exposed — the API stays private behind it.
That means a single [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
quick-tunnel shares the whole app:

```bash
brew install cloudflared        # once
./scripts/share-demo.sh         # from the repo root
```

It starts the API + UI and prints an `https://….trycloudflare.com` link — send
that to your manager. Ctrl-C tears it all down.

> The tunnel makes the login page publicly reachable, but the app still requires
> a provisioned REGWATCH user. Every authenticated query spends your OpenAI key
> while the tunnel is live. Share it only with provisioned users and Ctrl-C when
> done. The URL changes each time you start the tunnel, and it dies if your
> laptop sleeps.

Manual equivalent, in three terminals:

```bash
uv run uvicorn regwatch.api.main:app --port 8000        # 1) API
cd regwatch/frontend && npm run build && npm run start  # 2) UI (:3000, proxies /api → :8000)
cloudflared tunnel --url http://localhost:3000          # 3) public link
```

## Pages

- **Ask** (`/`) — cited Q&A. Renders by status: `answer` (markdown + clickable
  sources), `clarify` (interpretation line + clickable options that resend the
  query/filters), `refused` (plain refusal text). Answer turns carry a quiet
  thumbs up/down (`POST /feedback`, upsert per answer+user; thumbs-down offers
  a one-line optional note). Turns restored from session history have no
  `audit_id`, so they render without the affordance.
- **Assemble** (`/assemble`) — cited dossier for a target product.
- **Watch** (`/watch`) — recent change-feed alerts + the watchlist.
- **White Paper** (`/whitepaper`) — CRA White Paper population + `.docx`
  download from the reviewed result.

## Error monitoring (Sentry, opt-in)

`@sentry/nextjs` is wired through the standard files (`sentry.client.config.ts`,
`sentry.server.config.ts`, `sentry.edge.config.ts`, `instrumentation.ts`,
`app/global-error.tsx`) but initializes **only** when `NEXT_PUBLIC_SENTRY_DSN`
is set at build time — unset, the whole thing is a no-op. Optional
`NEXT_PUBLIC_SENTRY_ENVIRONMENT` tags events (default `dev`). Deliberately
minimal: no session replay, `sendDefaultPii: false`, no source-map upload —
question/answer text never leaves the app's own audit log.
