"""Cross-service contract-test harness (R1, step-5 PR A).

Unlike tests/ (in-process TestClient), this suite composes the REAL stack:
the compiled Go proxy binary (go/cmd/proxy) on the public edge, the real
uvicorn app (``regwatch serve``) behind it, and a disposable Postgres shared
by both runtimes. Tests drive HTTP through the Go edge and assert both the
wire responses AND the direct-Postgres state (query_log / chat_message /
auth_session), because these are the INV-6 compliance pins that must survive
the step-5 CompleteQuery cutover (docs/POLYGLOT_TARGET_2026-07-10.md, R1).

Scenario matrix (SNN test names):
  S1-S2, S21b      test_edge_proof.py             edge-proof + login mint
  S3-S5, S28       test_query_auth.py             pre-work 401/404/422, unaudited
  S6-S13, S30      test_query_outcomes.py         outcome golden rows; S30 = INV-5
                                                  filters whitelist at the edge
  S14-S16, S24-S27 test_query_failure_audit.py    failure -> defined audit trail
  S17-S18, S29,    test_sessions_cross_runtime.py session contract; S29 = NULL-owner
  S32                                             legacy-session adoption via /query;
                                                  S32 = chat_session.origin written by
                                                  both writers, filtered on the list
  S19-S21a, S23    test_query_stream.py           /query/stream frame grammar
  S31, S31b        test_query_stream.py           live-draft frame grammar
  (relay parity)   test_query_relay_parity.py     GO_NATIVE_QUERY=false smoke
Deletion-PR hardenings (docs/STEP5_INV_TEST_MAPPING.md gap list): S5 also pins
owner-preservation on a hijack (GAP-4) and S18 the second-user fresh rate-limit
budget (GAP-5).

Five stack flavors exist because the scenario matrix needs boot-time env
differences (settings are lru_cached in the app): base, low_score
(REFUSAL_SCORE_THRESHOLD=1.0), dead_provider (real openai SDK pointed at a
reserved closed port), rate_limited (RATE_LIMIT_PER_MINUTE=1), forced_refusal
(REGWATCH_ECHO_FORCE_REFUSAL=1: echo emits a NO_EVIDENCE turn, making the
synthesis-time model decline wire-reachable). Each flavor is its own uvicorn +
proxy pair; all share the one Postgres. Stacks boot lazily and live for the
session.

Isolation: schema bootstrapped once per session via a ``regwatch init-db``
subprocess run from a TEMP cwd -- pydantic-settings loads ``.env`` relative
to the child's cwd, and the repo .env carries the LIVE prod DATABASE_URL, so
every child (init-db, uvicorn) runs with cwd=tmp AND a fully explicit env.
Per test, one TRUNCATE over all public tables except alembic_version (the
same statement shape as tests/conftest.py). Postgres triggers installed by
failure tests survive TRUNCATE, so those tests drop them in try/finally.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import make_url

_TEST_DB_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
# Same structural guard as tests/conftest.py: the host .env carries the LIVE
# prod Supabase URL, and this suite truncates every table -- refuse anything
# that could be remote.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Two concurrent runs of this suite against one database TRUNCATE-corrupt each
# other (observed live: a parallel lane's run made S14 fail two different ways
# while both runs stayed individually correct). A session advisory lock held on
# a dedicated connection for the whole run makes the second run fail LOUD
# instead. The Go suites hold 721001 for the same reason
# (go/internal/store/store_test.go); 721002 keeps the suites disjoint so a Go
# test run never blocks this one.
_RUN_LOCK_KEY = 721002
_run_lock_conn: Any | None = None

DEFAULT_PASSWORD = "correct-horse-battery-staple"

# Wire literals the Go rewrite must keep emitting byte-for-byte. Hardcoded
# (not imported from config.settings or the guidance renderer) on purpose: a
# contract test that reads a constant from the implementation can never catch
# the implementation changing it. Sources: config/settings.py refusal_text,
# src/regwatch/generate/guidance.py render_guidance_message, and
# src/regwatch/generate/grounded_qa.py _SERVICE_UNAVAILABLE_TEXT (the \u2014
# escape is that constant's em-dash, kept as an escape so this file stays
# ASCII-only).
REFUSAL_TEXT = (
    "I couldn't find this in the current FDA guidance corpus, "
    "and I won't guess on a regulatory question."
)
# Retired from the live vocabulary by the audit #1715 change: an unresolved
# product now converses (need_product / product_not_covered) instead of
# refusing. Kept because turns persisted before that still carry it.
NO_PRODUCT_GUIDANCE_TEXT = (
    "I couldn't identify the product confidently enough to search the right FDA "
    "guidance. What generic ingredient should I use?"
)
NEED_PRODUCT_GUIDANCE_TEXT = "Sure — which product are you asking about?"
LOW_SCORE_GUIDANCE_TEXT = (
    "I found Albuterol Sulfate, but I couldn't verify that answer from the current FDA "
    "passages. Can you narrow the question to study design, strengths, dissolution, "
    "or dosage form?"
)
SERVICE_UNAVAILABLE_TEXT = (
    "The answer service is temporarily unavailable. Your question was "
    "not answered \u2014 please try again in a moment."
)

# Shared secret the harness sets on BOTH the uvicorn app (the compute endpoint's
# X-Internal-Token guard) and the Go proxy (the ragclient), so the native
# /query -> POST /internal/query/compute call authenticates. Any fixed value
# works; the endpoint fail-closes (404) when it is unset/mismatched.
_INTERNAL_RAG_TOKEN = "contract-internal-rag-token"  # test-only, not a real secret

# Shape pins (key SETS, not just parse-ability) -- a dropped or renamed key in
# the Go rewrite would pass any status-code test and break clients silently.
QUERY_RESPONSE_KEYS = frozenset(
    {
        "answer",
        "citations",
        "refused",
        "model_name",
        "audit_id",
        "session_id",
        "turn_id",
        "status",
        "reason",
        "interpretation",
        "clarify",
        "related",
        # Null on every turn except a /query/stream turn whose provisional
        # draft the gate withdrew (2026-08-10 live-draft amendment). Rides the
        # shared Python serializer, so BOTH the Go-native RawMessage
        # passthrough and the relay path carry it identically.
        "draft_withdrawn",
    }
)
CITATION_KEYS = frozenset(
    {
        "short_name",
        "page",
        "chunk_id",
        "doc_id",
        "version_id",
        "source_url",
        "snippet",
        "score",
        "recommended_date",
        "diff_summary",
        # Human-identifying provenance (audit #1716). Additive and optional:
        # "PSG_020911" is an FDA application number and names nothing a reader
        # can act on, so the client renders the product instead and falls back
        # to short_name when these are absent on a legacy row.
        "product_name",
        "dosage_form",
        "route",
        "psg_type",
    }
)
RETRIEVED_ITEM_KEYS = frozenset(
    {"chunk_id", "score", "doc_id", "version_id", "page", "normalized_name", "short_name"}
)
# "retrieval" is the stage-1 search ledger and rides on EVERY route, from both
# producers (grounded_qa._route_json and Go's errorRouteJSON). It is empty when
# the turn declined before search ran and populated when it ran, so "did stage-1
# happen" is asserted as a VALUE, never as key presence -- multi_form declines on
# both sides of retrieve(), so no reason-keyed rule could express it without
# encoding a false invariant.
ROUTE_JSON_KEYS = frozenset(
    {"route", "filters", "reason", "context_applied", "response_mode", "retrieval"}
)
# Healthy pre-synthesis non-answer routes carry a constrained router-model
# ledger. The model selects only an allowlisted next step and existing option
# ids; it cannot write display prose or alter status, filters, or citations.
GUIDED_ROUTE_JSON_KEYS = ROUTE_JSON_KEYS | frozenset({"prompt", "guidance"})
# "turn" is the turn_gate ledger (what the synthesizer emitted, what was
# admitted, what was dropped and why). It rides on every route that reached the
# claim gate: the answer path AND the post-gate declines (model_refusal,
# no_valid_citations, material_drop, audit_error). These routes do not also run
# guidance: every healthy turn gets exactly one model path.
#
# "synthesis" is the synthesis-call telemetry (max_output_tokens, retry,
# truncation class). It rides alongside "turn" on every route that reached the
# synthesizer, and is what separates a malformed_structure caused by the token
# cap from one caused by a JSON error.
ANSWER_ROUTE_JSON_KEYS = ROUTE_JSON_KEYS | frozenset(
    {"prompt", "partial_evidence", "turn", "synthesis"}
)

# The full status vocabulary (src/regwatch/generate/rag_contract.py).
QUERY_STATUSES = frozenset(
    {"answer", "summary", "clarify", "scope_warning", "meta", "refused", "error"}
)

# The proven answerable seed at the DEFAULT 0.30 threshold, ported verbatim
# from tests/test_provider_failure.py::_seed_corpus. The question resolves via
# the single-product-corpus fallback (retrieve/resolver.py) and both chunks
# clear 0.30 in echo space -- empirically verified end-to-end.
ANSWERABLE_QUESTION = "What study design is recommended?"
_ANSWERABLE_TEXTS = [
    "Fasting bioequivalence study with 36 subjects.",
    "Dissolution: USP Apparatus 2 at 50 rpm.",
]
_ANSWERABLE_BASE_META = {
    "doc_id": 1,
    "version_id": 10,
    "section_path": "II.A",
    "normalized_name": "albuterol sulfate",
    "dosage_form": "Aerosol, Metered",
    "route": "Inhalation",
    "source_url": "http://example/PSG_020503.pdf",
    "psg_type": "draft",
    "appl_no": "020503",
}

# Multi-form seed ported verbatim from tests/test_multiform_clarify.py
# (_MULTIFORM + _seed): one normalized_name, two (dosage_form, route) combos.
MULTIFORM_QUESTION = "What bioequivalence study design does FDA recommend for estradiol?"
_MULTIFORM_ROWS = [
    ("estradiol transdermal gel BE study guidance", "020001", "estradiol", "Gel", "Transdermal", 1),
    ("estradiol vaginal tablet BE study guidance", "020002", "estradiol", "Tablet", "Vaginal", 1),
]

# Names no corpus ever contains -> deterministic no_product refusal.
ABSENT_DRUG_QUESTION = "What bioequivalence studies are recommended for zorbifexol?"


def pytest_configure(config: pytest.Config) -> None:
    if not _TEST_DB_URL:
        raise pytest.UsageError(
            "TEST_DATABASE_URL is not set. The contract suite needs a DISPOSABLE "
            "local Postgres database with the pgvector extension available, e.g.\n"
            "  TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5499/regwatch_contract_test "
            "uv run pytest tests_contract\n"
            "(The database's contents are DESTROYED.)"
        )
    url = make_url(_TEST_DB_URL)
    host = (url.host or "").strip("[]").lower()
    if host not in _LOCAL_HOSTS:
        raise pytest.UsageError(
            f"TEST_DATABASE_URL host {host!r} is not local ({sorted(_LOCAL_HOSTS)}). "
            "The contract suite TRUNCATES every table -- refusing to run against "
            "anything that could be a real database."
        )
    # libpq/psycopg gives ?host=/?hostaddr= query params precedence over the
    # netloc, so a crafted local-netloc URL would pass the host check above yet
    # dial a remote server. Reject those keys outright (same guard, same
    # reason, in tests/conftest.py -- keep the two in sync).
    if any(key.lower() in ("host", "hostaddr") for key in url.query):
        raise pytest.UsageError(
            "TEST_DATABASE_URL must not carry 'host' or 'hostaddr' query "
            "parameters: libpq gives them precedence over the URL's netloc, "
            "which would bypass the local-host guard."
        )
    _acquire_run_lock()
    # The test process imports regwatch LIBRARY code for corpus seeding only
    # (vector_store.add_chunks + SQLModel sessions -- never the FastAPI app).
    # pydantic-settings reads .env from the cwd (the repo root under pytest),
    # so every leakable field is pinned here BEFORE any regwatch import.
    seed_tmp = Path(tempfile.mkdtemp(prefix="regwatch-contract-seed-"))
    os.environ.update(
        {
            "DATABASE_URL": _TEST_DB_URL,
            "EMBEDDING_PROVIDER": "echo",
            "LLM_PROVIDER": "echo",
            "REGWATCH_ALLOW_TEST_PROVIDERS": "1",
            "RATE_LIMIT_PER_MINUTE": "0",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "REGWATCH_RETRIEVAL_CORPUS": "legacy",
            "SENTRY_DSN": "",
            "SENTRY_ENVIRONMENT": "dev",
            # Unpinned secret-bearing fields would still load from the repo
            # .env into this process's in-memory Settings (latent exposure to
            # a future repr-in-error path) -- pin them off. "" means unset for
            # both (METRICS_TOKEN's validator normalizes "" to None).
            "METRICS_TOKEN": "",
            "WHITEPAPER_TEMPLATE_URL": "",
            "PGTZ": "UTC",
            "DATA_DIR": str(seed_tmp),
            "RAW_PDF_DIR": str(seed_tmp / "raw"),
            "PROCESSED_DIR": str(seed_tmp / "processed"),
        }
    )
    import config.settings as cs

    cs.get_settings.cache_clear()
    cs.settings = cs.get_settings()


def _acquire_run_lock() -> None:
    """Take the exclusive per-database run lock (see _RUN_LOCK_KEY) or die.

    autocommit so the dedicated connection never sits idle-in-transaction for
    the session (the Jun-18 incident class); a session advisory lock is held
    until the connection closes in pytest_unconfigure -- every exit path,
    including crashes, releases it because the OS closes the socket.
    """
    global _run_lock_conn
    import psycopg

    conn = psycopg.connect(_TEST_DB_URL, connect_timeout=10, autocommit=True)
    try:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (_RUN_LOCK_KEY,)).fetchone()
        if locked is not None and locked[0]:
            _run_lock_conn = conn
            return
        holder = conn.execute(
            "SELECT l.pid, a.application_name, a.backend_start "
            "FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'advisory' AND l.objid = %s AND l.granted",
            (_RUN_LOCK_KEY,),
        ).fetchone()
    except BaseException:
        conn.close()
        raise
    conn.close()
    detail = ""
    if holder is not None:
        detail = f" The other run is backend pid {holder[0]} (started {holder[2]})."
    raise pytest.UsageError(
        f"another contract-suite run already holds advisory lock {_RUN_LOCK_KEY} "
        f"on this database.{detail} Two concurrent runs TRUNCATE-corrupt each "
        "other -- wait for the other run to finish or point TEST_DATABASE_URL "
        "at a different disposable database."
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    # Closing the dedicated connection is what releases the session advisory
    # lock -- no explicit unlock, so the release can never be skipped.
    global _run_lock_conn
    if _run_lock_conn is not None:
        _run_lock_conn.close()
        _run_lock_conn = None


# ---------------------------------------------------------------------------
# Direct-Postgres helpers (psycopg, never through either runtime)
# ---------------------------------------------------------------------------


@contextmanager
def pg_conn() -> Iterator[Any]:
    """A bounded direct connection for seeding users / asserting DB state."""
    import psycopg

    conn = psycopg.connect(_TEST_DB_URL, connect_timeout=10)
    try:
        yield conn
    finally:
        conn.close()


def query_log_count() -> int:
    with pg_conn() as conn:
        row = conn.execute("SELECT count(*) FROM public.query_log").fetchone()
        return int(row[0])


def query_log_row(audit_id: int) -> dict[str, Any]:
    """The full 16-column query_log row, as a dict, for column-level pins."""
    cols = (
        "id, ts, session_id, turn_id, user_id, mode, query_text, retrieved_json, "
        "answer_text, citations_json, refused, status, route_json, model_name, "
        "input_tokens, output_tokens, cost_usd"
    )
    with pg_conn() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM public.query_log WHERE id = %s", (audit_id,)
        ).fetchone()
        assert row is not None, f"no query_log row with id {audit_id}"
        return dict(zip([c.strip() for c in cols.split(",")], row, strict=True))


def latest_query_log_row() -> dict[str, Any]:
    with pg_conn() as conn:
        row = conn.execute("SELECT max(id) FROM public.query_log").fetchone()
        assert row is not None and row[0] is not None, "query_log is empty"
        return query_log_row(int(row[0]))


def chat_messages_for_turn(turn_id: str) -> list[dict[str, Any]]:
    with pg_conn() as conn:
        rows = conn.execute(
            "SELECT role, audit_id, status, citations_json FROM public.chat_message "
            "WHERE turn_id = %s ORDER BY created_at",
            (turn_id,),
        ).fetchall()
    return [{"role": r[0], "audit_id": r[1], "status": r[2], "citations_json": r[3]} for r in rows]


def chat_message_count(session_id: str) -> int:
    with pg_conn() as conn:
        row = conn.execute(
            "SELECT count(*) FROM public.chat_message WHERE session_id = %s", (session_id,)
        ).fetchone()
        return int(row[0])


@contextmanager
def audit_boom_trigger(when: str | None = None) -> Iterator[None]:
    """Install a RAISE trigger on query_log; ALWAYS dropped on exit.

    ``when`` is an optional trigger WHEN clause body (e.g. "NEW.refused = false"
    to fail only the strict answer-path write while the fallback error row
    commits). TRUNCATE does not remove triggers, so the drop in the finally is
    what protects every later test.
    """
    when_sql = f"WHEN ({when}) " if when else ""
    with pg_conn() as conn:
        conn.execute(
            "CREATE FUNCTION contract_audit_boom() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'contract-harness simulated audit outage'; END "
            "$$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER contract_audit_boom_trg BEFORE INSERT ON public.query_log "
            f"FOR EACH ROW {when_sql}EXECUTE FUNCTION contract_audit_boom()"
        )
        conn.commit()
    try:
        yield
    finally:
        with pg_conn() as conn:
            conn.execute("DROP TRIGGER IF EXISTS contract_audit_boom_trg ON public.query_log")
            conn.execute("DROP FUNCTION IF EXISTS contract_audit_boom()")
            conn.commit()


# ---------------------------------------------------------------------------
# Corpus seeding (regwatch library in-process; never the FastAPI app)
# ---------------------------------------------------------------------------


def seed_answerable_corpus() -> None:
    """The proven single-product answer seed (tests/test_provider_failure.py)."""
    from regwatch.process.embedder import get_embedding_provider
    from regwatch.store.db import init_db
    from regwatch.store.vector_store import add_chunks

    init_db()
    embedder = get_embedding_provider()
    add_chunks(
        ids=["chunk-0", "chunk-1"],
        embeddings=embedder.embed(_ANSWERABLE_TEXTS),
        documents=_ANSWERABLE_TEXTS,
        metadatas=[dict(_ANSWERABLE_BASE_META, page=3), dict(_ANSWERABLE_BASE_META, page=4)],
    )


def seed_multiform_corpus() -> None:
    """SQL catalog (psg_document + psg_version) plus chunks with consistent ids
    (tests/test_multiform_clarify.py::_seed) so the pre-retrieval multi-form
    guard sees two combos for one product."""
    from regwatch.process.embedder import get_embedding_provider
    from regwatch.store.db import init_db, session_scope
    from regwatch.store.models import PsgDocument, PsgVersion
    from regwatch.store.vector_store import add_chunks

    init_db()
    doc_ids: dict[str, int] = {}
    version_ids: dict[str, int] = {}
    with session_scope() as s:
        for _text, appl, name, form, route, _page in _MULTIFORM_ROWS:
            doc = PsgDocument(
                active_ingredient=name.title(),
                normalized_name=name,
                dosage_form=form,
                route=route,
                appl_no=appl,
                psg_type="draft",
                recommended_date="2026-01-01",
                source_url=f"http://example/PSG_{appl}.pdf",
                content_hash=f"hash-{appl}",
            )
            s.add(doc)
            s.flush()
            assert doc.id is not None
            ver = PsgVersion(psg_document_id=doc.id, content_hash=f"hash-{appl}")
            s.add(ver)
            s.flush()
            assert ver.id is not None
            doc_ids[appl] = doc.id
            version_ids[appl] = ver.id

    embedder = get_embedding_provider()
    texts = [t for t, *_ in _MULTIFORM_ROWS]
    add_chunks(
        ids=[f"{appl}-{page}" for _, appl, _, _, _, page in _MULTIFORM_ROWS],
        embeddings=embedder.embed(texts),
        documents=texts,
        metadatas=[
            {
                "doc_id": doc_ids[appl],
                "version_id": version_ids[appl],
                "page": page,
                "normalized_name": name,
                "appl_no": appl,
                "source_url": f"http://example/PSG_{appl}.pdf",
                "section_path": "",
                "dosage_form": form,
                "route": route,
                "psg_type": "draft",
            }
            for _text, appl, name, form, route, page in _MULTIFORM_ROWS
        ],
    )


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


@dataclass
class SseBody:
    """Parsed event-stream: named frames in order, plus tolerated comments.

    Comment frames (``: keep-alive``) are LEGAL anywhere before the result and
    carry no event name -- the parser counts them but never asserts presence
    or cadence (S22 / R3 pass-through-era scoping).
    """

    frames: list[tuple[str, str]] = field(default_factory=list)
    comment_count: int = 0

    def events(self) -> list[str]:
        return [e for e, _ in self.frames]

    def data_for(self, event: str) -> list[str]:
        return [d for e, d in self.frames if e == event]

    def single_result(self) -> dict[str, Any]:
        results = self.data_for("result")
        assert len(results) == 1, f"expected exactly one result frame, got {len(results)}"
        assert self.frames[-1][0] == "result", f"result is not last: {self.events()}"
        payload = json.loads(results[0])
        assert isinstance(payload, dict)
        return payload


def parse_sse(body: str) -> SseBody:
    parsed = SseBody()
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data_lines: list[str] = []
        is_comment = False
        for line in block.split("\n"):
            if line.startswith(":"):
                is_comment = True
            elif line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].removeprefix(" "))
        if event is not None:
            parsed.frames.append((event, "\n".join(data_lines)))
        elif is_comment:
            parsed.comment_count += 1
    return parsed


# ---------------------------------------------------------------------------
# The composed stack
# ---------------------------------------------------------------------------

# Boot-time env deltas per flavor (settings are lru_cached in the app, so
# these can never be per-request). The dead-provider base URL is filled in at
# harness init with a port reserved-as-closed.
_FLAVOR_OVERRIDES: dict[str, dict[str, str]] = {
    "base": {},
    # Exactly 1.0, never above: the settings validator rejects >1.0. A question
    # that is not verbatim-identical to a seeded chunk scores < 1.0, so every
    # retrieval refuses with low_top_score while resolution still succeeds.
    "low_score": {"REFUSAL_SCORE_THRESHOLD": "1.0"},
    "dead_provider": {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test-dead"},
    # LANDMINE: TRUNCATE ... RESTART IDENTITY reuses user id 1 for every test's
    # first seeded user while the in-app query limiter (keyed "user:{id}") lives
    # for the uvicorn SESSION -- so only ONE test may use this flavor per
    # minute; a second would 429 on its very first call.
    "rate_limited": {"RATE_LIMIT_PER_MINUTE": "1"},
    # Echo emits a NO_EVIDENCE turn instead of an ANSWER turn -- the only way a
    # wire scenario can reach the synthesis-time model-decline branch, and so
    # the only way to prove over the wire that a decline replays ZERO token
    # frames (S23). Fenced from prod by the REGWATCH_ALLOW_TEST_PROVIDERS boot
    # guard like echo itself.
    "forced_refusal": {"REGWATCH_ECHO_FORCE_REFUSAL": "1"},
    # S24: force an UNEXPECTED raise inside retrieve() (fenced by the same
    # allow-test-providers guard) so the step-5 compute_turn audited-error
    # boundary must turn it into exactly one status="error"/pipeline_error row.
    "fault_retrieve": {"REGWATCH_FAULT_INJECT": "retrieve"},
    # S25: uvicorn is a healthy base, but the proxy's INTERNAL_RAG_URL is
    # pointed at the reserved closed port (in _boot), so native /query cannot
    # reach the compute endpoint and Go synthesizes an upstream_error row.
    "dead_internal": {},
    # S27: force the ask-pool saturation shed (main.py
    # _shed_if_ask_pool_saturated, fenced by allow-test-providers like the
    # other fault stages) so both the native and relay paths must serve the
    # defined 503 busy contract instead of queueing.
    "saturate": {"REGWATCH_FAULT_INJECT": "saturate"},
    # S31: both server halves of the live-draft dual gate on; echo streams its
    # deterministic two-chunk prose so draft frames are wire-reachable with no
    # external model. Fenced from prod by REGWATCH_ALLOW_TEST_PROVIDERS.
    "live_draft": {"REGWATCH_PROSE_SYNTHESIS": "1", "REGWATCH_LIVE_DRAFT": "1"},
    # S31b: refusal under the live-draft gate (echo emits the prose
    # NO_EVIDENCE sentinel, which _stream_structured's prefix hold must
    # swallow entirely -- a refusal never paints as a draft).
    "live_draft_refusal": {
        "REGWATCH_PROSE_SYNTHESIS": "1",
        "REGWATCH_LIVE_DRAFT": "1",
        "REGWATCH_ECHO_FORCE_REFUSAL": "1",
    },
}

# The relay has NO response timeout by design (a hung upstream would hang an
# unbounded client forever), so every harness HTTP call carries this.
CLIENT_TIMEOUT = 30.0
# The dead-provider flavor eats the openai SDK's 2 connection-refused retries
# (~4s observed; the SDK does not honor a max-retries env var) -- give those
# calls extra headroom without unbounding them.
DEAD_PROVIDER_TIMEOUT = 60.0

# Generous on purpose: the polls below exit as soon as the stack is up, so
# green runs pay nothing -- the headroom only ever buys a slow CI runner (the
# dead_provider flavor's SDK-import boot is ~7s locally, 2-4x that on GH).
_BOOT_DEADLINE_S = 120.0
# Proxy SIGTERM drain is bounded at 20s in-binary; wait a hair longer before
# escalating to SIGKILL.
_SHUTDOWN_WAIT_S = 25.0


@dataclass
class Stack:
    flavor: str
    edge_url: str
    uvicorn_url: str
    uvicorn_proc: subprocess.Popen[bytes]
    proxy_proc: subprocess.Popen[bytes]
    uvicorn_log: Path
    proxy_log: Path
    log_handles: list[Any]


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return f"<unreadable: {exc}>"
    return text[-limit:]


class Harness:
    """Owns the session tmp dir, the proxy binary, the schema bootstrap, and
    the lazily-booted stack cache. One instance per pytest session."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self._stacks: dict[str, Stack] = {}
        self._used_ports: set[int] = set()
        # Reserve a port that is KNOWN closed for the dead-provider flavor:
        # bind, record, close, and never hand it to any listener.
        self.closed_port = self._free_port()
        self._ip_counter = itertools.count(1)
        self._email_counter = itertools.count(1)
        self.proxy_bin = self._resolve_proxy_bin()
        self._init_db()

    # -- infrastructure -----------------------------------------------------

    def _free_port(self) -> int:
        while True:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            if port not in self._used_ports:
                self._used_ports.add(port)
                return port

    def _resolve_proxy_bin(self) -> Path:
        # CI prebuilds the binary and points REGWATCH_PROXY_BIN at it; locally
        # build once per session so the suite stays one command.
        env_bin = (os.environ.get("REGWATCH_PROXY_BIN") or "").strip()
        if env_bin:
            path = Path(env_bin)
            if not path.is_file():
                raise RuntimeError(f"REGWATCH_PROXY_BIN={env_bin!r} does not exist")
            return path
        out = self.tmp / "regwatch-proxy"
        build = subprocess.run(
            ["go", "build", "-trimpath", "-o", str(out), "./cmd/proxy"],
            cwd=_REPO_ROOT / "go",
            capture_output=True,
            timeout=300,
        )
        if build.returncode != 0:
            raise RuntimeError(
                "go build ./cmd/proxy failed:\n"
                + build.stdout.decode(errors="replace")
                + build.stderr.decode(errors="replace")
            )
        return out

    def _child_base_env(self) -> dict[str, str]:
        # Deliberately NOT os.environ.copy(): a developer shell exporting real
        # API keys (or the repo .env re-read by a child) must never reach the
        # composed stack. Only process-mechanics vars pass through.
        env: dict[str, str] = {}
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _uvicorn_env(self, overrides: dict[str, str]) -> dict[str, str]:
        data_dir = self.tmp / "data"
        env = self._child_base_env()
        env.update(
            {
                "DATABASE_URL": _TEST_DB_URL,
                # Force subprocesses to import this checkout. The developer
                # venv is editable and may point at a different worktree whose
                # schema head has advanced independently.
                "PYTHONPATH": os.pathsep.join((str(_REPO_ROOT / "src"), str(_REPO_ROOT))),
                "EMBEDDING_PROVIDER": "echo",
                "LLM_PROVIDER": "echo",
                # The lifespan guard refuses echo providers over a non-empty
                # corpus unless explicitly allowed -- this suite seeds corpora
                # on purpose.
                "REGWATCH_ALLOW_TEST_PROVIDERS": "1",
                "RATE_LIMIT_PER_MINUTE": "0",
                "REFUSAL_SCORE_THRESHOLD": "0.30",
                # The compute endpoint's token guard -- must match the proxy's
                # ragclient token (set on proxy_env in _boot) or every native
                # /query would 404 into a synthesized upstream_error.
                "INTERNAL_RAG_TOKEN": _INTERNAL_RAG_TOKEN,
                # The app process caches the resolver's distinct-product set;
                # this suite seeds from a SEPARATE process, so a near-zero TTL
                # keeps the app reading fresh state each request.
                "METADATA_CACHE_TTL_S": "0.01",
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "REGWATCH_RETRIEVAL_CORPUS": "legacy",
                "SENTRY_DSN": "",
                "SENTRY_ENVIRONMENT": "dev",
                "AUTH_COOKIE_SECURE": "false",
                "PGTZ": "UTC",
                "DATA_DIR": str(data_dir),
                "RAW_PDF_DIR": str(data_dir / "raw"),
                "PROCESSED_DIR": str(data_dir / "processed"),
                # The schema was bootstrapped by the session's init-db child;
                # the app then only re-verifies (prod entrypoint parity).
                "REGWATCH_DB_INITIALIZED": "1",
            }
        )
        env.update(overrides)
        return env

    def _init_db(self) -> None:
        # The canonical fresh-PG path, as a subprocess with cwd=tmp: alembic
        # config resolution is __file__-relative (store/db.py), so the child
        # never needs the repo cwd and therefore never sees the repo .env.
        # Launch by module under an explicit worktree PYTHONPATH. The venv's
        # generated console script belongs to whichever editable checkout last
        # installed it and is therefore unsafe in a parallel worktree.
        self._regwatch_command = (sys.executable, "-m", "regwatch.cli")
        env = self._uvicorn_env({})
        env.pop("REGWATCH_DB_INITIALIZED", None)
        result = subprocess.run(
            [*self._regwatch_command, "init-db"],
            cwd=self.tmp,
            env=env,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "regwatch init-db failed:\n"
                + result.stdout.decode(errors="replace")
                + result.stderr.decode(errors="replace")
            )

    # -- stacks -------------------------------------------------------------

    def stack(self, flavor: str, *, native: bool = True) -> Stack:
        # native=True (default) makes the proxy serve POST /query itself (the
        # step-5 cutover under test); native=False relays it to Python (the
        # Phase-0 / rollback path). Cached per (flavor, native) so both can boot.
        key = f"{flavor}:native" if native else f"{flavor}:relay"
        if key not in self._stacks:
            self._stacks[key] = self._boot(flavor, native=native)
        return self._stacks[key]

    def _boot(self, flavor: str, *, native: bool = True) -> Stack:
        overrides = dict(_FLAVOR_OVERRIDES[flavor])
        if flavor == "dead_provider":
            # Connection-refused on a reserved closed port: deterministic,
            # network-free provider death. The SDK honors OPENAI_BASE_URL from
            # env (verified for the pinned openai release).
            overrides["OPENAI_BASE_URL"] = f"http://127.0.0.1:{self.closed_port}"

        uvicorn_port = self._free_port()
        edge_port = self._free_port()
        uvicorn_log = self.tmp / f"uvicorn-{flavor}.log"
        proxy_log = self.tmp / f"proxy-{flavor}.log"
        uv_env = self._uvicorn_env(overrides)

        proxy_env = self._child_base_env()
        proxy_env.update(
            {
                "PORT": str(edge_port),
                "UPSTREAM_URL": f"http://127.0.0.1:{uvicorn_port}",
                # Without DATABASE_URL the proxy silently disables ALL native
                # routes and login 404s; REQUIRE makes a wiring bug fail boot
                # loudly instead.
                "DATABASE_URL": _TEST_DB_URL,
                "REQUIRE_DATABASE_URL": "true",
                # Prod parity, and the per-test Fly-Client-IP header trick
                # (fresh login rate-limit buckets) depends on it.
                "TRUST_PROXY_HEADERS": "true",
                # GET /settings is served natively by Go from its own env
                # mirror -- keep the two runtimes agreeing per flavor.
                "EMBEDDING_PROVIDER": uv_env["EMBEDDING_PROVIDER"],
                "LLM_PROVIDER": uv_env["LLM_PROVIDER"],
                # -- step-5 CompleteQuery --
                # Native /query cutover flag (the thing under test).
                "GO_NATIVE_QUERY": "true" if native else "false",
                # ragclient token: MUST match the uvicorn INTERNAL_RAG_TOKEN, or
                # the compute endpoint 404s and every turn upstream_errors.
                "INTERNAL_RAG_TOKEN": _INTERNAL_RAG_TOKEN,
                # Go is now the single rate-limit authority; mirror the flavor's
                # uvicorn value (base default 0 = unlimited, rate_limited = 1).
                "RATE_LIMIT_PER_MINUTE": overrides.get("RATE_LIMIT_PER_MINUTE", "0"),
            }
        )
        if flavor == "dead_internal":
            # Point the ragclient at the reserved CLOSED port: the compute call
            # is refused, so native /query synthesizes an upstream_error (S25).
            proxy_env["INTERNAL_RAG_URL"] = f"http://127.0.0.1:{self.closed_port}"

        # Everything from the FIRST resource on is inside one BaseException
        # guard: a raise anywhere in the boot window (the second log open, a
        # Popen exec failure, a KeyboardInterrupt) used to leak the
        # already-started uvicorn child forever -- it had not reached _stacks,
        # so session shutdown could never see it.
        handles: list[Any] = []
        procs: list[subprocess.Popen[bytes]] = []
        try:
            uv_handle = uvicorn_log.open("wb")
            handles.append(uv_handle)
            proxy_handle = proxy_log.open("wb")
            handles.append(proxy_handle)
            uvicorn_proc = subprocess.Popen(
                [*self._regwatch_command, "serve", "--port", str(uvicorn_port)],
                cwd=self.tmp,
                env=uv_env,
                stdout=uv_handle,
                stderr=subprocess.STDOUT,
            )
            procs.append(uvicorn_proc)
            proxy_proc = subprocess.Popen(
                [str(self.proxy_bin)],
                cwd=self.tmp,
                env=proxy_env,
                stdout=proxy_handle,
                stderr=subprocess.STDOUT,
            )
            procs.append(proxy_proc)
            stack = Stack(
                flavor=flavor,
                edge_url=f"http://127.0.0.1:{edge_port}",
                uvicorn_url=f"http://127.0.0.1:{uvicorn_port}",
                uvicorn_proc=uvicorn_proc,
                proxy_proc=proxy_proc,
                uvicorn_log=uvicorn_log,
                proxy_log=proxy_log,
                log_handles=handles,
            )
            self._wait_healthy(stack)
        except BaseException:
            self._stop_procs(procs, handles)
            raise
        return stack

    def _wait_healthy(self, stack: Stack) -> None:
        import httpx

        deadline = time.monotonic() + _BOOT_DEADLINE_S

        def _poll(url: str, want: int) -> None:
            while True:
                if stack.uvicorn_proc.poll() is not None or stack.proxy_proc.poll() is not None:
                    self._boot_failure(stack, f"a child exited while waiting for {url}")
                try:
                    if httpx.get(url, timeout=2.0).status_code == want:
                        return
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    self._boot_failure(stack, f"timed out waiting for {want} from {url}")
                time.sleep(0.1)

        # Go-local liveness first (fast, DB/upstream-independent), then the
        # end-to-end edge -> relay -> uvicorn -> DB readiness signal, the same
        # semantics as prod's health check.
        _poll(f"{stack.edge_url}/healthz", 200)
        _poll(f"{stack.edge_url}/health", 200)

    def _boot_failure(self, stack: Stack, reason: str) -> None:
        raise RuntimeError(
            f"stack {stack.flavor!r} failed to become healthy: {reason}\n"
            f"--- uvicorn log tail ({stack.uvicorn_log}) ---\n{_log_tail(stack.uvicorn_log)}\n"
            f"--- proxy log tail ({stack.proxy_log}) ---\n{_log_tail(stack.proxy_log)}"
        )

    def _stop_procs(self, procs: list[subprocess.Popen[bytes]], handles: list[Any]) -> None:
        """Stop whatever subset of a stack's children exists; shared by the
        normal teardown and the mid-_boot failure path (which may hold only
        some of the resources)."""
        try:
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
            for proc in procs:
                try:
                    proc.wait(timeout=_SHUTDOWN_WAIT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            for handle in handles:
                handle.close()

    def _stop_stack(self, stack: Stack) -> None:
        self._stop_procs([stack.proxy_proc, stack.uvicorn_proc], stack.log_handles)

    def shutdown(self) -> None:
        for stack in self._stacks.values():
            self._stop_stack(stack)
        self._stacks.clear()

    # -- auth ---------------------------------------------------------------

    def next_email(self) -> str:
        return f"analyst-{next(self._email_counter)}@contract.example"

    def next_client_ip(self) -> str:
        # A distinct Fly-Client-IP per login mints an independent per-IP
        # rate-limit bucket in the proxy (30/min per IP, compile-time), so the
        # suite can never trip it however many logins a run performs.
        n = next(self._ip_counter)
        return f"10.9.{n // 250}.{n % 250 + 1}"

    def seed_user(self, email: str, password: str = DEFAULT_PASSWORD) -> int:
        import bcrypt

        # Cost 4 for speed; Go's CompareHashAndPassword verifies any cost.
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode()
        with pg_conn() as conn:
            row = conn.execute(
                'INSERT INTO public."user" '
                "(email, password_hash, display_name, role, is_active, created_at) "
                "VALUES (%s, %s, 'Contract Analyst', 'analyst', true, now()) RETURNING id",
                (email.lower(), pw_hash),
            ).fetchone()
            conn.commit()
            return int(row[0])


class EdgeClient:
    """A cookie-authed httpx client bound to one stack's public edge."""

    def __init__(self, client: Any, user_id: int, email: str) -> None:
        self.http = client
        self.user_id = user_id
        self.email = email

    def close(self) -> None:
        self.http.close()


def _login(harness: Harness, stack: Stack, timeout: float) -> EdgeClient:
    import httpx

    email = harness.next_email()
    user_id = harness.seed_user(email)
    response = httpx.post(
        f"{stack.edge_url}/auth/login",
        json={"email": email, "password": DEFAULT_PASSWORD},
        headers={"Fly-Client-IP": harness.next_client_ip()},
        timeout=CLIENT_TIMEOUT,
    )
    assert response.status_code == 200, f"login failed: {response.status_code} {response.text}"
    client = httpx.Client(
        base_url=stack.edge_url,
        cookies={"regwatch_session": response.cookies["regwatch_session"]},
        timeout=timeout,
    )
    return EdgeClient(client, user_id, email)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Harness]:
    instance = Harness(tmp_path_factory.mktemp("stack"))
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def base_stack(harness: Harness) -> Stack:
    return harness.stack("base")


@pytest.fixture
def low_score_stack(harness: Harness) -> Stack:
    return harness.stack("low_score")


@pytest.fixture
def dead_provider_stack(harness: Harness) -> Stack:
    return harness.stack("dead_provider")


@pytest.fixture
def rate_limited_stack(harness: Harness) -> Stack:
    return harness.stack("rate_limited")


@pytest.fixture
def forced_refusal_stack(harness: Harness) -> Stack:
    return harness.stack("forced_refusal")


@pytest.fixture
def fault_retrieve_stack(harness: Harness) -> Stack:
    return harness.stack("fault_retrieve")


@pytest.fixture
def live_draft_stack(harness: Harness) -> Stack:
    return harness.stack("live_draft")


@pytest.fixture
def live_draft_refusal_stack(harness: Harness) -> Stack:
    return harness.stack("live_draft_refusal")


@pytest.fixture
def dead_internal_stack(harness: Harness) -> Stack:
    return harness.stack("dead_internal")


@pytest.fixture
def base_relay_stack(harness: Harness) -> Stack:
    # The Phase-0 / rollback path: /query relayed to Python (GO_NATIVE_QUERY
    # off). Proves the flag-off default still serves the query surface while the
    # native path (every other stack) proves the cutover.
    return harness.stack("base", native=False)


@pytest.fixture(autouse=True)
def _reset_db(harness: Harness) -> Iterator[None]:
    # Same statement shape as tests/conftest.py::_reset_database's fast path:
    # one TRUNCATE over every public table except alembic_version resets all
    # data and sequences for BOTH runtimes' pools. The schema itself is never
    # wrecked by this suite, so no rebuild path is needed.
    with pg_conn() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            ).fetchall()
        ]
        if tables:
            joined = ", ".join(f'public."{t}"' for t in tables)
            conn.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")
        conn.commit()
    # The TEST process's own vector-store handles are reset so seeding helpers
    # never reuse state across truncations.
    from regwatch.store import vector_store as vs_module

    vs_module.reset_for_tests()
    yield


@pytest.fixture
def edge_login(harness: Harness) -> Iterator[Callable[..., EdgeClient]]:
    """Factory: seed a fresh user and log in THROUGH THE EDGE (S2's flow is
    the only mint path -- Python lost its auth surface in step 4). Every
    client is closed on teardown regardless of test outcome."""
    created: list[EdgeClient] = []

    def make(stack: Stack, *, timeout: float = CLIENT_TIMEOUT) -> EdgeClient:
        client = _login(harness, stack, timeout)
        created.append(client)
        return client

    yield make
    for client in created:
        client.close()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
