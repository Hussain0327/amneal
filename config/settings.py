"""Application settings, sourced from environment via pydantic-settings.

Nothing is hard-coded in business logic. Anything that might change between
demos, environments, or experiments lives here.

Last updated: 2026-08-11. Where a default differs from what production runs,
the comment says so. The defaults here are local-dev defaults; production
values live in fly.toml and in Fly secrets.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# OpenAI dated-snapshot suffix: what follows "<alias>-" in the server-reported
# model name, e.g. "gpt-5.4-nano-2026-01-15" or legacy "gpt-4-0613". Digits and
# hyphens only: "gpt-5-nano-mini" is a DIFFERENT model, not a snapshot.
_SNAPSHOT_SUFFIX_RE = re.compile(r"\d[\d-]*")

# Default final-k after optional reranking. Used to detect whether RERANK_TOP_K
# was set explicitly (vs. the legacy RETRIEVAL_TOP_K) in effective_rerank_top_k.
_DEFAULT_RERANK_TOP_K = 8

# Hard ceiling on ONE synthesis call, independent of SYNTHESIZER_MAX_TOKENS.
# The truncation retry doubles the budget, so this is what stops an operator
# from making a single turn cost an unbounded number of tokens.
#
# It lives HERE, next to the setting it bounds, rather than in
# generate/grounded_qa.py where it used to sit. Splitting them let the
# validator accept anything up to 32768 while the runtime silently clamped to
# the ceiling AND silently disabled the retry (a budget at the ceiling has no
# larger retry to escalate to). The validator below now refuses that range
# outright, which it can only do by seeing this number.
#
# Sized so the retry is real at the configured default: 3000 * 2 == 6000, and
# the 20-claim schema worst case (20 x 400 chars x 4 cites = 3,817 output
# tokens by o200k_harmony, + 500 reasoning) fits under it.
SYNTH_MAX_TOKENS_CEILING = 6000

# Databricks serving-endpoint name prefixes for PARTNER-hosted model families.
# Databricks brands these alongside its open-weight endpoints and they are
# indistinguishable at the call site, but they carry the partner's retention
# terms, which is precisely the exposure D1 exists to remove. Matched
# case-insensitively as a prefix; see the _check_d1_enforcement validator.
_D1_PARTNER_MODEL_PREFIXES = ("databricks-gpt", "databricks-claude", "databricks-gemini")

# OpenAI-compatible reasoning budget levels accepted by the Databricks AI
# Gateway. Unset (None) means "do not send the parameter at all", which is the
# only way to keep an endpoint that does not understand it from 400-ing.
_REASONING_EFFORTS = ("low", "medium", "high")


def d1_model_rejection(model: str, allowed: Iterable[str]) -> str | None:
    """Why this model is outside the D1 perimeter, or None when it is eligible.

    Extracted so the BOOT check (on DATABRICKS_LLM_MODEL, below) and the
    RUNTIME check (on the model the endpoint reports it actually served, in
    generate/llm.py) can never drift into two different definitions of
    "in-perimeter". Returns a clause, not a sentence: each caller prefixes it
    with the name of the thing it inspected, since those differ (a Unity
    Catalog serving-endpoint alias vs. a served model id).
    """
    name = model.strip()
    if name not in {m.strip() for m in allowed if m.strip()}:
        return "is not in D1_ALLOWED_LLM_MODELS."
    # Checked even for an allowlisted name: an allowlist is typed by a human,
    # and these prefixes are exactly the ones that look native while carrying a
    # partner retention path.
    lowered = name.lower()
    for prefix in _D1_PARTNER_MODEL_PREFIXES:
        if lowered.startswith(prefix):
            return (
                f"names a partner-hosted model family ({prefix!r}); these "
                "carry their own retention terms regardless of allowlisting."
            )
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Providers ----------
    # DELIBERATELY UNSET. There is no safe implicit provider: the 2026-08-14
    # backfill outage was a worker booted without EMBEDDING_PROVIDER silently
    # falling back to a local 384-dim model and failing the write on all 295
    # fresh documents AFTER paying their fetch/parse/OCR cost. Every resolution
    # point now refuses loudly instead of guessing. Prod sets qwen3 (fly.toml
    # [env]); tests set echo (conftest). The same posture applies to
    # llm_provider: prod sets databricks (gpt-oss-120b), tests set echo.
    embedding_provider: str | None = None
    llm_provider: str | None = None
    # Private Qwen3 embedding endpoint. This is deliberately a separate
    # OpenAI-compatible client from the LLM client: pointing the shared OpenAI
    # base URL at Databricks would also misroute generation requests.
    qwen_embedding_base_url: str | None = None
    qwen_embedding_token: str | None = None
    # The SERVED endpoint, not a HuggingFace repo id: the deployed Model Service
    # is `workspace.default.regwatch-embed` (served entity
    # `qwen3-embedding-0-6b-112025`). The previous default named
    # Qwen/Qwen3-Embedding-4B, which was never deployed and which no endpoint
    # answers -- the same trap `databricks_llm_model` already documents below.
    # Prod sets this via env; the default exists so a misconfigured deployment
    # fails against a real name instead of an imaginary one.
    qwen_embedding_model: str = "qwen3-embedding-0-6b"
    # The 1536 default is a Matryoshka profile chosen so local dev matches the
    # legacy pgvector shape; it is not the model's default and it is not what
    # prod runs. The live prod profile is 1024-dim on the Databricks Qwen3
    # serving endpoint. Left at 1536 deliberately: this value is folded into the
    # embedding profile fingerprint, so changing it here would silently
    # invalidate a staged profile.
    qwen_embedding_dimension: int = 1536
    # Bulk-embed request shaping. The serving endpoint's per-request cap
    # behaves as a token budget, not an input count (measured 2026-08-13:
    # ~140-token inputs passed at 24 per request, 429'd at 32), so the token
    # budget is the primary guard and batch size is a maximum-item backstop.
    # Defaults mirror QWEN3_DEFAULT_* in regwatch.process.embedder; a test
    # pins the two together.
    qwen_embedding_batch_size: int = 8
    qwen_embedding_request_token_budget: int = 3000
    qwen_embedding_query_instruction: str = (
        "Given a pharmaceutical regulatory question, retrieve FDA "
        "product-specific guidance passages containing the evidence needed to answer it."
    )
    qwen_embedding_query_instruction_version: str = "regwatch-regulatory-retrieval-v1"
    qwen_embedding_revision: str = "5cf2132abc99cad020ac570b19d031efec650f2b"
    # Process-wide LRU over embed_query results (process/embedder.py): repeated
    # canonical Ask queries skip the serial pre-synthesis embedding round-trip
    # (issue #221). Bounded at 256 entries (~2 MB); successes only, errors are
    # never cached. Aliased so an emergency off-flip reads as a REGWATCH_* Fly
    # secret like the flags below.
    query_embedding_cache_enabled: bool = Field(
        default=True, validation_alias="REGWATCH_QUERY_EMBED_CACHE"
    )
    # This is what picks the embedder on the query path. "legacy" keeps the
    # chunk.embedding column and is the only arm that reads embedding_provider.
    # A non-legacy profile must already have complete coverage and a compatible
    # index before it can be selected. The optional shadow profile is
    # dual-written/backfilled but never serves user retrieval until explicitly
    # promoted. Prod promoted a Databricks Qwen3 profile on 2026-07-30, so
    # "legacy" below is a local-dev default only.
    active_embedding_profile: str = "legacy"
    embedding_shadow_profile: str | None = None
    # Serving normally requires the active profile's deterministic HNSW index
    # (assert_profile_ready_for_activation, checked at boot and before every
    # embedding write). pgvector's exact scan returns identical results
    # without the index -- only slower -- while the index roughly doubles
    # vector storage. On a logical-size-capped database (Lakebase free tier:
    # 512 MB) that doubling is decisive, so an operator may waive the index
    # requirement explicitly. Coverage completeness is never waived.
    profile_hnsw_index_required: bool = True

    # Private Databricks Chat Completions endpoint. Prod points this at the
    # Unity Catalog alias workspace.default.regwatch, which serves gpt-oss-120b
    # (served id gpt-oss-120b-080525) for every role. On endpoints where
    # thinking is a runtime mode rather than a different checkpoint, the
    # provider allows it only for the synthesizer role and strips reasoning
    # from outputs.
    databricks_llm_base_url: str | None = None
    databricks_llm_token: str | None = None
    # No default: this is a Databricks SERVING ENDPOINT NAME, which only the
    # operator who deployed the endpoint knows. A wrong default would 404 every
    # synthesis, and llm.py's provider-error boundary would turn each 404 into
    # an audited refusal, so the app would look alive while refusing every
    # question. Unset instead, so get_llm_provider's `missing` check fails the
    # turn loudly.
    databricks_llm_model: str | None = None
    databricks_thinking_enabled: bool = False
    # Bound what a reasoning model spends THINKING before it answers.
    # Open-weight reasoning models (gpt-oss-120b) draw thought and answer from
    # the SAME max_tokens budget, so an unbounded effort level burns the whole
    # synthesis budget on reasoning, returns finish_reason="length", and llm.py
    # raises -- which grounded_qa degrades into an audited refusal.
    #
    # HISTORICAL MEASUREMENT: taken on the gpt-oss-20b endpoint at a 900-token
    # cap, before the alias was repointed to gpt-oss-120b on 2026-08-05. There,
    # "low" finished (272 completion tokens, visible answer) and
    # default/medium/high all hit the cap and raised. It has NOT been
    # re-measured on 120b, so "low" is the default on the strength of that
    # older measurement, not a fresh one. Unset ("") sends no parameter, for
    # endpoints that reject it.
    databricks_reasoning_effort: str | None = "low"
    # v6 prose synthesis (slm-layer Phase A). False = the v5 claims-JSON
    # synthesis contract, byte-identical to before the flag existed. True =
    # prose + [n] markers parsed server-side (generate/prose_turn.py) and
    # admitted by the same gate. v5 and v6 share one policy, cite or refuse;
    # only the model-facing FORMAT flips. The policy change is the v7 flag
    # below. Aliased so the prod flip reads as a REGWATCH_* Fly secret, like
    # REGWATCH_ALLOW_TEST_PROVIDERS. ON in prod.
    prose_synthesis_enabled: bool = Field(
        default=False, validation_alias="REGWATCH_PROSE_SYNTHESIS"
    )
    # Live provisional draft streaming over SSE (owner-amended INV-1,
    # 2026-08-10). Dark by default; effective only when prose synthesis is
    # also on AND the request opts in. Alias so the prod flip is a REGWATCH_*
    # Fly secret like the prose flag. ON in prod.
    live_draft_enabled: bool = Field(default=False, validation_alias="REGWATCH_LIVE_DRAFT")
    # Conversational route-call rollout. ``off``/``shadow`` are unchanged from
    # PR11b: no call, or observe-only. PR12 gave ``live`` a real meaning --
    # generate/grounded_qa.py's no-product branch may carry a session product
    # over on the route's compiled scope + standalone rewrite instead of the
    # word-list heuristic, guarded the same way (no suggestion/brand
    # candidate) and failing open to the heuristic on any route failure. A
    # live-classified corpus scope still only compiles and audits
    # (retrieve/scope.py, unchanged); it does not execute (see
    # grounded_qa._compile_route_live_scope). Default stays off.
    route_call_mode: Literal["off", "shadow", "live"] = Field(
        default="off", validation_alias="REGWATCH_ROUTE_CALL"
    )
    # Sized above the recorded ~761-token reasoning floor plus the small JSON
    # body. Operators must still probe the actually served endpoint before
    # enabling shadow; this remains configurable without a deploy.
    route_call_max_tokens: int = Field(
        default=1200,
        ge=256,
        le=SYNTH_MAX_TOKENS_CEILING,
        validation_alias="REGWATCH_ROUTE_MAX_TOKENS",
    )
    # v7 selective citation (slm-layer Phase B). This is the live answer
    # policy: cite the facts, talk like a person. False = v6's older
    # cite-or-refuse policy. True = SOURCE_FACT must carry its passage
    # number(s), REASONING is framed and uncited, CONVERSATION is plain, and
    # "found nothing" is written as ordinary prose with no code word. It is a
    # POLICY change on top of v6's FORMAT change. INV-1 is unchanged and still
    # enforced in code either way: an uncited source fact is still dropped.
    # Only honored when prose_synthesis_enabled is also true (v7 is a prose
    # prompt). Aliased so the flip reads as a REGWATCH_* Fly secret. ON in
    # prod.
    selective_citation_enabled: bool = Field(
        default=False, validation_alias="REGWATCH_SELECTIVE_CITATION"
    )
    # Output cap for the SYNTHESIZER role only (see generate/grounded_qa.py). A
    # setting rather than a constant because a reasoning model needs headroom
    # the gpt-5.4-nano tuning never did, and an operator must be able to give it
    # that during an incident without a deploy.
    #
    # Raised 900 -> 1600 when synthesis moved to a JSON claims envelope. The
    # envelope spends tokens on structure (per-claim objects with named cite
    # objects) that prose spent on content, and a truncated JSON payload is
    # UNPARSEABLE where truncated prose merely lost a sentence -- the failure
    # got sharper, so the budget has to get bigger.
    #
    # Raised 1600 -> 3000, and this one IS measured. Sized off the SCHEMA
    # CEILING, never off observed p95: because a truncated payload is
    # unparseable, undershooting costs the WHOLE TURN (an audited
    # malformed_structure refusal), not a lost sentence. Budgeting for the
    # median would be budgeting for the failure.
    #
    # Arithmetic, o200k_harmony (the gpt-oss-120b tokenizer), pretty-printed
    # because the worked example the model imitates is pretty-printed:
    #     20 claims x 250 chars x 2 cites = 2,317 output tokens
    #   + reasoning residual                =   500 (observed 13-485, mean 144)
    #                                        -------
    #                                         2,817  -> 3000, leaving ~180 for
    # JSON \uXXXX escaping, which is unbounded per character.
    #
    # Note 1600 never covered even the OLD 10-claim cap: 10 x 400 x 4 = 1,927
    # + 500 = 2,427. The raise is justified independently of any cap change.
    #
    # Must stay strictly below SYNTH_MAX_TOKENS_CEILING (enforced at boot by
    # _check_synthesizer_max_tokens) or the truncation retry stops working.
    synthesizer_max_tokens: int = 3000
    # ---------- LLM client transport (B3) ----------
    # The OpenAI-compatible SDK (the Databricks transport) defaults to a 600s
    # read timeout with 2 retries, so a stalled provider would pin a sync-route
    # worker for 10-20 min. Bound it.
    # The embedder owns its own retry loop, so it constructs the shared client
    # with max_retries=0 to avoid stacking SDK retries on top of that loop.
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # Test-grade `echo` providers against a real (non-empty) corpus are an
    # invisible quality degradation; the API refuses to boot unless this is set.
    allow_test_providers: bool = Field(
        default=False, validation_alias="REGWATCH_ALLOW_TEST_PROVIDERS"
    )

    @field_validator(
        "embedding_provider",
        "llm_provider",
        "qwen_embedding_base_url",
        "qwen_embedding_token",
        "databricks_llm_base_url",
        "databricks_llm_token",
        "databricks_reasoning_effort",
        "embedding_shadow_profile",
        mode="before",
    )
    @classmethod
    def _normalize_optional_provider_value(cls, v: object) -> str | None:
        """Whitespace-only endpoint settings are equivalent to unset."""
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator(
        "qwen_embedding_model",
        "qwen_embedding_dimension",
        # The prose flag's rollback story is "unset the Fly secret"; a secret
        # set to the empty string must read as OFF, not take the app down at
        # boot with a bool_parsing error. Same story for the v7 flag it gates.
        "prose_synthesis_enabled",
        "route_call_mode",
        "route_call_max_tokens",
        "selective_citation_enabled",
        mode="before",
    )
    @classmethod
    def _blank_env_falls_back_to_default(cls, v: object, info: ValidationInfo) -> object:
        """An env var set to "" means "not configured", not "override with empty".

        CI templating renders an unset value as the EMPTY STRING rather than
        omitting the variable, so ``QWEN_EMBEDDING_DIMENSION: ${{ vars.X }}``
        with no variable configured arrives here as ''. Without this, that
        fails int parsing at IMPORT time and takes down every process that
        imports settings -- including the whole test suite, whose failure
        message says nothing about the workflow that caused it. The same shape
        appears in any deploy template that interpolates optional config.

        Deliberately opt-in per field rather than a blanket rule: this class
        has fields where "" is MEANINGFUL, notably
        ``databricks_reasoning_effort``, where blank documents "send no
        parameter" and must stay distinct from its "low" default.
        """
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[str(info.field_name)].default
        return v

    @field_validator("qwen_embedding_base_url", "databricks_llm_base_url")
    @classmethod
    def _require_https_endpoint(cls, v: str | None) -> str | None:
        """Private inference endpoints must be TLS.

        These two URLs carry the confidential analyst QUESTION off the box:
        the query embedding and the synthesis prompt. A typo'd ``http://``
        would ship it in plaintext and nothing downstream would notice: the
        OpenAI-compatible client honors whatever scheme it is given. Refusing
        at Settings construction makes the mistake a boot failure instead of a
        silent, per-request disclosure.
        """
        if v is None:
            return None
        if not v.lower().startswith("https://"):
            raise ValueError(
                "provider endpoint URLs must use https:// - these carry the "
                f"analyst question off-host; got {v.split(':', 1)[0]}://"
            )
        return v

    @field_validator("qwen_embedding_dimension")
    @classmethod
    def _check_qwen_embedding_dimension(cls, v: int) -> int:
        if not 32 <= v <= 2560:
            raise ValueError("QWEN_EMBEDDING_DIMENSION must be in [32, 2560]")
        return v

    @field_validator("qwen_embedding_batch_size")
    @classmethod
    def _check_qwen_embedding_batch_size(cls, v: int) -> int:
        if not 1 <= v <= 512:
            raise ValueError("QWEN_EMBEDDING_BATCH_SIZE must be in [1, 512]")
        return v

    @field_validator("qwen_embedding_request_token_budget")
    @classmethod
    def _check_qwen_embedding_request_token_budget(cls, v: int) -> int:
        if not 1 <= v <= 65536:
            raise ValueError("QWEN_EMBEDDING_REQUEST_TOKEN_BUDGET must be in [1, 65536]")
        return v

    @field_validator("databricks_reasoning_effort")
    @classmethod
    def _check_databricks_reasoning_effort(cls, v: str | None) -> str | None:
        """Reject a typo at boot rather than 400-ing every synthesis at runtime."""
        if v is None:
            return None
        effort = v.strip().lower()
        if effort not in _REASONING_EFFORTS:
            raise ValueError(
                "DATABRICKS_REASONING_EFFORT must be one of "
                f"{', '.join(_REASONING_EFFORTS)} (or empty to send no parameter)"
            )
        return effort

    @field_validator("synthesizer_max_tokens")
    @classmethod
    def _check_synthesizer_max_tokens(cls, v: int) -> int:
        # A zero/negative cap would truncate every completion to nothing and
        # degrade every turn to the empty_completion refusal -- the app would
        # look alive while answering nothing.
        #
        # The upper bound is the synthesis ceiling, EXCLUSIVE, not an arbitrary
        # 32768. At or above it two things break silently and together: the
        # first call is clamped down to the ceiling with no log, and the
        # truncation retry (min(capped * 2, CEILING)) can no longer produce a
        # larger budget, so a truncated turn raises instead of retrying. Both
        # are invisible in prod. Fail the boot instead.
        if not 1 <= v < SYNTH_MAX_TOKENS_CEILING:
            raise ValueError(
                f"SYNTHESIZER_MAX_TOKENS must be in [1, {SYNTH_MAX_TOKENS_CEILING - 1}]: "
                f"a value at or above the synthesis ceiling ({SYNTH_MAX_TOKENS_CEILING}) "
                "is silently clamped AND silently disables the truncation retry"
            )
        return v

    # ---------- D1 residency tripwires ----------
    # D1 itself is CLOSED: generation, query embedding and the database all sit
    # inside the company's own Databricks tenant. This is the guardrail that
    # keeps it closed, and it stays inert until an operator arms D1_ENFORCED.
    #
    # It exists because, once LLM_PROVIDER=databricks, the two ways to silently
    # break the residency claim are both a one-string edit away and neither
    # fails on its own:
    #
    #   1. Pointing DATABRICKS_LLM_MODEL at a partner-brand serving endpoint.
    #      Byte-identical at the call site (llm.py passes `model` verbatim), but
    #      partner-hosted models carry their own documented retention regimes,
    #      so the question reaches the provider D1 exists to keep it away from.
    #   2. A half-flip: generation on Databricks while retrieval still embeds
    #      the query on OpenAI, or the inverse. The question still leaves to
    #      OpenAI, while a status page would read "migrated".
    #
    # Fail-closed by construction: arming with no declared allowlist refuses,
    # rather than trusting whatever endpoint name happens to be configured.
    d1_enforced: bool = False
    # Deliberately EMPTY by default. No model name is hard-coded here: the
    # operator declares which serving endpoints they have verified as
    # open-weight and in-perimeter, which makes the claim auditable instead of
    # inherited from a guess in this file.
    #
    # This list is checked TWICE against two different strings, so it must
    # contain both: the configured DATABRICKS_LLM_MODEL (checked at boot below)
    # and the model id the endpoint reports in its responses (checked per
    # response in generate/llm.py). Those differ whenever DATABRICKS_LLM_MODEL
    # names a Unity Catalog alias, which is repointable with no deploy -- the
    # exact move the boot check structurally cannot see.
    d1_allowed_llm_models: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_d1_enforcement(self) -> Settings:
        if not self.d1_enforced:
            return self
        provider = (self.llm_provider or "").strip().lower()
        on_databricks_llm = provider == "databricks"
        # "legacy" is the unversioned OpenAI vector space; any other value is a
        # registered profile, which selects its own (private) query embedder.
        on_profile_embedding = (self.active_embedding_profile or "").strip().lower() != "legacy"
        if on_databricks_llm != on_profile_embedding:
            raise ValueError(
                "D1_ENFORCED: generation and query embedding must move together. "
                f"LLM_PROVIDER={provider!r} with "
                f"ACTIVE_EMBEDDING_PROFILE={self.active_embedding_profile!r} leaves "
                "one half of every question going to OpenAI."
            )
        if not on_databricks_llm:
            return self
        model = (self.databricks_llm_model or "").strip()
        allowed = {m.strip() for m in self.d1_allowed_llm_models if m.strip()}
        if not allowed:
            raise ValueError(
                "D1_ENFORCED requires D1_ALLOWED_LLM_MODELS to list the serving "
                "endpoints verified as open-weight and in-perimeter."
            )
        # Boot-time half of the perimeter check. It can only ever see the
        # CONFIGURED name, which for a Unity Catalog alias says nothing about
        # what is actually serving -- generate/llm.py runs the same predicate
        # against the model the endpoint reports on each response.
        why = d1_model_rejection(model, allowed)
        if why is not None:
            raise ValueError(f"D1_ENFORCED: DATABRICKS_LLM_MODEL={model!r} {why}")
        return self

    # ---------- LLM pricing (H3) ----------
    # USD per 1M tokens, keyed by model name. Env-overridable as JSON, e.g.
    #   LLM_MODEL_PRICES='{"gpt-oss-120b-080525": {"input": 0.15, "output": 0.60}}'
    # EMPTY by default: the served model is a Databricks endpoint whose rate
    # only the operator knows, and a guessed price is worse than none. An
    # unknown model yields cost_usd NULL in the audit log, never a guess.
    llm_model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)

    def price_for_model(self, model: str) -> dict[str, float] | None:
        """Per-1M-token prices for a model, or None when unknown (cost stays NULL).

        Exact table match first. The OpenAI Responses path reports the RESOLVED
        dated snapshot id (e.g. ``gpt-5.4-nano-2026-01-15``) rather than the
        configured alias, so a miss falls back to the longest table key that is
        a dated-snapshot prefix of the reported name. Genuinely unknown model
        families (including non-snapshot suffixes like ``-mini``) stay None,
        never a guessed price.
        """
        entry = self.llm_model_prices.get(model)
        if entry is None:
            snapshot_keys = [
                key
                for key in self.llm_model_prices
                if model.startswith(f"{key}-")
                and _SNAPSHOT_SUFFIX_RE.fullmatch(model[len(key) + 1 :])
            ]
            if snapshot_keys:
                entry = self.llm_model_prices[max(snapshot_keys, key=len)]
        if entry is None:
            return None
        if "input" not in entry or "output" not in entry:
            return None
        return entry

    # ---------- Observability (H1) ----------
    # Sentry is OFF unless SENTRY_DSN is set: zero behavior change otherwise.
    # No question text ever goes to Sentry: query_text lives in our own audit
    # log (query_log), and request bodies are never attached to events.
    sentry_dsn: str | None = None
    sentry_environment: str = "dev"

    @field_validator("sentry_dsn", mode="before")
    @classmethod
    def _normalize_sentry_dsn(cls, v: object) -> str | None:
        """Empty/whitespace SENTRY_DSN means OFF, same as unset."""
        if v is None:
            return None
        dsn = str(v).strip()
        return dsn or None

    # ---------- Company ----------
    company_name: str = "Amneal"
    company_applicant_aliases: str = "AMNEAL PHARMS,AMNEAL PHARMACEUTICALS,AMNEAL PHARMS LLC"

    @property
    def applicant_aliases(self) -> list[str]:
        return [s.strip().upper() for s in self.company_applicant_aliases.split(",") if s.strip()]

    # ---------- Retrieval / refusal ----------
    # Corpus cutover is a reversible configuration change. ``legacy`` keeps
    # the serving path on the pre-0021 PSG index while the new FDA corpus is
    # built and verified. ``authoritative_fda`` admits only chunks carrying
    # one of the five policy-approved source families; API boot additionally
    # refuses that mode unless a successful full-universe sync has 100%
    # embedding coverage.
    retrieval_corpus: Literal["legacy", "authoritative_fda"] = Field(
        default="legacy", validation_alias="REGWATCH_RETRIEVAL_CORPUS"
    )
    # Two-stage retrieval (per spec diagram):
    #   stage 1: vector search returns VECTOR_TOP_K candidates (wide net)
    #   stage 2: rerank to RERANK_TOP_K (the set we actually cite from)
    # When the reranker is off, stage 2 is the identity: we just take the
    # first RERANK_TOP_K of the wide net. This keeps the diagram and the
    # config in agreement at all times.
    vector_top_k: int = 50
    rerank_top_k: int = _DEFAULT_RERANK_TOP_K
    # Phase-2 cross-encoder rerank. Off by default: when false, stage 2 is the
    # identity (first rerank_top_k of the wide net). Read via Settings (not a
    # bare os.getenv) so the knob is documented and validated like every other.
    reranker_enabled: bool = False
    # MMR diversity in stage 2 (docs/DSA.md section 33). OFF by default: when
    # false the trim is the unchanged first-RERANK_TOP_K slice, so production
    # stays bit-identical until an eval A/B flips it. When true the same NUMBER
    # of passages is kept, but a candidate that repeats what is already
    # selected loses to a distinct one -- eight near-clones of one paragraph
    # are one piece of evidence, not eight. Similarity is text-based
    # (retrieve/diversity.py), so the flip costs no extra embedding or query.
    # Aliased so the flip reads as a REGWATCH_* Fly secret like the flags above.
    mmr_diversity_enabled: bool = Field(default=False, validation_alias="REGWATCH_MMR_DIVERSITY")
    # Legacy alias, populated from RETRIEVAL_TOP_K if set (backwards compat).
    retrieval_top_k: int | None = None
    # Cosine floor: passages below it are withheld from the synthesizer, so a
    # turn with nothing above it declines before any model call.
    # 0.30 was validated against the OLD OpenAI vector space. The live space is
    # Qwen3 1024-dim, so that validation does not carry over and 0.30 is
    # unvalidated in production today.
    refusal_score_threshold: float = 0.30
    # TTL for the in-process distinct-metadata cache (the resolver's "which
    # drugs exist" set). Bounds how long the long-lived API process can serve a
    # stale set after a SEPARATE ingest process adds a drug. 0 disables the TTL
    # (cache only invalidated by same-process writes / restart).
    metadata_cache_ttl_s: float = 60.0

    @property
    def effective_rerank_top_k(self) -> int:
        """Final-k after optional reranking.

        Prefers an explicitly-set RERANK_TOP_K (the current name). The legacy
        RETRIEVAL_TOP_K is honored ONLY when RERANK_TOP_K is still at its
        default, so a stale legacy var lingering in the environment can no
        longer silently override an explicit new RERANK_TOP_K.
        """
        if self.rerank_top_k != _DEFAULT_RERANK_TOP_K:
            return self.rerank_top_k
        if self.retrieval_top_k is not None:
            return self.retrieval_top_k
        return self.rerank_top_k

    @field_validator("refusal_score_threshold")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("REFUSAL_SCORE_THRESHOLD must be in [0, 1]")
        return v

    # ---------- Storage ----------
    # DATABASE_URL names the one and only datastore: Postgres + pgvector.
    # Production is Databricks Lakebase; tests use a disposable local Postgres.
    # Postgres-only since R5, so the SQLite/Chroma dual-mode is gone. The field
    # stays optional at the pydantic layer so tooling can construct Settings
    # without a DB, but store/db.py refuses to build an engine when it is empty
    # (the B1 fail-loud posture, now unconditional).
    database_url: str | None = None

    # Postgres connection-level timeouts. The app connects as an ordinary role
    # that ships with NO server-side statement/lock/idle timeouts. Without them
    # a connection that stalls mid-transaction holds its locks forever: on
    # 2026-06-18 an idle-in-transaction chunk read blocked the boot-time
    # `ALTER TABLE chunk ENABLE RLS` and wedged prod. These are applied
    # per-connection via libpq `options` in store/db.py:get_engine(). Each takes
    # a GUC duration string ('30s', '500ms'); set to '0' or '' to disable one.
    # idle_in_transaction + lock timeouts are the load-bearing fix and are safe
    # for bulk work (idle-in-tx never fires on an actively-running statement);
    # a one-off bulk migration that needs a long single statement can relax
    # DB_STATEMENT_TIMEOUT via env.
    db_statement_timeout: str = "30s"
    db_idle_in_tx_timeout: str = "60s"
    db_lock_timeout: str = "10s"
    # Bound the TCP/TLS connection handshake itself (libpq `connect_timeout`,
    # integer seconds). statement_timeout only bounds a query AFTER the session
    # exists, and pool_pre_ping opens a fresh connection on checkout, so without
    # this a stalled handshake to the remote Postgres endpoint hangs a request
    # thread forever (store-1). Integer seconds; '0' or '' disables the bound.
    db_connect_timeout: str = "10"
    # Recycle pooled connections before the server's own idle cutoff so a stale
    # server-side socket is never handed to a request (pairs with pool_pre_ping).
    db_pool_recycle_s: int = 1800

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, v: object) -> str | None:
        """Normalize DATABASE_URL to the SQLAlchemy psycopg v3 driver form.

        - empty/whitespace -> None (store/db.py refuses to build an engine)
        - 'postgres://' (Heroku/Supabase shorthand) -> 'postgresql://'
        - bare 'postgresql://' -> 'postgresql+psycopg://' (psycopg v3)
        - 'postgresql+psycopg://' passes through unchanged
        """
        if v is None:
            return None
        url = str(v).strip()
        if not url:
            return None
        # Match the scheme case-insensitively (a 'POSTGRES://' would otherwise
        # slip through unrewritten and fail SQLAlchemy's dialect lookup), but
        # leave the credentials/host portion untouched.
        scheme, sep, rest = url.partition("://")
        if sep and scheme.lower() in ("postgres", "postgresql"):
            return f"postgresql+psycopg://{rest}"
        return url

    data_dir: Path = Path("./data")
    raw_pdf_dir: Path = Path("./data/raw")
    processed_dir: Path = Path("./data/processed")

    # ---------- Crawler ----------
    user_agent: str = "RegWatch/0.1 (clinical-regulatory-affairs; +https://example.invalid/contact)"
    http_timeout_s: float = 30.0
    # The authoritative corpus worker uses this bounded concurrency after
    # schema initialization. Request starts remain host-paced by
    # crawl_min_interval_ms, so concurrency overlaps download/parse/database
    # time without turning into an unbounded FDA request fan-out.
    crawl_concurrency: int = 4
    crawl_min_interval_ms: int = 250
    # Directory for the HOST-GLOBAL request-start limiter. Unset (the default)
    # keeps pacing in-process, which is correct for a single worker process.
    # Set it (one shared path per host) whenever MORE THAN ONE process crawls
    # concurrently -- e.g. Dagster max_concurrent_runs > 1 -- because the
    # in-process lock cannot see sibling runs: N runs each pacing themselves to
    # crawl_min_interval_ms multiplies polite-crawl pressure on FDA by N. With
    # this set, every process start-paces through one flock-serialized
    # timestamp per FDA host, so the interval is a true host-wide budget.
    crawl_pace_dir: Path | None = None
    # In-process cache-aside TTLs for the two official FDA data snapshots.
    # Drugs@FDA is refreshed on business days; Orange Book is monthly.  A
    # day-long cache keeps one process on one auditable snapshot while still
    # refreshing on the next scheduled run. Set either to 0 to disable.
    drugsfda_cache_ttl_s: float = 86_400.0
    orange_book_cache_ttl_s: float = 86_400.0
    # Compressed snapshot byte caps. The parsers separately bound member count
    # and total uncompressed bytes to defend against ZIP bombs.
    drugsfda_zip_max_bytes: int = 128 * 1024 * 1024
    orange_book_zip_max_bytes: int = 32 * 1024 * 1024
    # Scientific/action-package PDFs can be much larger than PSGs.  These
    # source-worker limits are still finite and are enforced while streaming
    # and before page extraction; the API never parses untrusted PDFs.
    fda_corpus_pdf_max_bytes: int = 200 * 1024 * 1024
    fda_corpus_pdf_max_pages: int = 3_000
    fda_corpus_pdf_parse_timeout_s: float = 180.0
    # Corpus-sync blast-radius guards (2026-08-14 postmortem: 295 documents
    # paid fetch/parse/OCR and then all failed the SAME downstream embedding
    # write). The canary rule aborts a run whose first N processed documents
    # ALL failed -- the signature of systemic misconfiguration, not of one bad
    # document. The consecutive rule catches a systemic failure that starts
    # mid-run. Isolated per-document failures trip neither guard, and an
    # aborted run stays resumable (per-document commits are durable). 0
    # disables a guard.
    fda_corpus_canary_documents: int = Field(default=5, ge=0)
    fda_corpus_max_consecutive_failures: int = Field(default=10, ge=0)
    # A source record can become a formally handled terminal outcome only
    # after this many durable observations of the same missing URL or parser
    # version. Dagster currently permits the initial attempt plus three
    # retries, so four preserves the full retry budget before terminalization.
    fda_corpus_terminal_attempts: int = Field(default=4, ge=2, le=20)
    # Corpus documents are always staged one-at-a-time under this directory and
    # unlinked in a finally block. None delegates placement to the operating
    # system temp directory. The full corpus must never accumulate below
    # DATA_DIR on an ephemeral or capacity-constrained worker disk.
    fda_corpus_temp_dir: Path | None = None
    # Raw FDA artifacts are pluggable. ``discard`` preserves only the checksum
    # and source URL after successful processing; ``filesystem`` is suitable
    # for local development; ``s3`` is the production-grade durable option.
    fda_artifact_store: Literal["discard", "filesystem", "s3"] = "discard"
    fda_artifact_dir: Path = Path("./data/fda_corpus/artifacts")
    fda_artifact_s3_bucket: str | None = None
    fda_artifact_s3_prefix: str = "regwatch/fda-corpus"
    fda_artifact_s3_endpoint_url: str | None = None
    fda_artifact_s3_region: str | None = None
    fda_artifact_s3_access_key_id: str | None = None
    fda_artifact_s3_secret_access_key: str | None = None
    fda_artifact_s3_session_token: str | None = None
    fda_artifact_s3_sse: Literal["AES256", "aws:kms"] | None = "AES256"
    fda_artifact_s3_kms_key_id: str | None = None

    @field_validator(
        "fda_artifact_s3_bucket",
        "fda_artifact_s3_endpoint_url",
        "fda_artifact_s3_region",
        "fda_artifact_s3_access_key_id",
        "fda_artifact_s3_secret_access_key",
        "fda_artifact_s3_session_token",
        "fda_artifact_s3_kms_key_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_artifact_setting(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    # OCR runs only in the killable PDF-parser child. It invokes the reviewed
    # tesseract executable without a shell, bounds rendered pixels, page count,
    # per-page runtime, and the enclosing document parse wall clock.
    fda_corpus_ocr_enabled: bool = True
    fda_corpus_ocr_binary: str = "tesseract"
    fda_corpus_ocr_language: str = "eng"
    fda_corpus_ocr_dpi: int = Field(default=200, ge=72, le=400)
    fda_corpus_ocr_page_timeout_s: float = Field(default=60.0, gt=0, le=300)
    fda_corpus_ocr_max_pages: int = Field(default=500, ge=1, le=3_000)
    fda_corpus_ocr_max_pixels: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    fda_corpus_ocr_max_output_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=64 * 1024,
        le=100 * 1024 * 1024,
    )
    fda_corpus_ocr_memory_limit_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=256 * 1024 * 1024,
        le=8 * 1024 * 1024 * 1024,
    )

    # ---------- PDF ingest safety (cron/ingest worker only) ----------
    # The daily `regwatch watch` run is the SOLE driver of FDA alerts and it
    # fetches+parses PDFs from accessdata.fda.gov. A malformed or oversized PDF
    # must not be able to hang or OOM that run: that would silently stop all
    # alerting. These bound the input size, the page count, and the parse
    # wall-clock. None of the guards is reachable from the API (parse runs only
    # in the CLI/cron ingest path). Set any of them to 0 to disable that guard.
    #
    # Cap the downloaded PDF before it is fully buffered/parsed. Real PSG PDFs
    # are <2 MiB; 50 MiB is a wide margin that still stops a runaway body.
    pdf_max_bytes: int = 50 * 1024 * 1024
    # Hard wall-clock cap on text extraction, enforced by running the parse in a
    # killable child process (pdfminer's native loops do not reliably honor
    # SIGALRM). 0 disables isolation and parses in-process.
    pdf_parse_timeout_s: float = 60.0
    # Bound the page count, checked inside the parse child by BOTH engines
    # before any per-page text extraction. Complements the byte cap: hundreds
    # of thousands of near-empty pages fit under 50 MiB, and extraction walks
    # pages one at a time, so a page-flood would burn the whole parse budget
    # instead of failing fast. Real PSGs run under ~20 pages; 500 is a wide
    # margin over any legitimate FDA guidance document while still cutting a
    # flood off in well under the wall-clock cap. 0 disables.
    pdf_max_pages: int = 500

    # ---------- White Paper populator ----------
    # The Word template the CRA White Paper populator fills (python-docx). It is
    # gitignored but present on a real deployment; when absent (CI), the docx
    # writer generates a structurally-equivalent document from scratch.
    whitepaper_template_path: Path = Path("./CRA White Paper Template May 2026 - Raja.docx")
    # Prod machines have no persistent volume, so the gitignored template never
    # survives a deploy and every prod render fell back. When set (a long-lived
    # signed URL to a private object-store copy of the template, delivered as a
    # Fly secret), the render path lazily fetches and caches the template at
    # whitepaper_template_path on first use; any fetch failure keeps today's
    # loud FALLBACK_MARKER behavior. Rotation = re-sign + update the secret.
    whitepaper_template_url: str | None = None
    # Overall deadline (seconds) for the populate's live FDA fetch phase.
    # POST /whitepaper is a sync handler on the shared thread pool: per-call
    # HTTP timeouts bound each request but not the whole chain (DailyMed alone
    # paginates up to 10 pages with retries), so without this a slow source
    # run pins a shared worker thread for minutes. Enforced at the populator's
    # parallel-stage checkpoints; on a fetch-phase breach the build fails with
    # an audited mode="whitepaper" status="error" row
    # (reason=build_deadline_exceeded) and the API returns 504. During the
    # post-fetch cell build the lazy REMS index fetch is bounded by the
    # remaining time and the nested PSG ask() is entry-gated (never started
    # past the deadline); those breaches degrade the affected cells to analyst
    # input instead of failing the completed build. Default sits safely under
    # the UI's 120s bound for POST /whitepaper (LONG_TIMEOUT_MS in
    # regwatch/frontend/lib/api.ts) so the client is still listening when the
    # audited 504 arrives, with headroom for an in-flight PSG ask() (bounded
    # only by its own LLM/HTTP timeouts).
    # 0 disables the deadline (CLI/batch use; per-call timeouts still apply).
    whitepaper_build_timeout_s: float = 90.0

    # ---------- Deficiency analysis (DefPredict) ----------
    # Upload->analyze runs execute as background tasks INSIDE the API process
    # (a deliberate, documented exception to "the Fly image never parses a
    # PDF" -- see DECISIONS.md 2026-07-30). These bound that work.
    #
    # Overall wall-clock deadline for one analysis (parse + 4-stage detection,
    # including its LLM fan-out). On breach the run is marked error and the
    # worker thread is abandoned (it cannot be killed mid-C-extension); the
    # store's status guard makes a late finish a no-op. 0 disables.
    deficiency_analyze_timeout_s: float = 600.0
    # Concurrent analyses per process (dedicated CapacityLimiter, never the
    # default anyio pool). Excess runs queue in accepted state.
    deficiency_analyze_concurrency: int = 2
    # A run still in accepted/parsing/detecting whose last heartbeat
    # (updated_at) is older than this is flipped to error on read: the process
    # died mid-run and nothing else will ever finish it.
    deficiency_run_stale_minutes: int = 20
    # Historical-deficiency precedents fetched per detection domain.
    deficiency_precedent_top_k: int = 3
    # Remote OCR for scanned pages (Databricks model-serving invocations URL +
    # bearer token). Unset = OCR disabled; scanned pages degrade to whatever
    # embedded text layer exists, exactly like the vendored fallback path.
    deficiency_ocr_invocations_url: str | None = None
    deficiency_ocr_token: str | None = None

    # ---------- Auth ----------
    # Cookie-session auth: opaque tokens in an HttpOnly cookie; the DB stores
    # only the sha256 of the token. Secure stays False for the localhost pilot
    # (no TLS); set true the moment the API sits behind HTTPS.
    auth_cookie_secure: bool = False
    auth_session_ttl_hours: int = 72
    # Per-user requests/minute on POST /query and POST /assemble. 0 disables.
    rate_limit_per_minute: int = 30
    # Trust platform forwarding headers for the per-IP LOGIN limiter key.
    # Since the step-4 auth cutover the login limiter lives in the Go proxy,
    # which reads TRUST_PROXY_HEADERS from env itself (go/internal/api,
    # clientip.go documents the header-trust rationale: Fly-Client-IP first,
    # rightmost XFF fallback, never the spoofable leftmost hop). The field
    # stays DECLARED here so the env contract remains one documented list and
    # .env files carrying it keep validating; nothing Python-side reads it
    # anymore. tests/test_trust_proxy_fly_toml.py guards the prod fly.toml
    # value the Go keying depends on.
    trust_proxy_headers: bool = False
    # Opt-in bearer gate for GET /metrics. UNSET (default) keeps /metrics open
    # exactly as today so an existing Prometheus scrape keeps working with no
    # config change; ops turns the gate on by setting METRICS_TOKEN, after which
    # /metrics requires `Authorization: Bearer <token>` (compared constant-time).
    # Never gate /health (the Fly healthcheck) or /ready.
    metrics_token: str | None = None

    @field_validator("metrics_token", mode="before")
    @classmethod
    def _normalize_metrics_token(cls, v: object) -> str | None:
        """Empty/whitespace METRICS_TOKEN means OFF (open), same as unset.

        Without this, METRICS_TOKEN="" would arm the gate with a blank secret
        that a blank/absent Authorization header could satisfy.
        """
        if v is None:
            return None
        token = str(v).strip()
        return token or None

    # Shared secret gating the internal RAG compute endpoint (POST
    # /internal/query/compute), which the Go control plane calls to run the
    # stateless RAG core (step-5 CompleteQuery). FAIL-CLOSED: unset ("") makes
    # the endpoint 404 unconditionally, so it is inert until an operator sets
    # INTERNAL_RAG_TOKEN on both runtimes (a Fly app-wide secret in prod). The
    # endpoint is never exposed at the public edge (the Go proxy 404s /internal/
    # subtree); this token is the second layer, not the sole one.
    internal_rag_token: str = ""

    # Comma-separated CORS allowlist for the Next.js UI in regwatch/frontend/.
    # Defaults to the Next.js dev server. With allow_credentials=True on the
    # API, this allowlist is what stops other origins from riding the HttpOnly
    # session cookie, so keep it tight.
    cors_allow_origins_csv: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_allow_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins_csv.split(",") if o.strip()]

    # ---------- Refusal ----------
    # The application's own decline copy. Under v7 the model normally writes its
    # own "I do not have that" reply in plain words; this string is what gets
    # served instead when a gate guard blocks the model's wording, or when the
    # turn declines before there is any model text at all (no product resolved,
    # weak retrieval, provider error). The echo test provider also returns it.
    # Keep it short and stable: tests and the UI's reason copy read it.
    refusal_text: str = (
        "I couldn't find this in the current FDA guidance corpus, "
        "and I won't guess on a regulatory question."
    )

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.raw_pdf_dir, self.processed_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings; tests clear the cache after monkeypatching env."""
    s = Settings()  # type: ignore[call-arg]
    return s


# Default instance for convenience; tests that monkeypatch env clear get_settings() first.
settings = get_settings()
