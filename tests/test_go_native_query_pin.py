"""Prod-config guard: fly.toml must keep the step-5 GO_NATIVE_QUERY pin on,
and the runbook must not contradict it.

The flag flipped live on 2026-07-24 by Fly SECRET, and the "After the flip is
proven" step unsets that secret once the fly.toml [env] pin deploys -- at
which point this one line is the ONLY thing keeping prod on the Go native
POST /query path. Nothing else notices if it is dropped: Go's own tests set
GO_NATIVE_QUERY with t.Setenv, and go/internal/api/config.go reads it with
envBool(default false), so a silently deleted pin does not fail anything,
it just reverts prod to the relay path on the next machine restart.

The second test pins the DOC against the CONFIG. The runbook is what an
on-call reads at 02:00; a stale "PROD IS FLAG-OFF" status line while the
config pins the flag on is how someone runs the wrong revert. It matches a
stable machine-readable marker (the "Status <date>: PROD IS FLAG-(ON|OFF)"
line) rather than an exact sentence, so the surrounding prose stays free to
change while the one load-bearing claim cannot drift.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNBOOK = _REPO_ROOT / "docs" / "GO_NATIVE_QUERY_ROLLOUT.md"

# The one clause in the runbook that must track fly.toml. Anchored to line
# start so a quoted mention inside a paragraph cannot satisfy or break it.
_STATUS_MARKER = re.compile(r"^Status \d{4}-\d{2}-\d{2}: PROD IS FLAG-(ON|OFF)\b", re.MULTILINE)


def _fly_env() -> dict[str, str]:
    cfg = tomllib.loads((_REPO_ROOT / "fly.toml").read_text(encoding="utf-8"))
    return cfg["env"]


def test_prod_fly_toml_pins_go_native_query_on() -> None:
    # Must be the string "true": Go parses it via envBool
    # (go/internal/api/config.go), which accepts "true"/"1"/"yes"/"on".
    assert _fly_env().get("GO_NATIVE_QUERY") == "true"


def test_runbook_status_line_agrees_with_the_fly_toml_pin() -> None:
    matches = _STATUS_MARKER.findall(_RUNBOOK.read_text(encoding="utf-8"))
    # Exactly one, so a second stale status block cannot hide behind a fresh
    # one (the doc is edited in place at every rollout phase).
    assert len(matches) == 1, f"expected exactly one status marker, got {matches}"
    expected = "ON" if _fly_env().get("GO_NATIVE_QUERY") == "true" else "OFF"
    assert matches[0] == expected


def test_runbook_gives_both_state_dependent_revert_commands() -> None:
    # The revert is state-dependent and getting it wrong is a live incident:
    # `secrets unset` reverts only while a secret holds the flag on, and is a
    # NO-OP once the [env] pin is the sole authority. Both forms must survive
    # any future edit of this runbook. Matching on the COMMANDS (stable) and
    # not on the prose around them.
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert "fly secrets unset GO_NATIVE_QUERY -a amneal" in runbook
    assert "fly secrets set GO_NATIVE_QUERY=false -a amneal" in runbook
