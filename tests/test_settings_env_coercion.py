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
    assert _settings(openai_embedding_dimension=blank).openai_embedding_dimension == 1024


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_model_falls_back_to_default(blank: str) -> None:
    """An empty string would otherwise silently replace a good default with ''.

    Compared against the unset default rather than a literal: the previous
    ``startswith("Qwen/")`` assertion pinned a HuggingFace repo id that no
    endpoint ever served, so retargeting the default at the deployed endpoint
    broke a test that was only ever meant to prove the fallback fires.
    """
    assert (
        _settings(openai_embedding_model=blank).openai_embedding_model == "text-embedding-3-large"
    )


def test_real_values_still_parse() -> None:
    """The fallback must not swallow a configured value. Prod runs 1024."""
    s = _settings(openai_embedding_dimension="2048", openai_embedding_model="custom/model")
    assert s.openai_embedding_dimension == 2048
    assert s.openai_embedding_model == "custom/model"


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
        _settings(openai_embedding_dimension="not-a-number")


def test_blank_reasoning_effort_is_still_meaningful() -> None:
    """The reason the fallback is opt-in per field.

    OpenAI settings treat blank workflow interpolation as unset and retain the
    selected medium default.
    """
    assert _settings(openai_reasoning_effort="").openai_reasoning_effort == "medium"
    assert _settings().openai_reasoning_effort == "medium"


def test_whole_provider_block_blank_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow shape end to end: every optional provider var set to ''."""
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_LLM_MODEL",
        "OPENAI_REASONING_EFFORT",
        "OPENAI_EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_DIMENSION",
    ):
        monkeypatch.setenv(name, "")
    s = _settings()
    assert s.openai_embedding_dimension == 1024
    assert s.openai_embedding_model == "text-embedding-3-large"
    assert s.openai_llm_model == "gpt-5.6-terra"
    assert s.openai_reasoning_effort == "medium"
    assert s.openai_api_key is None


def test_blank_artifact_credentials_preserve_workload_identity() -> None:
    s = _settings(
        fda_artifact_s3_access_key_id="",
        fda_artifact_s3_secret_access_key="   ",
        fda_artifact_s3_session_token="",
        fda_artifact_s3_endpoint_url="",
    )

    assert s.fda_artifact_s3_access_key_id is None
    assert s.fda_artifact_s3_secret_access_key is None
    assert s.fda_artifact_s3_session_token is None
    assert s.fda_artifact_s3_endpoint_url is None
