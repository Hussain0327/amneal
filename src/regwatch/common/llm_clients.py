"""Process-wide SDK client cache.

This module is the single seam through which regwatch touches the ``openai``
SDK (pinned by the import-linter contract in pyproject.toml).

An SDK client owns an httpx connection pool; constructing one per request (or
per complete() call) pays TLS + TCP setup on every LLM round-trip. Providers
stay cheap throwaway objects — the underlying client is shared here, keyed by
its connection and authentication settings so token rotation or an endpoint
cutover never reuses the wrong pool.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=4)
def shared_openai_api_client(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> Any:
    """OpenAI SDK client dedicated to the Responses API.

    Explicit timeout/max_retries avoids the SDK's long default timeout pinning
    a synchronous route worker.

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
