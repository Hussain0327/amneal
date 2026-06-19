"""CD-3/OBS-1: the daily ingest cron must alert on failure and run a dead-man's-
switch on success — and BOTH must no-op (never error) when their secret is unset,
so a fork/contributor without the secrets is unaffected.

These assertions fail if someone drops a secret-presence guard or the
failure()/success() outcome gate from .github/workflows/watch-daily.yml — the
exact regression that would silently re-break paging for stale FDA guidance.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "watch-daily.yml"


def _steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    return doc["jobs"]["watch"]["steps"]


def _step(name: str) -> dict:
    for s in _steps():
        if s.get("name") == name:
            return s
    raise AssertionError(f"step {name!r} not found in watch-daily.yml")


def test_secrets_mapped_to_job_env() -> None:
    # `if:` can only test secret presence when the secret is mapped to env.
    doc = yaml.safe_load(WORKFLOW.read_text())
    job_env = doc["jobs"]["watch"]["env"]
    assert job_env["SLACK_WEBHOOK_URL"] == "${{ secrets.SLACK_WEBHOOK_URL }}"
    assert job_env["WATCH_HEALTHCHECK_URL"] == "${{ secrets.WATCH_HEALTHCHECK_URL }}"


def test_slack_notify_gated_on_failure_and_secret() -> None:
    cond = _step("notify slack on failure")["if"]
    assert "failure()" in cond
    # No paging spam / no fork errors when the webhook is unset.
    assert "env.SLACK_WEBHOOK_URL != ''" in cond


def test_healthcheck_success_ping_gated_on_success_and_secret() -> None:
    cond = _step("healthcheck ping on success")["if"]
    assert "success()" in cond
    assert "env.WATCH_HEALTHCHECK_URL != ''" in cond


def test_healthcheck_failure_ping_gated_on_failure_and_secret() -> None:
    cond = _step("healthcheck ping on failure")["if"]
    assert "failure()" in cond
    assert "env.WATCH_HEALTHCHECK_URL != ''" in cond


def test_no_new_notify_step_runs_unconditionally() -> None:
    # Every alerting step must carry a guard; an unguarded step would error on a
    # fork (missing secret) or spam on every run.
    for name in (
        "notify slack on failure",
        "healthcheck ping on success",
        "healthcheck ping on failure",
    ):
        assert _step(name).get("if"), f"{name} has no `if:` guard"
