from __future__ import annotations

import json
from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.vector_index import VectorIndexError, VectorIndexStore, VectorRecord, parse_vector_settings
from netsuite_llm_wiki_mcp.vector_provider import DeterministicFakeProvider


class RecordingFakeProvider(DeterministicFakeProvider):
    def __init__(self) -> None:
        super().__init__({"intent": [1, 0, 0, 0], "other": [0, 1, 0, 0]})
        self.document_batches: list[list[str]] = []

    def embed_documents(self, texts):
        self.document_batches.append(list(texts))
        return super().embed_documents(texts)


def _records() -> list[VectorRecord]:
    return [
        VectorRecord("wiki/concepts/intent.md", "hash-a", "intent document body", "wiki"),
        VectorRecord("wiki/concepts/other.md", "hash-b", "other document body", "wiki"),
    ]


def test_build_status_update_and_search_are_explicit_and_incremental(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    provider = RecordingFakeProvider()
    store = VectorIndexStore(root)
    records = _records()

    assert store.status(records, include_raw_sources=False)["code"] == "index_missing"
    built = store.build(records, provider, include_raw_sources=False)

    assert built["state"] == "fresh"
    assert built["document_count"] == 2
    assert {"model_load", "embed_documents", "write_index", "total"} <= set(built["timings_ms"])
    assert provider.document_batches == [["intent document body", "other document body"]]
    stored = store.documents_path.read_text(encoding="utf-8")
    assert "document body" not in stored
    assert all(not Path(item["path"]).is_absolute() for item in map(json.loads, stored.splitlines()))
    search = store.search(provider.embed_query("intent"), allowed_paths={record.path for record in records}, limit=2)
    assert [item.path for item in search] == ["wiki/concepts/intent.md", "wiki/concepts/other.md"]

    changed = [VectorRecord("wiki/concepts/intent.md", "hash-a2", "intent changed body", "wiki"), records[1]]
    assert store.status(changed, include_raw_sources=False)["state"] == "stale"
    updated = store.update(changed, provider, include_raw_sources=False)

    assert updated["state"] == "fresh"
    assert updated["changes"] == {"added": 0, "modified": 1, "deleted": 0, "unchanged": 1}
    assert {"read_manifest", "model_load", "read_documents", "compare_records", "embed_documents", "write_index", "total"} <= set(updated["timings_ms"])
    assert provider.document_batches[-1] == ["intent changed body"]


def test_update_rejects_model_or_raw_policy_changes_that_require_full_build(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    store = VectorIndexStore(root)
    records = _records()
    store.build(records, DeterministicFakeProvider(model_id="one"), include_raw_sources=False)

    with pytest.raises(VectorIndexError, match="full vector build") as provider_error:
        store.update(records, DeterministicFakeProvider(model_id="two"), include_raw_sources=False)
    assert provider_error.value.code == "index_incompatible"

    with pytest.raises(VectorIndexError, match="full vector build") as raw_error:
        store.update(records, DeterministicFakeProvider(model_id="one"), include_raw_sources=True)
    assert raw_error.value.code == "index_incompatible"


def test_local_only_settings_reject_credential_fields_and_external_provider(tmp_path: Path) -> None:
    with pytest.raises(VectorIndexError, match="credentials"):
        parse_vector_settings(tmp_path, {"api_key": "never-accepted"})
    with pytest.raises(VectorIndexError, match="local_bge_m3"):
        parse_vector_settings(tmp_path, {"provider": "remote"})


def test_parse_vector_settings_applies_defaults_and_bounds(tmp_path: Path) -> None:
    settings = parse_vector_settings(tmp_path, None)
    assert settings.candidate_limit == 50
    assert settings.rrf_k == 60
    assert settings.min_vector_score == 0.5
    custom = parse_vector_settings(tmp_path, {"rrf_k": 30, "min_vector_score": 0.3, "candidate_limit": 20})
    assert custom.rrf_k == 30
    assert custom.min_vector_score == 0.3
    assert custom.candidate_limit == 20
    with pytest.raises(VectorIndexError, match="min_vector_score"):
        parse_vector_settings(tmp_path, {"min_vector_score": 2.0})
