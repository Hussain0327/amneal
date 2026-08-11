"""Blank environment values must not break Settings construction.

CI templating renders an unset variable as the EMPTY STRING rather than
omitting it, so a workflow that forwards optional provider config reaches
pydantic with ''. For a typed field that is a parse error raised while
`config.settings` is still being IMPORTED, which takes down every process that
imports it -- including conftest, so the whole suite dies with a message that
names neither the workflow nor the variable.

The fallback is opt-in per field, not blanket, because this class has a field
where "" is meaningful. These tests pin both halves of that.
"""

from __future__ import annotations

import pytest
from config.settings import Settings
from pydantic import ValidationError


def _settings(**overrides: object) -> Settings:
    """Construct Settings from explicit kwargs only, ignoring any local .env."""
    base: dict[str, object] = {"_env_file": None, "database_url": "postgresql://u@h/db"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_dimension_falls_back_to_default(blank: str) -> None:
    """The exact CI failure: '' is not a valid integer, and it crashed on import."""
    assert _settings(qwen_embedding_dimension=blank).qwen_embedding_dimension == 1536


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_model_falls_back_to_default(blank: str) -> None:
    """An empty string would otherwise silently replace a good default with ''.

    Compared against the unset default rather than a literal: the previous
    ``startswith("Qwen/")`` assertion pinned a HuggingFace repo id that no
    endpoint ever served, so retargeting the default at the deployed endpoint
    broke a test that was only ever meant to prove the fallback fires.
    """
    assert (
        _settings(qwen_embedding_model=blank).qwen_embedding_model
        == _settings().qwen_embedding_model
    )


def test_real_values_still_parse() -> None:
    """The fallback must not swallow a configured value. Prod runs 1024."""
    s = _settings(qwen_embedding_dimension="1024", qwen_embedding_model="custom/model")
    assert s.qwen_embedding_dimension == 1024
    assert s.qwen_embedding_model == "custom/model"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_route_shadow_settings_fall_back_to_dark_defaults(blank: str) -> None:
    s = _settings(
        REGWATCH_ROUTE_CALL=blank,
        REGWATCH_ROUTE_MAX_TOKENS=blank,
    )

    assert s.route_call_mode == "off"
    assert s.route_call_max_tokens == 1200


def test_route_shadow_settings_accept_staged_values_and_reject_typos() -> None:
    assert (
        _settings(REGWATCH_ROUTE_CALL="shadow", REGWATCH_ROUTE_MAX_TOKENS="1400").route_call_mode
        == "shadow"
    )
    assert _settings(REGWATCH_ROUTE_CALL="live").route_call_mode == "live"
    with pytest.raises(ValidationError):
        _settings(REGWATCH_ROUTE_CALL="enabled")
    with pytest.raises(ValidationError):
        _settings(REGWATCH_ROUTE_MAX_TOKENS="200")


def test_a_genuinely_invalid_dimension_still_fails() -> None:
    """Blank means 'unset'; garbage still means garbage."""
    with pytest.raises(ValidationError):
        _settings(qwen_embedding_dimension="not-a-number")


def test_blank_reasoning_effort_is_still_meaningful() -> None:
    """The reason the fallback is opt-in per field.

    DATABRICKS_REASONING_EFFORT documents "" as "send no parameter", for
    endpoints that reject the field. It must normalize to None, NOT fall back
    to the "low" default -- that would silently re-add a parameter an operator
    removed on purpose during an incident.
    """
    assert _settings(databricks_reasoning_effort="").databricks_reasoning_effort is None
    assert _settings().databricks_reasoning_effort == "low"


def test_whole_provider_block_blank_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow shape end to end: every optional provider var set to ''."""
    for name in (
        "QWEN_EMBEDDING_BASE_URL",
        "QWEN_EMBEDDING_TOKEN",
        "QWEN_EMBEDDING_MODEL",
        "QWEN_EMBEDDING_DIMENSION",
        "DATABRICKS_LLM_BASE_URL",
        "DATABRICKS_LLM_TOKEN",
        "DATABRICKS_LLM_MODEL",
        "DATABRICKS_SERVING_RUNTIME_VERSION",
    ):
        monkeypatch.setenv(name, "")
    s = _settings()
    assert s.qwen_embedding_dimension == 1536
    assert s.qwen_embedding_base_url is None
    assert s.databricks_llm_base_url is None
