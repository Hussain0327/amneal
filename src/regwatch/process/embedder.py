"""EmbeddingProvider interface + local bge-small default.

A provider takes a list of strings and returns a list of unit-norm float
vectors. Swappable via config — business logic never references a model name.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import ClassVar, Protocol

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


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Factory. Falls back to settings if no name provided."""
    name = name or get_settings().embedding_provider
    name = name.lower()
    if name == "local-bge-small":
        return LocalBgeSmallProvider()
    if name == "echo":
        return EchoEmbeddingProvider()
    raise ValueError(f"unknown embedding provider: {name}")
