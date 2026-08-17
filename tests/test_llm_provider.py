"""get_llm_provider factory contract: explicit config or loud refusal.

The Databricks provider's own behavior lives in
tests/test_databricks_llm_provider.py; this module pins the factory rules
introduced when the OpenAI-API/Anthropic paths were removed (2026-08-17):
LLM_PROVIDER has no default, unknown names refuse, and the audit label helper
never raises.
"""

from __future__ import annotations

import pytest

from regwatch.generate.llm import current_model_name, get_llm_provider


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import config.settings as cs

    cs.get_settings.cache_clear()


def test_unset_provider_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload(monkeypatch, LLM_PROVIDER="")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER is not set"):
        get_llm_provider()


def test_retired_provider_names_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    # openai/anthropic were removed 2026-08-17; a machine still configured
    # with one must refuse loudly, never fall back to another provider.
    for name in ("openai", "anthropic"):
        _reload(monkeypatch, LLM_PROVIDER=name)
        with pytest.raises(ValueError, match="unknown LLM provider"):
            get_llm_provider()


def test_databricks_requires_endpoint_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload(
        monkeypatch,
        LLM_PROVIDER="databricks",
        DATABRICKS_LLM_BASE_URL="",
        DATABRICKS_LLM_TOKEN="",
        DATABRICKS_LLM_MODEL="",
    )
    with pytest.raises(RuntimeError, match="DATABRICKS_LLM"):
        get_llm_provider()


def test_echo_provider_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload(monkeypatch, LLM_PROVIDER="echo")
    assert get_llm_provider().name == "echo"


def test_current_model_name_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit stamping must not be the thing that fails a turn."""
    _reload(monkeypatch, LLM_PROVIDER="echo")
    assert current_model_name() == "echo"

    _reload(
        monkeypatch,
        LLM_PROVIDER="databricks",
        DATABRICKS_LLM_MODEL="workspace.default.regwatch",
    )
    assert current_model_name() == "workspace.default.regwatch"
    # Every role maps to the one served model on the Databricks path.
    assert current_model_name(role="router") == "workspace.default.regwatch"

    _reload(monkeypatch, LLM_PROVIDER="databricks", DATABRICKS_LLM_MODEL="")
    assert current_model_name() == "unconfigured"

    _reload(monkeypatch, LLM_PROVIDER="")
    assert current_model_name() == "unconfigured"
