"""Embedding providers with explicit query/document semantics.

A provider returns unit-norm float vectors. Retrieval queries and indexed
documents have separate methods because instruction-tuned models such as Qwen3
embed them asymmetrically. ``embed()`` remains the document-style compatibility
surface for older providers and callers.
"""

from __future__ import annotations

import hashlib
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

from regwatch.common.logging import get_logger

log = get_logger(__name__)

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

# Conservative bulk-embedding defaults, sized against the Databricks endpoint's
# per-request cap measured live on 2026-08-13: ~140-token inputs passed at 24
# per request and 429'd at 32. The cap behaves as a token budget, not an input
# count, so token-aware packing is the primary guard and the item cap is a
# backstop. config/settings.py mirrors these defaults for the env vars; a test
# pins the two together.
QWEN3_DEFAULT_BATCH_SIZE = 8
QWEN3_DEFAULT_REQUEST_TOKEN_BUDGET = 3000

# OpenAI embeddings (migration target). text-embedding-3-large is natively
# 3072-dim; production serves it Matryoshka-truncated to 1024 through the API's
# `dimensions` parameter, which is what keeps the vector footprint inside the
# Lakebase branch size cap.
OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"
OPENAI_API_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_DIMENSION = 1024
# text-embedding-3-large's native width; a `dimensions` request above it is a
# hard 400, so refuse locally instead of paying the round trip.
OPENAI_MAX_DIMENSION = 3072
# OpenAI embeddings are SYMMETRIC: queries and passages get byte-identical
# handling. A Qwen-style "Instruct: ...\nQuery:" preamble would embed the
# instruction text itself into the query vector and skew every cosine score
# against passage vectors that never saw it. This constant records "no
# instruction" as a versioned policy so an embedding profile can pin it.
# The literal "none" is the vocabulary the registered OpenAI profile uses; a
# profile that says anything else was produced by a DIFFERENT query pipeline
# and is refused by _assert_configuration_matches_profile.
OPENAI_QUERY_INSTRUCTION_VERSION = "none"

# Text is sent without model-specific query/document prefixes or semantic
# rewriting. The API ultimately receives strings, so "byte-for-byte" is
# slightly stronger than what we actually guarantee.
OPENAI_DOCUMENT_PREPROCESSING_VERSION = "text-v1"

# Request packing limits.
OPENAI_DEFAULT_BATCH_SIZE = 256
OPENAI_MAX_BATCH_SIZE = 2048

# OpenAI also caps the sum of all input tokens in one embeddings request.
OPENAI_MAX_BATCH_TOKENS = 300_000
OPENAI_TARGET_BATCH_TOKENS = 275_000  # optional safety margin

# Maximum tokens in any one input.
OPENAI_MAX_INPUT_TOKENS = 8192

OPENAI_DEFAULT_MAX_RETRIES = 3

_OPENAI_PROVIDER_NAMES = frozenset(
    {
        "openai",
        "openai-embedding",
        "openai-embeddings",
    }
)
_OPENAI_PROFILE_PROVIDER_NAMES = _OPENAI_PROVIDER_NAMES


class EmbeddingInputTooLargeError(RuntimeError):
    """A single input exceeds the endpoint's per-request budget on its own.

    Raised instead of a generic 429 so bulk callers can identify, shorten, or
    skip the one poisonous chunk rather than losing the whole job to it. The
    packer always isolates such an input into its own request first, so
    neighboring chunks embed normally before this surfaces.
    """

    def __init__(self, index: int, estimated_tokens: int, budget: int) -> None:
        super().__init__(
            f"embedding input {index} is ~{estimated_tokens} estimated tokens, "
            f"over the per-request budget of {budget}; shorten or split this "
            "chunk before re-running"
        )
        self.index = index
        self.estimated_tokens = estimated_tokens
        self.budget = budget


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


# Process-wide memo for embed_query results. Deterministic follow-up rewrites
# ("why?") re-embed the exact same canonical query string, and embedding is the
# serial step before synthesis (issue #221), so a repeat should skip the HTTP
# round-trip. Keyed by provider VALUES, not object identity: retrieve() builds
# a fresh provider instance per call, so an instance-level cache would never
# hit. Values are immutable tuples so a caller mutating a returned list cannot
# poison later hits. 256 entries of 1024 float64s is roughly 2 MB.
_QUERY_EMBEDDING_CACHE_LOCK = Lock()
_QUERY_EMBEDDING_CACHE: OrderedDict[tuple[str, ...], tuple[float, ...]] = OrderedDict()
_QUERY_EMBEDDING_CACHE_MAX_SIZE = 256


def _query_embedding_cache_key(provider: Any, query: str) -> tuple[str, ...] | None:
    """Value-based identity of everything that determines a query vector.

    Returns None -- which bypasses the cache -- unless the provider exposes its
    full geometry: name, model, dim, query instruction text and version. Of the
    in-repo providers only Qwen3EmbeddingProvider (the production Ask path)
    does; echo is compute-only and has no instruction fields. A provider whose
    geometry cannot be read here must never share entries with one whose
    geometry can.
    """
    name = getattr(provider, "name", None)
    model = getattr(provider, "model", None)
    dim = getattr(provider, "dim", None)
    instruction = getattr(provider, "query_instruction", None)
    version = getattr(provider, "query_instruction_version", None)
    if not (isinstance(name, str) and name):
        return None
    if not (isinstance(model, str) and model):
        return None
    if not isinstance(dim, int):
        return None
    if not (isinstance(instruction, str) and instruction):
        return None
    if not (isinstance(version, str) and version):
        return None
    base_url = getattr(provider, "base_url", None)
    return (name, model, str(dim), str(base_url or ""), instruction, version, query)


def reset_query_embedding_cache_for_tests() -> None:
    """Clears the process-wide query-embedding cache (test isolation only)."""
    with _QUERY_EMBEDDING_CACHE_LOCK:
        _QUERY_EMBEDDING_CACHE.clear()


def embed_query(provider: Any, query: str) -> list[float]:
    """Embed one retrieval query, with a legacy ``embed`` fallback.

    The fallback preserves compatibility with test doubles and third-party
    providers that predate the asymmetric contract. New providers should
    implement ``embed_query`` directly.

    Successful results are memoized process-wide for providers that expose
    their full geometry (see _query_embedding_cache_key); errors are never
    cached. The provider call runs OUTSIDE the cache lock so a slow embedding
    request never serializes unrelated queries; two threads racing the same
    cold key may therefore both embed it, which is harmless (both compute the
    same vector, last write wins).
    """
    key = None
    if getattr(get_settings(), "query_embedding_cache_enabled", True):
        key = _query_embedding_cache_key(provider, query)
    if key is not None:
        with _QUERY_EMBEDDING_CACHE_LOCK:
            cached = _QUERY_EMBEDDING_CACHE.get(key)
            if cached is not None:
                _QUERY_EMBEDDING_CACHE.move_to_end(key)
                return list(cached)
    method = getattr(provider, "embed_query", None)
    if callable(method):
        vector = method(query)
    else:
        vectors = provider.embed([query])
        if len(vectors) != 1:
            raise RuntimeError(
                f"embedding provider returned {len(vectors)} query vectors for one query"
            )
        vector = vectors[0]
    if key is not None:
        with _QUERY_EMBEDDING_CACHE_LOCK:
            _QUERY_EMBEDDING_CACHE[key] = tuple(vector)
            _QUERY_EMBEDDING_CACHE.move_to_end(key)
            while len(_QUERY_EMBEDDING_CACHE) > _QUERY_EMBEDDING_CACHE_MAX_SIZE:
                _QUERY_EMBEDDING_CACHE.popitem(last=False)
    return vector


def embed_documents(provider: Any, texts: list[str]) -> list[list[float]]:
    """Embed indexable documents, with a legacy ``embed`` fallback."""
    method = getattr(provider, "embed_documents", None)
    if callable(method):
        return method(texts)
    return provider.embed(texts)


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


def _full_jitter_delay_s(base_s: float, cap_s: float, retry_index: int) -> float:
    """Returns one full-jitter backoff delay, in seconds.

    Full jitter draws uniformly from ``[0, min(cap, base * 2**retry_index)]``
    rather than adding a small jitter on top of the whole exponential delay.
    Equal jitter left a hard floor under every waiting client, so callers that
    one 429 burst rejected together also woke up together. The served
    embedding endpoint rate-limits above roughly 24 inputs per request, which
    makes those synchronized retry waves routine during a bulk embed; drawing
    from zero is what actually spreads them out.

    Args:
      base_s: Delay ceiling for the first retry, in seconds.
      cap_s: Upper bound on the ceiling, in seconds.
      retry_index: 0 for the first retry, 1 for the second, and so on.

    Returns:
      Seconds to sleep before the next attempt.
    """
    ceiling = min(cap_s, base_s * (2**retry_index))
    # Retry jitter, not a cryptographic draw.
    return random.uniform(0, ceiling)  # noqa: S311


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
    # Deliberately low chars-per-token divisor (English runs ~4): overestimating
    # cost shrinks batches, which only costs extra requests, never a 429.
    _chars_per_token_estimate: ClassVar[int] = 3
    # The endpoint reports rate limiting and the size cap with the same bare
    # 429, so an ambiguous 429 gets this many same-size attempts (with backoff
    # between) before splitting is used as the probe that tells the two apart.
    _split_after_attempts: ClassVar[int] = 2
    _size_rejection_markers: ClassVar[tuple[str, ...]] = (
        "token",
        "too large",
        "payload",
        "context length",
        "input length",
        "input size",
    )

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
        batch_size: int = QWEN3_DEFAULT_BATCH_SIZE,
        request_token_budget: int = QWEN3_DEFAULT_REQUEST_TOKEN_BUDGET,
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
        self.request_token_budget = int(request_token_budget)
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
        if not 1 <= self.request_token_budget <= 65536:
            raise ValueError("Qwen embedding request token budget must be in [1, 65536]")
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

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        """Conservative token overestimate for request packing."""
        divisor = cls._chars_per_token_estimate
        return max(1, (len(text) + divisor - 1) // divisor)

    def _pack_spans(self, texts: list[str]) -> list[tuple[int, int]]:
        """Greedy consecutive packing under the token budget and item cap.

        An input whose estimate alone exceeds the budget gets its own span, so
        its rejection cannot poison neighboring chunks.
        """
        spans: list[tuple[int, int]] = []
        start = 0
        tokens = 0
        for index, text in enumerate(texts):
            cost = self._estimate_tokens(text)
            if index > start and (
                index - start >= self.batch_size or tokens + cost > self.request_token_budget
            ):
                spans.append((start, index))
                start = index
                tokens = 0
            tokens += cost
        spans.append((start, len(texts)))
        return spans

    @classmethod
    def _rejection_text(cls, exc: Exception) -> str:
        parts = [str(exc)]
        body = getattr(exc, "body", None)
        if body is not None:
            parts.append(str(body))
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                text = response.text
            except Exception:  # an unread streamed body must not mask the original error
                text = None
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts).lower()

    @classmethod
    def _is_size_rejection(cls, exc: Exception) -> bool:
        """Whether the endpoint rejected the request for its size, not its rate.

        413 is unambiguous. Databricks reports both rate limits and the
        per-request cap as 429 REQUEST_LIMIT_EXCEEDED, so 400/429 bodies are
        sniffed for size/token wording; a bare 429 stays classified as rate.
        """
        status = cls._status_code(exc)
        if status == 413:
            return True
        if status not in (400, 429):
            return False
        text = cls._rejection_text(exc)
        return any(marker in text for marker in cls._size_rejection_markers)

    def _should_split(self, exc: Exception, attempt: int, batch_len: int) -> bool:
        if batch_len <= 1:
            return False
        if self._is_size_rejection(exc):
            return True
        return self._status_code(exc) == 429 and attempt >= self._split_after_attempts

    def _embed_span(
        self,
        client: Any,
        texts: list[str],
        start: int,
        stop: int,
    ) -> list[list[float]]:
        """Embed ``texts[start:stop]`` as one request, splitting on size rejections.

        Rate-limit and transient errors back off and retry the same batch. A
        size-classified rejection - or a 429 that survives backoff - splits the
        batch in half instead of re-sending an identical oversized request. A
        single input that is still rejected for size raises
        ``EmbeddingInputTooLargeError`` naming its absolute index.
        """
        batch = list(texts[start:stop])
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._request_once(client, batch)
            except Exception as exc:
                if self._should_split(exc, attempt, len(batch)):
                    mid = start + (len(batch) + 1) // 2
                    left = self._embed_span(client, texts, start, mid)
                    return left + self._embed_span(client, texts, mid, stop)
                if len(batch) == 1 and self._is_size_rejection(exc):
                    raise EmbeddingInputTooLargeError(
                        start,
                        self._estimate_tokens(batch[0]),
                        self.request_token_budget,
                    ) from exc
                if attempt >= self._max_attempts or not self._is_retryable(exc):
                    if (
                        len(batch) == 1
                        and self._status_code(exc) == 429
                        and self._estimate_tokens(batch[0]) > self.request_token_budget
                    ):
                        # Isolated over-budget input that never cleared 429:
                        # the size cap is the overwhelmingly likely cause.
                        raise EmbeddingInputTooLargeError(
                            start,
                            self._estimate_tokens(batch[0]),
                            self.request_token_budget,
                        ) from exc
                    raise
                time.sleep(
                    _full_jitter_delay_s(self._backoff_base_s, self._backoff_cap_s, attempt - 1)
                )
            else:
                return self._validated_batch(response, len(batch))
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
        for start, stop in self._pack_spans(texts):
            out.extend(self._embed_span(client, texts, start, stop))
        return out

    def embed_query(self, query: str) -> list[float]:
        instructed = f"Instruct: {self.query_instruction}\nQuery:{query}"
        return self._embed_inputs([instructed])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_inputs(texts)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Backward-compatible document embedding; never instruct documents."""
        return self.embed_documents(texts)


@lru_cache(maxsize=8)
def _shared_openai_http_client(base_url: str, token: str, timeout_s: float) -> httpx.Client:
    """One connection-pooled client per OpenAI endpoint/credential tuple."""
    return httpx.Client(
        base_url=f"{base_url.rstrip('/')}/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=timeout_s,
    )


# One log line per process the first time a returned vector needed
# renormalizing. Per-instance state would log on every call: retrieve() builds
# a fresh provider for each question.
_OPENAI_RENORMALIZED_LOGGED = False


class OpenAIEmbeddingProvider:
    """OpenAI ``/v1/embeddings`` (the text-embedding-3 family).

    Deliberately NOT a subclass of, or a copy of, the Qwen arm. Three
    differences are load-bearing for retrieval correctness:

    * **No instruction prefix.** ``embed_query`` sends the query verbatim.
      See OPENAI_QUERY_INSTRUCTION_VERSION for why prefixing corrupts scores.
    * **Vectors are renormalized, not asserted.** The model returns unit-norm
      vectors at its native width, but a Matryoshka truncation via
      ``dimensions`` keeps only a prefix of that vector, whose norm is only
      approximately 1. pgvector cosine ordering and the stored-dot-product
      assumptions downstream want unit vectors, so divide by the measured norm
      instead of raising. Only a zero, NaN, or infinite vector is fatal.
    * **Large batches.** OpenAI takes up to 2048 inputs per request.

    The client is duck-typed the same way as the Qwen arm: an injected object
    exposing ``embeddings.create(**payload)`` is used if present, otherwise
    ``post("embeddings", json=payload)``. The OpenAI SDK is deliberately not
    imported -- an import-linter contract confines it to the LLM client seam.
    """

    name = "openai"
    default_model = OPENAI_EMBEDDING_MODEL
    default_dim = OPENAI_DEFAULT_DIMENSION
    # Non-empty ON PURPOSE, and never prepended to anything. The process-wide
    # query cache (_query_embedding_cache_key) refuses to memoize a provider
    # that cannot report its full geometry, including an instruction string; a
    # provider returning "" here would silently lose that cache. The value is
    # phrased so it cannot be mistaken for an instruction to send.
    query_instruction = "(none: OpenAI embeddings are symmetric, no instruction prefix)"
    query_instruction_version = OPENAI_QUERY_INSTRUCTION_VERSION

    _backoff_base_s: ClassVar[float] = 1.0
    _backoff_cap_s: ClassVar[float] = 30.0
    # Tolerance for "already unit norm"; outside it the vector is scaled.
    _unit_norm_tolerance: ClassVar[float] = 1e-3
    # cl100k averages ~4 chars/token; only used to report an over-long input.
    _chars_per_token_estimate: ClassVar[int] = 4
    _size_rejection_markers: ClassVar[tuple[str, ...]] = (
        "maximum context length",
        "token",
        "too large",
        "too many",
        "payload",
        "input length",
        "input size",
    )

    def __init__(
        self,
        *,
        client: Any = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = OPENAI_EMBEDDING_MODEL,
        dim: int = OPENAI_DEFAULT_DIMENSION,
        batch_size: int = OPENAI_DEFAULT_BATCH_SIZE,
        timeout_s: float = 60.0,
        max_retries: int = OPENAI_DEFAULT_MAX_RETRIES,
    ) -> None:
        self._client = client
        self.base_url = (base_url or "").strip() or OPENAI_API_BASE_URL
        self._api_key = (api_key or "").strip() or None
        self.model = model.strip()
        self.dim = int(dim)
        self.batch_size = int(batch_size)
        self.timeout_s = float(timeout_s)
        self.max_retries = int(max_retries)

        if not self.model:
            raise ValueError("OpenAI embedding model must not be empty")
        if not 1 <= self.dim <= OPENAI_MAX_DIMENSION:
            raise ValueError(f"OpenAI embedding dimension must be in [1, {OPENAI_MAX_DIMENSION}]")
        if not 1 <= self.batch_size <= OPENAI_MAX_BATCH_SIZE:
            raise ValueError(f"OpenAI embedding batch size must be in [1, {OPENAI_MAX_BATCH_SIZE}]")
        if self.timeout_s <= 0:
            raise ValueError("OpenAI embedding timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("OpenAI embedding max retries must not be negative")

    @property
    def _max_attempts(self) -> int:
        return self.max_retries + 1

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY to be set")
        self._client = _shared_openai_http_client(
            self.base_url,
            self._api_key,
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

    @classmethod
    def _rejection_text(cls, exc: Exception) -> str:
        parts = [str(exc)]
        body = getattr(exc, "body", None)
        if body is not None:
            parts.append(str(body))
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                text = response.text
            except Exception:  # an unread streamed body must not mask the error
                text = None
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts).lower()

    @classmethod
    def _is_size_rejection(cls, exc: Exception) -> bool:
        """Whether the request was refused for its size rather than its rate.

        OpenAI reports rate limiting as a bare 429 and an oversized request as
        a 400 whose message names the context length or the input count, so
        unlike the Databricks endpoint the two are separable without probing.
        """
        status = cls._status_code(exc)
        if status == 413:
            return True
        if status != 400:
            return False
        text = cls._rejection_text(exc)
        return any(marker in text for marker in cls._size_rejection_markers)

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        divisor = cls._chars_per_token_estimate
        return max(1, (len(text) + divisor - 1) // divisor)

    def _request_once(self, client: Any, batch: list[str]) -> Any:
        payload = {
            "model": self.model,
            "input": batch,
            # Matryoshka truncation. Sent on every request: the stored vectors
            # only match the profile geometry because of this parameter.
            "dimensions": self.dim,
            # Explicit because the OpenAI SDK defaults to base64 while the raw
            # REST API defaults to float; pinning it keeps both client shapes
            # returning the same thing.
            "encoding_format": "float",
        }
        embeddings_api = getattr(client, "embeddings", None)
        create = getattr(embeddings_api, "create", None)
        if callable(create):
            return create(**payload)

        post = getattr(client, "post", None)
        if not callable(post):
            raise TypeError(
                "OpenAI embedding client must expose embeddings.create(...) or post(...)"
            )
        response = post("embeddings", json=payload)
        response.raise_for_status()
        return response.json()

    def _spans(self, count: int) -> list[tuple[int, int]]:
        """Fixed-width request spans; OpenAI's cap is inputs, not tokens."""
        return [
            (start, min(start + self.batch_size, count))
            for start in range(0, count, self.batch_size)
        ]

    def _embed_span(
        self,
        client: Any,
        texts: list[str],
        start: int,
        stop: int,
    ) -> list[list[float]]:
        """Embed ``texts[start:stop]`` as one request, splitting when oversized.

        Rate-limit (429), 5xx and transport failures back off with full jitter
        and retry the same batch, bounded by ``max_retries``. A size rejection
        halves the batch rather than re-sending an identical oversized request;
        a single input still rejected raises ``EmbeddingInputTooLargeError``
        naming its absolute index, so a bulk caller can skip one poisonous
        chunk instead of losing the job.
        """
        batch = list(texts[start:stop])
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._request_once(client, batch)
            except Exception as exc:
                if self._is_size_rejection(exc):
                    if len(batch) > 1:
                        mid = start + (len(batch) + 1) // 2
                        left = self._embed_span(client, texts, start, mid)
                        return left + self._embed_span(client, texts, mid, stop)
                    raise EmbeddingInputTooLargeError(
                        start,
                        self._estimate_tokens(batch[0]),
                        OPENAI_MAX_INPUT_TOKENS,
                    ) from exc
                if attempt >= self._max_attempts or not self._is_retryable(exc):
                    raise
                time.sleep(
                    _full_jitter_delay_s(self._backoff_base_s, self._backoff_cap_s, attempt - 1)
                )
            else:
                return self._validated_batch(response, len(batch))
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _response_field(value: Any, field: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(field)
        return getattr(value, field, None)

    def _normalize(self, vector: list[float]) -> list[float]:
        """Scales one vector to unit norm, refusing only degenerate ones.

        Unlike the Qwen arm, a norm away from 1.0 is expected here rather than
        a bug: truncating a unit vector to its first ``dimensions`` components
        shortens it, so an assertion would fire on healthy output. Zero, NaN
        and infinite vectors ARE still fatal -- they cannot be scaled and would
        poison every cosine comparison silently.

        The division is unconditional. A live 1024-dim call returned norms of
        0.99994884 / 0.99978212 / 1.00012535 (2026-08-20), inside any sane
        tolerance yet not 1.0; leaving those alone would make stored vectors
        "unit norm to within a tolerance", which is exactly the ambiguity the
        dot-product-equals-cosine assumption downstream cannot afford. The
        tolerance only decides whether the deviation is worth a log line.
        """
        global _OPENAI_RENORMALIZED_LOGGED
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError("OpenAI embedding contains a non-finite value")
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 0.0:
            raise RuntimeError(f"OpenAI embedding has a degenerate L2 norm: {norm!r}")
        if not _OPENAI_RENORMALIZED_LOGGED and not math.isclose(
            norm,
            1.0,
            rel_tol=self._unit_norm_tolerance,
            abs_tol=self._unit_norm_tolerance,
        ):
            _OPENAI_RENORMALIZED_LOGGED = True
            log.info(
                "openai_embedding_renormalized",
                model=self.model,
                dim=self.dim,
                norm=round(norm, 8),
            )
        return [value / norm for value in vector]

    def _validated_batch(self, response: Any, expected_count: int) -> list[list[float]]:
        raw_data = self._response_field(response, "data")
        if raw_data is None:
            raise RuntimeError("OpenAI embeddings response is missing data")
        try:
            items = list(raw_data)
        except TypeError as exc:
            raise RuntimeError("OpenAI embeddings response data is not a sequence") from exc
        if len(items) != expected_count:
            raise RuntimeError(
                f"OpenAI embeddings returned {len(items)} vectors for {expected_count} inputs"
            )

        # Response order is not part of the API contract; `index` is.
        indexed: list[tuple[int, Any]] = []
        for item in items:
            raw_index = self._response_field(item, "index")
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("OpenAI embedding item has an invalid index") from exc
            indexed.append((index, item))
        indexed.sort(key=lambda pair: pair[0])
        indices = [index for index, _ in indexed]
        if indices != list(range(expected_count)):
            raise RuntimeError(
                f"OpenAI embeddings returned invalid indices {indices!r} "
                f"for {expected_count} inputs"
            )

        vectors: list[list[float]] = []
        for _index, item in indexed:
            raw_embedding = self._response_field(item, "embedding")
            try:
                vector = [float(value) for value in raw_embedding]
            except (TypeError, ValueError) as exc:
                raise RuntimeError("OpenAI embedding is not a numeric vector") from exc
            if len(vector) != self.dim:
                raise RuntimeError(
                    f"OpenAI embedding has {len(vector)} dims, expected {self.dim} ({self.model})"
                )
            vectors.append(self._normalize(vector))
        return vectors

    def _embed_inputs(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        for index, text in enumerate(texts):
            if not text.strip():
                # OpenAI 400s on an empty input. Naming the index beats losing
                # a whole backfill batch to "'$.input' is invalid".
                raise ValueError(f"OpenAI embedding input {index} is empty")
        client = self._client_or_create()
        out: list[list[float]] = []
        for start, stop in self._spans(len(texts)):
            out.extend(self._embed_span(client, texts, start, stop))
        return out

    def embed_query(self, query: str) -> list[float]:
        """Embeds one query VERBATIM -- no instruction prefix. See class docs."""
        return self._embed_inputs([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_inputs(texts)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Backward-compatible document embedding."""
        return self.embed_documents(texts)


def _require_provider_name(name: str | None) -> str:
    """Resolve a provider name from the argument or settings, or refuse.

    EMBEDDING_PROVIDER deliberately has no default. The 2026-08-14 backfill
    outage was a worker booted without it silently falling back to a local
    384-dim model: every fresh document paid its fetch/parse/OCR cost and then
    failed the same 1536-dim write. A missing provider is a configuration
    error, and configuration errors refuse at resolution time, before any
    document work is spent.
    """
    resolved = (name or get_settings().embedding_provider or "").strip().lower()
    if not resolved:
        raise RuntimeError(
            "EMBEDDING_PROVIDER is not set and has no default. Set "
            "EMBEDDING_PROVIDER=openai (text-embedding-3-large), "
            "EMBEDDING_PROVIDER=qwen3 (the Databricks serving endpoint) "
            "or EMBEDDING_PROVIDER=echo (tests only); this process refuses to "
            "guess an embedding space."
        )
    return resolved


def assert_embedding_runtime_available(name: str | None = None) -> None:
    """Boot-time fail-fast: the configured provider must be fully usable.

    The Qwen provider reads its endpoint credentials lazily on first
    ``embed()``, so a process missing QWEN_EMBEDDING_BASE_URL/TOKEN would
    otherwise boot cleanly, report healthy, and then fail every query/ingest
    at embed time. The API lifespan and the corpus preflight call this so the
    misconfiguration refuses to start with the same remediation message the
    lazy path would raise. An unset EMBEDDING_PROVIDER refuses here too.
    """
    resolved = _require_provider_name(name)
    if resolved in _QWEN3_PROVIDER_NAMES:
        settings = get_settings()
        if not str(getattr(settings, "qwen_embedding_base_url", "") or "").strip():
            raise RuntimeError(
                "EMBEDDING_PROVIDER=qwen3 requires QWEN_EMBEDDING_BASE_URL to be set"
            )
        if not str(getattr(settings, "qwen_embedding_token", "") or "").strip():
            raise RuntimeError("EMBEDDING_PROVIDER=qwen3 requires QWEN_EMBEDDING_TOKEN to be set")
        return
    if resolved in _OPENAI_PROVIDER_NAMES:
        settings = get_settings()
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY to be set")
        # No fallback to the module constant: the model name IS the embedding
        # space, and guessing it is how a corpus silently ends up in two.
        if not str(getattr(settings, "openai_embedding_model", "") or "").strip():
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai requires OPENAI_EMBEDDING_MODEL to be set "
                f"(production uses {OPENAI_EMBEDDING_MODEL})"
            )
        return
    if resolved != "echo":
        raise ValueError(f"unknown embedding provider: {resolved}")


def _configured_qwen3_model() -> str:
    settings = get_settings()
    return str(
        getattr(settings, "qwen_embedding_model", QWEN3_EMBEDDING_MODEL) or QWEN3_EMBEDDING_MODEL
    )


def _configured_qwen3_dimension() -> int:
    return int(getattr(get_settings(), "qwen_embedding_dimension", None) or 1536)


def _configured_qwen3_query_instruction_version() -> str:
    settings = get_settings()
    return str(
        getattr(
            settings,
            "qwen_embedding_query_instruction_version",
            QWEN3_QUERY_INSTRUCTION_VERSION,
        )
        or QWEN3_QUERY_INSTRUCTION_VERSION
    )


def _build_qwen3_provider(
    *,
    model: str,
    dim: int,
    query_instruction_version: str,
) -> EmbeddingProvider:
    """Builds the Qwen3 arm with an EXPLICIT geometry.

    Credentials, batching and timeouts come from settings; model, dimension
    and instruction version are passed in, so the profile path can demand its
    own geometry rather than inheriting whatever the environment happens to
    say.
    """
    settings = get_settings()
    return Qwen3EmbeddingProvider(
        base_url=getattr(settings, "qwen_embedding_base_url", None),
        token=getattr(settings, "qwen_embedding_token", None),
        model=model,
        dim=dim,
        query_instruction=str(
            getattr(settings, "qwen_embedding_query_instruction", QWEN3_QUERY_INSTRUCTION)
            or QWEN3_QUERY_INSTRUCTION
        ),
        query_instruction_version=query_instruction_version,
        batch_size=int(
            getattr(settings, "qwen_embedding_batch_size", None) or QWEN3_DEFAULT_BATCH_SIZE
        ),
        request_token_budget=int(
            getattr(settings, "qwen_embedding_request_token_budget", None)
            or QWEN3_DEFAULT_REQUEST_TOKEN_BUDGET
        ),
        timeout_s=float(getattr(settings, "llm_timeout_s", None) or 60.0),
    )


def _configured_openai_model() -> str:
    return str(getattr(get_settings(), "openai_embedding_model", "") or "").strip()


def _configured_openai_dimension() -> int:
    return int(
        getattr(get_settings(), "openai_embedding_dimension", None) or OPENAI_DEFAULT_DIMENSION
    )


def _build_openai_provider(*, model: str, dim: int) -> EmbeddingProvider:
    """Builds the OpenAI arm with an EXPLICIT geometry (see _build_qwen3_provider)."""
    settings = get_settings()
    raw_retries = getattr(settings, "openai_max_retries", None)
    return OpenAIEmbeddingProvider(
        base_url=str(getattr(settings, "openai_base_url", "") or OPENAI_API_BASE_URL),
        api_key=getattr(settings, "openai_api_key", None),
        model=model,
        dim=dim,
        batch_size=int(
            getattr(settings, "openai_embedding_batch_size", None) or OPENAI_DEFAULT_BATCH_SIZE
        ),
        timeout_s=float(getattr(settings, "openai_timeout_s", None) or 60.0),
        # `or` would turn a deliberate 0 (no retries) back into the default.
        max_retries=(OPENAI_DEFAULT_MAX_RETRIES if raw_retries is None else int(raw_retries)),
    )


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Factory. Falls back to settings; refuses when nothing is configured."""
    name = _require_provider_name(name)
    if name == "echo":
        return EchoEmbeddingProvider()
    if name in _QWEN3_PROVIDER_NAMES:
        return _build_qwen3_provider(
            model=_configured_qwen3_model(),
            dim=_configured_qwen3_dimension(),
            query_instruction_version=_configured_qwen3_query_instruction_version(),
        )
    if name in _OPENAI_PROVIDER_NAMES:
        model = _configured_openai_model()
        if not model:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai requires OPENAI_EMBEDDING_MODEL to be set "
                f"(production uses {OPENAI_EMBEDDING_MODEL})"
            )
        return _build_openai_provider(model=model, dim=_configured_openai_dimension())
    raise ValueError(f"unknown embedding provider: {name}")


def _assert_configuration_matches_profile(
    profile: Any,
    configured: dict[str, Any],
    *,
    endpoint: str,
) -> None:
    """Refuses unless the ambient endpoint configuration serves this profile.

    ``configured`` is what the process is set up to talk to; the profile row is
    what the stored vectors were produced with. Any disagreement means the next
    embed would write (or query with) vectors from a different space, so it is
    a hard refusal rather than a warning. ``revision`` remains an operator
    assertion: the endpoint/deployment must be pinned to that revision.
    """
    expected = {
        "model": str(getattr(profile, "model", "")),
        "dimension": int(getattr(profile, "dimension", 0)),
        "revision": str(getattr(profile, "revision", "")),
        "query_instruction_version": str(getattr(profile, "query_instruction_version", "")),
        "preprocessing_version": str(getattr(profile, "preprocessing_version", "")),
    }
    mismatches = [
        f"{field}={configured[field]!r} (profile requires {expected[field]!r})"
        for field in expected
        if configured[field] != expected[field]
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
            f"configured {endpoint} endpoint does not match embedding profile "
            f"{getattr(profile, 'profile_id', '<unknown>')}: " + "; ".join(mismatches)
        )


def _qwen3_provider_for_profile(profile: Any) -> EmbeddingProvider:
    assert_embedding_runtime_available("qwen3")
    configured = {
        "model": _configured_qwen3_model(),
        "dimension": _configured_qwen3_dimension(),
        "revision": str(getattr(get_settings(), "qwen_embedding_revision", "") or ""),
        "query_instruction_version": _configured_qwen3_query_instruction_version(),
        "preprocessing_version": QWEN3_DOCUMENT_PREPROCESSING_VERSION,
    }
    _assert_configuration_matches_profile(profile, configured, endpoint="Qwen")
    # Built from the PROFILE, not from settings. The check above proves the two
    # agree today; sourcing the geometry here from the profile means a future
    # loosening of that check cannot silently reintroduce ambient drift.
    return _build_qwen3_provider(
        model=str(getattr(profile, "model", "")),
        dim=int(getattr(profile, "dimension", 0)),
        query_instruction_version=str(getattr(profile, "query_instruction_version", "")),
    )


def _openai_provider_for_profile(profile: Any) -> EmbeddingProvider:
    assert_embedding_runtime_available("openai")
    settings = get_settings()
    model = _configured_openai_model()
    configured = {
        "model": model,
        "dimension": _configured_openai_dimension(),
        # OpenAI exposes no revision separate from the model id -- the snapshot
        # IS the name -- so the model doubles as the revision unless an
        # explicit OPENAI_EMBEDDING_REVISION is ever configured.
        "revision": str(getattr(settings, "openai_embedding_revision", "") or "").strip() or model,
        "query_instruction_version": OPENAI_QUERY_INSTRUCTION_VERSION,
        "preprocessing_version": OPENAI_DOCUMENT_PREPROCESSING_VERSION,
    }
    _assert_configuration_matches_profile(profile, configured, endpoint="OpenAI")
    return _build_openai_provider(
        model=str(getattr(profile, "model", "")),
        dim=int(getattr(profile, "dimension", 0)),
    )


def get_embedding_provider_for_profile(profile: Any) -> EmbeddingProvider:
    """Build a provider that exactly matches one immutable embedding profile.

    Dispatch is on ``profile.provider`` and nothing else. Qwen3@1024 and
    OpenAI@1024 are the same width, so a factory that read the profile's
    provider and then built one fixed arm anyway would return vectors from the
    wrong embedding space with no error at any layer: they would write, index
    and query cleanly, and simply retrieve nonsense. That is the single most
    dangerous failure mode of the OpenAI migration, so it is a dispatch, not a
    validation.

    Profile-backed retrieval and backfill must not use the global legacy
    provider by accident. The serving endpoint is configured outside the
    database, so every geometry-defining value that can be checked locally is
    compared before a request is sent, and the returned provider takes its
    model and dimension from the profile itself.
    """
    profile_provider = str(getattr(profile, "provider", "") or "").strip().lower()
    if profile_provider in _OPENAI_PROFILE_PROVIDER_NAMES:
        return _openai_provider_for_profile(profile)
    if profile_provider in _QWEN3_PROFILE_PROVIDER_NAMES:
        return _qwen3_provider_for_profile(profile)
    raise RuntimeError(
        f"embedding profile {getattr(profile, 'profile_id', '<unknown>')} uses "
        f"unsupported provider {profile_provider!r}; supported providers are "
        "OpenAI and Qwen3 (served by Databricks or vLLM)"
    )
