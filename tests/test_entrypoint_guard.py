"""Behavioural checks for docker/entrypoint.sh -- the boot-time init-db guard.

No docker: the script runs under /bin/sh with a stub-only PATH (fake regwatch,
uvicorn, alembic and regwatch-proxy record their argv plus the exported
REGWATCH_DB_INITIALIZED into a shared log) and every path the script writes is
pointed under tmp_path, so nothing touches the repo, /app, or a real database.

The contract under test (entrypoint dispatch on $1, across today's app group
and the staged phase-3 proxy group -- docs/GO_PROXY_ROLLOUT.md):

* ``alembic ...`` (the Fly release_command) skips init-db: the release machine
  exists to MOVE the alembic stamp to head, and the stamp guard would refuse
  and abort the whole deploy first.
* ``regwatch-proxy`` (proxy process group), plain or path-qualified, skips
  init-db: the proxy must boot DB-independent. A proxy machine crash-looping
  on the stamp guard while holding the public port is the 2026-06-18/07-07
  incident class.
* ``regwatch serve`` (app process group, the real fly.toml/Dockerfile boot
  command since the phase-2 dual-stack listener) runs ``regwatch init-db``
  FIRST, exports REGWATCH_DB_INITIALIZED=1, then hands off with argv intact.
  $1 is "regwatch", which matches no skip branch, so it takes the init-db
  default -- the behaviour the app group has always had.
* ``uvicorn ...`` (the pre-phase-2 app command) behaves identically. Kept as a
  regression row: the dispatch is on $1 alone, so it must not start depending
  on which app command we happen to ship.
* an init-db failure aborts the boot with its exit code (the app never runs:
  that refusal IS the boot guard).
* REGWATCH_INIT_DB=false skips init-db for any command (compose opt-out).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

STUBS = ("regwatch", "uvicorn", "alembic", "regwatch-proxy")

# Each stub appends "name|argv|db_init=<REGWATCH_DB_INITIALIZED or unset>" so a
# single log proves WHAT ran, in WHICH order, with WHICH env the entrypoint
# exported. Exit code is injectable per stub (STUB_EXIT_<NAME>) to drive the
# failure paths.
_STUB_TEMPLATE = """#!/bin/sh
printf '%s\\n' "{name}|$*|db_init=${{REGWATCH_DB_INITIALIZED:-unset}}" >> "$STUB_LOG"
exit "${{STUB_EXIT_{slug}:-0}}"
"""


def _write_stub(bindir: Path, name: str) -> None:
    stub = bindir / name
    slug = name.upper().replace("-", "_")
    stub.write_text(_STUB_TEMPLATE.format(name=name, slug=slug))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path,
    argv: list[str],
    *,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Drive the real entrypoint under /bin/sh with a hermetic environment."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    for name in STUBS:
        _write_stub(bindir, name)
    log = tmp_path / "invocations.log"
    data = tmp_path / "data"
    env = {
        # Stubs first; /usr/bin:/bin supply the script's real utilities
        # (mkdir, dirname, cp) on both macOS and Linux CI. The parent PATH is
        # deliberately absent so a real regwatch/uvicorn can never leak in.
        "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "STUB_LOG": str(log),
        # Redirect every directory/file the script creates under tmp_path.
        "DATA_DIR": str(data),
        "RAW_PDF_DIR": str(data / "raw"),
        "PROCESSED_DIR": str(data / "processed"),
        "WHITEPAPER_TEMPLATE_PATH": str(data / "templates" / "template.docx"),
    }
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["/bin/sh", str(SCRIPT), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    return proc, lines


def test_serve_runs_init_db_then_hands_off(tmp_path: Path) -> None:
    """The REAL app-group boot command (fly.toml [processes].app = `regwatch serve`).

    $1 is "regwatch", not "uvicorn", so this proves the phase-2 command still
    lands on the init-db default branch rather than silently matching a skip.
    One stub serves both rows here, so assert on the argv field -- the stub
    name alone no longer distinguishes init-db from the handoff.
    """
    proc, lines = _run(tmp_path, ["regwatch", "serve"])
    assert proc.returncode == 0, proc.stderr
    assert lines == [
        "regwatch|init-db|db_init=unset",
        "regwatch|serve|db_init=1",
    ]


def test_uvicorn_runs_init_db_then_hands_off(tmp_path: Path) -> None:
    # The entrypoint dispatches on $1 only, so this argv is illustrative, not a
    # contract -- it is NOT parsed from fly.toml and must not be read as the
    # canonical boot command. (It once carried "--host ::" and quietly outlived
    # the config it claimed to mirror; that bind is now refuted -- see
    # docs/GO_PROXY_ROLLOUT.md root cause 2.)
    proc, lines = _run(
        tmp_path,
        ["uvicorn", "regwatch.api.main:app", "--host", "0.0.0.0", "--port", "8000"],
    )
    assert proc.returncode == 0, proc.stderr
    # init-db ran first (before the export), then uvicorn with argv intact and
    # REGWATCH_DB_INITIALIZED=1 visible to it.
    assert lines == [
        "regwatch|init-db|db_init=unset",
        "uvicorn|regwatch.api.main:app --host 0.0.0.0 --port 8000|db_init=1",
    ]


def test_alembic_release_command_skips_init_db(tmp_path: Path) -> None:
    proc, lines = _run(tmp_path, ["alembic", "upgrade", "head"])
    assert proc.returncode == 0, proc.stderr
    assert lines == ["alembic|upgrade head|db_init=unset"]


def test_proxy_skips_init_db(tmp_path: Path) -> None:
    proc, lines = _run(tmp_path, ["regwatch-proxy"])
    assert proc.returncode == 0, proc.stderr
    assert lines == ["regwatch-proxy||db_init=unset"]


def test_path_qualified_proxy_skips_init_db(tmp_path: Path) -> None:
    # fly.toml runs the bare name, but a path-qualified invocation (e.g.
    # /usr/local/bin/regwatch-proxy from a one-off `fly machine run`) must not
    # sneak past the guard and crash-loop on the stamp.
    proxy_path = str(tmp_path / "stub-bin" / "regwatch-proxy")
    proc, lines = _run(tmp_path, [proxy_path])
    assert proc.returncode == 0, proc.stderr
    assert lines == ["regwatch-proxy||db_init=unset"]


def test_regwatch_init_db_false_skips_for_app_command(tmp_path: Path) -> None:
    proc, lines = _run(
        tmp_path,
        ["uvicorn", "regwatch.api.main:app"],
        env_extra={"REGWATCH_INIT_DB": "false"},
    )
    assert proc.returncode == 0, proc.stderr
    assert lines == ["uvicorn|regwatch.api.main:app|db_init=unset"]


def test_init_db_failure_aborts_boot(tmp_path: Path) -> None:
    # set -e: a stamp-guard refusal must stop the boot with init-db's exit
    # code; uvicorn must never start against a drifted schema.
    proc, lines = _run(
        tmp_path,
        ["uvicorn", "regwatch.api.main:app"],
        env_extra={"STUB_EXIT_REGWATCH": "3"},
    )
    assert proc.returncode == 3
    assert lines == ["regwatch|init-db|db_init=unset"]


def test_final_command_exit_code_propagates(tmp_path: Path) -> None:
    # exec "$@" semantics: the entrypoint's exit status is the command's own.
    proc, lines = _run(
        tmp_path,
        ["uvicorn", "regwatch.api.main:app"],
        env_extra={"STUB_EXIT_UVICORN": "7"},
    )
    assert proc.returncode == 7
    assert [line.split("|")[0] for line in lines] == ["regwatch", "uvicorn"]
