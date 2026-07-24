"""Python SAST (ruff's flake8-bandit "S" rules) must stay wired into the lint job.

The lint job is the only thing standing between this repo and unreviewed
`eval`/`pickle`/`shell=True`/hardcoded-credential code. These tests fail if "S"
is dropped from [tool.ruff.lint] select, or if a per-file-ignores entry is
widened from a specific rule list to a blanket "S" -- both of which would
silently disable security linting while leaving a green build.

The probes shell out to ruff with the project config rather than asserting on
pyproject strings alone, so they verify the rules actually FIRE rather than
merely that a code appears in a list.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# ruff is a dev dependency, so it sits next to the interpreter running pytest.
_RUFF = Path(sys.executable).parent / "ruff"

# Lint fixtures, not executable paths: these strings are piped to ruff's stdin
# and parsed. Nothing here is ever exec'd, imported, or eval'd by the test.
_EVAL_SNIPPET = 'value = eval("2")\n'
_ASSERT_SNIPPET = "def check(x: object) -> None:\n    assert x\n"


def _ruff_codes(source: str, *, as_path: str) -> set[str]:
    """Rule codes ruff reports for `source` when linted as `as_path`.

    `as_path` decides which per-file-ignores apply, which is exactly what these
    tests need to distinguish src/ from tests/.
    """
    assert _RUFF.exists(), f"ruff not installed at {_RUFF}; install the dev extra"
    proc = subprocess.run(
        [
            str(_RUFF),
            "check",
            "--stdin-filename",
            as_path,
            "--no-cache",
            "--output-format",
            "concise",
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=60,
    )
    # ruff exits 1 when it finds violations; anything above that is a real
    # failure (bad config, bad flag) and must not be read as "no findings".
    assert proc.returncode in (
        0,
        1,
    ), f"ruff exited {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    return {
        line.split(": ", 1)[1].split(" ", 1)[0]
        for line in proc.stdout.splitlines()
        if line.startswith(as_path) and ": " in line
    }


def test_select_declares_flake8_bandit() -> None:
    """ "S" is in the ruff select list (fast, precise failure message)."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        config = tomllib.load(fh)
    select = config["tool"]["ruff"]["lint"]["select"]
    assert "S" in select, f'flake8-bandit ("S") missing from ruff select: {select}'


def test_bandit_fires_on_production_code() -> None:
    """The canonical SAST finding is reported for a file under src/."""
    codes = _ruff_codes(_EVAL_SNIPPET, as_path="src/regwatch/_sast_probe.py")
    assert "S307" in codes, f"eval() not flagged in src/; SAST is off. got: {codes}"


def test_bandit_still_fires_inside_tests() -> None:
    """tests/ ignores specific noisy S rules, never the whole "S" family.

    A blanket `"tests/**" = ["S"]` would let `eval` of untrusted input into a
    test helper unreviewed, so pin that the family is still active here.
    """
    codes = _ruff_codes(_EVAL_SNIPPET, as_path="tests/test__sast_probe.py")
    assert "S307" in codes, f"eval() not flagged in tests/; S is blanket-ignored: {codes}"


def test_assert_flagged_in_src_but_allowed_in_tests() -> None:
    """S101 is scoped, not globally disabled: on in src/, off in tests/.

    `assert` is the oracle in tests, but in production code it is stripped under
    -O and must never be load-bearing (e.g. `assert user.is_admin`).
    """
    src_codes = _ruff_codes(_ASSERT_SNIPPET, as_path="src/regwatch/_sast_probe.py")
    test_codes = _ruff_codes(_ASSERT_SNIPPET, as_path="tests/test__sast_probe.py")
    assert "S101" in src_codes, f"assert not flagged in src/: {src_codes}"
    assert "S101" not in test_codes, f"assert wrongly flagged in tests/: {test_codes}"
