"""H4: POST /feedback — ownership, upsert-per-(audit_id, user_id), validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.conftest import create_user, login_client


def _qa_audit(user_id: int, *, mode: str = "qa") -> int:
    from regwatch.common.audit import log_query

    return log_query(
        mode=mode,
        query_text="What study design is recommended?",
        retrieved=[],
        answer_text="A fasting study [PSG_020503, p.3].",
        citations=[],
        refused=False,
        model_name="echo",
        user_id=str(user_id),
    )


def _feedback_rows() -> list[tuple[int, str, int, str | None]]:
    from regwatch.store.db import session_scope
    from regwatch.store.models import AnswerFeedback

    with session_scope() as s:
        return [
            (r.audit_id, r.user_id, r.rating, r.comment)
            for r in s.scalars(select(AnswerFeedback)).all()
        ]


def test_feedback_requires_auth() -> None:
    from regwatch.api.main import app

    with TestClient(app) as client:
        r = client.post("/feedback", json={"audit_id": 1, "rating": 1})
    assert r.status_code == 401


def test_feedback_thumbs_up(auth_client: TestClient) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"])
    r = auth_client.post("/feedback", json={"audit_id": audit_id, "rating": 1})
    assert r.status_code == 200, r.text
    assert r.json() == {"audit_id": audit_id, "rating": 1, "comment": None}
    assert _feedback_rows() == [(audit_id, str(me["id"]), 1, None)]


def test_feedback_thumbs_down_with_comment(auth_client: TestClient) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"])
    r = auth_client.post(
        "/feedback",
        json={"audit_id": audit_id, "rating": -1, "comment": "cited the wrong dosage form"},
    )
    assert r.status_code == 200, r.text
    assert _feedback_rows() == [(audit_id, str(me["id"]), -1, "cited the wrong dosage form")]


def test_feedback_rerating_replaces_not_duplicates(auth_client: TestClient) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"])
    assert (
        auth_client.post(
            "/feedback", json={"audit_id": audit_id, "rating": -1, "comment": "bad"}
        ).status_code
        == 200
    )
    assert (
        auth_client.post("/feedback", json={"audit_id": audit_id, "rating": 1}).status_code == 200
    )
    # One row, latest rating wins, the stale comment does not linger.
    assert _feedback_rows() == [(audit_id, str(me["id"]), 1, None)]


def test_feedback_404_for_missing_audit_row(auth_client: TestClient) -> None:
    r = auth_client.post("/feedback", json={"audit_id": 999_999, "rating": 1})
    assert r.status_code == 404


def test_feedback_404_for_foreign_audit_row(auth_client: TestClient) -> None:
    """Another user's audit row: same 404 as missing — never confirms existence."""
    other_id = create_user(email="other@example.com", password="pw-pw-pw-pw-pw")
    foreign_audit = _qa_audit(other_id)
    r = auth_client.post("/feedback", json={"audit_id": foreign_audit, "rating": 1})
    assert r.status_code == 404
    assert _feedback_rows() == []


def test_feedback_404_for_non_qa_mode(auth_client: TestClient) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"], mode="whitepaper")
    r = auth_client.post("/feedback", json={"audit_id": audit_id, "rating": 1})
    assert r.status_code == 404


def test_feedback_rejects_out_of_range_rating(auth_client: TestClient) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"])
    for bad in (0, 2, -2, 5):
        r = auth_client.post("/feedback", json={"audit_id": audit_id, "rating": bad})
        assert r.status_code == 422, f"rating={bad} must be rejected"
    assert _feedback_rows() == []


def test_feedback_rejects_oversized_comment(auth_client: TestClient) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"])
    r = auth_client.post(
        "/feedback", json={"audit_id": audit_id, "rating": -1, "comment": "x" * 2001}
    )
    assert r.status_code == 422


def test_two_users_can_rate_independent_rows() -> None:
    """The (audit_id, user_id) key scopes the upsert per user."""
    uid_a = create_user()
    uid_b = create_user(email="b@example.com", password="pw-pw-pw-pw-pw")
    client_a = login_client()
    client_b = login_client(email="b@example.com", password="pw-pw-pw-pw-pw")
    try:
        audit_a = _qa_audit(uid_a)
        audit_b = _qa_audit(uid_b)
        assert (
            client_a.post("/feedback", json={"audit_id": audit_a, "rating": 1}).status_code == 200
        )
        assert (
            client_b.post("/feedback", json={"audit_id": audit_b, "rating": -1}).status_code == 200
        )
        rows = sorted(_feedback_rows())
        assert rows == sorted([(audit_a, str(uid_a), 1, None), (audit_b, str(uid_b), -1, None)])
    finally:
        client_a.__exit__(None, None, None)
        client_b.__exit__(None, None, None)


@pytest.mark.parametrize("rating", [-1, 1])
def test_feedback_accepts_both_valid_ratings(auth_client: TestClient, rating: int) -> None:
    me = auth_client.get("/auth/me").json()["user"]
    audit_id = _qa_audit(me["id"])
    r = auth_client.post("/feedback", json={"audit_id": audit_id, "rating": rating})
    assert r.status_code == 200
    assert r.json()["rating"] == rating
