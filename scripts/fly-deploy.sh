#!/usr/bin/env bash
#
# Robust `flyctl deploy` wrapper with a NARROW, bounded retry.
#
# WHY: on 2026-06-29 a production deploy failed with
#     error starting release_command machine: failed to start VM ...:
#     deadline_exceeded: machine still starting
# Fly could not BOOT the one-off release_command machine (which runs
# `alembic upgrade head`) within its internal ~60s machine-start deadline, so
# `flyctl deploy` aborted before the rolling replace. That is a transient Fly
# platform/orchestration blip -- the byte-identical image had deployed cleanly
# ~2h earlier -- and NO flyctl flag extends that start deadline (--wait-timeout
# and --release-command-timeout both govern later phases), so the only available
# lever is to re-run the deploy. See docs and the incident memo for detail.
#
# CONTRACT: retry ONLY that class of transient platform error (see
# TRANSIENT_ERROR_RE) and FAIL FAST on everything else -- a real migration error,
# a bad image, an auth failure, a failed health check -- so genuine breakage is
# never silently re-attempted behind a green check.
#
# WHY a blind re-run of the whole deploy is SAFE here (idempotency):
#   * release_command = `regwatch release`: its Alembic phase is a no-op once
#     the DB is stamped at head (the linear migrations are re-runnable), and its
#     following serving-readiness phase is idempotent validation/ensure work.
#   * Fly runs release_command in a TEMPORARY machine BEFORE the rolling replace
#     and aborts the entire deploy if it fails, so a failed attempt never leaves
#     a half-rolled fleet for the next attempt to corrupt.
#
# Tunables (env -- for ops overrides and for the unit test):
#   FLY_DEPLOY_MAX_ATTEMPTS        total attempts incl. the first (default 3)
#   FLY_DEPLOY_BASE_DELAY_SECONDS  backoff base; <=0 disables the sleep (def 15)
#   FLY_DEPLOY_MAX_DELAY_SECONDS   backoff cap in seconds (default 60)
#
# Run from the repo root (where fly.toml lives); the deploy workflow does.
set -euo pipefail

MAX_ATTEMPTS="${FLY_DEPLOY_MAX_ATTEMPTS:-3}"
BASE_DELAY_SECONDS="${FLY_DEPLOY_BASE_DELAY_SECONDS:-15}"
MAX_DELAY_SECONDS="${FLY_DEPLOY_MAX_DELAY_SECONDS:-60}"

# Transient Fly platform errors a retry can ride through. Kept deliberately
# NARROW and anchored on Fly machine-start / capacity / gateway signatures. A
# pattern that ALSO matched a deterministic failure (a real migration error, a
# failed health/smoke check) would silently retry genuine breakage behind a
# green check -- which the CONTRACT above forbids. Bare network/guidance phrases
# (connection reset, i/o timeout, context deadline exceeded, please try again,
# temporarily unavailable) are intentionally EXCLUDED: they appear verbatim in
# deterministic alembic/Postgres errors and in flyctl's health-check-failure
# text, so those now fail FAST. NOTE "error starting release_command machine" is
# the machine-START signature (transient); a real migration failure instead
# reads "release_command failed running on machine ..." and is not matched here.
# Matched case-insensitively against the whole captured deploy output.
#
# The 50x branch exists for Fly control-plane / remote-builder gateway blips
# (deploys run `flyctl deploy --remote-only`), whose flyctl error shape is a Go
# HTTP error: the API host, then the status, ADJACENTLY ("Post
# https://api.machines.dev: 503 Service Unavailable"). Host-anchored 2026-07-16:
# a bare 50x phrase also matches an echoed health-CHECK body -- the live
# 2026-07-15 flip-deadlock body was "502 Bad Gateway / upstream unavailable"
# (docs/GO_PROXY_ROLLOUT.md) -- which would retry a structurally deadlocked
# deploy instead of failing fast, if a future flyctl starts printing check
# output on failure.
#
# `[^ ]*: *` (not `.*`) is why this holds: the status must follow the host
# across a URL path and colon but NO intervening words, so a line carrying BOTH
# an API URL and a check body ("Post https://api.machines.dev/...: check
# failed: 502 Bad Gateway / upstream unavailable") does NOT match. Shapes we
# have no sample of -- status before host, wordy registry/docker forms
# ("received unexpected HTTP status: 502 Bad Gateway"), *.fly.dev app hosts --
# deliberately fail fast: a wrong fail-fast costs one manual re-run, a wrong
# retry costs ~17min of wedged deploy plus a stray proxy machine per attempt,
# on the group that now holds the public edge.
TRANSIENT_ERROR_RE='deadline_exceeded'
TRANSIENT_ERROR_RE="${TRANSIENT_ERROR_RE}|machine still starting"
TRANSIENT_ERROR_RE="${TRANSIENT_ERROR_RE}|error starting release_command machine"
TRANSIENT_ERROR_RE="${TRANSIENT_ERROR_RE}|failed to (start|launch) (the )?vm"
TRANSIENT_ERROR_RE="${TRANSIENT_ERROR_RE}|no capacity|insufficient capacity"
TRANSIENT_ERROR_RE="${TRANSIENT_ERROR_RE}|(api\.machines\.dev|api\.fly\.io|registry\.fly\.io)[^ ]*: *50[234] (bad gateway|service unavailable|gateway timeout)"

log() {
  printf '%s [fly-deploy] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

# Exit 0 (true) iff the captured deploy log FILE ($1) matches a transient
# pattern. Grep the FILE directly rather than `printf "$contents" | grep`: a real
# deadline_exceeded dump far exceeds the 64KB pipe buffer, and `grep -q` closing
# the pipe on its first match would SIGPIPE the producer, which `set -o pipefail`
# then surfaces as a failed pipeline -- misclassifying a transient blip as a
# fail-fast and defeating the retry on the exact incident this exists to handle.
is_transient_error() {
  grep -qiE "$TRANSIENT_ERROR_RE" "$1"
}

# Seconds to wait before the next attempt: capped exponential backoff with full
# jitter. $1 is the 1-based attempt number. Echoes 0 when backoff is disabled
# (BASE_DELAY_SECONDS<=0), which the test uses to run without sleeping.
backoff_seconds() {
  local attempt="$1"
  local delay
  if [ "$BASE_DELAY_SECONDS" -le 0 ]; then
    printf '0'
    return 0
  fi
  delay=$(( BASE_DELAY_SECONDS * (2 ** (attempt - 1)) ))
  if [ "$delay" -gt "$MAX_DELAY_SECONDS" ]; then
    delay="$MAX_DELAY_SECONDS"
  fi
  # Full jitter in [1, delay] to avoid hammering Fly on a fixed cadence.
  printf '%s' "$(( (RANDOM % delay) + 1 ))"
}

main() {
  local outfile attempt rc delay
  outfile="$(mktemp "${TMPDIR:-/tmp}/fly-deploy.XXXXXX")"
  # shellcheck disable=SC2064  # expand outfile now: it is fixed for this run.
  trap "rm -f '$outfile'" EXIT

  attempt=1
  while : ; do
    log "deploy attempt ${attempt}/${MAX_ATTEMPTS}"

    # Stream flyctl output to the console (observability) AND capture it (so we
    # can classify the failure). PIPESTATUS[0] preserves flyctl's real exit code
    # past the tee. set +e so a nonzero deploy doesn't trip `set -e` here.
    set +e
    flyctl deploy --remote-only "$@" 2>&1 | tee "$outfile"
    rc="${PIPESTATUS[0]}"
    set -e

    if [ "$rc" -eq 0 ]; then
      log "deploy succeeded on attempt ${attempt}"
      return 0
    fi

    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
      log "deploy failed (rc=${rc}) on attempt ${attempt}; no attempts left -- failing"
      return "$rc"
    fi

    if ! is_transient_error "$outfile"; then
      log "deploy failed (rc=${rc}) with a non-transient error -- failing fast, not retrying"
      return "$rc"
    fi

    delay="$(backoff_seconds "$attempt")"
    log "deploy failed (rc=${rc}) with a transient Fly error -- retrying in ${delay}s"
    if [ "$delay" -gt 0 ]; then
      sleep "$delay"
    fi
    attempt=$(( attempt + 1 ))
  done
}

# Auto-run only when executed directly, so the test can source this file and
# exercise the helpers in isolation without launching a real deploy.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
