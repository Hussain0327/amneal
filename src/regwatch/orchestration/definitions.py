"""Dagster definitions for REGWATCH local orchestration."""

import os
import subprocess
from typing import Any

import dagster as dg
from dagster import AssetExecutionContext


def _tail(text: str, max_chars: int = 4_000) -> str:
    """Keep Dagster metadata readable while preserving the end of command logs."""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _run_cli(
    context: AssetExecutionContext, command: list[str], *, failure: str
) -> dg.MaterializeResult[Any]:
    """Run a REGWATCH CLI command as a Dagster asset materialization."""
    env = os.environ.copy()
    env.setdefault("REGWATCH_INIT_DB", "false")

    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )
    if result.stdout:
        context.log.info(result.stdout)
    if result.stderr:
        context.log.warning(result.stderr)
    if result.returncode != 0:
        raise dg.Failure(
            description=failure,
            metadata={
                "command": dg.MetadataValue.text(" ".join(command)),
                "exit_code": result.returncode,
                "stdout_tail": dg.MetadataValue.text(_tail(result.stdout)),
                "stderr_tail": dg.MetadataValue.text(_tail(result.stderr)),
            },
        )

    return dg.MaterializeResult(
        metadata={
            "command": dg.MetadataValue.text(" ".join(command)),
            "exit_code": result.returncode,
            "data_dir": dg.MetadataValue.path(env.get("DATA_DIR", "/app/data")),
            "stdout_tail": dg.MetadataValue.text(_tail(result.stdout)),
        }
    )


@dg.asset(group_name="ingest", compute_kind="regwatch-cli")
def seed_corpus(context: AssetExecutionContext) -> dg.MaterializeResult[Any]:
    """Seed the verified PSG corpus through the existing REGWATCH CLI."""
    return _run_cli(context, ["regwatch", "seed"], failure="regwatch seed failed")


@dg.asset(group_name="watch", compute_kind="regwatch-cli")
def watch_digest(context: AssetExecutionContext) -> dg.MaterializeResult[Any]:
    """Run the Watch pipeline (crawl → match → ingest matched → alert → digest)."""
    return _run_cli(context, ["regwatch", "watch"], failure="regwatch watch failed")


seed_corpus_job = dg.define_asset_job("seed_corpus_job", selection=[seed_corpus])

watch_digest_job = dg.define_asset_job("watch_digest_job", selection=[watch_digest])

watch_daily_schedule = dg.ScheduleDefinition(
    name="watch_daily_schedule",
    job=watch_digest_job,
    cron_schedule="0 6 * * *",
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)

defs = dg.Definitions(
    assets=[seed_corpus, watch_digest],
    jobs=[seed_corpus_job, watch_digest_job],
    schedules=[watch_daily_schedule],
)
