from __future__ import annotations

from pathlib import Path

import dagster as dg
import yaml

from regwatch.corpus.dagster_defs import (
    FDA_SHARD_PARTITIONS,
    defs,
    manifest_schedule,
)


def test_authoritative_fda_definitions_are_loadable_and_bounded() -> None:
    dg.Definitions.validate_loadable(defs)

    partition_keys = FDA_SHARD_PARTITIONS.get_partition_keys()
    assert len(partition_keys) == 512
    assert partition_keys[0] == "000"
    assert partition_keys[-1] == "511"

    job_names = {job.name for job in defs.resolve_all_job_defs()}
    assert {
        "authoritative_fda_manifest_job",
        "authoritative_fda_chunk_shards_job",
        "authoritative_fda_embedding_shards_job",
        "authoritative_fda_canary_job",
        "authoritative_fda_acceptance_job",
    } <= job_names


def test_full_backfills_are_not_automatically_scheduled() -> None:
    assert manifest_schedule.default_status is dg.DefaultScheduleStatus.STOPPED
    assert manifest_schedule.cron_schedule == "0 6 * * 1"
    assert defs.schedules is not None
    assert manifest_schedule in defs.schedules


def test_worker_runtime_is_durable_bounded_and_non_root() -> None:
    root = Path(__file__).resolve().parents[1]
    instance_config = yaml.safe_load((root / "docker/dagster.yaml").read_text())
    assert instance_config["storage"]["postgres"]["postgres_url"] == {"env": "DAGSTER_POSTGRES_URL"}
    assert instance_config["concurrency"]["runs"]["max_concurrent_runs"] == 4
    assert instance_config["concurrency"]["pools"] == {
        "default_limit": 4,
        "granularity": "run",
    }

    worker_dockerfile = (root / "Dockerfile.corpus-worker").read_text()
    assert "tesseract-ocr-eng" in worker_dockerfile
    assert "USER regwatch" in worker_dockerfile
    assert 'CMD ["dagster-daemon", "run"' in worker_dockerfile
