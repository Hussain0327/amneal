#!/bin/sh
set -eu

: "${DATA_DIR:=/app/data}"
: "${RAW_PDF_DIR:=/app/data/raw}"
: "${PROCESSED_DIR:=/app/data/processed}"
: "${WHITEPAPER_TEMPLATE_PATH:=/app/data/templates/cra_white_paper_template.docx}"

mkdir -p "$DATA_DIR" "$RAW_PDF_DIR" "$PROCESSED_DIR" "$(dirname "$WHITEPAPER_TEMPLATE_PATH")"

# Boot-time DB init runs the stamp guard (refuses if the live schema != the
# build's alembic head) + idempotent ensures + RLS. Two commands must skip it:
#   * alembic: the Fly release_command (`alembic upgrade head`, see fly.toml
#     [deploy]) exists to MOVE the stamp to head; the guard would otherwise
#     refuse and abort the whole deploy before the migration ever ran.
#   * regwatch-proxy: the Go proxy must boot DB-independent -- a proxy machine
#     crash-looping on the stamp guard while holding the public port is the
#     2026-06-18/07-07 incident class, amplified from one API machine to the
#     entire public edge. Staged for the phase-3 "proxy" process group
#     (docs/GO_PROXY_ROLLOUT.md); no fly.toml group execs the binary today.
# The real app boot (group "app") runs `regwatch serve`, so $1 is `regwatch`,
# which matches no skip branch and takes the init-db default below -- the same
# behaviour the pre-phase-2 `uvicorn ...` command had. Dispatch is on $1 alone,
# so it does not depend on which app command ships (docs/GO_PROXY_ROLLOUT.md).
run_init_db="${REGWATCH_INIT_DB:-true}"
case "${1:-}" in
  alembic|regwatch-proxy|*/regwatch-proxy) run_init_db="false" ;;
esac
if [ "$run_init_db" = "true" ]; then
  regwatch init-db
  export REGWATCH_DB_INITIALIZED=1
fi

exec "$@"
