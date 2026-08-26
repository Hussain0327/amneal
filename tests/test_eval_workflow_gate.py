"""The live eval must exercise the production OpenAI geometry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_EVAL_WORKFLOW = _ROOT / ".github" / "workflows" / "openai-eval.yml"
_CI_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps() -> list[dict[str, Any]]:
    return _load(_EVAL_WORKFLOW)["jobs"]["eval"]["steps"]


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in {_EVAL_WORKFLOW}")


def test_ci_calls_only_the_openai_eval_workflow() -> None:
    jobs = _load(_CI_WORKFLOW)["jobs"]

    assert jobs["openai-eval"]["uses"] == "./.github/workflows/openai-eval.yml"
    assert "databricks-eval" not in jobs


def test_eval_pins_openai_models_and_exact_retrieval() -> None:
    env = _load(_EVAL_WORKFLOW)["jobs"]["eval"]["env"]

    assert env["LLM_PROVIDER"] == "openai"
    assert env["OPENAI_LLM_MODEL"] == "gpt-5.6-luna"
    assert env["OPENAI_REASONING_EFFORT"] == "medium"
    assert env["INGEST_EMBEDDING_PROVIDER"] == "openai"
    assert env["OPENAI_EMBEDDING_MODEL"] == "text-embedding-3-large"
    assert env["OPENAI_EMBEDDING_DIMENSION"] == "1024"
    assert env["PROFILE_HNSW_INDEX_REQUIRED"] == "false"


def test_missing_openai_key_fails_before_billable_work() -> None:
    names = [step.get("name") for step in _steps()]
    preflight = _step("preflight OpenAI config")

    assert "exit 1" in preflight["run"]
    assert names.index("preflight OpenAI config") < names.index("register OpenAI embedding profile")


def test_profile_registration_is_openai_and_requires_no_hnsw_index() -> None:
    run = _step("register OpenAI embedding profile")["run"]
    names = [step.get("name") for step in _steps()]

    assert "--provider openai" in run
    assert "--serving-runtime-version openai-api-v1" in run
    assert not any("index" in str(name).lower() for name in names)


def test_eval_can_assert_it_matches_production() -> None:
    assert "--assert-prod-mode" in _step("eval OpenAI arm")["run"]


def test_no_databricks_configuration_remains_in_active_workflows() -> None:
    paths = (
        _EVAL_WORKFLOW,
        _CI_WORKFLOW,
        _ROOT / ".github" / "workflows" / "watch-daily.yml",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        assert "databricks" not in text, path
        assert "qwen" not in text, path
