"""OpenAI-only LLM factory contracts."""

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
    # Anthropic was removed 2026-08-17 and stays removed; a machine still
    # configured with it must refuse loudly, never fall back to another
    # provider. ("openai" was un-retired by the 2026-08-20 migration -- see
    # the two tests below.)
    _reload(monkeypatch, LLM_PROVIDER="anthropic")
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_llm_provider()


def test_openai_provider_constructs_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 2026-08-20 migration reinstated this path; a configured machine must
    # get a real OpenAI provider, not the retired-name ValueError.
    _reload(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test-not-a-real-key",
        OPENAI_LLM_MODEL="gpt-5.6-terra",
    )
    provider = get_llm_provider()
    assert provider.name == "openai"


def test_openai_requires_endpoint_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Missing credentials must name the exact env var, not fall back.
    _reload(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="",
        OPENAI_LLM_MODEL="",
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_llm_provider()


def test_databricks_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload(monkeypatch, LLM_PROVIDER="databricks")
    with pytest.raises(ValueError, match="unknown LLM provider: databricks"):
        get_llm_provider()


def test_echo_provider_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload(monkeypatch, LLM_PROVIDER="echo")
    assert get_llm_provider().name == "echo"


def test_current_model_name_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit stamping must not be the thing that fails a turn."""
    _reload(monkeypatch, LLM_PROVIDER="echo")
    assert current_model_name() == "echo"

    _reload(monkeypatch, LLM_PROVIDER="openai", OPENAI_LLM_MODEL="gpt-5.6-terra")
    assert current_model_name() == "gpt-5.6-terra"
    assert current_model_name(role="router") == "gpt-5.6-terra"

    _reload(monkeypatch, LLM_PROVIDER="databricks")
    assert current_model_name() == "unconfigured"

    _reload(monkeypatch, LLM_PROVIDER="")
    assert current_model_name() == "unconfigured"
