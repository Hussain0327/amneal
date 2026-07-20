"""Auth verification + session ownership + audit attribution (Python side).

Since the step-4 cutover (docs/POLYGLOT_TARGET_2026-07-10.md) the Go proxy
owns the /auth/* and /sessions* WIRE surface, and its contract tests
(go/internal/api/contract_test.go) carry the assertions that used to live
here: login/logout/me bodies and cookie attributes, the login limiter, the
sessions list/detail/delete shapes. What Python still owns -- and this file
still pins -- is the VERIFY side of the cookie contract (require_user ->
resolve_token guarding every remaining route), session ownership at the
/query boundary, INV-6 attribution, and the query rate limiter.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import select
from typer.testing import CliRunner

from regwatch.api.main import app
from regwatch.auth.deps import SESSION_COOKIE
from regwatch.auth.passwords import verify_password
from regwatch.cli import app as cli_app
from regwatch.common.conversation import SessionOwnershipError, ensure_session
from regwatch.common.ratelimit import RateLimiter
from regwatch.generate.grounded_qa import ask
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import AuthSession, ChatMessage, ChatSession, PsgDocument, QueryLog, User
from tests.conftest import DEFAULT_USER_EMAIL, create_user, session_client


def _anon() -> TestClient:
    c = TestClient(app)
    c.__enter__()  # lifespan -> init_db on the per-test DB
    return c


# ---------- the 401 wall ----------

_PROTECTED: list[tuple[str, str, dict[str, object] | None]] = [
    ("post", "/query", {"question": "what is required?"}),
    ("post", "/sources/search", {"query_text": "x", "sources": ["psg"]}),
    ("post", "/assemble", {"active_ingredient": "albuterol"}),
    ("get", "/watch/latest", None),
    ("get", "/products", None),
    ("post", "/products", {"active_ingredient": "Foo", "source": "manual"}),
    ("get", "/settings", None),
]


@pytest.mark.parametrize(("method", "path", "body"), _PROTECTED)
def test_every_protected_endpoint_requires_auth(
    method: str, path: str, body: dict[str, object] | None
) -> None:
    c = _anon()
    r = (
        c.request(method.upper(), path, json=body)
        if body is not None
        else c.request(method.upper(), path)
    )
    assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"
    assert r.json() == {"detail": "authentication required"}


def test_invalid_session_cookie_rejected() -> None:
    # resolve_token's unknown-token branch: a garbage cookie is the same 401
    # as no cookie (never confirms whether a token was close). The mint side
    # lives in Go; this pins the Python VERIFY side over the wire.
    c = _anon()
    c.cookies.set(SESSION_COOKIE, "not-a-real-token")
    r = c.get("/settings")
    assert r.status_code == 401
    assert r.json() == {"detail": "authentication required"}


def test_expired_session_cookie_rejected_and_purged() -> None:
    # resolve_token's expiry branch: presenting an expired cookie rejects AND
    # deletes exactly that row (stale token hashes have no reason to stay at
    # rest); a live session is untouched. Expiry-at-mint wire behavior is
    # pinned in Go (TestExpiredSessionRejectedAndPurged); this keeps the
    # Python verifier's branch covered against the same rows.
    uid = create_user()
    expired = session_client(uid)
    live = session_client(uid)
    with session_scope() as s:
        rows = s.scalars(select(AuthSession)).all()
        assert len(rows) == 2
        oldest = min(rows, key=lambda r: r.id or 0)
        oldest.expires_at = datetime.now(UTC) - timedelta(hours=1)
        s.add(oldest)

    assert expired.get("/settings").status_code == 401
    with session_scope() as s:
        assert len(s.scalars(select(AuthSession)).all()) == 1  # only the live row
    assert live.get("/settings").status_code == 200

    # And the mint-side sweep (create_session deletes ALL expired rows before
    # inserting -- the same sweep the Go login performs): expire the live row,
    # mint a fresh session, and only the fresh row remains.
    with session_scope() as s:
        for row in s.scalars(select(AuthSession)):
            row.expires_at = datetime.now(UTC) - timedelta(hours=1)
            s.add(row)
    fresh = session_client(uid)
    with session_scope() as s:
        assert len(s.scalars(select(AuthSession)).all()) == 1  # sweep ran
    assert fresh.get("/settings").status_code == 200


def test_health_stays_open() -> None:
    assert _anon().get("/health").status_code == 200


def test_docs_routes_are_disabled() -> None:
    # The auto-docs register at app level -- outside the protected router -- so
    # they are off entirely rather than leaking the API surface (routes,
    # schemas, the session-cookie name) to anonymous visitors.
    c = _anon()
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert c.get(path).status_code == 404, path


# ---------- session ownership ----------


def test_query_rejects_foreign_session_with_404() -> None:
    # The /query half of the old sessions-invisibility contract stays Python:
    # a hijack attempt binds a session another user owns and must read as
    # 404 -- never 403, never confirming the session exists. The GET/DELETE
    # /sessions halves are pinned in Go (TestSessionsOwnershipInvisibility).
    uid_a = create_user("a@example.com", "password-for-a")
    a = session_client(uid_a)
    b = session_client(create_user("b@example.com", "password-for-b"))

    sid = a.post("/query", json={"question": "Does this exist?"}).json()["session_id"]

    hijack = b.post("/query", json={"question": "And dissolution?", "session_id": sid})
    assert hijack.status_code == 404
    assert hijack.json() == {"detail": "session not found"}  # 404, never 403

    # The owner is unaffected and the hijack wrote nothing: only A's single
    # turn (user + assistant) exists, still owned by A.
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.user_id == str(uid_a)
        n = s.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == sid)
        )
    assert int(n or 0) == 2


def test_legacy_null_user_session_adopted_via_query() -> None:
    client = session_client(create_user())
    sid = ensure_session(None)  # legacy demo row: user_id is NULL

    # Unowned == invisible: the Go /sessions handlers select strictly by
    # user_id, so a NULL owner can never match (pinned on the wire by Go's
    # TestSessionsOwnershipInvisibility "legacy-null" probe).
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.user_id is None

    r = client.post("/query", json={"question": "Adopt this session?", "session_id": sid})
    assert r.status_code == 200
    assert r.json()["session_id"] == sid

    # Adopted == owned: user_id equality IS the visibility contract.
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.user_id == str(client.user_id)


def test_lost_ownership_race_aborts_instead_of_writing() -> None:
    """Defense in depth below the API pre-check: a request that loses an
    adoption/creation race binds a session another user now owns -- it must
    abort, never interleave both users' turns in one session."""
    init_db()
    sid = ensure_session(None, user_id="1")
    with pytest.raises(SessionOwnershipError):
        ensure_session(sid, user_id="2")
    with pytest.raises(SessionOwnershipError):
        ask("And dissolution?", session_id=sid, user_id="2")
    with session_scope() as s:
        assert s.scalars(select(ChatMessage)).all() == []  # nothing was written


# ---------- audit attribution (INV-6) ----------


def test_query_log_records_user_id_for_query() -> None:
    user_id = create_user()
    client = session_client(user_id)
    assert client.post("/query", json={"question": "Out of corpus?"}).status_code == 200
    with session_scope() as s:
        attributions = [row.user_id for row in s.scalars(select(QueryLog))]
    assert attributions == [str(user_id)]


def test_query_log_records_user_id_for_assemble() -> None:
    user_id = create_user()
    client = session_client(user_id)
    r = client.post("/assemble", json={"active_ingredient": "Imaginary Drug XYZ"})
    assert r.status_code == 200
    with session_scope() as s:
        attributions = [
            row.user_id for row in s.scalars(select(QueryLog).where(QueryLog.mode == "assemble"))
        ]
    assert attributions == [str(user_id)]


def test_assemble_keeps_internal_qa_out_of_chat_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful assemble attributes BOTH audit rows (INV-6) without binding
    the inner Q&A's bookkeeping session to the caller -- no phantom conversation
    may become visible as the caller's. Visibility is ownership (the Go
    /sessions handlers select strictly by user_id), so the DB-level owner
    assertions below ARE the no-phantom-conversation contract."""
    from regwatch.assemble import dossier as dossier_mod

    monkeypatch.setattr(dossier_mod, "_fetch_rld_label", lambda *a, **k: None)  # network-free
    user_id = create_user()
    client = session_client(user_id)
    with session_scope() as s:
        s.add(
            PsgDocument(
                active_ingredient="Albuterol Sulfate",
                normalized_name="albuterol sulfate",
                dosage_form="Aerosol, Metered",
                route="Inhalation",
                appl_no="020503",
                psg_type="draft",
                recommended_date="2020-01-01",
                source_url="http://example/PSG_020503.pdf",
                content_hash="hash-020503",
            )
        )

    r = client.post("/assemble", json={"active_ingredient": "Albuterol Sulfate"})
    assert r.status_code == 200
    assert r.json()["refused"] is False  # past the early refusal -> the inner ask() ran

    with session_scope() as s:
        owned = s.scalars(select(ChatSession).where(ChatSession.user_id == str(user_id))).all()
        owners = [row.user_id for row in s.scalars(select(ChatSession))]
        modes = {row.mode: row.user_id for row in s.scalars(select(QueryLog))}
    assert owned == []  # nothing the caller's /sessions list would ever show
    assert owners == [None]  # the inner Q&A session stays unowned and invisible
    assert modes == {"assemble": str(user_id), "qa": str(user_id)}  # attribution intact


# ---------- rate limiting ----------


def test_query_and_assemble_rate_limited_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    client = session_client(create_user())
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    cs.get_settings.cache_clear()

    assert client.post("/query", json={"question": "First one?"}).status_code == 200
    # /assemble draws from the same per-user budget.
    assert client.post("/assemble", json={"active_ingredient": "Foo"}).status_code == 200
    r = client.post("/query", json={"question": "Over the limit?"}).status_code
    assert r == 429

    # Another user has their own budget.
    other = session_client(create_user("other@example.com", "other-password"))
    assert other.post("/query", json={"question": "Fresh budget?"}).status_code == 200


def test_rate_limiter_evicts_idle_keys() -> None:
    # Keys can embed caller-supplied identifiers, so idle keys must be swept
    # once their window expires -- not retained for process life. (The login
    # limiter that motivated this moved to Go; the class and its eviction
    # guard still back query_limiter.)
    limiter = RateLimiter(window_s=0.01)
    for i in range(100):
        assert limiter.allow(f"user:spray-{i}", 10)
    time.sleep(0.03)
    assert limiter.allow("user:fresh", 10)
    assert set(limiter._hits) == {"user:fresh"}


# ---------- CLI provisioning ----------


def test_cli_create_user_and_list_users() -> None:
    runner = CliRunner()
    password = "from-the-cli-prompt"
    created = runner.invoke(
        cli_app,
        ["create-user", "Cli.User@Example.com", "--name", "CLI User"],
        input=f"{password}\n{password}\n",
    )
    assert created.exit_code == 0, created.output

    # Stored lowercased, and the prompted password provisions a WORKING bcrypt
    # hash -- verified directly; the HTTP login for a CLI-provisioned user is
    # Go's TestLoginSuccessBodyAndCookie against the same bcrypt scheme.
    with session_scope() as s:
        row = s.scalars(select(User).where(User.email == "cli.user@example.com")).one()
        assert verify_password(password, row.password_hash)

    listing = runner.invoke(cli_app, ["list-users"])
    assert listing.exit_code == 0
    assert "cli.user@example.com" in listing.output
    assert "$2b$" not in listing.output  # bcrypt hashes never printed

    duplicate = runner.invoke(
        cli_app,
        ["create-user", "cli.user@example.com", "--name", "Again"],
        input=f"{password}\n{password}\n",
    )
    assert duplicate.exit_code == 2


def test_cli_deactivate_user_blocks_access() -> None:
    runner = CliRunner()
    client = session_client(create_user())
    assert client.get("/settings").status_code == 200
    result = runner.invoke(cli_app, ["deactivate-user", DEFAULT_USER_EMAIL])
    assert result.exit_code == 0, result.output
    # The CLI deletes the user's session rows in the same transaction as the
    # is_active flip, so this exercises resolve_token's row-gone branch over
    # the wire (the inactive-user branch is pinned separately below). Refusal
    # of NEW logins for a deactivated account is Go's
    # TestLoginFailuresShareOneMessage.
    assert client.get("/settings").status_code == 401


def test_deactivated_user_with_live_session_rejected() -> None:
    # resolve_token's inactive-user branch, isolated: flip is_active WITHOUT
    # touching the session rows (unlike the CLI, which revokes them too), so
    # the live cookie resolves the row and must be rejected on is_active
    # alone. Without this, deleting the is_active check in resolve_token
    # passes the whole suite (mutation-verified during the B2 review).
    uid = create_user()
    client = session_client(uid)
    assert client.get("/settings").status_code == 200
    with session_scope() as s:
        row = s.get(User, uid)
        assert row is not None
        row.is_active = False
        s.add(row)
    assert client.get("/settings").status_code == 401
