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
            "EMBEDDING_PROVIDER=qwen3 (the Databricks serving endpoint, prod) "
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
    if resolved != "echo":
        raise ValueError(f"unknown embedding provider: {resolved}")


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Factory. Falls back to settings; refuses when nothing is configured."""
    settings = get_settings()
    name = _require_provider_name(name)
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
            batch_size=int(
                getattr(settings, "qwen_embedding_batch_size", None) or QWEN3_DEFAULT_BATCH_SIZE
            ),
            request_token_budget=int(
                getattr(settings, "qwen_embedding_request_token_budget", None)
                or QWEN3_DEFAULT_REQUEST_TOKEN_BUDGET
            ),
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
