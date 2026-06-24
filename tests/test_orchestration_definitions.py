from __future__ import annotations

import pytest

pytest.importorskip("dagster")


def test_dagster_seed_job_loads() -> None:
    from regwatch.orchestration import definitions

    assert definitions.seed_corpus_job.name == "seed_corpus_job"
    assert definitions.defs.resolve_job_def("seed_corpus_job").name == "seed_corpus_job"


def test_dagster_watch_job_loads() -> None:
    from regwatch.orchestration import definitions

    assert definitions.watch_digest_job.name == "watch_digest_job"
    assert definitions.defs.resolve_job_def("watch_digest_job").name == "watch_digest_job"


def test_dagster_watch_schedule_is_daily_utc_and_running() -> None:
    import dagster as dg

    from regwatch.orchestration import definitions

    schedule = definitions.defs.resolve_schedule_def("watch_daily_schedule")
    assert schedule.cron_schedule == "0 6 * * *"
    assert schedule.execution_timezone == "UTC"
    assert schedule.default_status == dg.DefaultScheduleStatus.RUNNING
    assert schedule.job.name == "watch_digest_job"


class _StubLog:
    def info(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
        pass

    def warning(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
        pass


class _StubContext:
    """Minimal stand-in for AssetExecutionContext; only `.log` is touched."""

    log = _StubLog()


def test_run_cli_timeout_raises_failure_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung CLI subprocess must surface as dg.Failure (alertable), not stall.

    Reverting the timeout= / TimeoutExpired handling would let the TimeoutExpired
    propagate as a bare exception (or, with no timeout=, hang forever), so this
    test fails if the fix is removed.
    """
    import subprocess

    import dagster as dg

    from regwatch.orchestration import definitions

    def _fake_run(*_args: object, **kwargs: object) -> object:
        # Mirror what subprocess.run would raise on a hung crawl, including the
        # partial output captured before the kill.
        raw_timeout = kwargs.get("timeout", 0.0)
        timeout_s = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else 0.0
        raise subprocess.TimeoutExpired(
            cmd=["regwatch", "watch"],
            timeout=timeout_s,
            output="partial-stdout",
            stderr="partial-stderr",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(dg.Failure) as excinfo:
        definitions._run_cli(
            _StubContext(),  # type: ignore[arg-type]
            ["regwatch", "watch"],
            failure="regwatch watch failed",
        )

    failure = excinfo.value
    assert "timed out" in (failure.description or "")
    # Partial output is carried in metadata so the hang is diagnosable.
    assert "stdout_tail" in failure.metadata
    assert "stderr_tail" in failure.metadata
    assert "timeout_seconds" in failure.metadata


def test_cli_timeout_seconds_env_override_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from regwatch.orchestration import definitions

    monkeypatch.delenv("REGWATCH_CLI_TIMEOUT_SECONDS", raising=False)
    assert definitions._cli_timeout_seconds() == float(definitions._DEFAULT_CLI_TIMEOUT_SECONDS)

    monkeypatch.setenv("REGWATCH_CLI_TIMEOUT_SECONDS", "120")
    assert definitions._cli_timeout_seconds() == 120.0

    # Garbage and non-positive values fall back to the safe default rather than
    # disabling the cron's only guard against a silent hang.
    monkeypatch.setenv("REGWATCH_CLI_TIMEOUT_SECONDS", "not-a-number")
    assert definitions._cli_timeout_seconds() == float(definitions._DEFAULT_CLI_TIMEOUT_SECONDS)
    monkeypatch.setenv("REGWATCH_CLI_TIMEOUT_SECONDS", "0")
    assert definitions._cli_timeout_seconds() == float(definitions._DEFAULT_CLI_TIMEOUT_SECONDS)
