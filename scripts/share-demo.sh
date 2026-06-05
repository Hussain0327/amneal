#!/usr/bin/env bash
#
# Expose the whole RegWatch app (API + Next.js UI) to a manager via ONE public
# link, using a free cloudflared quick-tunnel. The UI proxies /api/* to the
# backend, so only the UI's :3000 origin is tunneled — the API stays private.
#
#   ./scripts/share-demo.sh
#
# Copy the printed https://….trycloudflare.com URL and send it to your manager.
# Press Ctrl-C to tear everything down.
#
# CAUTION: the link is OPEN (no auth) and every query spends your OpenAI key
# while the tunnel is live. Share it only with the manager and Ctrl-C when done.
# The URL changes every time you start the tunnel.

set -euo pipefail
cd "$(dirname "$0")/.."

command -v cloudflared >/dev/null 2>&1 || {
  echo "cloudflared not found. Install it with:  brew install cloudflared"
  exit 1
}

if [ ! -d web/.next ]; then
  echo "Building the UI (first run)…"
  ( cd web && npm install && npm run build )
fi

cleanup() {
  echo
  echo "Tearing down…"
  kill "${API_PID:-}" "${UI_PID:-}" 2>/dev/null || true
  # belt-and-suspenders: free the ports we started
  for p in 8000 3000; do
    pid=$(lsof -ti tcp:$p 2>/dev/null || true)
    [ -n "$pid" ] && kill $pid 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "Starting API on :8000…"
uv run uvicorn regwatch.api.main:app --port 8000 --no-access-log &
API_PID=$!

echo "Starting UI on :3000…"
( cd web && npm run start ) &
UI_PID=$!

echo "Waiting for the app to come up…"
for _ in $(seq 1 60); do
  if curl -s -o /dev/null http://localhost:3000/ 2>/dev/null \
     && curl -s -o /dev/null http://localhost:3000/api/health 2>/dev/null; then
    break
  fi
  sleep 1
done

echo
echo "=== Public link — send this to your manager (Ctrl-C here to stop) ==="
cloudflared tunnel --url http://localhost:3000
