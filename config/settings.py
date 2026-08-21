"""Application settings, sourced from environment via pydantic-settings.

Nothing is hard-coded in business logic. Anything that might change between
demos, environments, or experiments lives here.

Last updated: 2026-08-11. Where a default differs from what production runs,
the comment says so. The defaults here are local-dev defaults; production
values live in fly.toml and in Fly secrets.
"""

from __future__ import annotations

import os
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import (
    AliasChoices,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
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

# Reasoning levels supported by gpt-5.6-terra on the Responses API.
_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

# Preferred (new) -> deprecated (old) environment names. Resolution itself is
# AliasChoices on the fields below (new name wins when both are set); this
# table only drives the deprecation warnings in _warn_deprecated_env_names.
# The new names say which PATH each knob steers: RETRIEVAL_EMBEDDING_PROFILE
# picks the profile the query path serves with; INGEST_EMBEDDING_PROVIDER
# picks the provider the ingest/backfill WRITE path embeds with. The old
# names keep working unchanged -- prod Fly secrets still use them.
_DEPRECATED_ENV_ALIASES: tuple[tuple[str, str], ...] = (
    ("RETRIEVAL_EMBEDDING_PROFILE", "ACTIVE_EMBEDDING_PROFILE"),
    ("INGEST_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER"),
)


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
    # point now refuses loudly instead of guessing. Production sets OpenAI;
    # tests set echo. The same posture applies to llm_provider.
    #
    # INGEST_EMBEDDING_PROVIDER is the preferred env name: this knob steers
    # the ingest/backfill WRITE path (and the legacy retrieval arm), while
    # retrieval serving is steered by active_embedding_profile below. The old
    # EMBEDDING_PROVIDER keeps working as a deprecated alias; when both are
    # set the new name wins (AliasChoices order, warned about below).
    embedding_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices("INGEST_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER"),
    )
    llm_provider: str | None = None
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
    #
    # RETRIEVAL_EMBEDDING_PROFILE is the preferred env name (it names the path
    # this knob actually steers); the old ACTIVE_EMBEDDING_PROFILE keeps
    # working as a deprecated alias, and the new name wins when both are set.
    active_embedding_profile: str = Field(
        default="legacy",
        validation_alias=AliasChoices("RETRIEVAL_EMBEDDING_PROFILE", "ACTIVE_EMBEDDING_PROFILE"),
    )
    refusal_score_threshold_by_profile: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-embedding-profile INV-2 cosine floor. A threshold is a property "
            "of one model's score distribution, so the Qwen3-Embedding cutover "
            "gets its own calibrated entry rather than inheriting the legacy "
            "0.30. An absent profile falls back to refusal_score_threshold."
        ),
    )
    embedding_shadow_profile: str | None = None
    # Retrieval is exact, so coverage is required but an HNSW index is not.
    profile_hnsw_index_required: bool = False

    def effective_refusal_threshold(self) -> float:
        """Returns the INV-2 cosine floor the synthesizer actually applies.

        The per-profile entry for the active embedding profile wins; a profile
        with no calibrated entry falls back to the global
        ``refusal_score_threshold``. This is the one resolver for the floor:
        synthesis, ``regwatch status``, and the Go proxy's ``GET /settings``
        (``effectiveRefusalThreshold`` in ``go/internal/api/config.go``) all
        follow the same rule, so a UI cut derived from the reported floor is
        derived from the floor answers are gated on.
        """
        profile = (self.active_embedding_profile or "legacy").strip()
        return self.refusal_score_threshold_by_profile.get(
            profile, self.refusal_score_threshold
        )

    # OpenAI Responses + embeddings: gpt-5.6-terra generation and
    # text-embedding-3-large at 1024 dimensions.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_llm_model: str | None = "gpt-5.6-terra"
    openai_reasoning_effort: str | None = "medium"
    openai_embedding_model: str | None = "text-embedding-3-large"
    # 1024 is the RegWatch profile width (Matryoshka truncation of the model's
    # native 3072 dimensions via the API's `dimensions` parameter). Changing
    # it changes the embedding profile fingerprint.
    openai_embedding_dimension: int = 1024
    # Maximum inputs to place in one OpenAI embeddings request.
    openai_embedding_batch_size: int = 256
    openai_timeout_s: float = 60.0
    openai_max_retries: int = 3
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
        "embedding_shadow_profile",
        "openai_api_key",
        mode="before",
    )
    @classmethod
    def _normalize_optional_provider_value(cls, v: object) -> str | None:
        """Whitespace-only endpoint settings are equivalent to unset."""
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @staticmethod
    def _raw_env_value(name: str) -> str | None:
        """Raw value of ``name`` from the process env or the .env file, or None.

        Mirrors the sources and precedence pydantic-settings itself reads
        (process env wins over the env file; names match case-insensitively,
        parsed by the same python-dotenv), but only to decide the deprecation
        warnings below -- field VALUES are resolved by pydantic-settings
        through AliasChoices, never here.
        """
        wanted = name.upper()
        for key, env_value in os.environ.items():
            if key.upper() == wanted:
                return env_value
        env_file = Path(".env")
        if env_file.is_file():
            for key, file_value in dotenv_values(env_file).items():
                if key.upper() == wanted and file_value is not None:
                    return file_value
        return None

    @model_validator(mode="after")
    def _warn_deprecated_env_names(self) -> Settings:
        """One clear nudge per deprecated env name still steering config.

        Blank values count as unset, matching the normalize validators. Old
        name set with the new one absent: still works, warns once. Both set
        and disagreeing: the new name wins (AliasChoices order above) and the
        warning says so. Both set and agreeing: silent -- that is the safe
        transition state a deployment passes through while renaming secrets.

        FutureWarning, not DeprecationWarning: the audience is the operator
        reading boot logs, and Python's default filters hide DeprecationWarning
        outside __main__ -- exactly where this module loads in production. The
        default "print each unique warning once" filter is what keeps this to
        a single line per process.
        """
        for new_name, old_name in _DEPRECATED_ENV_ALIASES:
            old_value = (self._raw_env_value(old_name) or "").strip()
            if not old_value:
                continue
            new_value = (self._raw_env_value(new_name) or "").strip()
            if not new_value:
                warnings.warn(
                    f"{old_name} is deprecated; set {new_name} instead (same value, new name).",
                    FutureWarning,
                    stacklevel=1,
                )
            elif new_value != old_value:
                warnings.warn(
                    f"Both {new_name} and {old_name} are set and disagree; {new_name} "
                    f"({new_value!r}) wins over deprecated {old_name} ({old_value!r}).",
                    FutureWarning,
                    stacklevel=1,
                )
        return self

    @field_validator(
        # The prose flag's rollback story is "unset the Fly secret"; a secret
        # set to the empty string must read as OFF, not take the app down at
        # boot with a bool_parsing error. Same story for the v7 flag it gates.
        "prose_synthesis_enabled",
        "route_call_mode",
        "route_call_max_tokens",
        "selective_citation_enabled",
        # Same rollback story: this flag ships as a Fly secret / workflow env
        # value, and a blank must mean "default (required)", never a
        # bool_parsing crash at import.
        "profile_hnsw_index_required",
        # An unconfigured OPENAI_BASE_URL/_EMBEDDING_DIMENSION/_BATCH_SIZE
        # arrives here as "", which would otherwise fail int parsing or the
        # https:// check at import time.
        "openai_base_url",
        "openai_llm_model",
        "openai_reasoning_effort",
        "openai_embedding_model",
        "openai_embedding_dimension",
        "openai_embedding_batch_size",
        mode="before",
    )
    @classmethod
    def _blank_env_falls_back_to_default(cls, v: object, info: ValidationInfo) -> object:
        """An env var set to "" means "not configured", not "override with empty".

        CI templating can render an unset value as the empty string rather than
        omitting the variable. Without this normalization, a blank numeric
        setting fails integer parsing at import time and takes down every
        process that imports settings -- including the whole test suite. Its
        failure message says nothing about the workflow that caused it. The same
        shape appears in any deploy template that interpolates optional config.

        Deliberately opt-in per field rather than a blanket rule.
        """
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[str(info.field_name)].default
        return v

    @field_validator("openai_base_url")
    @classmethod
    def _require_https_endpoint(cls, v: str | None) -> str | None:
        """The OpenAI endpoint must use TLS.

        This URL carries the analyst question for embedding and synthesis.
        A typo'd ``http://``
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

    @field_validator("openai_reasoning_effort")
    @classmethod
    def _check_openai_reasoning_effort(cls, v: str | None) -> str | None:
        """Reject a typo at boot rather than 400-ing every synthesis at runtime."""
        if v is None:
            return None
        effort = v.strip().lower()
        if effort not in _REASONING_EFFORTS:
            raise ValueError(
                "OPENAI_REASONING_EFFORT must be one of "
                f"{', '.join(_REASONING_EFFORTS)} (or empty to send no parameter)"
            )
        return effort

    @field_validator("openai_embedding_dimension")
    @classmethod
    def _check_openai_embedding_dimension(cls, v: int) -> int:
        # 3072 is text-embedding-3-large's native size; we run 1024 via
        # Matryoshka truncation, but a smaller custom profile is still valid.
        if not 32 <= v <= 3072:
            raise ValueError("OPENAI_EMBEDDING_DIMENSION must be in [32, 3072]")
        return v

    @field_validator("openai_embedding_batch_size")
    @classmethod
    def _check_openai_embedding_batch_size(cls, v: int) -> int:
        if not 1 <= v <= 2048:
            raise ValueError("OPENAI_EMBEDDING_BATCH_SIZE must be in [1, 2048]")
        return v

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

    # ---------- LLM pricing (H3) ----------
    # USD per 1M tokens, keyed by model name. Env-overridable as JSON, e.g.
    #   LLM_MODEL_PRICES='{"gpt-oss-120b-080525": {"input": 0.15, "output": 0.60}}'
    # Empty by default. An unknown model yields cost_usd NULL, never a guess.
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
    # Scoped-activation amendment (2026-08-18): when set, activation counts
    # against the durable manifest with this exact logical sha256 instead of
    # requiring a complete-universe run. The full 140,438-doc universe became
    # permanently unreachable under the Lakebase free tier's 512MB cap, so
    # the served corpus is a curated manifest the operator names EXPLICITLY
    # here -- a scoped sync still can never activate by accident. Unset keeps
    # the original complete-universe-only behavior.
    serving_manifest_sha: str | None = Field(
        default=None, validation_alias="REGWATCH_SERVING_MANIFEST_SHA"
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
