# REGWATCH Frontend Workspace

Next.js (App Router, TypeScript) UI for RegWatch on the Amneal brand. Five
surfaces (Ask / Assemble / Watch / White Paper / Deficiency) render inside one
shared App Router shell: one sidebar, one set of design tokens, one URL-scoped
current product. The **Compliance Studio** (`/studio`) sits outside that shell
and is the one surface that reads our own CMC drafts rather than public FDA
material. (The earlier Streamlit POC is fully retired.)

It is a thin client over the FastAPI backend; all logic and compliance live in
the API, with one exception. The Studio is a three-way split: working
documents and findings are still frontend fixtures
(`lib/studio-fixtures.ts`, domain model in `lib/studio-marks.ts`); the PSG
reference rail and its chat assistant call the real backend; and
`POST /studio/check` is a real, persisted backend endpoint the page does not
call yet. See
[`docs/PRODUCTION_TRUTH.md`](../../docs/PRODUCTION_TRUTH.md) section 8.

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
UI's `:3000` origin needs to be exposed; the API stays private behind it.
That means a single [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
quick-tunnel shares the whole app:

```bash
brew install cloudflared        # once
./scripts/share-demo.sh         # from the repo root
```

It starts the API + UI and prints an `https://....trycloudflare.com` link;
send that to your manager. Ctrl-C tears it all down.

> The tunnel makes the login page publicly reachable, but the app still requires
> a provisioned REGWATCH user. Every authenticated query spends your OpenAI key
> while the tunnel is live. Share it only with provisioned users and Ctrl-C when
> done. The URL changes each time you start the tunnel, and it dies if your
> laptop sleeps.

Manual equivalent, in three terminals:

```bash
uv run uvicorn regwatch.api.main:app --port 8000        # 1) API
cd regwatch/frontend && npm run build && npm run start  # 2) UI (:3000, proxies /api -> :8000)
cloudflared tunnel --url http://localhost:3000          # 3) public link
```

## Shell & current product

All five shell surfaces live in one App Router route-group layout
(`app/(shell)/layout.tsx`): a single sidebar, one canvas, and a slim sticky
**"Under review"** product-scope bar (`components/ProductScopeBar.tsx`) across
the top of every page. The login, fixtures and studio routes sit outside the
group and never see it.

The scoped current product is URL-encoded (`?rp=<reference product>&appl=<six-
digit application number>`), so it is shareable, survives reload, and is read by
all five shell surfaces. It is settable from three places: the scope-bar picker,
White Paper on a successful populate, and a Watch row, each writing the same
canonical `{normalized_name, application_number}`.

**The Studio is not scoped to a product.** It sits outside the shell, so it never
sees the scope bar and its repository tree is a fixture rather than a query for
the current product's documents. Wiring it into `CurrentProductProvider` is a
prerequisite for folding any other surface into it.

Pinning a product is **not** an LLM turn. The scope-bar picker calls
`POST /resolve` (`resolveProduct` in `lib/api.ts`), the backend's deterministic
entity resolution that reuses the White Paper's spine builder. On a mismatch it
returns 422 with a `detail` explaining what *was* found, and the scope is left
unset; refuse over guess. `/resolve` writes no audit row and returns no answer
text.

## Pages

- **Ask** (`/`) - a cited conversational **chat** over FDA product-specific
  guidance: right-aligned user bubbles, the gold RW assistant avatar, a
  bottom-pinned composer (Enter sends, Shift+Enter newlines). Answers render
  markdown with citation chips that link to FDA sources (full snippets in a
  Sources disclosure). A `clarify` turn shows an interpretation line plus
  clickable option pills that resend the query/filters; you can also reply to a
  clarify in your own words. A `refused` turn shows plain refusal text. Answer
  turns carry a quiet thumbs up/down (`POST /feedback`, upsert per answer+user;
  thumbs-down offers a one-line optional note). Turns restored from session
  history have no `audit_id`, so they render without the affordance.

  `POST /query/stream` (SSE) streams `status` progress frames, post-audit
  `token` replay frames, optional flag-gated provisional `draft` /
  `draft_reset` frames (2026-08-10 amendment; dark behind REGWATCH_LIVE_DRAFT
  until the flag flips), and exactly one terminal `result` frame; the client
  falls back to plain `POST /query` if the stream fails (lib/api.ts).
- **Assemble** (`/assemble`) - cited dossier for a target product.
- **Watch** (`/watch`) - recent change-feed alerts + the watchlist; a row can
  set the current product scope.
- **White Paper** (`/whitepaper`) - CRA White Paper population + `.docx`
  download from the reviewed result; a successful populate sets the scope.
- **Deficiency** (`/deficiency`) - upload a submission and get the deficiencies
  an FDA reviewer is likely to raise, each with its evidence. `POST
  /deficiency/analyze` returns 202 and the run completes in the background.
- **Compliance Studio** (`/studio`) - outside the shell. Repository tree on the
  left, the document in the middle, findings and a cited assistant sliding in
  from the right. A finding anchors to a span of the document, so it highlights
  in place and goes stale when the analyst edits underneath it; "Fixed" cannot be
  recorded until that has actually happened. See the real-vs-fixture split in
  [`docs/PRODUCTION_TRUTH.md`](../../docs/PRODUCTION_TRUTH.md) section 8 and
  the UI design detail in
  [`docs/COMPLIANCE_STUDIO.md`](../../docs/COMPLIANCE_STUDIO.md).

## Error monitoring (Sentry, opt-in)

`@sentry/nextjs` is wired through the standard files (`sentry.client.config.ts`,
`sentry.server.config.ts`, `sentry.edge.config.ts`, `instrumentation.ts`,
`app/global-error.tsx`) but initializes **only** when `NEXT_PUBLIC_SENTRY_DSN`
is set at build time; unset, the whole thing is a no-op. Optional
`NEXT_PUBLIC_SENTRY_ENVIRONMENT` tags events (default `dev`). Deliberately
minimal: no session replay, `sendDefaultPii: false`, no source-map upload.
Question/answer text never leaves the app's own audit log.
