"""Application settings, sourced from environment via pydantic-settings.

Nothing is hard-coded in business logic. Anything that might change between
demos, environments, or experiments lives here.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# OpenAI dated-snapshot suffix: what follows "<alias>-" in the server-reported
# model name, e.g. "gpt-5.4-nano-2026-01-15" or legacy "gpt-4-0613". Digits and
# hyphens only — "gpt-5-nano-mini" is a DIFFERENT model, not a snapshot.
_SNAPSHOT_SUFFIX_RE = re.compile(r"\d[\d-]*")

# Default final-k after optional reranking. Used to detect whether RERANK_TOP_K
# was set explicitly (vs. the legacy RETRIEVAL_TOP_K) in effective_rerank_top_k.
_DEFAULT_RERANK_TOP_K = 8


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
    # ---------- LLM client transport (B3) ----------
    # The OpenAI/Anthropic SDKs default to a 600s read timeout with 2 retries —
    # a stalled provider would pin a sync-route worker for ~10-20 min. Bound it.
    # The embedder owns its own retry loop, so it constructs the shared client
    # with max_retries=0 to avoid stacking SDK retries on top of that loop.
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # Test-grade `echo` providers against a real (non-empty) corpus are an
    # invisible quality degradation; the API refuses to boot unless this is set.
    allow_test_providers: bool = Field(
        default=False, validation_alias="REGWATCH_ALLOW_TEST_PROVIDERS"
    )

    # ---------- LLM pricing (H3) ----------
    # USD per 1M tokens, keyed by model name. Env-overridable as JSON, e.g.
    #   LLM_MODEL_PRICES='{"gpt-5.4-nano": {"input": 0.05, "output": 0.40}}'
    # Defaults cover the gpt-5 nano family the app actually runs. An unknown
    # model yields cost_usd NULL in the audit log — never a guessed price.
    llm_model_prices: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "gpt-5-nano": {"input": 0.05, "output": 0.40},
            "gpt-5.4-nano": {"input": 0.05, "output": 0.40},
        }
    )

    def price_for_model(self, model: str) -> dict[str, float] | None:
        """Per-1M-token prices for a model, or None when unknown (cost stays NULL).

        Exact table match first. The OpenAI Responses path reports the RESOLVED
        dated snapshot id (e.g. ``gpt-5.4-nano-2026-01-15``) rather than the
        configured alias, so a miss falls back to the longest table key that is
        a dated-snapshot prefix of the reported name. Genuinely unknown model
        families (including non-snapshot suffixes like ``-mini``) stay None —
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
    # Sentry is OFF unless SENTRY_DSN is set — zero behavior change otherwise.
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
    rerank_top_k: int = _DEFAULT_RERANK_TOP_K
    # Phase-2 cross-encoder rerank. Off by default: when false, stage 2 is the
    # identity (first rerank_top_k of the wide net). Read via Settings (not a
    # bare os.getenv) so the knob is documented and validated like every other.
    reranker_enabled: bool = False
    # Legacy alias — populated from RETRIEVAL_TOP_K if set (backwards compat).
    retrieval_top_k: int | None = None
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
        default — so a stale legacy var lingering in the environment can no
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
    # DATABASE_URL switches the structured store to Postgres (Supabase). Empty
    # or unset -> SQLite at sqlite_path, exactly as before (dev/test default).
    # Vector backend rule (K1): vectors live in pgvector iff database_url is
    # set; Chroma remains the SQLite-mode backend. There is no separate toggle.
    database_url: str | None = None
    # Production safety (B1): when true, the app refuses to boot on the SQLite
    # fallback — a missing/typo'd DATABASE_URL would otherwise silently run on
    # an ephemeral disk and lose all users, sessions, and the query_log audit
    # trail (INV-6 evidence) on the next machine recycle. Set in fly.toml.
    require_database_url: bool = False

    # Postgres connection-level timeouts (Supabase session pooler). The app
    # connects as the `postgres` role, which — unlike Supabase's
    # anon/authenticated roles — ships with NO server-side statement/lock/idle
    # timeouts. Without them a connection that stalls mid-transaction holds its
    # locks forever: on 2026-06-18 an idle-in-transaction chunk read blocked the
    # boot-time `ALTER TABLE chunk ENABLE RLS` and wedged prod. These are applied
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
    # exists, and pool_pre_ping opens a fresh connection on checkout — so without
    # this a stalled handshake to the public Supabase pooler hangs a request
    # thread forever (store-1). Integer seconds; '0' or '' disables the bound.
    db_connect_timeout: str = "10"
    # Recycle pooled connections before Supavisor's own idle cutoff so a stale
    # server-side socket is never handed to a request (pairs with pool_pre_ping).
    db_pool_recycle_s: int = 1800

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, v: object) -> str | None:
        """Normalize DATABASE_URL to the SQLAlchemy psycopg v3 driver form.

        - empty/whitespace -> None (SQLite mode)
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
    chroma_dir: Path = Path("./data/chroma")
    sqlite_path: Path = Path("./data/regwatch.db")
    raw_pdf_dir: Path = Path("./data/raw")
    processed_dir: Path = Path("./data/processed")

    # ---------- Crawler ----------
    user_agent: str = "RegWatch/0.1 (clinical-regulatory-affairs; +https://example.invalid/contact)"
    http_timeout_s: float = 30.0
    # Reserved / not yet wired: the PSG crawl and ingest run sequentially today
    # (politeness + the single-threaded alembic init path). Kept so the knob
    # exists if a concurrent fetch path is added; it currently has no effect.
    crawl_concurrency: int = 4
    crawl_min_interval_ms: int = 250
    # In-process cache-aside TTL for the Orange Book products ZIP. The ~50k-row
    # file changes at most monthly, so a day-long TTL avoids re-downloading and
    # re-parsing on every query. Set to 0 to disable caching.
    orange_book_cache_ttl_s: float = 86_400.0

    # ---------- PDF ingest safety (cron/ingest worker only) ----------
    # The daily `regwatch watch` run is the SOLE driver of FDA alerts and it
    # fetches+parses PDFs from accessdata.fda.gov. A malformed or oversized PDF
    # must not be able to hang or OOM that run — that would silently stop all
    # alerting. These bound the input at the I/O boundary and the parse
    # wall-clock. Neither guard is reachable from the API (parse runs only in the
    # CLI/cron ingest path). Set either to 0 to disable that guard.
    #
    # Cap the downloaded PDF before it is fully buffered/parsed. Real PSG PDFs
    # are <2 MiB; 50 MiB is a wide margin that still stops a runaway body.
    pdf_max_bytes: int = 50 * 1024 * 1024
    # Hard wall-clock cap on text extraction, enforced by running the parse in a
    # killable child process (pdfminer's native loops do not reliably honor
    # SIGALRM). 0 disables isolation and parses in-process.
    pdf_parse_timeout_s: float = 60.0

    # ---------- White Paper populator ----------
    # The Word template the CRA White Paper populator fills (python-docx). It is
    # gitignored but present on a real deployment; when absent (CI), the docx
    # writer generates a structurally-equivalent document from scratch.
    whitepaper_template_path: Path = Path("./CRA White Paper Template May 2026 - Raja.docx")

    # ---------- API ----------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # ---------- Auth ----------
    # Cookie-session auth: opaque tokens in an HttpOnly cookie; the DB stores
    # only the sha256 of the token. Secure stays False for the localhost pilot
    # (no TLS); set true the moment the API sits behind HTTPS.
    auth_cookie_secure: bool = False
    auth_session_ttl_hours: int = 72
    # Per-user requests/minute on POST /query and POST /assemble. 0 disables.
    rate_limit_per_minute: int = 30
    # Comma-separated CORS allowlist for the Next.js UI in regwatch/frontend/.
    # Defaults to the Next.js dev server. With allow_credentials=True on the
    # API, this allowlist is what stops other origins from riding the HttpOnly
    # session cookie — keep it tight.
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
