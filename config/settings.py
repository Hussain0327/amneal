"""Application settings, sourced from environment via pydantic-settings.

Nothing is hard-coded in business logic. Anything that might change between
demos, environments, or experiments lives here.
"""

from __future__ import annotations

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
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # ---------- openFDA ----------
    openfda_api_key: str | None = None

    # ---------- Company ----------
    company_name: str = "Amneal"
    company_applicant_aliases: str = "AMNEAL PHARMS,AMNEAL PHARMACEUTICALS,AMNEAL PHARMS LLC"

    @property
    def applicant_aliases(self) -> list[str]:
        return [s.strip().upper() for s in self.company_applicant_aliases.split(",") if s.strip()]

    # ---------- Retrieval / refusal ----------
    retrieval_top_k: int = 8
    refusal_score_threshold: float = 0.30

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

    # ---------- API ----------
    api_host: str = "127.0.0.1"
    api_port: int = 8000

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


def get_settings() -> Settings:
    """Construct settings on each call so tests can monkeypatch env."""
    s = Settings()  # type: ignore[call-arg]
    return s


# Default instance for convenience; tests that monkeypatch env should call
# get_settings() explicitly after patching.
settings = get_settings()
