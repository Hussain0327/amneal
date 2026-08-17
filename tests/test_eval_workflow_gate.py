"""The live eval must not be able to report success without measuring.

Two silent-green holes lived in databricks-eval.yml at once:

  * no provider credentials resolved ``arm=none``, which printed a ``::warning::``
    and let the job PASS -- "the eval could not run" was indistinguishable from
    "the eval ran and passed" on the PR page;
  * a lapsed Databricks credential fell back to the OpenAI-1536 arm, which is the
    ROLLBACK embedding space, and the blocking gate reported green on geometry
    production does not serve.

Neither is visible in a Python test that only imports code, so these read the
committed workflow. Same precedent as tests/test_go_native_query_pin.py and
tests/test_trust_proxy_fly_toml.py: the CI contract is part of the product.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_EVAL_WORKFLOW = _ROOT / ".github" / "workflows" / "databricks-eval.yml"
_CI_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"


# PyYAML follows YAML 1.1, where a bare `on:` key parses as the boolean True,
# not the string "on". Naming it once keeps that surprise out of every test.
_ON: Any = True


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers() -> dict[str, Any]:
    return _load(_EVAL_WORKFLOW)[_ON]


def _steps() -> list[dict[str, Any]]:
    return _load(_EVAL_WORKFLOW)["jobs"]["eval"]["steps"]


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in {_EVAL_WORKFLOW}")


def test_no_credentials_fails_the_job_instead_of_warning() -> None:
    """A measurement that did not happen must never read as one that passed."""
    step = _step("eval could not run")
    assert "exit 1" in step["run"]
    assert "::warning::" not in step["run"]


def test_no_step_lets_the_missing_arm_pass_with_only_a_warning() -> None:
    """Guards the shape, not one step name: any arm=none branch must fail."""
    for step in _steps():
        run = step.get("run") or ""
        guard = str(step.get("if") or "")
        if "arm == 'none'" in guard and "upload" not in (step.get("name") or ""):
            assert "exit 1" in run, f"{step.get('name')!r} tolerates a missing arm"


def test_the_blocking_call_requires_the_arm_production_serves() -> None:
    step = _step("require the arm production serves")
    assert "exit 1" in step["run"]
    assert "require_databricks_arm" in str(step["if"])
    assert "!= 'databricks'" in str(step["if"])


def test_the_prod_arm_requirement_defaults_on_for_the_blocking_call() -> None:
    """ci.yml passes no override, so the default IS the blocking behavior."""
    on = _triggers()
    assert on["workflow_call"]["inputs"]["require_databricks_arm"]["default"] is True


def test_the_rollback_arm_stays_hand_runnable_on_dispatch() -> None:
    """Failing a deliberate rollback comparison would be the opposite error."""
    on = _triggers()
    assert on["workflow_dispatch"]["inputs"]["require_databricks_arm"]["default"] is False


def test_the_required_arm_check_runs_before_any_billable_work() -> None:
    """A misconfigured run must not seed, index, embed or hold the eval slot."""
    names = [s.get("name") for s in _steps()]
    guard = names.index("require the arm production serves")
    for costly in ("prepare databricks eval arm", "seed vector store (databricks arm)"):
        assert guard < names.index(costly), f"{costly} runs before the arm guard"


def test_the_eval_arm_can_assert_it_matches_production() -> None:
    assert "--assert-prod-mode" in _step("eval (databricks arm)")["run"]


def test_the_legacy_openai_arm_stays_removed() -> None:
    # The rollback arm was removed with the OpenAI provider (2026-08-17); a
    # reintroduced legacy step would silently demote the gate's geometry.
    names = [s.get("name") for s in _steps()]
    assert "eval (legacy openai arm)" not in names
    assert "seed vector store (legacy arm)" not in names


def test_the_blocking_call_does_not_yet_assert_prod_mode() -> None:
    """Deliberate, and temporary.

    ci.yml still calls with prose=false/selective=false, so asserting now would
    fail every merge by design. The flip and this default move together, in the
    PR that retunes the thresholds against a measured v7 scorecard.
    """
    on = _triggers()
    assert on["workflow_call"]["inputs"]["assert_prod_mode"]["default"] is False
    call = _load(_CI_WORKFLOW)["jobs"]["databricks-eval"]["with"]
    assert call["prose"] is False
    assert call["selective"] is False
