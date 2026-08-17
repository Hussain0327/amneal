"""Process-wide SDK client cache.

The OpenAI-compatible SDK client (the Databricks serving-endpoint transport)
owns an httpx connection pool; constructing one per request (or per
complete() call) pays TLS + TCP setup on every LLM round-trip. Providers stay
cheap throwaway objects — the underlying client is shared here, keyed by its
connection and authentication settings so token rotation or an endpoint
cutover never reuses the wrong pool.
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
