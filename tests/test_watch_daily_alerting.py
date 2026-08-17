"""CD-3/OBS-1: the daily ingest cron must alert on failure and run a dead-man's-
switch on success — and BOTH must no-op (never error) when their secret is unset,
so a fork/contributor without the secrets is unaffected.

These assertions fail if someone drops a secret-presence guard, an outcome gate
(failure()/success()/cancelled()), or a load-bearing run payload (the /fail
suffix, the fail-fast curl flags) from .github/workflows/watch-daily.yml — the
exact regressions that would silently re-break paging for stale FDA guidance.

The success-path Slack digest (analyst delivery, not paging) is pinned here too:
its gates, the UTC digest-file path, the quiet-day no-op (INV-4), the || true
guard that keeps a flaky webhook from failing a green pipeline, and the 5-line
truncation cap.
"""

from __future__ import annotations

import re
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


def test_failure_alerts_also_fire_on_timeout_cancellation() -> None:
    # The job-level timeout-minutes CANCELS a hung run (it does not fail it),
    # and failure() is false on cancellation, so a bare failure() gate means an
    # overrun of the sole prod pipeline pages nobody. cancelled() must stay in
    # both failure-class gates.
    for name in ("notify slack on failure", "healthcheck ping on failure"):
        cond = _step(name)["if"]
        assert "cancelled()" in cond, f"{name} must also fire when the job is cancelled"
        assert "failure()" in cond


def test_failure_ping_hits_fail_endpoint_and_success_ping_does_not() -> None:
    # healthchecks.io semantics: bare URL = success signal, URL/fail = failure.
    # A failure ping that loses the /fail suffix reports SUCCESS on a failed
    # run: the monitor stays green AND the missed-ping alarm is suppressed.
    fail_run = _step("healthcheck ping on failure")["run"]
    ok_run = _step("healthcheck ping on success")["run"]
    assert "${WATCH_HEALTHCHECK_URL}/fail" in fail_run
    assert "$WATCH_HEALTHCHECK_URL" in ok_run
    assert "/fail" not in ok_run


def test_slack_payload_posts_to_webhook_with_failfast_curl() -> None:
    run = _step("notify slack on failure")["run"]
    assert '"$SLACK_WEBHOOK_URL"' in run
    # -f: non-2xx exits non-zero (a dead webhook shows up as a failed step
    # instead of silently swallowing the alert); --max-time bounds the hang.
    assert "curl -fsS" in run
    assert "--max-time" in run


def test_healthcheck_pings_use_failfast_curl() -> None:
    for name in ("healthcheck ping on success", "healthcheck ping on failure"):
        run = _step(name)["run"]
        assert "curl -fsS" in run, f"{name}: curl must fail fast on HTTP errors"
        assert "--max-time" in run, f"{name}: curl must bound its runtime"


def test_llm_secret_preflight_fails_fast_before_watch() -> None:
    # The LLM endpoint is only exercised at summarize/extract time, i.e. on the
    # day a PSG actually changes; without a presence check a revoked secret
    # stays green through every no-change day and first errors on the run that
    # mattered.
    steps = _steps()
    names = [s.get("name") for s in steps]
    assert "preflight Databricks LLM config" in names, "fail-fast secret check missing"
    assert names.index("preflight Databricks LLM config") < names.index("regwatch watch")
    step = _step("preflight Databricks LLM config")
    # Same skip contract as the pipeline: no DB secret => clean no-op (forks).
    assert "env.DATABASE_URL != ''" in step["if"]
    for name in (
        "DATABRICKS_LLM_BASE_URL",
        "DATABRICKS_LLM_TOKEN",
        "DATABRICKS_LLM_MODEL",
    ):
        assert name in step["run"]
    assert "exit 1" in step["run"]


def test_success_digest_gated_on_success_db_and_secret() -> None:
    # Delivery must never fire for a failed or skipped pipeline (INV-4: no
    # digest for a run that did not happen), and must no-op cleanly on forks
    # without the webhook or DB secrets.
    cond = _step("slack digest on success")["if"]
    assert "success()" in cond
    assert "env.DATABASE_URL != ''" in cond
    assert "env.SLACK_WEBHOOK_URL != ''" in cond


def test_success_digest_reads_todays_utc_digest_file() -> None:
    # write_digest names the file after datetime.now(UTC).date(); a local-time
    # `date` (or a drifted path) would read yesterday's file — or none — and
    # silently deliver the wrong day's alerts.
    run = _step("slack digest on success")["run"]
    assert "data/processed/alerts/digest-$(date -u +%F).jsonl" in run


def test_success_digest_quiet_day_posts_nothing() -> None:
    # INV-4: a missing OR empty digest means "ran, no changes" — the step must
    # exit 0 without touching Slack, never fabricate a notification.
    run = _step("slack digest on success")["run"]
    assert '[ ! -s "$DIGEST" ]' in run
    assert "exit 0" in run
    assert "no alerts today" in run


def test_success_digest_post_is_guarded_and_failfast() -> None:
    # A flaky webhook must never fail a run whose pipeline succeeded — the
    # failure-alerting path stays the only thing that can page. curl still
    # fail-fasts and bounds its runtime so the guard never hides a hang.
    run = _step("slack digest on success")["run"]
    assert '"$SLACK_WEBHOOK_URL" || true' in run
    assert "curl -fsS" in run
    assert "--max-time" in run


def test_success_digest_truncates_with_more_marker() -> None:
    # Payload shape: jq-built from the JSONL (escaping by construction), at
    # most 5 alert lines plus a "+N more" marker — pinned so a refactor cannot
    # silently start dumping a full crawl day into one Slack message.
    run = _step("slack digest on success")["run"]
    assert "jq" in run
    assert ".[:5]" in run
    assert "more" in run


# Env vars a step may curl for alerting; a step referencing one is notify/ping
# class and must be fully gated. Matches $VAR and ${VAR} forms.
_ALERT_ENV_RE = re.compile(r"\$\{?([A-Z_]*(?:WEBHOOK|HEALTHCHECK)[A-Z_]*)\}?")
_OUTCOME_FNS = ("failure()", "success()", "cancelled()", "always()")


def test_no_new_notify_step_runs_unconditionally() -> None:
    # Scan ALL steps, not a hardcoded name list: a NEW notify/ping step added
    # without gates would error on forks (empty secret) or spam on every run,
    # and a fixed list can never catch it.
    gated = 0
    for step in _steps():
        alert_vars = set(_ALERT_ENV_RE.findall(step.get("run") or ""))
        if not alert_vars:
            continue
        gated += 1
        name = step.get("name", "<unnamed>")
        cond = step.get("if") or ""
        assert any(fn in cond for fn in _OUTCOME_FNS), f"{name}: no outcome gate in `if:`"
        for var in sorted(alert_vars):
            assert f"env.{var} != ''" in cond, f"{name}: missing presence gate for {var}"
    # The scan must not silently go blind if steps are renamed/refactored.
    assert gated >= 3, f"expected >=3 notify/ping steps, scan matched {gated}"
