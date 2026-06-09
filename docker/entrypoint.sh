#!/bin/sh
set -eu

: "${DATA_DIR:=/app/data}"
: "${CHROMA_DIR:=/app/data/chroma}"
: "${SQLITE_PATH:=/app/data/regwatch.db}"
: "${RAW_PDF_DIR:=/app/data/raw}"
: "${PROCESSED_DIR:=/app/data/processed}"
: "${DAGSTER_CONFIG_DIR:=/app/dagster_config}"
: "${DAGSTER_HOME:=/app/data/dagster/home}"

mkdir -p "$DATA_DIR" "$CHROMA_DIR" "$RAW_PDF_DIR" "$PROCESSED_DIR" "$(dirname "$SQLITE_PATH")" "$DAGSTER_HOME"

if [ -d "$DAGSTER_CONFIG_DIR" ]; then
  cp "$DAGSTER_CONFIG_DIR/dagster.yaml" "$DAGSTER_HOME/dagster.yaml"
  cp "$DAGSTER_CONFIG_DIR/workspace.yaml" "$DAGSTER_HOME/workspace.yaml"
fi

if [ "${REGWATCH_INIT_DB:-true}" = "true" ]; then
  regwatch init-db
  export REGWATCH_DB_INITIALIZED=1
fi

exec "$@"
