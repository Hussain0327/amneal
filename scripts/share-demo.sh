#!/usr/bin/env bash
#
# Expose the whole RegWatch app (API + Next.js UI) to a manager via ONE public
# link, using a free cloudflared quick-tunnel. The UI proxies /api/* to the
# backend, so only the UI's :3000 origin is tunneled — the API stays private.
#
#   ./scripts/share-demo.sh             # start everything + open a public link
#   SHARE_NO_TUNNEL=1 ./scripts/share-demo.sh   # local only, no public link (for testing)
#
# Copy the printed https://….trycloudflare.com URL and send it to your manager.
# Press Ctrl-C to tear everything down.
#
# CAUTION: the link is OPEN (no auth) and every query spends your OpenAI key
# while the tunnel is live. Share it only with the manager and Ctrl-C when done.
# The URL changes every time you start the tunnel.

set -euo pipefail
cd "$(dirname "$0")/.."

NO_TUNNEL="${SHARE_NO_TUNNEL:-0}"

if [ "$NO_TUNNEL" != "1" ]; then
  command -v cloudflared >/dev/null 2>&1 || {
    echo "cloudflared not found. Install it with:  brew install cloudflared"
    exit 1
  }
fi

# Always rebuild the UI before sharing. Next bakes NEXT_PUBLIC_* env into the
# build, so a stale build can serve the wrong API base — rebuilding every run
# guarantees the served app matches the current code and env.
echo "Building the UI…"
( cd web && { [ -d node_modules ] || npm install; } && npm run build ) >/tmp/regwatch-build.log 2>&1 || {
  echo "UI build failed — see /tmp/regwatch-build.log"
  exit 1
}

cleanup() {
  echo
  echo "Tearing down…"
  kill "${API_PID:-}" "${UI_PID:-}" 2>/dev/null || true
  for p in 8000 3000; do
    pid=$(lsof -ti tcp:$p 2>/dev/null || true)
    [ -n "$pid" ] && kill $pid 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# Wait until a URL responds, showing a single line of progress. Args: url label maxsecs
wait_for() {
  local url="$1" label="$2" max="${3:-90}"
  printf "Waiting for %s" "$label"
  for _ in $(seq 1 "$max"); do
    if curl -s -o /dev/null -m 2 "$url" 2>/dev/null; then printf " ✓\n"; return 0; fi
    printf "."
    sleep 1
  done
  printf " ✗ timed out\n"
  return 1
}

# 1) API first. It imports ChromaDB + the embedding model on boot (a few
#    seconds), so we wait for it to actually answer BEFORE starting the UI —
#    otherwise the UI's proxy spews ECONNREFUSED while the API is still loading.
echo "Starting API on :8000…"
uv run uvicorn regwatch.api.main:app --port 8000 --no-access-log >/tmp/regwatch-api.log 2>&1 &
API_PID=$!
wait_for "http://127.0.0.1:8000/health" "API" 90 || {
  echo "API failed to start — see /tmp/regwatch-api.log"
  exit 1
}

# 2) UI second, once the API is reachable.
echo "Starting UI on :3000…"
( cd web && npm run start ) >/tmp/regwatch-ui.log 2>&1 &
UI_PID=$!
wait_for "http://127.0.0.1:3000/api/health" "UI (proxying to API)" 60 || {
  echo "UI failed to start — see /tmp/regwatch-ui.log"
  exit 1
}

if [ "$NO_TUNNEL" = "1" ]; then
  echo
  echo "App is up at http://localhost:3000 (no public link — SHARE_NO_TUNNEL=1)."
  echo "Self-test:"
  curl -s http://127.0.0.1:3000/api/health && echo "  <- /api/health OK"
  echo "Ctrl-C to stop."
  wait
else
  echo
  echo "=== Public link — send this to your manager (Ctrl-C here to stop) ==="
  cloudflared tunnel --url http://localhost:3000
fi
