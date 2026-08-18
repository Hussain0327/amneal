"""API boot fail-fast for the generation provider.

Mirrors the #247 embedding posture: generation is an unconditional capability
of the API deployment (every answer turn synthesizes), so an unset
LLM_PROVIDER or missing DATABRICKS_LLM_* credentials must refuse the boot in
the lifespan -- not surface lazily as an audited refusal on the first
question while /health reads green. Non-serving entrypoints (the corpus
worker's `authoritative-corpus-init-db`, plain `init-db`, the rest of the
CLI) never run the API lifespan and keep NO generation requirement.
"""

from __future__ import annotations

import config.settings as cs
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from regwatch.api.main import app


def _reload_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cs.get_settings.cache_clear()


def test_unset_llm_provider_refuses_api_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_settings(monkeypatch, LLM_PROVIDER="")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER is not set"), TestClient(app):
        pass


def test_databricks_llm_without_credentials_refuses_api_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # conftest already blanks BASE_URL/TOKEN; blank MODEL too so a developer's
    # host env can never leak a value in and green-light this boot.
    _reload_settings(
        monkeypatch,
        LLM_PROVIDER="databricks",
        DATABRICKS_LLM_BASE_URL="",
        DATABRICKS_LLM_TOKEN="",
        DATABRICKS_LLM_MODEL="",
    )
    with pytest.raises(RuntimeError, match="DATABRICKS_LLM_BASE_URL"), TestClient(app):
        pass


def test_configured_llm_provider_boots() -> None:
    # The check must be inert when generation is configured (conftest sets
    # LLM_PROVIDER=echo suite-wide); this is the positive control for the two
    # refusals above.
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_corpus_worker_entrypoint_needs_no_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docker/entrypoint.sh boots the Dagster worker via this CLI command.

    It must keep working with NO generation provider configured: the boot
    check is scoped to the API lifespan, and the corpus worker never
    generates. reset_for_tests forces init_db to run for real under this
    env rather than returning from the per-process memo.
    """
    from regwatch import cli
    from regwatch.store import db as db_module

    _reload_settings(monkeypatch, LLM_PROVIDER="")
    db_module.reset_for_tests()
    try:
        result = CliRunner().invoke(cli.app, ["authoritative-corpus-init-db"])
        assert result.exit_code == 0, result.output
    finally:
        db_module.reset_for_tests()
