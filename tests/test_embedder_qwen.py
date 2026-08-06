"""Offline contract tests for asymmetric Qwen3 embeddings."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from regwatch.ingest import pipeline as pipeline_module
from regwatch.ingest.pdf_parser import ParsedPdf
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.process.chunker import Chunk
from regwatch.process.embedder import (
    QWEN3_EMBEDDING_MODEL,
    Qwen3EmbeddingProvider,
    embed_documents,
    embed_query,
    get_embedding_provider,
)
from regwatch.retrieve import retriever as retriever_module
from regwatch.retrieve.mode import RetrievalMode

DIM = 32


def _unit_vector(dim: int, coordinate: int = 0) -> list[float]:
    vector = [0.0] * dim
    vector[coordinate % dim] = 1.0
    return vector


class _ApiError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"api error {status_code}")
        self.status_code = status_code


class _FakeEmbeddingsApi:
    def __init__(
        self,
        *,
        errors: list[Exception] | None = None,
        vector: list[float] | None = None,
    ) -> None:
        self.errors = list(errors or [])
        self.vector = vector
        self.calls: list[dict[str, Any]] = []

    def create(
        self,
        *,
        model: str,
        input: list[str],
        dimensions: int,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "model": model,
                "input": list(input),
                "dimensions": dimensions,
            }
        )
        if self.errors:
            raise self.errors.pop(0)
        data = [
            SimpleNamespace(
                index=index,
                embedding=(
                    list(self.vector)
                    if self.vector is not None
                    else _unit_vector(dimensions, index)
                ),
            )
            for index in reversed(range(len(input)))
        ]
        return SimpleNamespace(data=data)


def _fake_client(api: _FakeEmbeddingsApi) -> SimpleNamespace:
    return SimpleNamespace(embeddings=api)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("regwatch.process.embedder.time.sleep", lambda _seconds: None)


def test_query_is_instructed_and_documents_stay_raw() -> None:
    api = _FakeEmbeddingsApi()
    provider = Qwen3EmbeddingProvider(
        client=_fake_client(api),
        dim=DIM,
        query_instruction="Retrieve the supporting PSG evidence",
        query_instruction_version="regwatch-test-v7",
    )

    query_vector = provider.embed_query("What changed?")
    document_vectors = provider.embed_documents(["raw passage A", " raw passage B "])
    compatibility_vectors = provider.embed(["legacy raw passage"])

    assert query_vector == _unit_vector(DIM)
    assert len(document_vectors) == 2
    assert len(compatibility_vectors) == 1
    assert provider.query_instruction_version == "regwatch-test-v7"
    assert [call["input"] for call in api.calls] == [
        ["Instruct: Retrieve the supporting PSG evidence\nQuery:What changed?"],
        ["raw passage A", " raw passage B "],
        ["legacy raw passage"],
    ]
    assert all(call["dimensions"] == DIM for call in api.calls)


def test_batches_and_restores_response_index_order() -> None:
    api = _FakeEmbeddingsApi()
    provider = Qwen3EmbeddingProvider(
        client=_fake_client(api),
        dim=DIM,
        batch_size=2,
    )

    vectors = provider.embed_documents(["d0", "d1", "d2", "d3", "d4"])

    assert [len(call["input"]) for call in api.calls] == [2, 2, 1]
    assert [vector.index(1.0) for vector in vectors] == [0, 1, 0, 1, 0]


def test_empty_document_batch_does_not_require_or_call_client() -> None:
    provider = Qwen3EmbeddingProvider(dim=DIM)
    assert provider.embed_documents([]) == []
    assert provider.embed([]) == []


def test_nonempty_batch_requires_dedicated_endpoint_credentials() -> None:
    provider = Qwen3EmbeddingProvider(dim=DIM)
    with pytest.raises(RuntimeError, match="QWEN_EMBEDDING_BASE_URL"):
        provider.embed_documents(["passage"])

    provider = Qwen3EmbeddingProvider(
        base_url="https://workspace.example/serving-endpoints",
        dim=DIM,
    )
    with pytest.raises(RuntimeError, match="QWEN_EMBEDDING_TOKEN"):
        provider.embed_documents(["passage"])


def test_retries_only_retryable_statuses() -> None:
    api = _FakeEmbeddingsApi(errors=[_ApiError(429), _ApiError(503)])
    provider = Qwen3EmbeddingProvider(client=_fake_client(api), dim=DIM)

    assert provider.embed_documents(["passage"]) == [_unit_vector(DIM)]
    assert len(api.calls) == 3

    bad_request_api = _FakeEmbeddingsApi(errors=[_ApiError(400)])
    provider = Qwen3EmbeddingProvider(client=_fake_client(bad_request_api), dim=DIM)
    with pytest.raises(_ApiError):
        provider.embed_documents(["passage"])
    assert len(bad_request_api.calls) == 1


@pytest.mark.parametrize(
    ("vector", "message"),
    [
        (_unit_vector(DIM - 1), "31 dims, expected 32"),
        ([float("nan"), *([0.0] * (DIM - 1))], "non-finite"),
        ([0.5, *([0.0] * (DIM - 1))], "not unit norm"),
    ],
)
def test_rejects_wrong_dimension_non_finite_and_non_unit_vectors(
    vector: list[float],
    message: str,
) -> None:
    api = _FakeEmbeddingsApi(vector=vector)
    provider = Qwen3EmbeddingProvider(client=_fake_client(api), dim=DIM)

    with pytest.raises(RuntimeError, match=message):
        provider.embed_documents(["passage"])


def test_openai_compatible_http_client_receives_dimensions() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"index": 0, "embedding": _unit_vector(DIM)}]}

    class _HttpClient:
        def post(self, path: str, *, json: dict[str, Any]) -> _Response:
            calls.append((path, json))
            return _Response()

    provider = Qwen3EmbeddingProvider(client=_HttpClient(), dim=DIM)
    provider.embed_documents(["raw passage"])

    assert calls == [
        (
            "embeddings",
            {
                "model": QWEN3_EMBEDDING_MODEL,
                "input": ["raw passage"],
                "dimensions": DIM,
            },
        )
    ]


def test_factory_uses_dedicated_qwen_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        embedding_provider="qwen3",
        qwen_embedding_base_url="https://workspace.example/serving-endpoints",
        qwen_embedding_token="secret",
        qwen_embedding_model="qwen-endpoint",
        qwen_embedding_dimension=768,
        qwen_embedding_batch_size=17,
        qwen_embedding_query_instruction="Retrieve PSG evidence",
        qwen_embedding_query_instruction_version="instruction-v2",
        llm_timeout_s=23.0,
    )
    monkeypatch.setattr("regwatch.process.embedder.get_settings", lambda: settings)

    provider = get_embedding_provider(" databricks-qwen3 ")

    assert isinstance(provider, Qwen3EmbeddingProvider)
    assert provider.base_url == "https://workspace.example/serving-endpoints"
    assert provider.model == "qwen-endpoint"
    assert provider.dim == 768
    assert provider.batch_size == 17
    assert provider.query_instruction == "Retrieve PSG evidence"
    assert provider.query_instruction_version == "instruction-v2"
    assert provider.timeout_s == 23.0


def test_contract_helpers_keep_legacy_embed_only_providers_working() -> None:
    class _LegacyProvider:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text))] for text in texts]

    provider = _LegacyProvider()
    assert embed_query(provider, "question") == [8.0]
    assert embed_documents(provider, ["one", "three"]) == [[3.0], [5.0]]


def test_retriever_uses_query_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _QueryProvider:
        def embed_query(self, query: str) -> list[float]:
            seen["query"] = query
            return _unit_vector(DIM)

        def embed(self, _texts: list[str]) -> list[list[float]]:
            raise AssertionError("retrieval must not use document-style embed()")

    def fake_search(
        vector: list[float],
        *,
        k: int,
        where: dict[str, Any] | None,
    ) -> list[Any]:
        seen["search"] = (vector, k, where)
        return []

    monkeypatch.setattr(
        retriever_module,
        "get_embedding_provider",
        lambda: _QueryProvider(),
    )
    monkeypatch.setattr(
        retriever_module,
        "_current_version_ids_for_filters",
        lambda _filters: None,
    )
    monkeypatch.setattr(retriever_module, "similarity_search", fake_search)

    assert retriever_module.retrieve("Which PSG changed?", k=3) == []
    assert seen["query"] == "Which PSG changed?"
    assert seen["search"] == (_unit_vector(DIM), 3, None)


def test_retriever_routes_named_active_profile_without_legacy_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id = "ep_" + ("a" * 32)
    profile = SimpleNamespace(profile_id=profile_id)
    seen: dict[str, Any] = {}

    class _ProfileProvider:
        def embed_query(self, query: str) -> list[float]:
            seen["query"] = query
            return _unit_vector(DIM)

    def profile_search(
        selected_profile_id: str,
        vector: list[float],
        *,
        k: int,
        where: dict[str, Any] | None,
        mode: Any = None,
    ) -> list[Any]:
        seen["search"] = (selected_profile_id, vector, k, where)
        seen["mode"] = mode
        return []

    monkeypatch.setattr(
        retriever_module,
        "get_settings",
        lambda: SimpleNamespace(
            active_embedding_profile=profile_id,
            vector_top_k=50,
        ),
    )
    monkeypatch.setattr(
        retriever_module,
        "get_embedding_profile",
        lambda selected_profile_id: (
            profile if selected_profile_id == profile_id else pytest.fail("wrong profile selected")
        ),
    )
    monkeypatch.setattr(
        retriever_module,
        "get_embedding_provider_for_profile",
        lambda selected_profile: (
            _ProfileProvider()
            if selected_profile is profile
            else pytest.fail("wrong profile provider")
        ),
    )
    monkeypatch.setattr(
        retriever_module,
        "_current_version_ids_for_filters",
        lambda _filters: [7],
    )
    monkeypatch.setattr(retriever_module, "similarity_search_profile", profile_search)
    monkeypatch.setattr(
        retriever_module,
        "similarity_search",
        lambda *_args, **_kwargs: pytest.fail("legacy search must not run"),
    )

    assert retriever_module.retrieve("Which PSG changed?", k=3) == []
    assert seen["query"] == "Which PSG changed?"
    assert seen["search"] == (
        profile_id,
        _unit_vector(DIM),
        3,
        {"version_id": {"$in": [7]}},
    )
    # The current-version clause is NOT a product scope, so an unfiltered
    # question is EXACT_CORPUS -- and never the approximate path.
    assert seen["mode"] is RetrievalMode.EXACT_CORPUS


def test_pipeline_uses_document_semantics_without_mutating_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_text = "  raw PSG passage\nwith original whitespace  "
    chunk = Chunk(
        text=raw_text,
        page=1,
        section_path=None,
        ordinal=0,
        metadata={"section_path": ""},
    )
    seen: dict[str, Any] = {}

    class _DocumentProvider:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            seen["embedded"] = list(texts)
            return [_unit_vector(DIM) for _text in texts]

        def embed(self, _texts: list[str]) -> list[list[float]]:
            raise AssertionError("ingestion must use document semantics")

    def fake_add_chunks(**kwargs: Any) -> None:
        seen["stored"] = kwargs

    listing = PsgListing(
        appl_no="012345",
        active_ingredient="Example",
        normalized_name="example",
        stripped_name="example",
        psg_type="draft",
        route="Oral",
        dosage_form="Tablet",
        rld_or_rs_numbers=["012345"],
        recommended_date="2026-01-01",
        pdf_url="https://example.invalid/PSG_012345.pdf",
        source_url="https://example.invalid",
    )
    parsed = ParsedPdf(text=raw_text, pages=[raw_text], engine="test")
    monkeypatch.setattr(
        pipeline_module,
        "chunk_pdf",
        lambda *_args, **_kwargs: [chunk],
    )
    monkeypatch.setattr(
        pipeline_module,
        "get_embedding_provider",
        lambda: _DocumentProvider(),
    )
    monkeypatch.setattr(pipeline_module, "add_chunks", fake_add_chunks)
    monkeypatch.setattr(
        pipeline_module,
        "_cleanup_stale_chunks",
        lambda _doc_id, _version_id: None,
    )
    # add_chunks is stubbed above, so no chunk rows exist for the graph refs'
    # FK; stub the (orthogonal) derivation out the same way.
    monkeypatch.setattr(
        pipeline_module,
        "derive_document_graph",
        lambda **_kwargs: None,
    )

    pipeline_module._regenerate_chunks(7, 11, parsed, listing)

    assert seen["embedded"] == [raw_text]
    assert seen["stored"]["documents"] == [raw_text]


def test_qwen_cutover_never_writes_into_unversioned_legacy_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _QwenProvider:
        name = "qwen3"

        def embed_documents(self, _texts: list[str]) -> list[list[float]]:
            raise AssertionError("named-profile vectors must not enter the legacy column")

    monkeypatch.setattr(
        pipeline_module,
        "get_settings",
        lambda: SimpleNamespace(active_embedding_profile="ep_" + ("c" * 32)),
    )
    monkeypatch.setattr(
        pipeline_module,
        "get_embedding_provider",
        lambda: _QwenProvider(),
    )

    assert pipeline_module._legacy_document_embeddings(["raw A", "raw B"]) == [
        None,
        None,
    ]
