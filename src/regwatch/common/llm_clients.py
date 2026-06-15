"""Process-wide SDK client cache.

OpenAI/Anthropic SDK clients own an httpx connection pool; constructing one
per request (or per complete() call) pays TLS + TCP setup on every LLM and
embedding round-trip. Providers stay cheap throwaway objects — the underlying
client is shared here, keyed by api_key so key rotation still works.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


# Cache keyed by (api_key, timeout, max_retries) so the embedder (which owns its
# own retry loop and passes max_retries=0) and the synthesizer (SDK retries on)
# get distinct clients without ever constructing one per call. maxsize covers a
# couple of key/config pairs.
@lru_cache(maxsize=8)
def shared_openai_client(
    api_key: str | None, *, timeout: float = 60.0, max_retries: int = 2
) -> Any:
    from openai import OpenAI

    # Explicit timeout/max_retries: the SDK default is a 600s read timeout with
    # 2 retries, which can pin a sync-route worker for ~10-20 min (B3).
    return OpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)


@lru_cache(maxsize=8)
def shared_anthropic_client(api_key: str, *, timeout: float = 60.0, max_retries: int = 2) -> Any:
    from anthropic import Anthropic

    return Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)
