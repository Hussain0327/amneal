"""Dagster definitions for REGWATCH local orchestration."""

import os
import subprocess
from typing import Any

import dagster as dg
from dagster import AssetExecutionContext

# Wall-clock cap on a single CLI subprocess. Sized to cover a full A-Z catalog
# crawl (~1,795 PSGs fetched + embedded) with generous headroom for slow FDA
# responses. WHY a hard cap at all: without it a hung crawl or a >64KB pipe
# deadlock under capture_output=True stalls the 06:00 UTC daily watch cron
# forever and silently -- the recurring silent-cron-failure class. With the cap,
# a hang surfaces as a failed, alertable materialization instead of a dead run.
# Override via REGWATCH_CLI_TIMEOUT_SECONDS for unusually large backfills.
_DEFAULT_CLI_TIMEOUT_SECONDS = 3 * 60 * 60  # 3 hours


def _cli_timeout_seconds() -> float:
    """Resolve the subprocess wall-clock timeout (env-overridable, fail-safe)."""
    raw = os.environ.get("REGWATCH_CLI_TIMEOUT_SECONDS")
    if raw is None:
        return float(_DEFAULT_CLI_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(_DEFAULT_CLI_TIMEOUT_SECONDS)
    # A non-positive timeout would mean "give up immediately"; treat it as a
    # misconfiguration and fall back to the safe default rather than never run.
    if value <= 0:
        return float(_DEFAULT_CLI_TIMEOUT_SECONDS)
    return value


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

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=_cli_timeout_seconds(),
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and reaps the child before raising, so no process
        # leaks here. Surface the hang as a visible, alertable failed
        # materialization instead of letting the cron stall silently.
        partial_stdout = exc.stdout or ""
        partial_stderr = exc.stderr or ""
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", "replace")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", "replace")
        raise dg.Failure(
            description=f"{failure} (timed out after {exc.timeout:.0f}s)",
            metadata={
                "command": dg.MetadataValue.text(" ".join(command)),
                "timeout_seconds": exc.timeout,
                "stdout_tail": dg.MetadataValue.text(_tail(partial_stdout)),
                "stderr_tail": dg.MetadataValue.text(_tail(partial_stderr)),
            },
        ) from exc

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
