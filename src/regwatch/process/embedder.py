"""EmbeddingProvider interface + local bge-small default.

A provider takes a list of strings and returns a list of unit-norm float
vectors. Swappable via config — business logic never references a model name.
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import time
from collections import OrderedDict
from typing import Any, ClassVar, Protocol

from config.settings import get_settings


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalBgeSmallProvider:
    """sentence-transformers BAAI/bge-small-en-v1.5, runs locally."""

    name = "local-bge-small"
    dim = 384

    _model: ClassVar[object | None] = None
    _cache: ClassVar[OrderedDict[str, list[float]]] = OrderedDict()
    _cache_max_size: ClassVar[int] = 4096

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=local-bge-small requires installing "
                "`regwatch[local-embeddings]` or running "
                "`uv sync --extra local-embeddings`."
            ) from exc

        LocalBgeSmallProvider._model = SentenceTransformer("BAAI/bge-small-en-v1.5")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        misses: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            key = hashlib.sha256(t.encode("utf-8")).hexdigest()
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
            out.append(cached if cached is not None else [])
            if cached is None:
                misses.append((i, key))
        if misses:
            self._ensure_model()
            if self._model is None:
                raise RuntimeError("local embedding model failed to initialize")
            batch_texts = [texts[i] for i, _ in misses]
            vecs = self._model.encode(  # type: ignore[attr-defined]
                batch_texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ).tolist()
            for (i, key), v in zip(misses, vecs, strict=False):
                self._cache[key] = v
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_max_size:
                    self._cache.popitem(last=False)
                out[i] = v
        return out


class EchoEmbeddingProvider:
    """Deterministic, network-free provider for tests.

    Maps each input to a sparse 384-dim hash-based vector. Quality is bad, but
    it is deterministic and self-contained — useful for unit tests that don't
    want to download a model.
    """

    name = "echo"
    dim = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            h = hashlib.sha256(t.encode("utf-8")).digest()
            for i, b in enumerate(h):
                v[(i * 7) % self.dim] += (b - 128) / 128.0
            # L2 normalize
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


class OpenAIEmbeddingProvider:
    """OpenAI `text-embedding-3-small` (1536 dims, unit-norm vectors).

    Batches up to 512 inputs per API call and retries 429/5xx responses with
    exponential backoff + jitter. The chunk table in Postgres mode stores
    `vector(1536)`, so `dim` must stay in lockstep with it (the pgvector
    store asserts this at startup — see store/pgvector_store.py).

    `client` is injectable for tests, mirroring generate/llm.OpenAIProvider.
    """

    name = "openai"
    dim = 1536
    model = "text-embedding-3-small"

    _max_batch_size: ClassVar[int] = 512
    _max_attempts: ClassVar[int] = 6
    _backoff_base_s: ClassVar[float] = 1.0
    _backoff_cap_s: ClassVar[float] = 30.0

    def __init__(self, *, client: Any = None) -> None:
        self._client = client

    def _client_or_create(self) -> Any:
        if self._client is None:
            api_key = get_settings().openai_api_key
            if not api_key:
                raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY to be set")
            from regwatch.common.llm_clients import shared_openai_client

            try:
                self._client = shared_openai_client(api_key)
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "EMBEDDING_PROVIDER=openai requires installing `regwatch[llm]` "
                    "or running `uv sync --extra llm`."
                ) from exc
        return self._client

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Retry only rate limits (429) and server errors (5xx)."""
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            return False
        return status == 429 or status >= 500

    def _create_with_retry(self, client: Any, batch: list[str]) -> Any:
        delay = self._backoff_base_s
        for attempt in range(1, self._max_attempts + 1):
            try:
                return client.embeddings.create(model=self.model, input=batch)
            except Exception as exc:
                if attempt >= self._max_attempts or not self._is_retryable(exc):
                    raise
                time.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, self._backoff_cap_s)
        raise RuntimeError("unreachable")  # pragma: no cover

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._client_or_create()
        out: list[list[float]] = []
        for start in range(0, len(texts), self._max_batch_size):
            batch = list(texts[start : start + self._max_batch_size])
            resp = self._create_with_retry(client, batch)
            data = sorted(resp.data, key=lambda d: int(d.index))
            if len(data) != len(batch):
                raise RuntimeError(
                    f"openai embeddings returned {len(data)} vectors for {len(batch)} inputs"
                )
            for item in data:
                vec = [float(x) for x in item.embedding]
                if len(vec) != self.dim:
                    raise RuntimeError(
                        f"openai embedding has {len(vec)} dims, expected {self.dim} "
                        f"({self.model})"
                    )
                out.append(vec)
        return out


def _module_available(module: str) -> bool:
    """Cheap importability probe (no actual import — torch never loads here)."""
    return importlib.util.find_spec(module) is not None


def assert_embedding_runtime_available(name: str | None = None) -> None:
    """Boot-time fail-fast: the configured provider's runtime deps must exist.

    Providers import their heavy dependencies lazily on first ``embed()``, so a
    slim image (``INSTALL_LOCAL_EMBEDDINGS=false``) configured with
    ``EMBEDDING_PROVIDER=local-bge-small`` would otherwise boot cleanly,
    report healthy, and then 500 on every query/ingest at embed time. The API
    lifespan calls this so that misconfiguration refuses to start with the
    same remediation message the lazy path would raise.
    """
    name = (name or get_settings().embedding_provider).lower()
    if name == "local-bge-small" and not _module_available("sentence_transformers"):
        raise RuntimeError(
            "EMBEDDING_PROVIDER=local-bge-small requires installing "
            "`regwatch[local-embeddings]` or running "
            "`uv sync --extra local-embeddings` (Docker: build with "
            "INSTALL_LOCAL_EMBEDDINGS=true), or set EMBEDDING_PROVIDER=openai "
            "for the slim image."
        )
    if name == "openai" and not _module_available("openai"):
        raise RuntimeError(
            "EMBEDDING_PROVIDER=openai requires installing `regwatch[llm]` "
            "or running `uv sync --extra llm`."
        )


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Factory. Falls back to settings if no name provided."""
    name = name or get_settings().embedding_provider
    name = name.lower()
    if name == "local-bge-small":
        return LocalBgeSmallProvider()
    if name == "openai":
        return OpenAIEmbeddingProvider()
    if name == "echo":
        return EchoEmbeddingProvider()
    raise ValueError(f"unknown embedding provider: {name}")
