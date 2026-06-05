#!/bin/sh
set -eu

: "${DATA_DIR:=/app/data}"
: "${CHROMA_DIR:=/app/data/chroma}"
: "${SQLITE_PATH:=/app/data/regwatch.db}"
: "${RAW_PDF_DIR:=/app/data/raw}"
: "${PROCESSED_DIR:=/app/data/processed}"

mkdir -p "$DATA_DIR" "$CHROMA_DIR" "$RAW_PDF_DIR" "$PROCESSED_DIR" "$(dirname "$SQLITE_PATH")"

if [ "${REGWATCH_INIT_DB:-true}" = "true" ]; then
  regwatch init-db
  export REGWATCH_DB_INITIALIZED=1
fi

exec "$@"
