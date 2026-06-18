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
# build's alembic head) + idempotent ensures + RLS. Skip it for the Fly
# release_command (`alembic upgrade head`, see fly.toml [deploy]): that command
# must run the migration to MOVE the stamp to head, and the guard would
# otherwise refuse and abort the whole deploy before the migration ever ran.
# The real app boot (CMD = uvicorn) still runs init-db normally.
if [ "${REGWATCH_INIT_DB:-true}" = "true" ] && [ "${1:-}" != "alembic" ]; then
  regwatch init-db
  export REGWATCH_DB_INITIALIZED=1
fi

exec "$@"
