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


@dg.asset(group_name="ingest", compute_kind="regwatch-cli")
def seed_corpus(context: AssetExecutionContext) -> dg.MaterializeResult[Any]:
    """Seed the verified PSG corpus through the existing REGWATCH CLI."""
    command = ["regwatch", "seed"]
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
            description="regwatch seed failed",
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


seed_corpus_job = dg.define_asset_job("seed_corpus_job", selection=[seed_corpus])

defs = dg.Definitions(
    assets=[seed_corpus],
    jobs=[seed_corpus_job],
)
