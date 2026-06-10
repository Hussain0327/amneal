"""Auth + per-user chat history.

Covers the cookie-session contract: login/logout/me, the 401 wall on every
protected endpoint, session ownership (404 — never confirm another user's
session exists), legacy NULL-user session adoption, /sessions list/read/delete,
user attribution on audit rows (INV-6), and the rate limiters.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import select
from typer.testing import CliRunner

from regwatch.api.main import app
from regwatch.cli import app as cli_app
from regwatch.common.conversation import SessionOwnershipError, ensure_session
from regwatch.common.ratelimit import RateLimiter
from regwatch.generate.grounded_qa import ask
from regwatch.store.db import init_db, session_scope
from regwatch.store.models import AuthSession, ChatMessage, ChatSession, PsgDocument, QueryLog
from tests.conftest import (
    DEFAULT_USER_EMAIL,
    DEFAULT_USER_PASSWORD,
    create_user,
    login_client,
)


def _anon() -> TestClient:
    c = TestClient(app)
    c.__enter__()  # lifespan → init_db on the per-test DB
    return c


# ---------- login / logout / me ----------


def test_login_success_returns_user_and_httponly_cookie() -> None:
    create_user()
    r = _anon().post(
        "/auth/login",
        json={"email": DEFAULT_USER_EMAIL.upper(), "password": DEFAULT_USER_PASSWORD},
    )
    assert r.status_code == 200
    user = r.json()["user"]
    assert set(user.keys()) == {"id", "email", "display_name", "role"}
    assert user["email"] == DEFAULT_USER_EMAIL  # lookup is case-insensitive
    assert user["role"] == "analyst"
    set_cookie = r.headers["set-cookie"].lower()
    assert "regwatch_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    assert "max-age=" in set_cookie
    assert "secure" not in set_cookie  # AUTH_COOKIE_SECURE defaults off (localhost pilot)


def test_login_failures_share_one_message() -> None:
    create_user()
    create_user("inactive@example.com", "inactive-password", is_active=False)
    c = _anon()
    wrong = c.post("/auth/login", json={"email": DEFAULT_USER_EMAIL, "password": "nope"})
    unknown = c.post("/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    inactive = c.post(
        "/auth/login",
        json={"email": "inactive@example.com", "password": "inactive-password"},
    )
    for r in (wrong, unknown, inactive):
        assert r.status_code == 401
        assert r.json() == {"detail": "invalid email or password"}
        assert "set-cookie" not in r.headers


def test_me_returns_current_user(auth_client: TestClient) -> None:
    body = auth_client.get("/auth/me").json()
    assert body["user"]["email"] == DEFAULT_USER_EMAIL
    assert body["user"]["display_name"] == "Test Analyst"


def test_logout_revokes_server_side_session() -> None:
    create_user()
    client = login_client()
    assert client.get("/auth/me").status_code == 200
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_logout_without_session_never_errors() -> None:
    assert _anon().post("/auth/logout").status_code == 204


def test_expired_session_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    create_user()
    monkeypatch.setenv("AUTH_SESSION_TTL_HOURS", "0")
    cs.get_settings.cache_clear()
    client = login_client()  # session expires at issue time
    assert client.get("/auth/me").status_code == 401


def test_expired_session_rows_are_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    create_user()
    monkeypatch.setenv("AUTH_SESSION_TTL_HOURS", "0")
    cs.get_settings.cache_clear()
    expired = login_client()  # expires at issue time
    login_client()  # second expired row whose cookie is never presented again

    # Presenting an expired cookie rejects AND deletes that row.
    assert expired.get("/auth/me").status_code == 401
    with session_scope() as s:
        assert len(s.scalars(select(AuthSession)).all()) == 1

    # The next login sweeps whatever expiry left behind.
    monkeypatch.setenv("AUTH_SESSION_TTL_HOURS", "72")
    cs.get_settings.cache_clear()
    login_client()
    with session_scope() as s:
        assert len(s.scalars(select(AuthSession)).all()) == 1  # only the live session


# ---------- the 401 wall ----------

_PROTECTED: list[tuple[str, str, dict[str, object] | None]] = [
    ("post", "/query", {"question": "what is required?"}),
    ("post", "/sources/search", {"query_text": "x", "sources": ["psg"]}),
    ("post", "/assemble", {"active_ingredient": "albuterol"}),
    ("get", "/watch/latest", None),
    ("get", "/products", None),
    ("post", "/products", {"active_ingredient": "Foo", "source": "manual"}),
    ("get", "/settings", None),
    ("get", "/sessions", None),
    ("get", "/sessions/some-session-id", None),
    ("delete", "/sessions/some-session-id", None),
    ("get", "/auth/me", None),
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


def test_health_stays_open() -> None:
    assert _anon().get("/health").status_code == 200


def test_docs_routes_are_disabled() -> None:
    # The auto-docs register at app level — outside the protected router — so
    # they are off entirely rather than leaking the API surface (routes,
    # schemas, the session-cookie name) to anonymous visitors.
    c = _anon()
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert c.get(path).status_code == 404, path


# ---------- session ownership ----------


def test_other_users_sessions_are_invisible() -> None:
    create_user("a@example.com", "password-for-a")
    create_user("b@example.com", "password-for-b")
    a = login_client("a@example.com", "password-for-a")
    b = login_client("b@example.com", "password-for-b")

    sid = a.post("/query", json={"question": "Does this exist?"}).json()["session_id"]

    assert b.get(f"/sessions/{sid}").status_code == 404
    assert b.delete(f"/sessions/{sid}").status_code == 404
    hijack = b.post("/query", json={"question": "And dissolution?", "session_id": sid})
    assert hijack.status_code == 404
    assert hijack.json() == {"detail": "session not found"}  # 404, never 403

    # The owner is unaffected.
    assert a.get(f"/sessions/{sid}").status_code == 200
    assert [x["id"] for x in b.get("/sessions").json()["sessions"]] == []


def test_legacy_null_user_session_adopted_via_query() -> None:
    create_user()
    client = login_client()
    sid = ensure_session(None)  # legacy demo row: user_id is NULL

    # Invisible until adopted.
    assert client.get(f"/sessions/{sid}").status_code == 404
    assert sid not in [x["id"] for x in client.get("/sessions").json()["sessions"]]

    r = client.post("/query", json={"question": "Adopt this session?", "session_id": sid})
    assert r.status_code == 200
    assert r.json()["session_id"] == sid

    assert client.get(f"/sessions/{sid}").status_code == 200
    assert sid in [x["id"] for x in client.get("/sessions").json()["sessions"]]
    with session_scope() as s:
        row = s.get(ChatSession, sid)
        assert row is not None and row.user_id is not None


def test_lost_ownership_race_aborts_instead_of_writing() -> None:
    """Defense in depth below the API pre-check: a request that loses an
    adoption/creation race binds a session another user now owns — it must
    abort, never interleave both users' turns in one session."""
    init_db()
    sid = ensure_session(None, user_id="1")
    with pytest.raises(SessionOwnershipError):
        ensure_session(sid, user_id="2")
    with pytest.raises(SessionOwnershipError):
        ask("And dissolution?", session_id=sid, user_id="2")
    with session_scope() as s:
        assert s.scalars(select(ChatMessage)).all() == []  # nothing was written


# ---------- /sessions ----------


def test_sessions_list_ordering_titles_and_counts() -> None:
    user_id = create_user()
    client = login_client()
    long_q = (
        "What does the FDA guidance recommend about a topic that runs well "
        "past sixty characters in total length?"
    )
    sid1 = client.post("/query", json={"question": long_q}).json()["session_id"]
    sid2 = client.post("/query", json={"question": "Second question?"}).json()["session_id"]
    # Touch session 1 again so it becomes the most recently updated.
    client.post("/query", json={"question": "A follow-up?", "session_id": sid1})
    with session_scope() as s:
        s.add(ChatSession(id="empty-session", user_id=str(user_id)))

    body = client.get("/sessions").json()
    ids = [x["id"] for x in body["sessions"]]
    assert ids == ["empty-session", sid1, sid2]  # updated_at desc

    by_id = {x["id"]: x for x in body["sessions"]}
    assert by_id[sid1]["title"] == long_q[:60]  # first user message, truncated
    assert by_id["empty-session"]["title"] == "(untitled)"
    assert by_id[sid1]["message_count"] == 4
    assert by_id[sid2]["message_count"] == 2
    assert by_id["empty-session"]["message_count"] == 0
    for item in body["sessions"]:
        datetime.fromisoformat(item["created_at"])
        datetime.fromisoformat(item["updated_at"])


def test_get_session_returns_ordered_messages_with_shape() -> None:
    create_user()
    client = login_client()
    sid = client.post("/query", json={"question": "Shape check?"}).json()["session_id"]
    body = client.get(f"/sessions/{sid}").json()
    assert set(body["session"].keys()) == {"id", "title", "created_at", "updated_at"}
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]  # created_at asc
    for m in messages:
        assert set(m.keys()) == {
            "id",
            "turn_id",
            "role",
            "content",
            "status",
            "citations",
            "created_at",
        }
        assert isinstance(m["citations"], list)
    assert messages[0]["content"] == "Shape check?"
    assert messages[0]["turn_id"] == messages[1]["turn_id"]


def test_delete_session_removes_messages() -> None:
    create_user()
    client = login_client()
    sid = client.post("/query", json={"question": "Delete me later?"}).json()["session_id"]
    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert client.get(f"/sessions/{sid}").status_code == 404
    assert client.get("/sessions").json()["sessions"] == []
    with session_scope() as s:
        assert int(s.scalar(select(func.count()).select_from(ChatMessage)) or 0) == 0


# ---------- audit attribution (INV-6) ----------


def test_query_log_records_user_id_for_query() -> None:
    user_id = create_user()
    client = login_client()
    assert client.post("/query", json={"question": "Out of corpus?"}).status_code == 200
    with session_scope() as s:
        attributions = [row.user_id for row in s.scalars(select(QueryLog))]
    assert attributions == [str(user_id)]


def test_query_log_records_user_id_for_assemble() -> None:
    user_id = create_user()
    client = login_client()
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
    the inner Q&A's bookkeeping session to the caller — no phantom conversation
    titled with the synthetic BE question may appear in /sessions."""
    from regwatch.assemble import dossier as dossier_mod

    monkeypatch.setattr(dossier_mod, "_fetch_rld_label", lambda *a, **k: None)  # network-free
    user_id = create_user()
    client = login_client()
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
    assert r.json()["refused"] is False  # past the early refusal → the inner ask() ran

    assert client.get("/sessions").json()["sessions"] == []
    with session_scope() as s:
        owners = [row.user_id for row in s.scalars(select(ChatSession))]
        modes = {row.mode: row.user_id for row in s.scalars(select(QueryLog))}
    assert owners == [None]  # the inner Q&A session stays unowned and invisible
    assert modes == {"assemble": str(user_id), "qa": str(user_id)}  # attribution intact


# ---------- rate limiting ----------


def test_query_and_assemble_rate_limited_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    create_user()
    client = login_client()
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    cs.get_settings.cache_clear()

    assert client.post("/query", json={"question": "First one?"}).status_code == 200
    # /assemble draws from the same per-user budget.
    assert client.post("/assemble", json={"active_ingredient": "Foo"}).status_code == 200
    r = client.post("/query", json={"question": "Over the limit?"}).status_code
    assert r == 429

    # Another user has their own budget.
    create_user("other@example.com", "other-password")
    other = login_client("other@example.com", "other-password")
    assert other.post("/query", json={"question": "Fresh budget?"}).status_code == 200


def test_login_brute_force_guard() -> None:
    create_user()
    c = _anon()
    for _ in range(10):
        r = c.post("/auth/login", json={"email": DEFAULT_USER_EMAIL, "password": "wrong"})
        assert r.status_code == 401
    blocked = c.post(
        "/auth/login", json={"email": DEFAULT_USER_EMAIL, "password": DEFAULT_USER_PASSWORD}
    )
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}
    # The guard is per email — other accounts are unaffected.
    create_user("other@example.com", "other-password")
    ok = c.post("/auth/login", json={"email": "other@example.com", "password": "other-password"})
    assert ok.status_code == 200


def test_login_rejects_oversized_email() -> None:
    # Bounded input: the login limiter key embeds the client-supplied email,
    # so an unbounded string would pin arbitrary attacker memory per request.
    r = _anon().post("/auth/login", json={"email": "a" * 300 + "@example.com", "password": "x"})
    assert r.status_code == 422


def test_rate_limiter_evicts_idle_keys() -> None:
    # Login keys are attacker-controlled (one per unique email), so idle keys
    # must be swept once their window expires — not retained for process life.
    limiter = RateLimiter(window_s=0.01)
    for i in range(100):
        assert limiter.allow(f"login:attacker-{i}@example.com", 10)
    time.sleep(0.03)
    assert limiter.allow("login:fresh@example.com", 10)
    assert set(limiter._hits) == {"login:fresh@example.com"}


# ---------- CLI provisioning ----------


def test_cli_create_user_login_and_list_users() -> None:
    runner = CliRunner()
    password = "from-the-cli-prompt"
    created = runner.invoke(
        cli_app,
        ["create-user", "Cli.User@Example.com", "--name", "CLI User"],
        input=f"{password}\n{password}\n",
    )
    assert created.exit_code == 0, created.output

    # Stored lowercased; the prompted password actually logs in.
    client = login_client("cli.user@example.com", password)
    assert client.get("/auth/me").json()["user"]["email"] == "cli.user@example.com"

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


def test_cli_deactivate_user_blocks_login() -> None:
    runner = CliRunner()
    create_user()
    client = login_client()
    result = runner.invoke(cli_app, ["deactivate-user", DEFAULT_USER_EMAIL])
    assert result.exit_code == 0, result.output
    # Existing session revoked and new logins refused — same 401 as bad creds.
    assert client.get("/auth/me").status_code == 401
    r = _anon().post(
        "/auth/login", json={"email": DEFAULT_USER_EMAIL, "password": DEFAULT_USER_PASSWORD}
    )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}
