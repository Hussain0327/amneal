"""H3: token usage from providers, settings price table, audit threading, migration."""

from __future__ import annotations

from typing import Any

import pytest

from regwatch.generate.llm import (
    EchoLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    OpenAIProvider,
    estimate_cost_usd,
)

# ---------- provider usage extraction ----------


class _Usage:
    def __init__(self, **kw: int) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, usage: Any = None) -> None:
        self.output_text = "pong"
        self.model = "gpt-5.4-nano"
        self.usage = usage

    def model_dump(self) -> dict[str, Any]:
        return {"model": self.model}


class _Responses:
    def __init__(self, usage: Any) -> None:
        self.usage = usage

    def create(self, **kwargs: Any) -> _Resp:
        return _Resp(self.usage)


class _ChatResp:
    def __init__(self, usage: Any) -> None:
        class _Msg:
            content = "pong"

        class _Choice:
            message = _Msg()

        self.choices = [_Choice()]
        self.usage = usage

    def model_dump(self) -> dict[str, Any]:
        return {}


class _Completions:
    def __init__(self, usage: Any) -> None:
        self.usage = usage

    def create(self, **kwargs: Any) -> _ChatResp:
        return _ChatResp(self.usage)


class _FakeClient:
    def __init__(self, usage: Any = None, chat_usage: Any = None) -> None:
        self.responses = _Responses(usage)

        class _Chat:
            completions = _Completions(chat_usage)

        self.chat = _Chat()


def test_echo_provider_reports_zero_usage() -> None:
    r = EchoLLMProvider().complete([LLMMessage("user", "hi")])
    assert r.usage == LLMUsage(input_tokens=0, output_tokens=0)
    r_json = EchoLLMProvider().complete([LLMMessage("user", "hi")], response_format="json")
    assert r_json.usage == LLMUsage(input_tokens=0, output_tokens=0)


def test_responses_mode_extracts_usage() -> None:
    client = _FakeClient(usage=_Usage(input_tokens=120, output_tokens=45))
    p = OpenAIProvider(model="gpt-5.4-nano", api_key="x", mode="responses", client=client)
    r = p.complete([LLMMessage("user", "hi")])
    assert r.usage == LLMUsage(input_tokens=120, output_tokens=45)


def test_responses_mode_missing_usage_stays_none() -> None:
    client = _FakeClient(usage=None)
    p = OpenAIProvider(model="gpt-5.4-nano", api_key="x", mode="responses", client=client)
    r = p.complete([LLMMessage("user", "hi")])
    assert r.usage == LLMUsage(input_tokens=None, output_tokens=None)


def test_chat_mode_extracts_usage() -> None:
    client = _FakeClient(chat_usage=_Usage(prompt_tokens=80, completion_tokens=20))
    p = OpenAIProvider(model="gpt-5.4-nano", api_key="x", mode="chat", client=client)
    r = p.complete([LLMMessage("user", "hi")])
    assert r.usage == LLMUsage(input_tokens=80, output_tokens=20)


def test_llmresponse_usage_is_optional_for_existing_callers() -> None:
    """The richer return must not break constructor calls that ignore usage."""
    r = LLMResponse(text="t", model="m")
    assert r.usage == LLMUsage(input_tokens=None, output_tokens=None)


# ---------- cost from the settings price table ----------


def test_estimate_cost_known_model() -> None:
    # Default table: gpt-5.4-nano at $0.05/1M input, $0.40/1M output.
    cost = estimate_cost_usd("gpt-5.4-nano", LLMUsage(input_tokens=1_000_000, output_tokens=0))
    assert cost == pytest.approx(0.05)
    cost = estimate_cost_usd("gpt-5.4-nano", LLMUsage(input_tokens=200_000, output_tokens=100_000))
    assert cost == pytest.approx(0.2 * 0.05 + 0.1 * 0.40)


def test_estimate_cost_unknown_model_is_none_never_a_guess() -> None:
    assert estimate_cost_usd("totally-unknown-model", LLMUsage(10, 10)) is None
    assert estimate_cost_usd("echo", LLMUsage(0, 0)) is None


def test_estimate_cost_snapshot_suffixed_model_uses_family_price() -> None:
    """OpenAI's Responses path echoes the resolved dated snapshot id, not the
    configured alias — the family price must still resolve (review fix)."""
    cost = estimate_cost_usd(
        "gpt-5.4-nano-2026-01-15", LLMUsage(input_tokens=1_000_000, output_tokens=0)
    )
    assert cost == pytest.approx(0.05)
    # Legacy short-form snapshots (e.g. -0613) are digits-and-hyphens too.
    cost = estimate_cost_usd("gpt-5-nano-2025-08-07", LLMUsage(200_000, 100_000))
    assert cost == pytest.approx(0.2 * 0.05 + 0.1 * 0.40)


def test_snapshot_fallback_prefers_longest_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv(
        "LLM_MODEL_PRICES",
        '{"gpt-5": {"input": 9.0, "output": 9.0}, "gpt-5-nano": {"input": 1.0, "output": 2.0}}',
    )
    cs.get_settings.cache_clear()
    cost = estimate_cost_usd("gpt-5-nano-2025-08-07", LLMUsage(1_000_000, 0))
    assert cost == pytest.approx(1.0)  # gpt-5-nano, never the shorter gpt-5 key


def test_non_snapshot_suffix_never_falls_back() -> None:
    """A '-mini'/'-turbo' style suffix is a different model, not a snapshot."""
    assert estimate_cost_usd("gpt-5-nano-mini", LLMUsage(10, 10)) is None
    assert estimate_cost_usd("gpt-5.4-nanox", LLMUsage(10, 10)) is None
    assert estimate_cost_usd("gpt-5.4-nano-2026-01-15-mini", LLMUsage(10, 10)) is None


def test_estimate_cost_unreported_usage_is_none() -> None:
    assert estimate_cost_usd("gpt-5.4-nano", LLMUsage(None, 5)) is None
    assert estimate_cost_usd("gpt-5.4-nano", LLMUsage(5, None)) is None


def test_price_table_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("LLM_MODEL_PRICES", '{"my-model": {"input": 1.0, "output": 2.0}}')
    cs.get_settings.cache_clear()
    cost = estimate_cost_usd("my-model", LLMUsage(input_tokens=500_000, output_tokens=500_000))
    assert cost == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)
    # The override REPLACES the table; old defaults are gone, cost stays NULL.
    assert estimate_cost_usd("gpt-5.4-nano", LLMUsage(10, 10)) is None


def test_malformed_price_entry_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("LLM_MODEL_PRICES", '{"my-model": {"input": 1.0}}')  # no output price
    cs.get_settings.cache_clear()
    assert estimate_cost_usd("my-model", LLMUsage(10, 10)) is None


# ---------- audit threading (log_query -> query_log) ----------


def test_log_query_persists_token_and_cost_fields() -> None:
    from regwatch.common.audit import log_query
    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import QueryLog

    init_db()
    audit_id = log_query(
        mode="qa",
        query_text="q",
        retrieved=[],
        answer_text="a",
        citations=[],
        refused=False,
        model_name="gpt-5.4-nano",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.000025,
    )
    with session_scope() as s:
        row = s.get(QueryLog, audit_id)
        assert row is not None
        assert row.input_tokens == 100
        assert row.output_tokens == 50
        assert row.cost_usd == pytest.approx(0.000025)


def test_log_query_defaults_stay_null() -> None:
    from regwatch.common.audit import log_query
    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import QueryLog

    init_db()
    audit_id = log_query(
        mode="qa",
        query_text="q",
        retrieved=[],
        answer_text="a",
        citations=[],
        refused=True,
        model_name="echo",
    )
    with session_scope() as s:
        row = s.get(QueryLog, audit_id)
        assert row is not None
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.cost_usd is None


# ---------- grounded_qa threads the synthesizer call's usage ----------


def _seed_corpus() -> None:
    from regwatch.process.embedder import get_embedding_provider
    from regwatch.store.db import init_db
    from regwatch.store.vector_store import add_chunks

    init_db()
    texts = ["Fasting bioequivalence study with 36 subjects."]
    meta = {
        "doc_id": 1,
        "version_id": 10,
        "page": 3,
        "section_path": "II.A",
        "normalized_name": "albuterol sulfate",
        "dosage_form": "Aerosol, Metered",
        "route": "Inhalation",
        "source_url": "http://example/PSG_020503.pdf",
        "psg_type": "draft",
        "appl_no": "020503",
    }
    embeddings = get_embedding_provider().embed(texts)
    add_chunks(ids=["chunk-0"], embeddings=embeddings, documents=texts, metadatas=[meta])


class _StubLLM:
    name = "stub"

    def __init__(self, text: str, usage: LLMUsage, model: str = "gpt-5.4-nano") -> None:
        self._text = text
        self._usage = usage
        self._model = model

    def complete(self, *a: object, **kw: object) -> LLMResponse:
        return LLMResponse(text=self._text, model=self._model, usage=self._usage)


def test_ask_records_synthesizer_usage_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.generate import grounded_qa as qa_mod
    from regwatch.store.db import session_scope
    from regwatch.store.models import QueryLog

    _seed_corpus()
    stub = _StubLLM(
        "A fasting study is recommended [PSG_020503, p.3].",
        LLMUsage(input_tokens=1_000, output_tokens=500),
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)

    result = qa_mod.ask("What study design is recommended for albuterol sulfate?")
    assert not result.refused

    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.input_tokens == 1_000
        assert row.output_tokens == 500
        # gpt-5.4-nano default prices: (1000*0.05 + 500*0.40) / 1e6
        assert row.cost_usd == pytest.approx((1_000 * 0.05 + 500 * 0.40) / 1_000_000)


def test_ask_prices_server_reported_snapshot_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthesizer audit path prices response.model, which on the (default)
    Responses surface is the server-echoed dated snapshot id — not the alias
    the app configured. Cost must resolve, not stay NULL (review fix)."""
    from regwatch.generate import grounded_qa as qa_mod
    from regwatch.store.db import session_scope
    from regwatch.store.models import QueryLog

    _seed_corpus()
    stub = _StubLLM(
        "A fasting study is recommended [PSG_020503, p.3].",
        LLMUsage(input_tokens=1_000, output_tokens=500),
        model="gpt-5.4-nano-2026-01-15",  # what OpenAI actually echoes back
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)

    result = qa_mod.ask("What study design is recommended for albuterol sulfate?")
    assert not result.refused

    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.cost_usd == pytest.approx((1_000 * 0.05 + 500 * 0.40) / 1_000_000)


def test_pre_llm_refusal_keeps_token_fields_null() -> None:
    """No LLM call -> NULL token fields (not zeros): nothing was spent."""
    from regwatch.generate import grounded_qa as qa_mod
    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import QueryLog

    init_db()  # empty corpus -> no_product refusal before any LLM call
    result = qa_mod.ask("What does the FDA recommend for romidepsin?")
    assert result.refused
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.cost_usd is None


# ---------- migration 0008: both the sqlite upgrade and the metadata path ----------


def test_fresh_sqlite_has_token_columns_and_feedback_table() -> None:
    from sqlalchemy import inspect

    from regwatch.store.db import get_engine, init_db

    init_db()
    get_engine().dispose()  # drop pooled pre-migration schema caches
    inspector = inspect(get_engine())
    cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
    assert "answer_feedback" in inspector.get_table_names()
    fb_cols = {c["name"] for c in inspector.get_columns("answer_feedback")}
    assert {"id", "audit_id", "user_id", "rating", "comment", "created_at"} == fb_cols


def test_upgrade_path_from_0007_adds_columns_and_table() -> None:
    """An existing (0007-stamped) database reaches the same schema via upgrade."""
    from alembic import command
    from sqlalchemy import inspect

    from regwatch.store import db as db_module

    cfg = db_module._alembic_config()
    command.upgrade(cfg, "0007_chat_session_user_updated")
    inspector = inspect(db_module.get_engine())
    assert "answer_feedback" not in inspector.get_table_names()
    assert "input_tokens" not in {c["name"] for c in inspector.get_columns("query_log")}

    command.upgrade(cfg, "head")
    db_module.get_engine().dispose()
    inspector = inspect(db_module.get_engine())
    cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
    assert "answer_feedback" in inspector.get_table_names()


def test_downgrade_path_removes_columns_and_table() -> None:
    from alembic import command
    from sqlalchemy import inspect

    from regwatch.store import db as db_module

    cfg = db_module._alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007_chat_session_user_updated")
    db_module.get_engine().dispose()
    inspector = inspect(db_module.get_engine())
    assert "answer_feedback" not in inspector.get_table_names()
    assert "input_tokens" not in {c["name"] for c in inspector.get_columns("query_log")}


def test_unstamped_current_schema_db_is_upgraded_not_stamped_head() -> None:
    """A 0007-shaped DB without an alembic stamp must gain the 0008 schema on boot.

    Guards the _init_sqlite heuristic: stamping such a database at "head"
    would silently skip migration 0008 and break the first query insert.
    """
    from alembic import command
    from sqlalchemy import inspect, text

    from regwatch.store import db as db_module

    cfg = db_module._alembic_config()
    command.upgrade(cfg, "0007_chat_session_user_updated")
    with db_module.get_engine().begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    db_module.get_engine().dispose()

    db_module.init_db()
    db_module.get_engine().dispose()
    inspector = inspect(db_module.get_engine())
    cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
    assert "answer_feedback" in inspector.get_table_names()


def test_unstamped_pre_0006_schema_db_replays_0006_onward() -> None:
    """An unstamped DB with the post-0005/pre-0006 model shape must be stamped
    at what its shape actually proves (0005), not 0006/0007: 0006 (appl_type
    columns + the ob_product N->NDA normalization), 0007 (composite index) and
    0008 all replay instead of being silently skipped (review fix).
    """
    from alembic import command
    from sqlalchemy import inspect, text

    from regwatch.store import db as db_module

    cfg = db_module._alembic_config()
    command.upgrade(cfg, "0005_whitepaper_sources")
    with db_module.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ob_product (appl_no, product_no, appl_type, last_fetched_at) "
                "VALUES ('020503', '001', 'N', '2026-06-12 00:00:00')"
            )
        )
        conn.execute(text("DROP TABLE alembic_version"))
    db_module.get_engine().dispose()

    db_module.init_db()
    db_module.get_engine().dispose()
    inspector = inspect(db_module.get_engine())
    # 0006 landed: the appl_type columns AND the data normalization.
    for table in ("ob_patent", "ob_exclusivity"):
        assert "appl_type" in {c["name"] for c in inspector.get_columns(table)}
    with db_module.get_engine().connect() as conn:
        assert conn.execute(text("SELECT appl_type FROM ob_product")).scalar() == "NDA"
    # 0007 landed: the composite chat_session index.
    session_indexes = {ix["name"] for ix in inspector.get_indexes("chat_session")}
    assert "ix_chat_session_user_id_updated_at" in session_indexes
    # 0008 landed: token columns + answer_feedback.
    cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert {"input_tokens", "output_tokens", "cost_usd"} <= cols
    assert "answer_feedback" in inspector.get_table_names()


def test_migration_schema_matches_model_metadata() -> None:
    """The sqlite alembic path and the Postgres create_all path must agree.

    Fresh Postgres never replays migrations (create_all + stamp), so the
    answer_feedback/query_log shape declared in models.py IS the Postgres
    schema; this asserts the migration-produced sqlite schema carries the
    same columns and constraints.
    """
    from sqlalchemy import inspect

    from regwatch.store.db import get_engine, init_db
    from regwatch.store.models import AnswerFeedback, QueryLog

    init_db()
    get_engine().dispose()
    inspector = inspect(get_engine())

    model_fb_cols = set(AnswerFeedback.__table__.c.keys())  # type: ignore[attr-defined]
    db_fb_cols = {c["name"] for c in inspector.get_columns("answer_feedback")}
    assert model_fb_cols == db_fb_cols

    model_ql_cols = set(QueryLog.__table__.c.keys())  # type: ignore[attr-defined]
    db_ql_cols = {c["name"] for c in inspector.get_columns("query_log")}
    assert model_ql_cols == db_ql_cols

    uniques = {u["name"] for u in inspector.get_unique_constraints("answer_feedback")}
    assert "uq_answer_feedback_audit_user" in uniques


def test_answer_feedback_rating_check_constraint_enforced() -> None:
    from sqlalchemy.exc import IntegrityError

    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import AnswerFeedback

    init_db()
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(AnswerFeedback(audit_id=1, user_id="1", rating=5))
        s.flush()
