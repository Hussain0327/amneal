"""The production Watch cron must share serving's named OpenAI profile.

These tests pin the workflow contract that prevents a no-change cron from
looking healthy while a later FDA revision writes chunks outside the active
embedding profile.  Presence is checked before checkout; the registered
profile is validated before the crawl; coverage is checked after any attempted
ingest, including a failed one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "watch-daily.yml"

_PROFILE_ENV_TO_SECRET = {
    "RETRIEVAL_EMBEDDING_PROFILE": "WATCH_ACTIVE_EMBEDDING_PROFILE",
}

_VALID_PROFILE_ENV = {
    "RETRIEVAL_EMBEDDING_PROFILE": "ep_8caadbd608eca18b4d01086b0f793b39",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"]["watch"]["steps"]


def _step(name: str) -> dict:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found in watch-daily.yml")


def _run_profile_preflight(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), **_VALID_PROFILE_ENV, **overrides}
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _step("preflight Watch embedding profile")["run"]],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_watch_uses_openai_for_generation_and_embeddings() -> None:
    env = _workflow()["jobs"]["watch"]["env"]
    assert env["INGEST_EMBEDDING_PROVIDER"] == "openai"
    assert env["LLM_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "${{ secrets.OPENAI_API_KEY }}"
    assert env["OPENAI_LLM_MODEL"] == "gpt-5.6-terra"
    assert env["OPENAI_REASONING_EFFORT"] == "medium"
    assert env["OPENAI_EMBEDDING_MODEL"] == "text-embedding-3-large"
    assert env["OPENAI_EMBEDDING_DIMENSION"] == "1024"
    assert env["PROFILE_HNSW_INDEX_REQUIRED"] == "false"
    for env_name, secret_name in _PROFILE_ENV_TO_SECRET.items():
        assert env[env_name] == f"${{{{ secrets.{secret_name} }}}}"


def test_profile_preflight_is_mandatory_before_checkout_and_watch() -> None:
    steps = _steps()
    names = [step.get("name") for step in steps]
    preflight = _step("preflight Watch embedding profile")
    assert preflight["if"] == "env.DATABASE_URL != ''"
    assert names.index("preflight Watch embedding profile") < names.index("regwatch watch")
    assert names.index("preflight Watch embedding profile") < next(
        i for i, step in enumerate(steps) if "uses" in step
    )


def test_profile_preflight_accepts_a_complete_named_profile() -> None:
    result = _run_profile_preflight()
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize(
    ("env_name", "secret_name"),
    sorted(_PROFILE_ENV_TO_SECRET.items()),
)
@pytest.mark.parametrize("missing_value", ["", " \t"])
def test_profile_preflight_rejects_each_missing_value(
    env_name: str,
    secret_name: str,
    missing_value: str,
) -> None:
    result = _run_profile_preflight(**{env_name: missing_value})
    assert result.returncode != 0
    assert secret_name in result.stdout
    assert "No crawl or ingest was attempted" in result.stdout


@pytest.mark.parametrize("profile_id", ["legacy", "ep_not-a-profile", ""])
def test_profile_preflight_rejects_legacy_or_malformed_profile_ids(profile_id: str) -> None:
    result = _run_profile_preflight(RETRIEVAL_EMBEDDING_PROFILE=profile_id)
    assert result.returncode != 0
    assert "WATCH_ACTIVE_EMBEDDING_PROFILE" in result.stdout


def test_registered_profile_is_validated_before_ingest() -> None:
    names = [step.get("name") for step in _steps()]
    validation = _step("validate registered embedding profile")
    assert validation["if"] == "env.DATABASE_URL != ''"
    assert validation["run"] == "uv run regwatch init-db"
    assert names.index("validate registered embedding profile") < names.index("regwatch watch")


def test_coverage_is_checked_after_every_attempted_watch_run() -> None:
    steps = _steps()
    names = [step.get("name") for step in steps]
    watch = _step("regwatch watch")
    coverage = _step("verify embedding-profile coverage")
    assert watch["id"] == "watch"
    assert names.index("regwatch watch") < names.index("verify embedding-profile coverage")
    assert "always()" in coverage["if"]
    assert "steps.watch.outcome != 'skipped'" in coverage["if"]
    assert "init_db(assert_provider=False)" in coverage["run"]
    assert "pending_chunks" in coverage["run"]
    assert "sys.exit(1)" in coverage["run"]


def test_openai_key_preflight_covers_generation_and_embeddings() -> None:
    run = _step("preflight OpenAI config")["run"]

    assert "OPENAI_API_KEY" in run
    assert "exit 1" in run
