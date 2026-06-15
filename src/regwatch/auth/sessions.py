"""DB-backed opaque session tokens.

The raw token lives only in the client's HttpOnly cookie; the DB stores its
sha256, so a leaked DB cannot be replayed as a session. Login always creates a
brand-new session row (prevents session fixation).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from config.settings import get_settings
from sqlmodel import select

from regwatch.auth.passwords import _DUMMY_HASH, verify_password
from regwatch.store.db import session_scope
from regwatch.store.models import AuthSession, User

# last_seen_at is only persisted once it has drifted past this window, so a
# burst of read requests on one session doesn't serialize on a per-request write.
_LAST_SEEN_COALESCE = timedelta(minutes=5)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_utc(dt: datetime) -> datetime:
    # SQLite round-trips datetimes naive; everything we write is UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _detached_user(user: User) -> User:
    """Copy a row so callers can read it after the DB session closes."""
    return User(
        id=user.id,
        email=user.email,
        password_hash=user.password_hash,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
    )


def authenticate(email: str, password: str) -> User | None:
    """Credential check with a uniform cost/timing profile.

    Every call runs exactly one bcrypt verify — against the stored hash when the
    user exists, against a dummy hash when not — so unknown email, wrong
    password, and inactive user are indistinguishable to a caller.
    """
    normalized = email.strip().lower()
    with session_scope() as s:
        row = s.scalars(select(User).where(User.email == normalized)).first()
        user = _detached_user(row) if row is not None else None
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


def create_session(user_id: int) -> tuple[str, AuthSession]:
    """Create a fresh session row; return (raw token, detached row).

    Login doubles as the opportunistic sweep: expired rows are dead weight
    whose stale token hashes have no reason to stay at rest, and nothing else
    runs periodically at pilot scale.
    """
    raw = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=get_settings().auth_session_ttl_hours)
    with session_scope() as s:
        for stale in s.scalars(select(AuthSession).where(AuthSession.expires_at < now)):
            s.delete(stale)
        row = AuthSession(
            token_hash=_hash_token(raw),
            user_id=user_id,
            created_at=now,
            expires_at=expires_at,
        )
        s.add(row)
        s.flush()
        detached = AuthSession(
            id=row.id,
            token_hash=row.token_hash,
            user_id=row.user_id,
            created_at=now,
            expires_at=expires_at,
            last_seen_at=None,
        )
    return raw, detached


def resolve_token(raw: str) -> User | None:
    """Resolve a raw cookie token to its active user, enforcing expiry."""
    now = datetime.now(UTC)
    with session_scope() as s:
        row = s.scalars(
            select(AuthSession).where(AuthSession.token_hash == _hash_token(raw))
        ).first()
        if row is None:
            return None
        if now >= _as_utc(row.expires_at):
            s.delete(row)  # expired — purge the stale hash rather than keep it at rest
            return None
        user = s.get(User, row.user_id)
        if user is None or not user.is_active:
            return None
        # Coarsen the last_seen_at write: this runs for EVERY authenticated
        # request (incl. pure reads), and an unconditional write is a WAL-
        # generating UPDATE + row lock on the hottest path. last_seen_at is
        # informational (expiry uses expires_at), so only persist once it has
        # drifted past a coalescing window.
        prev = _as_utc(row.last_seen_at) if row.last_seen_at is not None else None
        if prev is None or (now - prev) >= _LAST_SEEN_COALESCE:
            row.last_seen_at = now
            s.add(row)
        return _detached_user(user)


def revoke_token(raw: str) -> None:
    """Delete the server-side session for a raw token. Silent when absent."""
    with session_scope() as s:
        row = s.scalars(
            select(AuthSession).where(AuthSession.token_hash == _hash_token(raw))
        ).first()
        if row is not None:
            s.delete(row)
