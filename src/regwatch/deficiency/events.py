"""Progress events for a deficiency run.

Upstream DefPredict streamed AgentEvents to the browser over an in-process
WebSocket bus (single-process only; events did not survive restarts). The
regwatch MVP surfaces progress through the deficiency_run.status column that
the UI polls, so events become structured operator logs. The signature is kept
byte-identical so the vendored detection code needs no changes; a durable
event feed can later replace this body without touching call sites.
"""

from __future__ import annotations

from regwatch.common.logging import get_logger

log = get_logger(__name__)


def emit_sync(job_id: str, layer: str, event_type: str, agent_name: str, message: str) -> None:
    log.info(
        "deficiency_event",
        job_id=job_id,
        layer=layer,
        event_type=event_type,
        agent=agent_name,
        message=message,
    )
