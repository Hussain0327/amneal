# REGWATCH Backend Workspace

This folder is the backend workspace for deployment notes and API-specific
assets. The Python backend package is intentionally not moved here.

## Canonical Backend Source

The application code stays in:

```text
src/regwatch/
```

That package owns:

- FastAPI endpoints: `src/regwatch/api/main.py`
- ingest and FDA source handling: `src/regwatch/ingest/`, `src/regwatch/sources/`
- retrieval and product resolution: `src/regwatch/retrieve/`
- grounded answer synthesis: `src/regwatch/generate/`
- database models and sessions: `src/regwatch/store/`
- migrations: `migrations/`

Keeping the package under `src/` preserves the current imports, tests,
`pyproject.toml` package discovery, Docker image, and CLI entrypoint.

## Run The API

From the repo root:

```bash
uv run regwatch init-db
uv run uvicorn regwatch.api.main:app --reload
```

The API listens on `http://127.0.0.1:8000` by default. The TypeScript frontend
in `../frontend` talks to it through the Next.js `/api/*` proxy.

## Do Not Add

Do not add `regwatch/backend/__init__.py` or a second Python package here. That
would create two competing backend roots and make imports, type checking, and
deployment harder to reason about.
