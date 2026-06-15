"""Process-wide SDK client cache.

OpenAI/Anthropic SDK clients own an httpx connection pool; constructing one
per request (or per complete() call) pays TLS + TCP setup on every LLM and
embedding round-trip. Providers stay cheap throwaway objects — the underlying
client is shared here, keyed by api_key so key rotation still works.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=4)
def shared_openai_client(api_key: str | None) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=api_key)


@lru_cache(maxsize=4)
def shared_anthropic_client(api_key: str) -> Any:
    from anthropic import Anthropic

    return Anthropic(api_key=api_key)
