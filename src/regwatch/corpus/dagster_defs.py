"""Worker-only Dagster control plane for authoritative FDA corpus builds.

Dagster owns scheduling, shard retries, lineage, and operator visibility. The
Lakebase lifecycle tables remain the source of truth, so rerunning a Dagster
partition is safe even if the orchestrator lost state after a document commit.
"""

import re

import dagster as dg
from config.settings import get_settings

from regwatch.corpus.acceptance import (
    finalize_orchestrated_manifest,
    shard_readiness,
)
from regwatch.corpus.artifact_store import build_artifact_store
from regwatch.corpus.discovery import discover_authoritative_manifest
from regwatch.corpus.embeddings import embed_pending_corpus
from regwatch.corpus.manifest import CorpusManifest
from regwatch.corpus.persisted_manifest import (
    load_persisted_manifest,
    persist_manifest,
)
from regwatch.corpus.sharding import (
    FDA_CORPUS_SHARD_COUNT,
    corpus_shard_id,
    parse_shard_partition_key,
    shard_partition_key,
)
from regwatch.corpus.status import authoritative_corpus_coverage
from regwatch.corpus.sync import stats_dict, sync_manifest
from regwatch.store.db import init_db

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
FDA_SHARD_PARTITIONS = dg.StaticPartitionsDefinition(
    [shard_partition_key(index) for index in range(FDA_CORPUS_SHARD_COUNT)]
)
_SHARD_RETRY = dg.RetryPolicy(
    max_retries=3,
    delay=30,
    backoff=dg.Backoff.EXPONENTIAL,
    jitter=dg.Jitter.PLUS_MINUS,
)
_MANIFEST_CONFIG = {
    "manifest_sha256": dg.Field(
        str,
        description="Logical SHA-256 of the exact persisted FDA manifest.",
    )
}

_CHUNK_CHECK = dg.AssetCheckSpec(
    name="all_manifest_documents_chunked",
    asset="authoritative_fda_chunk_shard",
    description=(
        "Every document assigned to this shard has one indexed version or an "
        "evidence-backed terminal resolution."
    ),
    blocking=True,
)
_EMBED_CHECK = dg.AssetCheckSpec(
    name="all_manifest_chunks_embedded",
    asset="authoritative_fda_embedding_shard",
    description="Every current chunk in this manifest shard has the selected profile.",
    blocking=True,
)
_CANARY_CHECK = dg.AssetCheckSpec(
    name="expected_canary_is_complete",
    asset="authoritative_fda_canary",
    description="The reviewed canary count is chunked and embedded with zero failures.",
    blocking=True,
)
_ACCEPTANCE_CHECK = dg.AssetCheckSpec(
    name="full_manifest_activation_gate",
    asset="authoritative_fda_acceptance",
    description="The exact full manifest satisfies every pre-cutover corpus gate.",
    blocking=True,
)


@dg.asset(
    group_name="authoritative_fda",
    kinds={"python", "fda", "manifest"},
    description="Discover and durably freeze the exact FDA-only source universe.",
    retry_policy=_SHARD_RETRY,
    pool="fda_manifest_discovery",
)
def authoritative_fda_manifest(
    context: dg.AssetExecutionContext,
) -> dg.MaterializeResult:  # type: ignore[type-arg]
    init_db(assert_provider=False)
    manifest = discover_authoritative_manifest()
    reference = persist_manifest(manifest)
    context.log.info(
        "Persisted authoritative FDA manifest %s with %s records",
        manifest.sha256,
        len(manifest.artifacts),
    )
    return dg.MaterializeResult(
        data_version=dg.DataVersion(manifest.sha256),
        metadata={
            "manifest_sha256": manifest.sha256,
            "artifact_uri": reference.artifact_uri,
            "documents": len(manifest.artifacts),
            "by_source_family": manifest.counts_by_family(),
            "complete_universe": manifest.complete_universe,
        },
    )


@dg.asset(
    group_name="authoritative_fda",
    kinds={"python", "fda", "postgres"},
    description="Stream, retain, parse, and atomically chunk one stable manifest shard.",
    partitions_def=FDA_SHARD_PARTITIONS,
    config_schema=_MANIFEST_CONFIG,
    retry_policy=_SHARD_RETRY,
    pool="fda_chunk_shards",
    check_specs=[_CHUNK_CHECK],
)
def authoritative_fda_chunk_shard(
    context: dg.AssetExecutionContext,
) -> dg.MaterializeResult:  # type: ignore[type-arg]
    init_db(assert_provider=False)
    manifest = _configured_manifest(context)
    shard_id = parse_shard_partition_key(context.partition_key)
    # crawl_concurrency, not a hardcoded 1: the shard's fetch/parse threads are
    # what overlap network wait with CPU work inside one run. Request starts
    # stay host-paced regardless (crawl_min_interval_ms; set crawl_pace_dir
    # when running shards concurrently so the budget holds across processes),
    # so raising this overlaps work without raising FDA pressure.
    stats = sync_manifest(
        manifest,
        defer_embeddings=True,
        workers=get_settings().crawl_concurrency,
        artifact_store=build_artifact_store(),
        shard_id=shard_id,
    )
    readiness = shard_readiness(manifest, shard_id)
    if not stats.succeeded or readiness.resolved_documents != readiness.expected_documents:
        raise dg.Failure(
            description=f"FDA chunk shard {shard_id:03d} is incomplete",
            metadata={
                "sync": stats_dict(stats),
                "issues": list(readiness.issues[:20]),
            },
        )
    return dg.MaterializeResult(
        metadata={
            "manifest_sha256": manifest.sha256,
            "shard_id": shard_id,
            "documents": readiness.expected_documents,
            "indexed_documents": readiness.chunked_documents,
            "terminal_documents": readiness.terminal_documents,
            "chunks": readiness.chunks,
            "sync_run_id": stats.run_id,
        },
        check_results=[
            dg.AssetCheckResult(
                passed=True,
                check_name=_CHUNK_CHECK.name,
                metadata={
                    "expected_documents": readiness.expected_documents,
                    "chunked_documents": readiness.chunked_documents,
                    "terminal_documents": readiness.terminal_documents,
                    "missing_at_source_documents": (readiness.missing_at_source_documents),
                    "unparseable_documents": readiness.unparseable_documents,
                },
            )
        ],
    )


@dg.asset(
    deps=[authoritative_fda_chunk_shard],
    group_name="authoritative_fda",
    kinds={"python", "qwen", "postgres"},
    description="Backfill the selected immutable embedding profile for one manifest shard.",
    partitions_def=FDA_SHARD_PARTITIONS,
    config_schema={
        **_MANIFEST_CONFIG,
        "profile_id": dg.Field(
            str,
            default_value="",
            description="Empty selects ACTIVE_EMBEDDING_PROFILE.",
        ),
        "batch_size": dg.Field(
            int,
            default_value=128,
            description=(
                "Database page size per embedding write. The per-HTTP-request"
                " input count is the separate QWEN_EMBEDDING_BATCH_SIZE"
                " setting, which must stay at or below 24: the endpoint"
                " rejects larger input arrays with 429."
            ),
        ),
    },
    retry_policy=_SHARD_RETRY,
    pool="fda_embedding_shards",
    check_specs=[_EMBED_CHECK],
)
def authoritative_fda_embedding_shard(
    context: dg.AssetExecutionContext,
) -> dg.MaterializeResult:  # type: ignore[type-arg]
    init_db(assert_provider=False)
    manifest = _configured_manifest(context)
    shard_id = parse_shard_partition_key(context.partition_key)
    profile_id = (
        str(context.op_execution_context.op_config["profile_id"]).strip()
        or get_settings().active_embedding_profile
        or "legacy"
    ).strip()
    batch_size = int(context.op_execution_context.op_config["batch_size"])
    if not 1 <= batch_size <= 512:
        # Deterministic operator input error: retrying reproduces it verbatim.
        raise dg.Failure("batch_size must be between 1 and 512", allow_retries=False)
    canonical_ids = _canonical_ids_for_shard(manifest, shard_id)
    processed = embed_pending_corpus(
        profile_id,
        batch_size=batch_size,
        shard_id=shard_id,
        canonical_ids=canonical_ids,
        on_batch=lambda count: context.log.info(
            "Embedded %s chunks for FDA shard %03d", count, shard_id
        ),
    )
    readiness = shard_readiness(manifest, shard_id, profile_id=profile_id)
    if not readiness.ready:
        raise dg.Failure(
            description=f"FDA embedding shard {shard_id:03d} is incomplete",
            metadata={"issues": list(readiness.issues[:20])},
        )
    return dg.MaterializeResult(
        metadata={
            "manifest_sha256": manifest.sha256,
            "shard_id": shard_id,
            "profile_id": profile_id,
            "embedded_this_run": processed,
            "chunks": readiness.chunks,
            "embedded_chunks": readiness.embedded_chunks,
        },
        check_results=[
            dg.AssetCheckResult(
                passed=True,
                check_name=_EMBED_CHECK.name,
                metadata={
                    "chunks": readiness.chunks,
                    "embedded_chunks": readiness.embedded_chunks,
                },
            )
        ],
    )


@dg.asset(
    group_name="authoritative_fda",
    kinds={"python", "fda", "canary"},
    config_schema={
        "applications": dg.Field([str], default_value=["NDA020503"]),
        "expected_documents": dg.Field(int, default_value=21),
        "profile_id": dg.Field(str, default_value=""),
        "batch_size": dg.Field(int, default_value=128),
    },
    retry_policy=_SHARD_RETRY,
    pool="fda_canary",
    check_specs=[_CANARY_CHECK],
)
def authoritative_fda_canary(
    context: dg.AssetExecutionContext,
) -> dg.MaterializeResult:  # type: ignore[type-arg]
    init_db(assert_provider=False)
    applications = tuple(
        str(value) for value in context.op_execution_context.op_config["applications"]
    )
    expected = int(context.op_execution_context.op_config["expected_documents"])
    manifest = discover_authoritative_manifest(application_numbers=applications)
    if len(manifest.artifacts) != expected:
        raise dg.Failure(
            description=(
                f"FDA canary discovery changed: expected {expected}, "
                f"found {len(manifest.artifacts)}"
            )
        )
    persist_manifest(manifest)
    stats = sync_manifest(
        manifest,
        defer_embeddings=True,
        workers=1,
        artifact_store=build_artifact_store(),
    )
    if not stats.succeeded:
        raise dg.Failure(
            description="FDA canary chunking failed",
            metadata={"sync": stats_dict(stats)},
        )
    profile_id = (
        str(context.op_execution_context.op_config["profile_id"]).strip()
        or get_settings().active_embedding_profile
        or "legacy"
    ).strip()
    processed = embed_pending_corpus(
        profile_id,
        batch_size=int(context.op_execution_context.op_config["batch_size"]),
        canonical_ids=[artifact.canonical_id for artifact in manifest.artifacts],
    )
    readiness = [
        shard_readiness(manifest, shard_id, profile_id=profile_id)
        for shard_id in sorted(
            {corpus_shard_id(artifact.canonical_id) for artifact in manifest.artifacts}
        )
    ]
    ready_documents = sum(result.embedded_documents for result in readiness)
    terminal_documents = sum(result.terminal_documents for result in readiness)
    if (
        ready_documents != expected
        or terminal_documents != 0
        or any(not result.ready for result in readiness)
    ):
        issues = [issue for result in readiness for issue in result.issues]
        raise dg.Failure(
            description=f"FDA canary reached only {ready_documents}/{expected}",
            metadata={"issues": issues[:20]},
        )
    return dg.MaterializeResult(
        data_version=dg.DataVersion(manifest.sha256),
        metadata={
            "manifest_sha256": manifest.sha256,
            "documents": expected,
            "chunks": sum(result.chunks for result in readiness),
            "profile_id": profile_id,
            "embedded_this_run": processed,
            "sync_run_id": stats.run_id,
        },
        check_results=[
            dg.AssetCheckResult(
                passed=True,
                check_name=_CANARY_CHECK.name,
                metadata={"covered_documents": ready_documents, "expected_documents": expected},
            )
        ],
    )


@dg.asset(
    group_name="authoritative_fda",
    kinds={"python", "postgres", "quality-gate"},
    config_schema={
        **_MANIFEST_CONFIG,
        "profile_id": dg.Field(str, default_value=""),
    },
    check_specs=[_ACCEPTANCE_CHECK],
)
def authoritative_fda_acceptance(
    context: dg.AssetExecutionContext,
) -> dg.MaterializeResult:  # type: ignore[type-arg]
    init_db(assert_provider=False)
    manifest = _configured_manifest(context)
    profile_id = (
        str(context.op_execution_context.op_config["profile_id"]).strip()
        or get_settings().active_embedding_profile
        or "legacy"
    ).strip()
    run_id, readiness = finalize_orchestrated_manifest(manifest, profile_id=profile_id)
    coverage = authoritative_corpus_coverage()
    if not coverage.activation_ready:
        raise dg.Failure(
            description="full FDA corpus failed the application activation gate",
            metadata={"blockers": list(coverage.activation_blockers)},
        )
    return dg.MaterializeResult(
        data_version=dg.DataVersion(manifest.sha256),
        metadata={
            "manifest_sha256": manifest.sha256,
            "acceptance_run_id": run_id,
            "documents": readiness.expected_documents,
            "indexed_documents": readiness.chunked_documents,
            "terminal_documents": readiness.terminal_documents,
            "missing_at_source_documents": readiness.missing_at_source_documents,
            "unparseable_documents": readiness.unparseable_documents,
            "chunks": readiness.chunks,
            "profile_id": profile_id,
            "coverage_percent": coverage.coverage_percent,
            "cutover": "manual: set REGWATCH_RETRIEVAL_CORPUS=authoritative_fda",
        },
        check_results=[
            dg.AssetCheckResult(
                passed=True,
                check_name=_ACCEPTANCE_CHECK.name,
                metadata={
                    "documents": readiness.expected_documents,
                    "indexed_documents": readiness.chunked_documents,
                    "terminal_documents": readiness.terminal_documents,
                    "chunks": readiness.chunks,
                    "embedded_chunks": readiness.embedded_chunks,
                },
            )
        ],
    )


def _configured_manifest(context: dg.AssetExecutionContext) -> CorpusManifest:
    manifest_sha256 = str(context.op_execution_context.op_config["manifest_sha256"]).strip().lower()
    if _SHA256_RE.fullmatch(manifest_sha256) is None:
        # Deterministic operator input error: retrying reproduces it verbatim.
        raise dg.Failure(
            "manifest_sha256 must be a lowercase SHA-256",
            allow_retries=False,
        )
    return load_persisted_manifest(manifest_sha256)


def _canonical_ids_for_shard(manifest: CorpusManifest, shard_id: int) -> list[str]:
    return [
        artifact.canonical_id
        for artifact in manifest.artifacts
        if corpus_shard_id(artifact.canonical_id) == shard_id
    ]


manifest_job = dg.define_asset_job(
    "authoritative_fda_manifest_job",
    selection=dg.AssetSelection.assets(authoritative_fda_manifest),
)
chunk_shards_job = dg.define_asset_job(
    "authoritative_fda_chunk_shards_job",
    selection=dg.AssetSelection.assets(authoritative_fda_chunk_shard),
)
embedding_shards_job = dg.define_asset_job(
    "authoritative_fda_embedding_shards_job",
    selection=dg.AssetSelection.assets(authoritative_fda_embedding_shard),
)
# THE JOB A FULL BACKFILL SHOULD ACTUALLY USE. One partition of this job runs chunk
# shard N and only then embedding shard N, so a shard is never left chunked but
# unembedded for longer than its own run.
#
# WHY THIS EXISTS, and why draining chunk_shards_job before starting
# embedding_shards_job is the wrong order at full-corpus scale: a chunk with no vector
# on the active profile is not merely incomplete, it is FATAL TO A COLD BOOT.
# store/embedding_profiles.py::assert_profile_ready_for_activation refuses to activate a
# profile whose embedded_chunks != total_chunks, and store/db.py::init_db runs that
# assertion at every process start. Running all 512 chunk partitions first therefore
# parks production one machine restart away from refusing to boot, for as long as the
# embedding phase takes -- days, at ~1.2M chunks. The 2026-08-13 outage was this exact
# state reached with only 347 chunks. Interleaving bounds that window to one shard.
#
# The ordering is not advisory. authoritative_fda_embedding_shard declares
# deps=[authoritative_fda_chunk_shard] and both carry FDA_SHARD_PARTITIONS, so Dagster
# resolves the embedding node behind a BlockingAssetChecksDependencyDefinition on the
# chunk node's blocking `all_manifest_documents_chunked` check: the shard's vectors are
# not attempted unless that shard's documents all chunked. Per-shard fail-closed.
#
# The two single-asset jobs above are deliberately KEPT, for repair: re-embedding one
# shard after a provider outage must not re-fetch and re-chunk its documents.
shard_job = dg.define_asset_job(
    "authoritative_fda_shard_job",
    selection=dg.AssetSelection.assets(
        authoritative_fda_chunk_shard,
        authoritative_fda_embedding_shard,
    ),
    description=(
        "Chunk then embed ONE manifest shard. Use this for full backfills: it keeps the "
        "unembedded-chunk window to a single shard instead of the whole corpus."
    ),
)
canary_job = dg.define_asset_job(
    "authoritative_fda_canary_job",
    selection=dg.AssetSelection.assets(authoritative_fda_canary),
)
acceptance_job = dg.define_asset_job(
    "authoritative_fda_acceptance_job",
    selection=dg.AssetSelection.assets(authoritative_fda_acceptance),
)

manifest_schedule = dg.ScheduleDefinition(
    name="authoritative_fda_weekly_manifest_schedule",
    job=manifest_job,
    cron_schedule="0 6 * * 1",
    execution_timezone="America/New_York",
    default_status=dg.DefaultScheduleStatus.STOPPED,
    description="Discover and freeze a manifest only; full backfills remain operator-launched.",
)

defs = dg.Definitions(
    assets=[
        authoritative_fda_manifest,
        authoritative_fda_chunk_shard,
        authoritative_fda_embedding_shard,
        authoritative_fda_canary,
        authoritative_fda_acceptance,
    ],
    jobs=[
        manifest_job,
        chunk_shards_job,
        embedding_shards_job,
        shard_job,
        canary_job,
        acceptance_job,
    ],
    schedules=[manifest_schedule],
)
