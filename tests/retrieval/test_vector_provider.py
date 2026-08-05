from __future__ import annotations

import pytest

from retrieval.vector_provider import DeterministicFakeProvider, LocalBgeM3Provider, VectorProviderError


def test_deterministic_fake_provider_returns_normalized_repeatable_embeddings() -> None:
    provider = DeterministicFakeProvider({"semantic intent": [3, 4, 0, 0]})

    document, query = provider.embed_documents(["semantic intent document"])[0], provider.embed_query("semantic intent")

    assert document == query
    assert sum(value * value for value in document) == pytest.approx(1.0)
    assert provider.identity().dimensions == 4


def test_local_provider_rejects_missing_model_before_importing_optional_dependency(tmp_path) -> None:
    provider = LocalBgeM3Provider(tmp_path / "missing")

    with pytest.raises(VectorProviderError, match="local embedding model") as error:
        provider.embed_query("offline only")

    assert error.value.code == "model_missing"
