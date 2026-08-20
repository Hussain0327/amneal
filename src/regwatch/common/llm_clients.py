"""Process-wide SDK client cache.

This module is the single seam through which regwatch touches the ``openai``
SDK (pinned by the import-linter contract in pyproject.toml). It serves both
transports that speak the OpenAI wire protocol: the Databricks serving
endpoint and api.openai.com itself.

An SDK client owns an httpx connection pool; constructing one per request (or
per complete() call) pays TLS + TCP setup on every LLM round-trip. Providers
stay cheap throwaway objects — the underlying client is shared here, keyed by
its connection and authentication settings so token rotation or an endpoint
cutover never reuses the wrong pool. Each transport gets its OWN cache so one
cannot evict the other's pool.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=8)
def shared_databricks_openai_client(
    base_url: str,
    token: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 2,
) -> Any:
    """OpenAI-compatible client dedicated to a Databricks serving endpoint.

    Explicit timeout/max_retries: the SDK default is a 600s read timeout with
    2 retries, which can pin a sync-route worker for ~10-20 min (B3). The
    cache key includes every transport/authentication input through
    ``lru_cache``'s normal argument keying, so a token rotation or endpoint
    cutover gets a fresh pool instead of reusing the old one.
    """
    from openai import OpenAI

    return OpenAI(
        api_key=token,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


@lru_cache(maxsize=4)
def shared_openai_api_client(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> Any:
    """OpenAI SDK client dedicated to the api.openai.com Chat Completions API.

    Deliberately a SEPARATE cache from
    ``shared_databricks_openai_client``: the two transports have independent
    lifecycles (key rotation, endpoint cutover), and sharing one bounded cache
    would let a Databricks repoint evict the OpenAI pool, paying TLS + TCP
    setup on the next Ask. Explicit timeout/max_retries for the same reason as
    there -- the SDK default is a 600s read timeout, long enough to pin a sync
    route worker for ~10-20 minutes.

    Args:
        base_url: The OpenAI API base URL.
        api_key: The OpenAI API key.
        timeout: Per-request read timeout, in seconds.
        max_retries: SDK-level retries for a single request.

    Returns:
        A process-wide ``openai.OpenAI`` client for these exact inputs.
    """
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )
