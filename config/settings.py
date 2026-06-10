"""Application settings, sourced from environment via pydantic-settings.

Nothing is hard-coded in business logic. Anything that might change between
demos, environments, or experiments lives here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Providers ----------
    embedding_provider: str = "local-bge-small"
    llm_provider: str = "openai"
    # OpenAI call surface: "responses" (default, GPT-5.x native) or "chat" (legacy
    # Chat Completions). The LLMProvider.complete() interface is identical either way.
    openai_api_mode: str = "responses"
    # Role-specific models. Cheap reasoning model for routing/classification; a more
    # capable one for grounded synthesis and BE extraction. Each falls back to
    # llm_model when unset. llm_model is the legacy single-model fallback.
    llm_model: str = "gpt-5.4-nano"
    router_model: str = "gpt-5-nano"
    synthesizer_model: str = "gpt-5.4-nano"
    extractor_model: str = "gpt-5.4-nano"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # Test-grade `echo` providers against a real (non-empty) corpus are an
    # invisible quality degradation; the API refuses to boot unless this is set.
    allow_test_providers: bool = Field(
        default=False, validation_alias="REGWATCH_ALLOW_TEST_PROVIDERS"
    )

    # ---------- openFDA ----------
    openfda_api_key: str | None = None

    # ---------- Company ----------
    company_name: str = "Amneal"
    company_applicant_aliases: str = "AMNEAL PHARMS,AMNEAL PHARMACEUTICALS,AMNEAL PHARMS LLC"

    @property
    def applicant_aliases(self) -> list[str]:
        return [s.strip().upper() for s in self.company_applicant_aliases.split(",") if s.strip()]

    # ---------- Retrieval / refusal ----------
    # Two-stage retrieval (per spec diagram):
    #   stage 1: vector search returns VECTOR_TOP_K candidates (wide net)
    #   stage 2: rerank to RERANK_TOP_K (the set we actually cite from)
    # When the reranker is off, stage 2 is the identity — we just take the
    # first RERANK_TOP_K of the wide net. This keeps the diagram and the
    # config in agreement at all times.
    vector_top_k: int = 50
    rerank_top_k: int = 8
    # Legacy alias — populated from RETRIEVAL_TOP_K if set (backwards compat).
    retrieval_top_k: int | None = None
    refusal_score_threshold: float = 0.30

    @property
    def effective_rerank_top_k(self) -> int:
        """Final-k after optional reranking. Prefers explicit RERANK_TOP_K."""
        if self.retrieval_top_k is not None and self.retrieval_top_k != self.rerank_top_k:
            # Honor legacy env var if user only set the old name.
            return self.retrieval_top_k
        return self.rerank_top_k

    @field_validator("refusal_score_threshold")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("REFUSAL_SCORE_THRESHOLD must be in [0, 1]")
        return v

    # ---------- Storage ----------
    data_dir: Path = Path("./data")
    chroma_dir: Path = Path("./data/chroma")
    sqlite_path: Path = Path("./data/regwatch.db")
    raw_pdf_dir: Path = Path("./data/raw")
    processed_dir: Path = Path("./data/processed")

    # ---------- Crawler ----------
    user_agent: str = "RegWatch/0.1 (clinical-regulatory-affairs; +https://example.invalid/contact)"
    http_timeout_s: float = 30.0
    crawl_concurrency: int = 4
    crawl_min_interval_ms: int = 250
    # In-process cache-aside TTL for the Orange Book products ZIP. The ~50k-row
    # file changes at most monthly, so a day-long TTL avoids re-downloading and
    # re-parsing on every query. Set to 0 to disable caching.
    orange_book_cache_ttl_s: float = 86_400.0

    # ---------- API ----------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Comma-separated CORS allowlist for the Next.js UI in web/. Defaults to the
    # Next.js dev server. There is no API auth yet, so keep this tight.
    cors_allow_origins_csv: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_allow_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins_csv.split(",") if o.strip()]

    # ---------- Refusal ----------
    refusal_text: str = (
        "I can't find this in the current FDA guidance corpus. "
        "I won't guess on a regulatory question."
    )

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.chroma_dir, self.raw_pdf_dir, self.processed_dir):
            p.mkdir(parents=True, exist_ok=True)
        # SQLite file's parent
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings; tests clear the cache after monkeypatching env."""
    s = Settings()  # type: ignore[call-arg]
    return s


# Default instance for convenience; tests that monkeypatch env clear get_settings() first.
settings = get_settings()
