"""FastAPI auth dependency — fail closed with the contract's 401 body."""

from __future__ import annotations

from fastapi import Cookie, HTTPException

from regwatch.auth.sessions import resolve_token
from regwatch.store.models import User

SESSION_COOKIE = "regwatch_session"


def require_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> User:
    user = resolve_token(session_token) if session_token else None
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user
