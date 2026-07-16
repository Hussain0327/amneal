"""Drift guard: the app's boot command must be the SAME argv everywhere.

fly.toml `[processes].app`, the image `CMD`, and compose's `command` each spell
out how the API starts. Until now a Dockerfile comment asserted they were
"byte-identical" and nothing checked it, which is exactly how this repo shipped
a boot command that outlived its own config: tests/test_entrypoint_guard.py's
docstring records that its argv "once carried `--host ::` and quietly outlived
the config it claimed to mirror".

Drift here is not cosmetic. The three copies are the deploy (fly.toml), the
image default and every `fly machine run` / `docker run` (CMD), and local dev
(compose). If they diverge, CI and dev exercise a listener prod does not run --
the precise blind spot that let root cause 2 merge (docs/GO_PROXY_ROLLOUT.md).

Precedent for parsing fly.toml in a test: tests/test_login_ratelimit_ip.py.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FLY_TOML = _REPO_ROOT / "fly.toml"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_COMPOSE = _REPO_ROOT / "compose.yaml"

# The one true boot argv. Written out rather than derived so that changing the
# boot command is a deliberate edit to a test, not an accident that quietly
# re-agrees with itself.
_EXPECTED = ["regwatch", "serve"]

# Final CMD in the runtime stage, JSON exec form: CMD ["a", "b"].
_CMD_RE = re.compile(r"^CMD\s+(\[.*\])\s*$", re.MULTILINE)


def _fly_process_argv() -> list[str]:
    with _FLY_TOML.open("rb") as fh:
        fly: dict[str, Any] = tomllib.load(fh)
    # Fly runs the process string through a shell-style split.
    return shlex.split(fly["processes"]["app"])


def _dockerfile_cmd_argv() -> list[str]:
    matches = _CMD_RE.findall(_DOCKERFILE.read_text(encoding="utf-8"))
    assert matches, "No JSON-exec-form CMD found in the Dockerfile"
    # The last CMD wins if the file ever grows more than one.
    argv = json.loads(matches[-1])
    assert isinstance(argv, list)
    return [str(part) for part in argv]


def _compose_api_command_argv() -> list[str]:
    compose: dict[str, Any] = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    command = compose["services"]["api"]["command"]
    # Accept either exec-form list or a shell string.
    return [str(part) for part in command] if isinstance(command, list) else shlex.split(command)


def test_fly_process_matches_expected_boot_argv() -> None:
    assert _fly_process_argv() == _EXPECTED


def test_dockerfile_cmd_matches_fly_process() -> None:
    """The deploy (fly.toml) and the image default must not diverge."""
    assert _dockerfile_cmd_argv() == _fly_process_argv()


def test_compose_api_command_matches_fly_process() -> None:
    """Local dev must exercise the same listener prod runs."""
    assert _compose_api_command_argv() == _fly_process_argv()


def test_boot_command_does_not_use_a_uvicorn_host_flag() -> None:
    """No --host bind can be correct here, so ban the whole shape.

    Single-process uvicorn binds via loop.create_server, where asyncio and
    uvloop force IPV6_V6ONLY=1: `--host ::` is IPv6-ONLY (refuses flyd's IPv4
    checks and Fly Proxy's private-IPv4 backhaul) and `--host 0.0.0.0` is
    IPv4-only (refuses the phase-3 proxy's 6PN dials -- the 2026-07-15 deploy
    #106 failure). One --host cannot serve both families; that is why
    `regwatch serve` exists. This guards the tempting "simplification" back.
    """
    for argv in (_fly_process_argv(), _dockerfile_cmd_argv(), _compose_api_command_argv()):
        assert "--host" not in argv, (
            f"{argv} pins a uvicorn --host bind. Single-process uvicorn cannot "
            "serve IPv4 and IPv6 through --host; use `regwatch serve` (see "
            "docs/GO_PROXY_ROLLOUT.md root cause 2)."
        )
