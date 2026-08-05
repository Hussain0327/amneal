"""D1 residency guards on Settings (docs/DATABRICKS_ADOPTION_2026-07-28.md, PR A).

These are tripwires, not features. Each one exists because a specific silent
misconfiguration would leave the analyst question going to OpenAI while the
deployment looked migrated. They are inert until D1_ENFORCED is armed, except
the https check, which is unconditional.
"""

from __future__ import annotations

import pytest
from config.settings import Settings, d1_model_rejection
from pydantic import ValidationError


def _settings(**overrides: object) -> Settings:
    """Construct Settings from explicit kwargs only.

    ``_env_file=None`` keeps a developer's local .env out of the assertion:
    these tests are about the validators, not about this machine.
    """
    base: dict[str, object] = {"_env_file": None, "database_url": "postgresql://u@h/db"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------- https on the two endpoints that carry the question ----------


@pytest.mark.parametrize(
    "field",
    ["qwen_embedding_base_url", "databricks_llm_base_url"],
)
def test_provider_endpoint_rejects_plaintext_http(field: str) -> None:
    """A typo'd http:// would ship the question in cleartext, silently."""
    with pytest.raises(ValidationError, match="https://"):
        _settings(**{field: "http://workspace.example/serving-endpoints"})


@pytest.mark.parametrize(
    "field",
    ["qwen_embedding_base_url", "databricks_llm_base_url"],
)
def test_provider_endpoint_accepts_https_and_unset(field: str) -> None:
    assert getattr(_settings(**{field: "https://workspace.example/x"}), field) == (
        "https://workspace.example/x"
    )
    assert getattr(_settings(), field) is None


def test_provider_token_is_not_url_validated() -> None:
    """The https validator must cover the URLs only.

    Tokens and the shadow-profile id share the whitespace normalizer; applying
    a scheme check to them would refuse every valid credential.
    """
    s = _settings(
        qwen_embedding_token="dapi-not-a-url",
        databricks_llm_token="dapi-also-not-a-url",
        embedding_shadow_profile="ep_abc123",
    )
    assert s.qwen_embedding_token == "dapi-not-a-url"
    assert s.databricks_llm_token == "dapi-also-not-a-url"
    assert s.embedding_shadow_profile == "ep_abc123"


# ---------- the model default must not name a nonexistent endpoint ----------


def test_databricks_model_has_no_default() -> None:
    """A defaulted endpoint name 404s every synthesis into an audited refusal.

    The app would look alive while refusing every question, which is far worse
    than failing to construct the provider.
    """
    assert _settings().databricks_llm_model is None


# ---------- D1_ENFORCED ----------


def test_d1_disabled_allows_any_combination() -> None:
    """Unarmed, the tripwire must not constrain the current OpenAI deployment."""
    s = _settings(llm_provider="databricks", databricks_llm_model="databricks-claude-opus")
    assert s.d1_enforced is False


@pytest.mark.parametrize(
    ("llm_provider", "profile"),
    [
        ("databricks", "legacy"),  # generation moved, query embedding did not
        ("openai", "ep_abc123"),  # query embedding moved, generation did not
    ],
)
def test_d1_refuses_half_flip(llm_provider: str, profile: str) -> None:
    """Both halves or neither: a half-flip still sends the question to OpenAI."""
    with pytest.raises(ValidationError, match="must move together"):
        _settings(
            d1_enforced=True,
            llm_provider=llm_provider,
            active_embedding_profile=profile,
            databricks_llm_model="open-weight-endpoint",
            d1_allowed_llm_models=["open-weight-endpoint"],
        )


def test_d1_requires_a_declared_allowlist() -> None:
    """Fail closed: arming with no declared allowlist must refuse."""
    with pytest.raises(ValidationError, match="D1_ALLOWED_LLM_MODELS"):
        _settings(
            d1_enforced=True,
            llm_provider="databricks",
            active_embedding_profile="ep_abc123",
            databricks_llm_model="open-weight-endpoint",
        )


def test_d1_refuses_model_outside_the_allowlist() -> None:
    with pytest.raises(ValidationError, match="not in D1_ALLOWED_LLM_MODELS"):
        _settings(
            d1_enforced=True,
            llm_provider="databricks",
            active_embedding_profile="ep_abc123",
            databricks_llm_model="some-other-endpoint",
            d1_allowed_llm_models=["open-weight-endpoint"],
        )


@pytest.mark.parametrize(
    "model",
    ["databricks-gpt-5-nano", "databricks-claude-sonnet-5", "DATABRICKS-GEMINI-PRO"],
)
def test_d1_refuses_partner_families_even_when_allowlisted(model: str) -> None:
    """The allowlist is typed by a human; partner brands look native.

    These endpoints are byte-identical at the call site (llm.py passes `model`
    verbatim) but carry the partner's own retention terms — the exposure D1
    exists to remove.
    """
    with pytest.raises(ValidationError, match="partner-hosted"):
        _settings(
            d1_enforced=True,
            llm_provider="databricks",
            active_embedding_profile="ep_abc123",
            databricks_llm_model=model,
            d1_allowed_llm_models=[model],
        )


def test_d1_accepts_a_fully_flipped_open_weight_deployment() -> None:
    s = _settings(
        d1_enforced=True,
        llm_provider="databricks",
        active_embedding_profile="ep_abc123",
        databricks_llm_model="open-weight-endpoint",
        d1_allowed_llm_models=["open-weight-endpoint"],
    )
    assert s.d1_enforced is True
    assert s.databricks_llm_model == "open-weight-endpoint"


def test_d1_accepts_the_unflipped_openai_deployment() -> None:
    """Armed before the cutover: neither half moved, which is consistent."""
    s = _settings(d1_enforced=True, llm_provider="openai", active_embedding_profile="legacy")
    assert s.d1_enforced is True


# The live gpt-oss-120b deployment. DATABRICKS_LLM_MODEL is the Unity Catalog
# Model Service alias `workspace.default.regwatch`, whose routing was repointed
# from system.ai.gpt-oss-20b to system.ai.gpt-oss-120b on 2026-08-05. The alias
# is what the boot check inspects; the served id is what the endpoint reports
# per response, so the allowlist must carry BOTH.
_PROD_ALIAS = "workspace.default.regwatch"
_PROD_SERVED_120B = "gpt-oss-120b-080525"
_PROD_SERVED_20B = "gpt-oss-20b-080525"
_PROD_ALLOWLIST = [_PROD_ALIAS, _PROD_SERVED_120B, _PROD_SERVED_20B]


def test_d1_accepts_the_live_gpt_oss_120b_allowlist() -> None:
    """Arming D1 against the deployed config must not refuse to boot.

    Without this, the first `D1_ENFORCED=true` would be discovered to be a boot
    crash in production rather than in CI, on every machine at once.
    """
    s = _settings(
        d1_enforced=True,
        llm_provider="databricks",
        active_embedding_profile="ep_2e7368b354d911ea3a013c3125e276c2",
        databricks_llm_model=_PROD_ALIAS,
        d1_allowed_llm_models=_PROD_ALLOWLIST,
    )
    assert s.d1_enforced is True


@pytest.mark.parametrize("served", [_PROD_SERVED_120B, _PROD_SERVED_20B])
def test_d1_accepts_both_served_ids_so_an_alias_rollback_stays_safe(served: str) -> None:
    """The per-response check runs on the SERVED id, not the alias.

    Repointing the Model Service back to 20b is a no-deploy rollback, so the id
    it would then report has to already be allowlisted or the rollback would
    fail every turn while looking like an outage.
    """
    assert d1_model_rejection(served, _PROD_ALLOWLIST) is None


def test_the_raw_120b_endpoint_name_is_still_refused() -> None:
    """Pins WHY the alias is used instead of the endpoint name directly.

    `databricks-gpt-oss-120b` is open-weight and in-perimeter, but it collides
    with the `databricks-gpt` partner prefix, which is checked even for an
    allowlisted name. Switching DATABRICKS_LLM_MODEL to it would make arming D1
    impossible without amending the guard. If that prefix rule is ever narrowed
    to exempt the open-weight `-oss` family, this test is the one to revisit --
    deliberately, not by accident.
    """
    endpoint_name = "databricks-gpt-oss-120b"
    reason = d1_model_rejection(endpoint_name, [endpoint_name])
    assert reason is not None
    assert "partner-hosted" in reason
