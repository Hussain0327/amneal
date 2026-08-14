from __future__ import annotations

import importlib
from pathlib import Path

import dagster as dg
import pytest
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
        "authoritative_fda_shard_job",
        "authoritative_fda_canary_job",
        "authoritative_fda_acceptance_job",
    } <= job_names


def test_shard_job_chunks_and_embeds_the_same_partition_in_one_run() -> None:
    """The full-backfill job must carry BOTH shard assets over the SAME 512 partitions.

    Draining every chunk partition before starting the embedding phase leaves the whole
    corpus chunked-but-unembedded, and `assert_profile_ready_for_activation` turns that
    state into a cold-boot failure for production. Interleaving is what bounds the window
    to one shard, so a job that lost either asset would silently restore the hazard.
    """
    job = defs.resolve_job_def("authoritative_fda_shard_job")

    assert {node.name for node in job.nodes} == {
        "authoritative_fda_chunk_shard",
        "authoritative_fda_embedding_shard",
    }
    assert job.partitions_def is not None
    assert job.partitions_def.get_partition_keys() == FDA_SHARD_PARTITIONS.get_partition_keys()


def test_shard_job_will_not_embed_a_shard_whose_documents_did_not_all_chunk() -> None:
    """Embedding must sit behind the chunk asset's BLOCKING completeness check.

    Embedding a shard whose documents failed to chunk would write vectors for a partial
    shard and report the partition green, so the acceptance gate could pass over a corpus
    that is quietly missing documents.
    """
    job = defs.resolve_job_def("authoritative_fda_shard_job")

    embed = next(
        invocation
        for invocation, dependencies in job.dependencies.items()
        if invocation.name == "authoritative_fda_embedding_shard" and dependencies
    )
    rendered = str(job.dependencies[embed])
    assert "BlockingAssetChecksDependencyDefinition" in rendered
    assert "all_manifest_documents_chunked" in rendered
    assert "authoritative_fda_chunk_shard" in rendered


def test_repair_jobs_survive_so_one_shard_can_be_re_embedded_alone() -> None:
    """Re-embedding after a provider outage must not re-fetch and re-parse the PDFs.

    The single-asset jobs are the repair path; deleting them in favour of the combined job
    would make every embedding retry pay the full download and OCR cost again.
    """
    embed_only = defs.resolve_job_def("authoritative_fda_embedding_shards_job")
    assert {node.name for node in embed_only.nodes} == {"authoritative_fda_embedding_shard"}


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


def test_instance_config_classes_are_importable() -> None:
    # DagsterInstance resolves these module/class strings with importlib at
    # daemon/webserver startup; a typo (e.g. `dagster.core` without the
    # underscore) crashes the whole control plane before any asset loads, and
    # nothing else in CI executes that resolution path.
    root = Path(__file__).resolve().parents[1]
    instance_config = yaml.safe_load((root / "docker/dagster.yaml").read_text())
    for section in ("run_coordinator",):
        block = instance_config[section]
        module = importlib.import_module(block["module"])
        assert hasattr(module, block["class"]), (section, block)


def test_chunk_shard_fetch_concurrency_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shard's worker count must follow crawl_concurrency, not a literal 1.

    workers=1 serializes fetch, parse, and DB time inside every shard run, so
    the whole backfill pays sum-of-stages instead of overlapping them; the
    politeness interval, not thread count, is what bounds FDA pressure.
    """
    import types

    import config.settings as cs

    from regwatch.corpus import dagster_defs as defs_mod

    monkeypatch.setenv("CRAWL_CONCURRENCY", "7")
    cs.get_settings.cache_clear()

    seen: dict[str, object] = {}

    def _fake_sync(manifest: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return types.SimpleNamespace(succeeded=True, run_id=1, workers=kwargs["workers"])

    readiness = types.SimpleNamespace(
        chunked_documents=2, expected_documents=2, chunks=5, issues=[]
    )
    monkeypatch.setattr(defs_mod, "init_db", lambda **_: None)
    monkeypatch.setattr(
        defs_mod, "_configured_manifest", lambda _ctx: types.SimpleNamespace(sha256="x")
    )
    monkeypatch.setattr(defs_mod, "sync_manifest", _fake_sync)
    monkeypatch.setattr(defs_mod, "shard_readiness", lambda _m, _s: readiness)
    monkeypatch.setattr(defs_mod, "build_artifact_store", lambda: object())
    monkeypatch.setattr(defs_mod, "stats_dict", lambda _s: {})

    result = dg.materialize(
        [defs_mod.authoritative_fda_chunk_shard],
        partition_key="000",
        run_config={"ops": {"authoritative_fda_chunk_shard": {"config": {"manifest_sha256": "x"}}}},
    )
    assert result.success
    assert seen["workers"] == 7
    cs.get_settings.cache_clear()
