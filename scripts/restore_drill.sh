#!/usr/bin/env bash
#
# Monthly STAGING restore drill — a thin wrapper around
# scripts/migrate_to_supabase.py (see docs/DEPLOY.md §6.3).
#
# Re-runs the snapshot migration against a STAGING Postgres with --truncate;
# the migrate script prints its per-table verification table as it finishes.
# The drill passes only when every verification row is OK and the exit code
# is 0 (the migrate script exits nonzero on ANY count mismatch).
#
# Usage:
#   DATABASE_URL='postgresql://postgres.<STAGING-ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres' \
#     ./scripts/restore_drill.sh [SNAPSHOT_DIR]
#
# Parameters (env):
#   DATABASE_URL      REQUIRED — the STAGING Postgres URL. The target of the
#                     drill; it WILL be truncated.
#   SNAPSHOT_DIR      directory holding regwatch.db + chroma/ (default
#                     /tmp/regwatch-snapshot; $1 overrides).
#   DRILL_SKIP_EMBED  set to 1 to pass --skip-embed: relational copy only, no
#                     OpenAI embedding spend, `chunk` table left empty.
#
# HARD GUARD (load-bearing): if the target URL references the PRODUCTION
# Supabase project ref, this script refuses with exit code 4 BEFORE any
# network call or subprocess. The ref appears in the pooler username
# (postgres.<ref>@...pooler.supabase.com) and in the direct-connection host
# (db.<ref>.supabase.co), so it is matched anywhere in the URL — after
# percent-decoding, so an encoded ref (%78vhbf…) cannot slip past. URLs whose
# host is a bare, non-loopback IP address are ALSO refused: they carry no
# textual ref to match, and a deliberately pre-resolved production IP must not
# bypass the guard (loopback stays allowed for local docker rehearsals).
# Known residual limit: the guard is textual — a production target reached
# through a hostname alias (CNAME) containing neither the ref nor an IP is
# out of scope. Covered by tests/test_restore_drill.py.

set -euo pipefail

PROD_PROJECT_REF="xvhbfmoynibkcghazzxc"

TARGET_URL="${DATABASE_URL:-}"
if [ -z "$TARGET_URL" ]; then
  echo "restore_drill: DATABASE_URL is not set — export the STAGING Postgres URL first" >&2
  echo "restore_drill: usage: DATABASE_URL='postgresql://...staging...' $0 [SNAPSHOT_DIR]" >&2
  exit 2
fi

# ---- production guard: nothing (no file IO, no subprocess, no network) runs
# ---- before this check. Exit 4 is reserved for guard refusals.
# Percent-decode before matching ('%78vhbf…' must not slip past): '%' -> '\x'
# then printf %b decodes the hex escapes. The decoded form is used ONLY for
# the match — the migrate script receives the original URL untouched.
DECODED_URL="$(printf '%b' "${TARGET_URL//\%/\\x}")"
case "$(printf '%s' "$DECODED_URL" | tr '[:upper:]' '[:lower:]')" in
  *"$PROD_PROJECT_REF"*)
    echo "restore_drill: REFUSING — DATABASE_URL references the PRODUCTION project ref ${PROD_PROJECT_REF}." >&2
    echo "restore_drill: this drill TRUNCATES its target; point DATABASE_URL at the STAGING project instead." >&2
    exit 4
    ;;
esac

# A raw-IP host carries no project ref for the guard to vet — refuse every
# non-loopback IP target. Loopback is exempt: a local docker Postgres is a
# legitimate rehearsal target and cannot be the Supabase production project.
AUTHORITY="${DECODED_URL#*://}"   # drop the scheme
AUTHORITY="${AUTHORITY%%/*}"      # drop path/query
AUTHORITY="${AUTHORITY##*@}"      # drop userinfo (never echoed — carries the password)
case "$AUTHORITY" in
  \[*) HOST="${AUTHORITY#\[}"; HOST="${HOST%%\]*}" ;;  # bracketed IPv6
  *)   HOST="${AUTHORITY%%:*}" ;;
esac
case "$HOST" in
  localhost|127.*|::1|0:0:0:0:0:0:0:1) : ;;
  *)
    if printf '%s' "$HOST" | grep -Eiq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$|^[0-9a-f:]*:[0-9a-f:]+$'; then
      echo "restore_drill: REFUSING — DATABASE_URL targets a bare IP address (${HOST}); the prod-ref guard cannot vet it." >&2
      echo "restore_drill: point DATABASE_URL at the STAGING pooler/direct hostname instead." >&2
      exit 4
    fi
    ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT_DIR="${1:-${SNAPSHOT_DIR:-/tmp/regwatch-snapshot}}"
SQLITE_SNAPSHOT="$SNAPSHOT_DIR/regwatch.db"
CHROMA_SNAPSHOT="$SNAPSHOT_DIR/chroma"

if [ ! -f "$SQLITE_SNAPSHOT" ]; then
  echo "restore_drill: $SQLITE_SNAPSHOT not found — take a snapshot first (docs/DEPLOY.md §2 step 1)" >&2
  exit 2
fi

EXTRA_ARGS=()
if [ "${DRILL_SKIP_EMBED:-0}" = "1" ]; then
  EXTRA_ARGS+=(--skip-embed)
elif [ ! -d "$CHROMA_SNAPSHOT" ]; then
  echo "restore_drill: $CHROMA_SNAPSHOT not found (set DRILL_SKIP_EMBED=1 for a relational-only drill)" >&2
  exit 2
fi

# Never echo the URL itself — it carries the database password.
SANITIZED_TARGET="$(printf '%s' "$TARGET_URL" | sed -E 's#://[^@/]*@#://***@#')"
echo "restore_drill: prod-ref guard passed (target is not ${PROD_PROJECT_REF})"
echo "restore_drill: snapshot ${SNAPSHOT_DIR} -> ${SANITIZED_TARGET} (--truncate)"

# The migrate script prints per-table copy progress and the final verification
# table; exec hands it the terminal and the exit code verbatim.
exec uv run python "$ROOT/scripts/migrate_to_supabase.py" \
  --sqlite "$SQLITE_SNAPSHOT" \
  --chroma "$CHROMA_SNAPSHOT" \
  --database-url "$TARGET_URL" \
  --truncate \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
