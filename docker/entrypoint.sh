#!/bin/sh
set -eu

: "${DATA_DIR:=/app/data}"
: "${CHROMA_DIR:=/app/data/chroma}"
: "${SQLITE_PATH:=/app/data/regwatch.db}"
: "${RAW_PDF_DIR:=/app/data/raw}"
: "${PROCESSED_DIR:=/app/data/processed}"
: "${DAGSTER_CONFIG_DIR:=/app/dagster_config}"
: "${DAGSTER_HOME:=/app/data/dagster/home}"
: "${WHITEPAPER_TEMPLATE_PATH:=/app/data/templates/cra_white_paper_template.docx}"

mkdir -p "$DATA_DIR" "$CHROMA_DIR" "$RAW_PDF_DIR" "$PROCESSED_DIR" "$(dirname "$SQLITE_PATH")" "$DAGSTER_HOME" "$(dirname "$WHITEPAPER_TEMPLATE_PATH")"

if [ -d "$DAGSTER_CONFIG_DIR" ]; then
  cp "$DAGSTER_CONFIG_DIR/dagster.yaml" "$DAGSTER_HOME/dagster.yaml"
  cp "$DAGSTER_CONFIG_DIR/workspace.yaml" "$DAGSTER_HOME/workspace.yaml"
fi

# Boot-time DB init runs the stamp guard (refuses if the live schema != the
# build's alembic head) + idempotent ensures + RLS. Two commands must skip it:
#   * alembic: the Fly release_command (`alembic upgrade head`, see fly.toml
#     [deploy]) exists to MOVE the stamp to head; the guard would otherwise
#     refuse and abort the whole deploy before the migration ever ran.
#   * regwatch-proxy: the Go proxy (fly.toml [processes] group "proxy") must
#     boot DB-independent -- a proxy machine crash-looping on the stamp guard
#     while holding the public port is the 2026-06-18/07-07 incident class,
#     amplified from one API machine to the entire public edge.
# The real app boot (group "app", uvicorn) still runs init-db normally.
run_init_db="${REGWATCH_INIT_DB:-true}"
case "${1:-}" in
  alembic|regwatch-proxy|*/regwatch-proxy) run_init_db="false" ;;
esac
if [ "$run_init_db" = "true" ]; then
  regwatch init-db
  export REGWATCH_DB_INITIALIZED=1
fi

exec "$@"
