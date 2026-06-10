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
