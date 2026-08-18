#!/bin/sh
set -eu

: "${DATA_DIR:=/app/data}"
: "${RAW_PDF_DIR:=/app/data/raw}"
: "${PROCESSED_DIR:=/app/data/processed}"
: "${WHITEPAPER_TEMPLATE_PATH:=/app/data/templates/cra_white_paper_template.docx}"

mkdir -p "$DATA_DIR" "$RAW_PDF_DIR" "$PROCESSED_DIR" "$(dirname "$WHITEPAPER_TEMPLATE_PATH")"

# Boot-time DB init runs the stamp guard (refuses if the live schema != the
# build's alembic head) + idempotent ensures + RLS. Four command classes use
# specialized handling:
#   * `regwatch release`: the Fly release_command migrates FIRST and then runs
#     the full serving guard itself. Running the entrypoint's guard before that
#     command would refuse a legitimately behind stamp before it could migrate.
#     Fly also marks its one-off machine with RELEASE_COMMAND=1; honor that
#     platform contract so the skip survives any command-wrapper argv shape.
#   * alembic: direct operator migration commands need the same pre-guard skip.
#   * regwatch-proxy: the Go proxy must boot DB-independent -- a proxy machine
#     crash-looping on the stamp guard while holding the public port is the
#     2026-06-18/07-07 incident class, amplified from one API machine to the
#     entire public edge. Staged for the phase-3 "proxy" process group
#     (docs/GO_PROXY_ROLLOUT.md); no fly.toml group execs the binary today.
#   * Dagster worker/webserver: run the maintenance-safe schema guard below;
#     serving-profile coverage may be incomplete precisely because this worker
#     has been started to repair it.
# The real app boot (group "app") runs `regwatch serve`, so $1 is `regwatch`,
# which matches no skip branch and takes the init-db default below -- the same
# behaviour the pre-phase-2 `uvicorn ...` command had. Dispatch is on $1 alone,
# so it does not depend on which app command ships (docs/GO_PROXY_ROLLOUT.md).
run_init_db="${REGWATCH_INIT_DB:-true}"
if [ "${RELEASE_COMMAND:-}" = "1" ]; then
  run_init_db="false"
fi
case "${1:-}" in
  alembic|regwatch-proxy|*/regwatch-proxy) run_init_db="false" ;;
  regwatch)
    if [ "${2:-}" = "release" ]; then
      run_init_db="false"
    fi
    ;;
  dagster-daemon|dagster-webserver)
    # Corpus maintenance intentionally creates pending vectors, so the public
    # serving-profile completeness gate cannot run before the daemon that
    # repairs them starts. This still verifies the exact Alembic head, RLS, and
    # database connectivity; each asset repeats the same maintenance-safe init.
    if [ "$run_init_db" = "true" ]; then
      regwatch authoritative-corpus-init-db
    fi
    run_init_db="false"
    ;;
esac
if [ "$run_init_db" = "true" ]; then
  regwatch init-db
  export REGWATCH_DB_INITIALIZED=1
fi

exec "$@"
