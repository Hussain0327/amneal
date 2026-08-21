"""H3: token usage from providers, settings price table, audit threading, migration."""

from __future__ import annotations

import pytest

from regwatch.generate.llm import (
    EchoLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    estimate_cost_usd,
)
from tests.conftest import synth_turn_json

# ---------- provider usage extraction ----------
# Provider-level usage extraction is covered by the OpenAI Responses tests;
# here only the provider-agnostic pieces remain.


def test_echo_provider_reports_zero_usage() -> None:
    r = EchoLLMProvider().complete([LLMMessage("user", "hi")])
    assert r.usage == LLMUsage(input_tokens=0, output_tokens=0)
    r_json = EchoLLMProvider().complete([LLMMessage("user", "hi")], response_format="json")
    assert r_json.usage == LLMUsage(input_tokens=0, output_tokens=0)


def test_llmresponse_usage_is_optional_for_existing_callers() -> None:
    """The richer return must not break constructor calls that ignore usage."""
    r = LLMResponse(text="t", model="m")
    assert r.usage == LLMUsage(input_tokens=None, output_tokens=None)


# ---------- cost from the settings price table ----------

# The default table is empty; priced-model tests declare their table through
# LLM_MODEL_PRICES.
_TEST_PRICES = '{"oss-model": {"input": 0.05, "output": 0.40}}'


def _set_prices(monkeypatch: pytest.MonkeyPatch, table: str = _TEST_PRICES) -> None:
    import config.settings as cs

    monkeypatch.setenv("LLM_MODEL_PRICES", table)
    cs.get_settings.cache_clear()


def test_default_price_table_is_empty_never_a_guess() -> None:
    # No hard-coded rate may survive for a model the operator did not price.
    from config.settings import Settings

    assert Settings(_env_file=None).llm_model_prices == {}  # type: ignore[call-arg]
    assert estimate_cost_usd("gpt-oss-120b-080525", LLMUsage(10, 10)) is None


def test_estimate_cost_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_prices(monkeypatch)
    cost = estimate_cost_usd("oss-model", LLMUsage(input_tokens=1_000_000, output_tokens=0))
    assert cost == pytest.approx(0.05)
    cost = estimate_cost_usd("oss-model", LLMUsage(input_tokens=200_000, output_tokens=100_000))
    assert cost == pytest.approx(0.2 * 0.05 + 0.1 * 0.40)


def test_estimate_cost_unknown_model_is_none_never_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_prices(monkeypatch)
    assert estimate_cost_usd("totally-unknown-model", LLMUsage(10, 10)) is None
    assert estimate_cost_usd("echo", LLMUsage(0, 0)) is None


def test_estimate_cost_snapshot_suffixed_model_uses_family_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A serving endpoint may report a dated snapshot id, not the configured
    alias — the family price must still resolve (review fix)."""
    _set_prices(monkeypatch)
    cost = estimate_cost_usd(
        "oss-model-2026-01-15", LLMUsage(input_tokens=1_000_000, output_tokens=0)
    )
    assert cost == pytest.approx(0.05)
    # Legacy short-form snapshots (e.g. -0613) are digits-and-hyphens too.
    cost = estimate_cost_usd("oss-model-0613", LLMUsage(200_000, 100_000))
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


def test_non_snapshot_suffix_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A '-mini'/'-turbo' style suffix is a different model, not a snapshot."""
    _set_prices(monkeypatch)
    assert estimate_cost_usd("oss-model-mini", LLMUsage(10, 10)) is None
    assert estimate_cost_usd("oss-modelx", LLMUsage(10, 10)) is None
    assert estimate_cost_usd("oss-model-2026-01-15-mini", LLMUsage(10, 10)) is None


def test_estimate_cost_unreported_usage_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_prices(monkeypatch)
    assert estimate_cost_usd("oss-model", LLMUsage(None, 5)) is None
    assert estimate_cost_usd("oss-model", LLMUsage(5, None)) is None


def test_price_table_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("LLM_MODEL_PRICES", '{"my-model": {"input": 1.0, "output": 2.0}}')
    cs.get_settings.cache_clear()
    cost = estimate_cost_usd("my-model", LLMUsage(input_tokens=500_000, output_tokens=500_000))
    assert cost == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)
    # The override REPLACES the table; unlisted models still cost NULL.
    assert estimate_cost_usd("gpt-oss-120b-080525", LLMUsage(10, 10)) is None


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


# The synthesizer returns a structured turn, not prose: the stub must be held to
# the same contract or these tests would measure the token accounting of a
# malformed_structure refusal instead of an answer turn.
_ANSWER_TURN = synth_turn_json([("A fasting study is recommended", [("PSG_020503", 3)])])


def test_ask_records_synthesizer_usage_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from regwatch.generate import grounded_qa as qa_mod
    from regwatch.store.db import session_scope
    from regwatch.store.models import QueryLog

    _set_prices(monkeypatch, '{"gpt-5.4-nano": {"input": 0.05, "output": 0.40}}')
    _seed_corpus()
    stub = _StubLLM(_ANSWER_TURN, LLMUsage(input_tokens=1_000, output_tokens=500))
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)

    result = qa_mod.ask("What study design is recommended for albuterol sulfate?")
    assert not result.refused

    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.input_tokens == 1_000
        assert row.output_tokens == 500
        # Priced via the operator table set above: (1000*0.05 + 500*0.40) / 1e6
        assert row.cost_usd == pytest.approx((1_000 * 0.05 + 500 * 0.40) / 1_000_000)


def test_ask_prices_server_reported_snapshot_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthesizer audit path prices response.model, which a serving
    endpoint may report as a dated snapshot id — not the alias the app
    configured. Cost must resolve, not stay NULL (review fix)."""
    from regwatch.generate import grounded_qa as qa_mod
    from regwatch.store.db import session_scope
    from regwatch.store.models import QueryLog

    _set_prices(monkeypatch, '{"gpt-5.4-nano": {"input": 0.05, "output": 0.40}}')
    _seed_corpus()
    stub = _StubLLM(
        _ANSWER_TURN,
        LLMUsage(input_tokens=1_000, output_tokens=500),
        model="gpt-5.4-nano-2026-01-15",  # a server-echoed dated snapshot id
    )
    monkeypatch.setattr(qa_mod, "get_llm_provider", lambda *a, **k: stub)

    result = qa_mod.ask("What study design is recommended for albuterol sulfate?")
    assert not result.refused

    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.cost_usd == pytest.approx((1_000 * 0.05 + 500 * 0.40) / 1_000_000)


def test_guidance_refusal_records_echo_usage() -> None:
    """A pre-synthesis guidance turn records the provider's real zero usage."""
    from regwatch.generate import grounded_qa as qa_mod
    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import QueryLog

    init_db()  # empty corpus -> no_product guidance instead of synthesis
    result = qa_mod.ask("What does the FDA recommend for romidepsin?")
    assert result.refused
    with session_scope() as s:
        row = s.get(QueryLog, result.audit_id)
        assert row is not None
        assert row.input_tokens == 0
        assert row.output_tokens == 0
        assert row.cost_usd is None


# ---------- migration 0008: both the upgrade-replay and the create_all path ----------


def _empty_schema() -> None:
    """Blank slate for migration-replay tests: drop everything, fresh engine.

    The conftest self-heal rebuilds the bootstrapped schema for whichever
    test runs next.
    """
    from sqlalchemy import text

    from regwatch.store import db as db_module

    with db_module.get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    db_module.reset_for_tests()


def test_fresh_bootstrap_has_token_columns_and_feedback_table() -> None:
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

    _empty_schema()
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

    _empty_schema()
    cfg = db_module._alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007_chat_session_user_updated")
    db_module.get_engine().dispose()
    inspector = inspect(db_module.get_engine())
    assert "answer_feedback" not in inspector.get_table_names()
    assert "input_tokens" not in {c["name"] for c in inspector.get_columns("query_log")}


def test_migration_schema_matches_model_metadata() -> None:
    """The alembic upgrade-replay path and the create_all path must agree.

    A DEPLOYED Postgres reaches head via `alembic upgrade` (the Fly
    release_command); a FRESH one via create_all + stamp (init_db). The
    answer_feedback/query_log shape declared in models.py IS the fresh-boot
    schema, so replaying the full migration history must produce the same
    columns and constraints -- otherwise the two bootstrap routes diverge.
    """
    from alembic import command
    from sqlalchemy import inspect

    from regwatch.store import db as db_module
    from regwatch.store.db import get_engine
    from regwatch.store.models import AnswerFeedback, QueryLog

    _empty_schema()
    command.upgrade(db_module._alembic_config(), "head")
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
