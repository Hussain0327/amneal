"""Behavioural checks for scripts/fly-deploy.sh -- the bounded Fly deploy retry.

No network, no flyctl, no Fly. The contract under test (the 2026-06-29
`deadline_exceeded: machine still starting` incident):

* A transient Fly platform error (e.g. the machine-start timeout) is RETRIED,
  up to FLY_DEPLOY_MAX_ATTEMPTS, and a later success makes the wrapper exit 0 --
  even when the marker is buried in a large, multi-line `flyctl` log (the case
  that SIGPIPEs a `printf | grep -q` classifier under `set -o pipefail`).
* A NON-transient failure (a real migration error, a failed health/smoke check,
  a release_command exit, or text carrying a deliberately-excluded phrase like
  "connection reset"/"please try again") FAILS FAST: nonzero after exactly one
  attempt, never retrying genuine breakage behind a green check.
* When the first attempt is transient but a later one is deterministic, the
  wrapper retries once then fails fast -- i.e. it classifies ONLY the current
  attempt's output (the capture file is truncated, not appended, each attempt).
* A persistently-transient failure exhausts the budget and propagates flyctl's
  nonzero exit code. A clean deploy runs flyctl exactly once.

Each case drives the real script via a stub ``flyctl`` placed first on PATH that
records every invocation and fails a configurable number of times with chosen
message(s). Backoff is disabled (FLY_DEPLOY_BASE_DELAY_SECONDS=0) so the suite
does not sleep.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fly-deploy.sh"

# A representative sample of the real Fly machine-start timeout text.
TRANSIENT_MSG = (
    "error starting release_command machine: failed to start VM d895132ae09628: "
    "deadline_exceeded: machine still starting"
)
# A deterministic failure a retry must NOT paper over.
NON_TRANSIENT_MSG = "Error: release command failed: alembic.util.exc.CommandError: migration broken"


def _make_flyctl_stub(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A fake ``flyctl`` that records argv and fails FLY_STUB_FAIL_TIMES times.

    The failure message is FLY_STUB_FAIL_MESSAGE, or -- when FLY_STUB_MESSAGES is
    set (newline-separated) -- the n-th line for attempt n, falling back to the
    last line. Returns (bindir, invocation_log, counter_file).
    """
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "flyctl-invocations.txt"
    counter = tmp_path / "flyctl-counter.txt"
    stub = bindir / "flyctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$FLY_STUB_LOG"\n'
        'n=$(cat "$FLY_STUB_COUNTER" 2>/dev/null || printf 0)\n'
        "n=$((n + 1))\n"
        'printf \'%s\' "$n" > "$FLY_STUB_COUNTER"\n'
        'if [ "$n" -le "${FLY_STUB_FAIL_TIMES:-0}" ]; then\n'
        '  if [ -n "${FLY_STUB_MESSAGES:-}" ]; then\n'
        '    msg=$(printf \'%s\\n\' "$FLY_STUB_MESSAGES" | sed -n "${n}p")\n'
        '    [ -z "$msg" ] && msg=$(printf \'%s\\n\' "$FLY_STUB_MESSAGES" | tail -n1)\n'
        "  else\n"
        '    msg="$FLY_STUB_FAIL_MESSAGE"\n'
        "  fi\n"
        "  printf '%s\\n' \"$msg\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "printf 'Deployment succeeded\\n'\n"
        "exit 0\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, log, counter


def _run(
    tmp_path: Path,
    *,
    fail_times: int,
    fail_message: str,
    fail_messages: str | None = None,
    max_attempts: int = 3,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Invoke fly-deploy.sh with a stub ``flyctl`` first on PATH."""
    bindir, log, counter = _make_flyctl_stub(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FLY_STUB_LOG"] = str(log)
    env["FLY_STUB_COUNTER"] = str(counter)
    env["FLY_STUB_FAIL_TIMES"] = str(fail_times)
    env["FLY_STUB_FAIL_MESSAGE"] = fail_message
    if fail_messages is not None:
        env["FLY_STUB_MESSAGES"] = fail_messages
    env["FLY_DEPLOY_MAX_ATTEMPTS"] = str(max_attempts)
    env["FLY_DEPLOY_BASE_DELAY_SECONDS"] = "0"  # never sleep in tests
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    invocations = log.read_text().splitlines() if log.exists() else []
    return proc, invocations


def test_succeeds_first_attempt_without_retry(tmp_path: Path) -> None:
    proc, invocations = _run(tmp_path, fail_times=0, fail_message=TRANSIENT_MSG)
    assert proc.returncode == 0, proc.stderr
    assert len(invocations) == 1, invocations
    assert invocations[0] == "deploy --remote-only"


@pytest.mark.parametrize(
    "transient_message",
    [
        TRANSIENT_MSG,
        "failed to launch VM: no capacity in iad",
        "Post https://api.machines.dev: 503 Service Unavailable",
        "error starting release_command machine: failed to start VM d8: deadline_exceeded",
    ],
    ids=["machine-start", "no-capacity", "machines-api-503", "release-machine-start"],
)
def test_retries_transient_then_succeeds(tmp_path: Path, transient_message: str) -> None:
    proc, invocations = _run(tmp_path, fail_times=1, fail_message=transient_message)
    assert proc.returncode == 0, proc.stderr
    assert len(invocations) == 2, invocations  # one transient failure, then success
    assert "retrying" in proc.stderr.lower()


def test_retries_transient_marker_buried_in_large_multiline_output(tmp_path: Path) -> None:
    """The real incident log is multi-line and far larger than the 64KB pipe
    buffer, with the marker neither first nor last. This is the case that a
    `printf | grep -q` classifier SIGPIPEs under `set -o pipefail` (misclassified
    as fail-fast) and that a `head -1`/`tail -1` grep would miss entirely."""
    filler = "x" * 70_000  # > the 64KB pipe buffer
    multiline = (
        "==> Building image\n"
        "image: registry.fly.io/amneal:deployment-01\n"
        "Running amneal release_command: alembic upgrade head\n"
        "error starting release_command machine: failed to start VM d895132ae09628: "
        "deadline_exceeded: machine still starting\n"
        f"{filler}\n"
        "Error: release command failed - aborting deployment.\n"
        "Check the logs at: https://fly.io/apps/amneal/monitoring"
    )
    proc, invocations = _run(tmp_path, fail_times=1, fail_message=multiline)
    assert proc.returncode == 0, proc.stderr
    assert len(invocations) == 2, invocations  # retried despite the buried marker
    assert "retrying" in proc.stderr.lower()


@pytest.mark.parametrize(
    "deterministic_message",
    [
        NON_TRANSIENT_MSG,
        "Error: release_command failed running on machine 17811952f00489: exit code: 1",
        "Error: failed to reach a good state: health checks not passing",
        # Each below embeds a phrase deliberately EXCLUDED from the allowlist;
        # these must fail fast, so they also guard against re-broadening it.
        "Error: smoke checks for machine 1781 failed: context deadline exceeded",
        "Error: release_command failed: could not connect: connection reset by peer",
        "Error: migration failed; please try again with --sql",
    ],
    ids=[
        "migration",
        "release-command-exit",
        "health-checks",
        "smoke-ctx-deadline",
        "conn-reset",
        "please-try-again",
    ],
)
def test_fails_fast_on_non_transient_error(tmp_path: Path, deterministic_message: str) -> None:
    # fail_times=99 => would fail every time; the wrapper must still stop after
    # ONE attempt because the error is not in the transient allowlist.
    proc, invocations = _run(tmp_path, fail_times=99, fail_message=deterministic_message)
    assert proc.returncode != 0
    assert len(invocations) == 1, invocations
    assert "failing fast" in proc.stderr.lower()
    # No second attempt was ever started (the retry path logs "attempt 2/...").
    assert "attempt 2" not in proc.stderr.lower()


def test_fails_fast_when_transient_first_then_deterministic(tmp_path: Path) -> None:
    """Transient on attempt 1 (retried), a real migration error from attempt 2.
    The classifier must see ONLY attempt 2's output; if the capture file were
    appended-to instead of truncated, attempt 1's stale 'deadline_exceeded' would
    mis-classify attempt 2 as transient and burn the whole budget."""
    proc, invocations = _run(
        tmp_path,
        fail_times=99,
        fail_message=NON_TRANSIENT_MSG,  # unused; overridden by fail_messages
        fail_messages=f"{TRANSIENT_MSG}\n{NON_TRANSIENT_MSG}",
    )
    assert proc.returncode != 0
    assert len(invocations) == 2, invocations  # transient retry, then fail fast
    assert "failing fast" in proc.stderr.lower()
    assert "attempt 3" not in proc.stderr.lower()


def test_exhausts_attempts_on_persistent_transient(tmp_path: Path) -> None:
    proc, invocations = _run(tmp_path, fail_times=99, fail_message=TRANSIENT_MSG, max_attempts=3)
    assert proc.returncode != 0
    assert len(invocations) == 3, invocations  # all attempts consumed
    assert "no attempts left" in proc.stderr.lower()


def test_propagates_flyctl_exit_code_after_exhaustion(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, fail_times=99, fail_message=TRANSIENT_MSG, max_attempts=2)
    assert proc.returncode == 1  # the stub's exit code, surfaced by the wrapper
