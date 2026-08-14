#!/usr/bin/env bash
#
# Assert that the Fly "app" process group is ACTUALLY RUNNING. Two modes:
#
#   wait    post-deploy gate (.github/workflows/deploy.yml): poll until EVERY
#           app-group machine reports state "started", else fail the deploy.
#   check   single shot (.github/workflows/machine-monitor.yml, every 10min):
#           fail if ANY app-group machine is "stopped".
#
# WHY (postmortem #224, the 2026-08-13 outage): an oversized embedding batch
# tripped the profile boot guard, both app machines crash-looped, exhausted
# their 10 restart attempts, STOPPED -- and prod stayed down 3h35m because
# nothing ever said so. That is two independent blind spots, one per mode:
#
#   * Deploy v137 completed GREEN while both app machines were crash-looping.
#     fly.toml's [checks.app_health] sat in Fly's "warning" state -- a check
#     that has not yet reported passing is not a FAILING check -- and flyctl's
#     own wait logic accepts warning. So `flyctl deploy` returned 0 over a
#     fleet that never served a request. Machine STATE is the signal that wait
#     logic misses, so this asserts state directly after the deploy returns.
#   * Once stopped, a machine emits nothing to notice: it runs no health
#     checks (so no check can fail), triggers no deploy, answers no request.
#     Only a poll from outside can see it -- hence the 10-minute `check` cron.
#
# CONTRACT: never accept a machine that is not literally serving.
#   * "started" is the ONLY success state in `wait`. Transitional states
#     ("created", "starting", "replacing") are tolerated solely because we poll
#     again; they are a failure once the attempt budget is gone.
#   * "stopped" ALONE is the failure state in `check`. The app group carries no
#     autostop config (fly.toml's auto_stop_machines belongs to [http_service],
#     i.e. the proxy group, and is false), so a stopped app machine is never
#     legitimate -- while a machine transiently restarting between two
#     10-minute polls must not page.
#
# Tunables (env -- for ops overrides and for the unit test):
#   FLY_APP                   Fly app name (default amneal, = fly.toml `app`)
#   FLY_VERIFY_POLL_SECONDS   seconds between `wait` polls; <=0 skips the sleep
#                             entirely, which is how the test runs (default 10)
#   FLY_VERIFY_MAX_ATTEMPTS   `wait` polls before failing (default 18 => ~3min:
#                             longer than a machine boot plus the 30s
#                             grace_period, far shorter than the deploy job's
#                             45m cap)
#
# Runs from anywhere: --app is passed explicitly, so no fly.toml is needed.
#
# File-scoped because EVERY single-quoted `$name` below is a jq variable bound
# with `--arg`, never a shell expansion -- expanding them in the shell is
# exactly the bug this script must not have.
# shellcheck disable=SC2016
set -euo pipefail

APP="${FLY_APP:-amneal}"
POLL_SECONDS="${FLY_VERIFY_POLL_SECONDS:-10}"
MAX_ATTEMPTS="${FLY_VERIFY_MAX_ATTEMPTS:-18}"

# The one process group this script gates. fly.toml pins the name explicitly
# ([processes] app = "regwatch serve"). The proxy group is deliberately OUT of
# scope: it holds the public edge behind its own /healthz rotation check, and a
# proxy machine cycling must never fail an app-group gate (nor vice versa).
PROCESS_GROUP="app"

# Selector for machines in that group within `flyctl machine list --json`
# output; the group lives at .config.metadata.fly_process_group.
#
# Three cases, because "no fly_process_group" means two opposite things:
#   1. group set          -> gate it iff it equals $group. An explicit
#                            declaration is always honoured.
#   2. no group, but the machine carries RELEASE provenance
#      (.config.metadata.fly_release_id, written by every `fly deploy`)
#      -> gate it as the default group. This is the original `// "app"`
#         default and it stays load-bearing: a release machine predating
#         process groups IS served by Fly's default group, whose name
#         fly.toml pins to this very string, and dropping it from the
#         selection would let the gate silently skip a real serving machine.
#   3. no group AND no release provenance -> a ONE-OFF from `fly machine run`
#      (empty .config.metadata, restart policy "no"), never part of a release.
#      IGNORE it.
#
# Case 3 is why this is not a plain `// "app"`. On 2026-08-14 deploy #413 rolled
# all four release machines green, then failed this gate on two stopped one-off
# embedding-backfill machines left over from the 2026-08-13 recovery: empty
# metadata, so the old default swept them into the app group, where `wait`
# demands "started" and `check` pages on "stopped". A finished one-off is
# SUPPOSED to be stopped. That reported prod down while prod was serving --
# and a monitor that cries wolf every 10 minutes is how a real page gets
# ignored, which is the exact failure this script exists to prevent.
MACHINES_IN_GROUP='.[] | select(if .config.metadata.fly_process_group != null'
MACHINES_IN_GROUP="${MACHINES_IN_GROUP}"' then .config.metadata.fly_process_group == $group'
MACHINES_IN_GROUP="${MACHINES_IN_GROUP}"' else .config.metadata.fly_release_id != null and $group == "app" end)'

# One TSV row (id, state, group) per machine whose state is unacceptable under
# $mode; empty output means healthy. Assembled in appends (one clause per line)
# so the two mode predicates stay legible; see the CONTRACT above for why they
# differ.
UNHEALTHY_FILTER="${MACHINES_IN_GROUP}"
UNHEALTHY_FILTER="${UNHEALTHY_FILTER}"' | select(if $mode == "wait"'
UNHEALTHY_FILTER="${UNHEALTHY_FILTER}"' then .state != "started"'
UNHEALTHY_FILTER="${UNHEALTHY_FILTER}"' else .state == "stopped" end)'
UNHEALTHY_FILTER="${UNHEALTHY_FILTER}"' | [.id, .state, $group] | @tsv'

COUNT_FILTER="[ ${MACHINES_IN_GROUP} ] | length"

log() {
  printf '%s [fly-verify-machines] %s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

# Capture `flyctl machine list --json` for $APP into file $1. flyctl's own
# stderr stays attached to the console for observability (the same instinct as
# the tee in fly-deploy.sh). Returns nonzero -- loudly -- when flyctl fails,
# when its stdout is not a JSON array, or when the array holds NO machine of
# the gated group. That last case is not pedantry: a gate whose selector
# matches nothing would otherwise report "no offending machines" and pass green
# forever the day flyctl renames the metadata key, and an app group with zero
# machines is prod being down regardless.
fetch_machines() {
  local file="$1"
  local rc count
  # set +e so a nonzero flyctl doesn't trip `set -e` before we can report it.
  set +e
  flyctl machine list --app "${APP}" --json > "${file}"
  rc="$?"
  set -e
  if (( rc != 0 )); then
    log "flyctl machine list failed (rc=${rc}) for app ${APP}"
    return 1
  fi
  if ! jq -e 'type == "array"' "${file}" > /dev/null 2>&1; then
    log "flyctl machine list did not return a JSON array; raw output follows"
    cat "${file}" >&2
    return 1
  fi
  count="$(jq "${COUNT_FILTER}" --arg group "${PROCESS_GROUP}" "${file}")"
  if (( count == 0 )); then
    log "app ${APP} reports ZERO ${PROCESS_GROUP}-group machines"
    return 1
  fi
  return 0
}

# See UNHEALTHY_FILTER. $1 is the captured machine list, $2 the mode.
unhealthy_machines() {
  local file="$1" mode="$2"
  jq -r "${UNHEALTHY_FILTER}" \
    --arg group "${PROCESS_GROUP}" --arg mode "${mode}" "${file}"
}

# Emit the failure to stdout, where GitHub Actions reads workflow commands, so
# a red run names the offending machines instead of only the step. $1 is the
# summary, $2 the (possibly empty) TSV rows from unhealthy_machines.
report_failure() {
  local summary="$1" rows="$2"
  local id state group
  printf '::error::%s\n' "${summary}"
  if [[ -z "${rows}" ]]; then
    # No rows means the list itself never yielded any: flyctl failed, returned
    # a non-array, or reported zero machines in the group. fetch_machines
    # logged which; do not guess at it here.
    printf '  (no machine rows -- see the machine-list error logged above)\n'
    return 0
  fi
  while IFS=$'\t' read -r id state group; do
    printf '  machine %s process-group=%s state=%s\n' "${id}" "${group}" "${state}"
  done <<< "${rows}"
}

# Post-deploy gate. Polls because a rolling replace legitimately leaves
# machines mid-transition for a few seconds; only the LAST poll's leftovers are
# a failure.
run_wait() {
  local file="$1"
  local attempt rows
  attempt=1
  while : ; do
    log "poll ${attempt}/${MAX_ATTEMPTS}: ${PROCESS_GROUP}-group machines of ${APP}"
    rows=""
    if fetch_machines "${file}"; then
      rows="$(unhealthy_machines "${file}" wait)"
      if [[ -z "${rows}" ]]; then
        log "every ${PROCESS_GROUP}-group machine of ${APP} reports started"
        return 0
      fi
    fi

    if (( attempt >= MAX_ATTEMPTS )); then
      report_failure \
        "app ${APP}: ${PROCESS_GROUP}-group machine(s) never reached state started after ${MAX_ATTEMPTS} polls -- the release is NOT serving" \
        "${rows}"
      return 1
    fi

    log "not started yet; re-polling in ${POLL_SECONDS}s"
    if (( POLL_SECONDS > 0 )); then
      sleep "${POLL_SECONDS}"
    fi
    attempt=$(( attempt + 1 ))
  done
}

# Monitor. Single shot by design: the cron cadence IS the polling, and a second
# in-run poll would only delay the page. A machine list we cannot read fails
# too -- a blind monitor is indistinguishable from a monitor watching a dead
# app, and 3h35m of silence is the failure mode this exists to end.
run_check() {
  local file="$1"
  local rows
  if ! fetch_machines "${file}"; then
    report_failure \
      "app ${APP}: could not read ${PROCESS_GROUP}-group machine state -- the monitor is blind, treat as an outage until proven otherwise" \
      ""
    return 1
  fi

  rows="$(unhealthy_machines "${file}" check)"
  if [[ -n "${rows}" ]]; then
    report_failure \
      "app ${APP}: ${PROCESS_GROUP}-group machine(s) are STOPPED -- the app group has no autostop, so this is prod down, not idling" \
      "${rows}"
    return 1
  fi

  log "no stopped ${PROCESS_GROUP}-group machines in ${APP}"
  return 0
}

main() {
  local mode outfile
  mode="${1:-}"
  outfile="$(mktemp "${TMPDIR:-/tmp}/fly-verify-machines.XXXXXX")"
  # shellcheck disable=SC2064  # expand outfile now: it is fixed for this run.
  trap "rm -f '${outfile}'" EXIT

  case "${mode}" in
    wait)
      run_wait "${outfile}"
      ;;
    check)
      run_check "${outfile}"
      ;;
    *)
      log "usage: fly-verify-machines.sh wait|check (got '${mode}')"
      # Exit 2, distinct from a real machine-state failure (1), so a workflow
      # typo can never read as "prod is down".
      return 2
      ;;
  esac
}

# Auto-run only when executed directly, so the test can source this file and
# exercise the helpers in isolation without calling Fly.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
