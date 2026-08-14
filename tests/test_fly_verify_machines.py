"""Behavioural checks for scripts/fly-verify-machines.sh -- the app-group machine-state gate.

No network, no flyctl, no Fly. The contract under test (postmortem #224, the 2026-08-13
outage: deploy v137 completed GREEN over two crash-looping app machines, which then
exhausted their restart budget, stopped, and stayed stopped for 3h35m with nothing paging):

* `wait` (deploy.yml's post-deploy gate) exits 0 only when EVERY app-group machine reports
  state "started". A machine still "starting" on one poll and "started" on the next passes
  -- a rolling replace is transiently untidy, which is why this polls -- but a machine that
  never gets there exhausts the attempt budget and exits 1, naming the machine.
* `check` (machine-monitor.yml, every 10 minutes) exits 1 the moment ANY app-group machine
  is "stopped" and 0 otherwise. The app group has no autostop config, so stopped is never
  legitimate.
* Machines in OTHER process groups are invisible to both modes: the proxy group holds the
  public edge and cycles on its own schedule, and must never fail (or rescue) this gate.
* A machine list that cannot be read, or that contains no app-group machine at all, is a
  FAILURE and never a silent pass -- a gate whose selector matches nothing would otherwise
  stay green forever the day flyctl renames a field, which is the failure mode that makes a
  monitor worse than none.
* An unknown mode exits 2, distinct from a real machine-state failure, and calls no Fly API.

Each case drives the real script via a stub ``flyctl`` placed first on PATH that records
every invocation and serves a scripted sequence of `machine list --json` payloads. Polling
is instant (FLY_VERIFY_POLL_SECONDS=0) so the suite does not sleep.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fly-verify-machines.sh"

Machine = dict[str, object]


def _machine(machine_id: str, state: str, group: str = "app") -> Machine:
    """One machine as `flyctl machine list --json` renders it.

    Trimmed to the fields the script reads (id, state, the process group under
    config.metadata) plus enough surrounding shape to stay recognisable against the real
    payload.
    """
    return {
        "id": machine_id,
        "name": f"regwatch-{machine_id}",
        "state": state,
        "region": "iad",
        "config": {"metadata": {"fly_platform_version": "v2", "fly_process_group": group}},
    }


def _make_flyctl_stub(tmp_path: Path, responses: list[list[Machine]]) -> tuple[Path, Path, Path]:
    """A fake ``flyctl`` that records argv and serves ``responses`` in order.

    Invocation n prints responses[n-1]; anything past the end repeats the last entry, so a
    poll loop can be driven with a one- or two-element script. With FLY_STUB_FAIL=1 it
    instead fails the way a token that may not read machine state does. Returns
    (bindir, invocation_log, response_dir); the response dir also holds the counter.
    """
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "flyctl-invocations.txt"
    respdir = tmp_path / "responses"
    respdir.mkdir(exist_ok=True)
    for index, machines in enumerate(responses, start=1):
        (respdir / f"response-{index}.json").write_text(json.dumps(machines))
    stub = bindir / "flyctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$FLY_STUB_LOG"\n'
        'n=$(cat "$FLY_STUB_DIR/counter" 2>/dev/null || printf 0)\n'
        "n=$((n + 1))\n"
        'printf \'%s\' "$n" > "$FLY_STUB_DIR/counter"\n'
        'if [ "${FLY_STUB_FAIL:-0}" = "1" ]; then\n'
        "  printf 'Error: unauthorized: token cannot read machines\\n' >&2\n"
        "  exit 1\n"
        "fi\n"
        # Fall back to the last scripted response so a poll loop longer than the
        # script keeps seeing the final state instead of erroring on a missing file.
        "k=$n\n"
        'while [ ! -f "$FLY_STUB_DIR/response-$k.json" ] && [ "$k" -gt 1 ]; do\n'
        "  k=$((k - 1))\n"
        "done\n"
        'cat "$FLY_STUB_DIR/response-$k.json"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, log, respdir


def _run(
    tmp_path: Path,
    mode: str,
    responses: list[list[Machine]],
    *,
    max_attempts: int = 3,
    flyctl_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Invoke fly-verify-machines.sh with a stub ``flyctl`` first on PATH."""
    bindir, log, respdir = _make_flyctl_stub(tmp_path, responses)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FLY_STUB_LOG"] = str(log)
    env["FLY_STUB_DIR"] = str(respdir)
    if flyctl_fails:
        env["FLY_STUB_FAIL"] = "1"
    # Drop any developer's real FLY_APP so the default (fly.toml's `amneal`) is what the
    # argv assertion below actually pins.
    env.pop("FLY_APP", None)
    env["FLY_VERIFY_MAX_ATTEMPTS"] = str(max_attempts)
    env["FLY_VERIFY_POLL_SECONDS"] = "0"  # never sleep in tests
    proc = subprocess.run(
        ["bash", str(SCRIPT), mode], env=env, capture_output=True, text=True, timeout=60
    )
    invocations = log.read_text().splitlines() if log.exists() else []
    return proc, invocations


def test_wait_passes_when_every_app_machine_is_started(tmp_path: Path) -> None:
    proc, invocations = _run(
        tmp_path, "wait", [[_machine("aaa", "started"), _machine("bbb", "started")]]
    )
    assert proc.returncode == 0, proc.stderr
    assert len(invocations) == 1, invocations  # no needless re-poll once healthy
    assert invocations[0] == "machine list --app amneal --json"


def test_wait_polls_until_a_transitional_machine_reaches_started(tmp_path: Path) -> None:
    """A rolling replace legitimately leaves a machine mid-transition for a few seconds.
    Failing on the first poll would turn every normal deploy red, which is why `wait`
    polls instead of asserting once."""
    proc, invocations = _run(
        tmp_path,
        "wait",
        [
            [_machine("aaa", "starting"), _machine("bbb", "started")],
            [_machine("aaa", "started"), _machine("bbb", "started")],
        ],
    )
    assert proc.returncode == 0, proc.stderr
    assert len(invocations) == 2, invocations  # polled again, then passed


@pytest.mark.parametrize("stuck_state", ["starting", "created", "replacing", "stopped"])
def test_wait_fails_when_a_machine_never_reaches_started(tmp_path: Path, stuck_state: str) -> None:
    """The v137 case: the deploy must go RED when the fleet never reaches a serving state,
    whatever non-started state it is stuck in."""
    proc, invocations = _run(
        tmp_path,
        "wait",
        [[_machine("aaa", stuck_state), _machine("bbb", "started")]],
        max_attempts=3,
    )
    assert proc.returncode == 1
    assert len(invocations) == 3, invocations  # whole budget consumed before failing
    assert "::error::" in proc.stdout
    assert "aaa" in proc.stdout
    assert "bbb" not in proc.stdout  # the healthy machine is not blamed


def test_check_passes_when_no_app_machine_is_stopped(tmp_path: Path) -> None:
    proc, invocations = _run(
        tmp_path, "check", [[_machine("aaa", "started"), _machine("bbb", "started")]]
    )
    assert proc.returncode == 0, proc.stderr
    assert len(invocations) == 1, invocations  # single shot; the cron is the polling


def test_check_fails_and_names_a_stopped_app_machine(tmp_path: Path) -> None:
    """The 3h35m of silence this exists to end. The machine id must reach the log, or the
    page names no target."""
    proc, _ = _run(tmp_path, "check", [[_machine("aaa", "stopped"), _machine("bbb", "started")]])
    assert proc.returncode == 1
    assert "::error::" in proc.stdout
    assert "aaa" in proc.stdout
    assert "stopped" in proc.stdout


def test_check_tolerates_a_restarting_machine(tmp_path: Path) -> None:
    """Only the stopped state pages. A machine caught mid-restart between two 10-minute
    polls is not an outage, and paging on it would train us to ignore the alert."""
    proc, _ = _run(tmp_path, "check", [[_machine("aaa", "starting"), _machine("bbb", "started")]])
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("mode", ["wait", "check"])
def test_other_process_groups_are_ignored(tmp_path: Path, mode: str) -> None:
    """The proxy group holds the public edge and cycles on its own schedule; a stopped or
    starting proxy machine must not fail an APP-group gate."""
    machines = [
        _machine("aaa", "started"),
        _machine("bbb", "started"),
        _machine("prx1", "stopped", group="proxy"),
        _machine("prx2", "starting", group="proxy"),
    ]
    proc, _ = _run(tmp_path, mode, [machines])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_release_machine_without_group_metadata_is_gated_as_app(tmp_path: Path) -> None:
    """Fly's default process group IS named "app" (fly.toml pins that name), so a RELEASE
    machine carrying no group metadata is served by the app group. Skipping it would let the
    gate ignore a real serving machine."""
    legacy: Machine = {
        "id": "old1",
        "name": "regwatch-old1",
        "state": "stopped",
        "region": "iad",
        "config": {"metadata": {"fly_platform_version": "v2", "fly_release_id": "rel_1"}},
    }
    proc, _ = _run(tmp_path, "check", [[_machine("aaa", "started"), legacy]])
    assert proc.returncode == 1
    assert "old1" in proc.stdout


@pytest.mark.parametrize("mode", ["wait", "check"])
def test_one_off_machine_with_no_release_metadata_is_ignored(tmp_path: Path, mode: str) -> None:
    """A `fly machine run` one-off carries EMPTY metadata and restart policy "no": it belongs
    to no release and no process group, and being stopped is its finished state, not an
    outage. Deploy #413 failed on exactly this -- two leftover embedding-backfill machines
    from the 2026-08-13 recovery reported "prod is NOT serving" while all four release
    machines were started and prod was answering."""
    one_off: Machine = {
        "id": "backfill1",
        "name": "blue-firefly-1352",
        "state": "stopped",
        "region": "iad",
        "config": {"metadata": {}, "restart": {"policy": "no"}},
    }
    proc, _ = _run(tmp_path, mode, [[_machine("aaa", "started"), one_off]])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "backfill1" not in proc.stdout


@pytest.mark.parametrize("mode", ["wait", "check"])
def test_fails_when_the_group_has_no_machines(tmp_path: Path, mode: str) -> None:
    """A selector matching nothing must never read as "nothing wrong": that is how a gate
    goes green forever the day flyctl renames a field. An app group with zero machines is
    also prod down on its own terms."""
    proc, _ = _run(tmp_path, mode, [[_machine("prx1", "started", group="proxy")]], max_attempts=2)
    assert proc.returncode == 1
    assert "::error::" in proc.stdout


@pytest.mark.parametrize("mode", ["wait", "check"])
def test_fails_when_the_machine_list_cannot_be_read(tmp_path: Path, mode: str) -> None:
    """FLY_API_TOKEN is a deploy-scoped token; if it may not read machine state, both modes
    must go RED rather than quietly pass. A blind gate is worth nothing."""
    proc, invocations = _run(tmp_path, mode, [], max_attempts=2, flyctl_fails=True)
    assert proc.returncode == 1
    assert invocations, "flyctl was never called"
    assert "::error::" in proc.stdout


def test_unknown_mode_exits_two_and_calls_no_fly_api(tmp_path: Path) -> None:
    """Exit 2 is deliberately distinct from a machine-state failure (1) so a workflow typo
    can never be read as "prod is down"."""
    proc, invocations = _run(tmp_path, "bogus", [[_machine("aaa", "started")]])
    assert proc.returncode == 2
    assert invocations == [], invocations
