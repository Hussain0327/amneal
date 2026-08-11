"""Embedding providers with explicit query/document semantics.

A provider returns unit-norm float vectors. Retrieval queries and indexed
documents have separate methods because instruction-tuned models such as Qwen3
embed them asymmetrically. ``embed()`` remains the document-style compatibility
surface for older providers and callers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import random
import time
from collections import OrderedDict
from collections.abc import Mapping
from functools import lru_cache
from threading import Lock
from typing import Any, ClassVar, Protocol

import httpx
from config.settings import get_settings

# Guards LocalBgeSmallProvider's process-wide model load and shared LRU cache.
# FastAPI runs the sync /query endpoint in a threadpool, so embed() executes
# concurrently across threads; unsynchronized OrderedDict mutation would raise
# (mirrors retrieve/reranker.py's _RERANKER_LOCK).
_LOCAL_CACHE_LOCK = Lock()

# The served Databricks endpoint (`workspace.default.regwatch-embed`, served
# entity `qwen3-embedding-0-6b-112025`), not a HuggingFace repo id: the old
# Qwen/Qwen3-Embedding-4B value was never deployed anywhere.
QWEN3_EMBEDDING_MODEL = "qwen3-embedding-0-6b"
QWEN3_QUERY_INSTRUCTION_VERSION = "regwatch-regulatory-retrieval-v1"
QWEN3_DOCUMENT_PREPROCESSING_VERSION = "raw-text-v1"
QWEN3_QUERY_INSTRUCTION = (
    "Given a pharmaceutical regulatory question, retrieve FDA product-specific "
    "guidance passages containing the evidence needed to answer it."
)
_QWEN3_PROVIDER_NAMES = frozenset(
    {
        "qwen",
        "qwen3",
        "qwen3-embedding",
        "qwen3-embedding-4b",
        "databricks-qwen3",
    }
)
_QWEN3_PROFILE_PROVIDER_NAMES = _QWEN3_PROVIDER_NAMES | frozenset({"databricks", "vllm"})


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


def embed_query(provider: Any, query: str) -> list[float]:
    """Embed one retrieval query, with a legacy ``embed`` fallback.

    The fallback preserves compatibility with test doubles and third-party
    providers that predate the asymmetric contract. New providers should
    implement ``embed_query`` directly.
    """
    method = getattr(provider, "embed_query", None)
    if callable(method):
        return method(query)
    vectors = provider.embed([query])
    if len(vectors) != 1:
        raise RuntimeError(
            f"embedding provider returned {len(vectors)} query vectors for one query"
        )
    return vectors[0]


def embed_documents(provider: Any, texts: list[str]) -> list[list[float]]:
    """Embed indexable documents, with a legacy ``embed`` fallback."""
    method = getattr(provider, "embed_documents", None)
    if callable(method):
        return method(texts)
    return provider.embed(texts)


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
        with _LOCAL_CACHE_LOCK:  # double-checked: only one thread loads the model
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
        # Cache reads/writes are guarded: the shared OrderedDict is mutated
        # (move_to_end / __setitem__ / popitem) and would corrupt under the
        # concurrent threadpool /query path without the lock.
        with _LOCAL_CACHE_LOCK:
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
            # Model inference runs OUTSIDE the lock so concurrent callers are not
            # serialized on the slow encode; only the cache mutation is guarded.
            vecs = self._model.encode(  # type: ignore[attr-defined]
                batch_texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ).tolist()
            with _LOCAL_CACHE_LOCK:
                for (i, key), v in zip(misses, vecs, strict=False):
                    self._cache[key] = v
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_max_size:
                        self._cache.popitem(last=False)
                    out[i] = v
        return out

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


class EchoEmbeddingProvider:
    """Deterministic, network-free provider for tests.

    Maps each input to a sparse 1536-dim hash-based vector. Quality is bad,
    but it is deterministic and self-contained — useful for unit tests that
    don't want to download a model. The dimension deliberately matches the
    pgvector ``chunk.embedding vector(1536)`` column (and the K6 boot assert),
    so the Postgres-only test suite can ingest and query real vectors without
    any network or model download.
    """

    name = "echo"
    dim = 1536

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

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


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
                # max_retries=0: _create_with_retry below owns the retry loop, so
                # SDK retries would stack on top of it (B3). The timeout still
                # bounds each attempt.
                self._client = shared_openai_client(
                    api_key, timeout=get_settings().llm_timeout_s, max_retries=0
                )
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
                # Retry jitter, not a cryptographic draw.
                time.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311
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
                        f"openai embedding has {len(vec)} dims, expected {self.dim} ({self.model})"
                    )
                out.append(vec)
        return out

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


@lru_cache(maxsize=8)
def _shared_qwen_http_client(base_url: str, token: str, timeout_s: float) -> httpx.Client:
    """One connection-pooled client per Qwen endpoint/credential tuple."""
    return httpx.Client(
        base_url=f"{base_url.rstrip('/')}/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=timeout_s,
    )


class Qwen3EmbeddingProvider:
    """A Qwen3 embedding model through an OpenAI-compatible serving endpoint.

    The deployed target is the 0.6B (``workspace.default.regwatch-embed``); the
    provider itself is model-agnostic, so the dimension bound below is the
    widest any Qwen3 embedding model accepts, not a claim about which one is
    served.

    The production target is a Databricks-served endpoint, while the same
    request shape works with vLLM's ``/v1/embeddings`` API. The configured
    ``base_url`` is the OpenAI API root (for example
    ``http://embedder:8000/v1``); this provider appends ``embeddings``.

    Queries receive Qwen's required one-sentence task instruction. Documents
    remain byte-for-byte raw. ``embed()`` deliberately aliases document
    semantics for backward compatibility, so query call sites must use
    ``embed_query``.
    """

    name = "qwen3"
    default_model = QWEN3_EMBEDDING_MODEL
    default_dim = 1536
    query_instruction_version_default = QWEN3_QUERY_INSTRUCTION_VERSION

    _max_attempts: ClassVar[int] = 6
    _backoff_base_s: ClassVar[float] = 1.0
    _backoff_cap_s: ClassVar[float] = 30.0
    _unit_norm_tolerance: ClassVar[float] = 1e-3

    def __init__(
        self,
        *,
        client: Any = None,
        base_url: str | None = None,
        token: str | None = None,
        model: str = QWEN3_EMBEDDING_MODEL,
        dim: int = 1536,
        query_instruction: str = QWEN3_QUERY_INSTRUCTION,
        query_instruction_version: str = QWEN3_QUERY_INSTRUCTION_VERSION,
        batch_size: int = 128,
        timeout_s: float = 60.0,
    ) -> None:
        self._client = client
        self.base_url = (base_url or "").strip() or None
        self._token = (token or "").strip() or None
        self.model = model.strip()
        self.dim = int(dim)
        self.query_instruction = query_instruction.strip()
        self.query_instruction_version = query_instruction_version.strip()
        self.batch_size = int(batch_size)
        self.timeout_s = float(timeout_s)

        if not self.model:
            raise ValueError("Qwen embedding model must not be empty")
        if not 32 <= self.dim <= 2560:
            raise ValueError("Qwen3 embedding dimension must be in [32, 2560]")
        if not self.query_instruction:
            raise ValueError("Qwen query instruction must not be empty")
        if not self.query_instruction_version:
            raise ValueError("Qwen query instruction version must not be empty")
        if not 1 <= self.batch_size <= 512:
            raise ValueError("Qwen embedding batch size must be in [1, 512]")
        if self.timeout_s <= 0:
            raise ValueError("Qwen embedding timeout must be positive")

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.base_url:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=qwen3 requires QWEN_EMBEDDING_BASE_URL to be set"
            )
        if not self._token:
            raise RuntimeError("EMBEDDING_PROVIDER=qwen3 requires QWEN_EMBEDDING_TOKEN to be set")
        self._client = _shared_qwen_http_client(
            self.base_url,
            self._token,
            self.timeout_s,
        )
        return self._client

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status if isinstance(status, int) else None

    @classmethod
    def _is_retryable(cls, exc: Exception) -> bool:
        status = cls._status_code(exc)
        if status is not None:
            return status == 429 or status >= 500
        return isinstance(exc, httpx.TransportError)

    def _request_once(self, client: Any, batch: list[str]) -> Any:
        payload = {
            "model": self.model,
            "input": batch,
            "dimensions": self.dim,
        }
        embeddings_api = getattr(client, "embeddings", None)
        create = getattr(embeddings_api, "create", None)
        if callable(create):
            return create(**payload)

        post = getattr(client, "post", None)
        if not callable(post):
            raise TypeError("Qwen embedding client must expose embeddings.create(...) or post(...)")
        response = post("embeddings", json=payload)
        response.raise_for_status()
        return response.json()

    def _create_with_retry(self, client: Any, batch: list[str]) -> Any:
        delay = self._backoff_base_s
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._request_once(client, batch)
            except Exception as exc:
                if attempt >= self._max_attempts or not self._is_retryable(exc):
                    raise
                # Retry jitter, not a cryptographic draw.
                time.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311
                delay = min(delay * 2, self._backoff_cap_s)
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _response_field(value: Any, field: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(field)
        return getattr(value, field, None)

    def _validated_batch(self, response: Any, expected_count: int) -> list[list[float]]:
        raw_data = self._response_field(response, "data")
        if raw_data is None:
            raise RuntimeError("Qwen embeddings response is missing data")
        try:
            items = list(raw_data)
        except TypeError as exc:
            raise RuntimeError("Qwen embeddings response data is not a sequence") from exc
        if len(items) != expected_count:
            raise RuntimeError(
                f"Qwen embeddings returned {len(items)} vectors for {expected_count} inputs"
            )

        indexed: list[tuple[int, Any]] = []
        for item in items:
            raw_index = self._response_field(item, "index")
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Qwen embedding item has an invalid index") from exc
            indexed.append((index, item))
        indexed.sort(key=lambda pair: pair[0])
        indices = [index for index, _ in indexed]
        if indices != list(range(expected_count)):
            raise RuntimeError(
                f"Qwen embeddings returned invalid indices {indices!r} for {expected_count} inputs"
            )

        vectors: list[list[float]] = []
        for _index, item in indexed:
            raw_embedding = self._response_field(item, "embedding")
            try:
                vector = [float(value) for value in raw_embedding]
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Qwen embedding is not a numeric vector") from exc
            if len(vector) != self.dim:
                raise RuntimeError(
                    f"Qwen embedding has {len(vector)} dims, expected {self.dim} ({self.model})"
                )
            if not all(math.isfinite(value) for value in vector):
                raise RuntimeError("Qwen embedding contains a non-finite value")
            norm = math.sqrt(math.fsum(value * value for value in vector))
            if not math.isclose(
                norm,
                1.0,
                rel_tol=self._unit_norm_tolerance,
                abs_tol=self._unit_norm_tolerance,
            ):
                raise RuntimeError(
                    f"Qwen embedding is not unit norm: norm={norm:.8f}, "
                    f"tolerance={self._unit_norm_tolerance}"
                )
            vectors.append(vector)
        return vectors

    def _embed_inputs(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._client_or_create()
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            response = self._create_with_retry(client, batch)
            out.extend(self._validated_batch(response, len(batch)))
        return out

    def embed_query(self, query: str) -> list[float]:
        instructed = f"Instruct: {self.query_instruction}\nQuery:{query}"
        return self._embed_inputs([instructed])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_inputs(texts)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Backward-compatible document embedding; never instruct documents."""
        return self.embed_documents(texts)


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
    name = (name or get_settings().embedding_provider).strip().lower()
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
    if name in _QWEN3_PROVIDER_NAMES:
        settings = get_settings()
        if not str(getattr(settings, "qwen_embedding_base_url", "") or "").strip():
            raise RuntimeError(
                "EMBEDDING_PROVIDER=qwen3 requires QWEN_EMBEDDING_BASE_URL to be set"
            )
        if not str(getattr(settings, "qwen_embedding_token", "") or "").strip():
            raise RuntimeError("EMBEDDING_PROVIDER=qwen3 requires QWEN_EMBEDDING_TOKEN to be set")


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Factory. Falls back to settings if no name provided."""
    settings = get_settings()
    name = name or settings.embedding_provider
    name = name.strip().lower()
    if name == "local-bge-small":
        return LocalBgeSmallProvider()
    if name == "openai":
        return OpenAIEmbeddingProvider()
    if name == "echo":
        return EchoEmbeddingProvider()
    if name in _QWEN3_PROVIDER_NAMES:
        return Qwen3EmbeddingProvider(
            base_url=getattr(settings, "qwen_embedding_base_url", None),
            token=getattr(settings, "qwen_embedding_token", None),
            model=str(
                getattr(settings, "qwen_embedding_model", QWEN3_EMBEDDING_MODEL)
                or QWEN3_EMBEDDING_MODEL
            ),
            dim=int(getattr(settings, "qwen_embedding_dimension", None) or 1536),
            query_instruction=str(
                getattr(settings, "qwen_embedding_query_instruction", QWEN3_QUERY_INSTRUCTION)
                or QWEN3_QUERY_INSTRUCTION
            ),
            query_instruction_version=str(
                getattr(
                    settings,
                    "qwen_embedding_query_instruction_version",
                    QWEN3_QUERY_INSTRUCTION_VERSION,
                )
                or QWEN3_QUERY_INSTRUCTION_VERSION
            ),
            batch_size=int(getattr(settings, "qwen_embedding_batch_size", None) or 128),
            timeout_s=float(getattr(settings, "llm_timeout_s", None) or 60.0),
        )
    raise ValueError(f"unknown embedding provider: {name}")


def get_embedding_provider_for_profile(profile: Any) -> EmbeddingProvider:
    """Build a provider that exactly matches one immutable Qwen profile.

    Profile-backed retrieval and backfill must not use the global legacy
    provider by accident.  The serving endpoint itself is configured outside
    the database, so every geometry-defining value that can be checked locally
    is compared before a request is sent.  ``revision`` remains an operator
    assertion: the endpoint/deployment must be pinned to that revision.
    """
    profile_provider = str(getattr(profile, "provider", "") or "").strip().lower()
    if profile_provider not in _QWEN3_PROFILE_PROVIDER_NAMES:
        raise RuntimeError(
            f"embedding profile {getattr(profile, 'profile_id', '<unknown>')} uses "
            f"unsupported provider {profile_provider!r}; this rollout supports Qwen3 "
            "profiles served by Databricks or vLLM"
        )

    assert_embedding_runtime_available("qwen3")
    provider = get_embedding_provider("qwen3")
    settings = get_settings()
    expected = {
        "model": str(getattr(profile, "model", "")),
        "dimension": int(getattr(profile, "dimension", 0)),
        "revision": str(getattr(profile, "revision", "")),
        "query_instruction_version": str(getattr(profile, "query_instruction_version", "")),
        "preprocessing_version": str(getattr(profile, "preprocessing_version", "")),
    }
    actual = {
        "model": str(getattr(provider, "model", "")),
        "dimension": int(provider.dim),
        "revision": str(getattr(settings, "qwen_embedding_revision", "") or ""),
        "query_instruction_version": str(getattr(provider, "query_instruction_version", "")),
        "preprocessing_version": QWEN3_DOCUMENT_PREPROCESSING_VERSION,
    }
    mismatches = [
        f"{field}={actual[field]!r} (profile requires {expected[field]!r})"
        for field in expected
        if actual[field] != expected[field]
    ]
    if str(getattr(profile, "dtype", "")).lower() not in {"float32", "fp32"}:
        mismatches.append(f"dtype='float32' (profile requires {getattr(profile, 'dtype', '')!r})")
    if str(getattr(profile, "normalization", "")).lower() not in {
        "l2",
        "unit",
        "unit-norm",
        "unit_norm",
    }:
        mismatches.append(
            "normalization='l2' " f"(profile requires {getattr(profile, 'normalization', '')!r})"
        )
    if mismatches:
        raise RuntimeError(
            f"configured Qwen endpoint does not match embedding profile "
            f"{getattr(profile, 'profile_id', '<unknown>')}: " + "; ".join(mismatches)
        )
    return provider
