# RegWatch Web (Next.js)

Next.js (App Router, TypeScript) UI for RegWatch — replaces the Streamlit POC
feature-for-feature (Ask / Assemble / Watch) on the Amneal brand. It is a thin
client over the FastAPI backend; all logic and compliance live in the API.

## Run

```bash
# 1) Start the API (from the repo root)
uv run uvicorn regwatch.api.main:app --reload      # http://localhost:8000

# 2) Start the UI (from web/)
cp .env.local.example .env.local                    # NEXT_PUBLIC_API_BASE
npm install
npm run dev                                          # http://localhost:3000
```

The API allows `http://localhost:3000` via CORS by default
(`cors_allow_origins_csv` in `config/settings.py`). If you serve the UI from a
different origin, add it there.

## Pages

- **Ask** (`/`) — cited Q&A. Renders by status: `answer` (markdown + clickable
  sources), `clarify` (interpretation line + clickable options that resend the
  query/filters), `refused` (plain refusal text).
- **Assemble** (`/assemble`) — cited dossier for a target product.
- **Watch** (`/watch`) — recent change-feed alerts + the watchlist.
