from pathlib import Path

from retrieval.vector_index import VectorIndexStore, VectorRecord
from retrieval.vector_provider import DeterministicFakeProvider


def test_vector_v2_allows_multiple_passages_for_one_page_without_text_on_disk(tmp_path: Path) -> None:
    store = VectorIndexStore(tmp_path)
    records = [
        VectorRecord("wiki/concepts/a.md", "one", "first passage", "concept", passage_id="a-1", page_path="wiki/concepts/a.md"),
        VectorRecord("wiki/concepts/a.md", "two", "second passage", "concept", passage_id="a-2", page_path="wiki/concepts/a.md"),
    ]

    result = store.build(records, DeterministicFakeProvider(), include_raw_sources=False)
    documents = store.documents_path.read_text(encoding="utf-8")

    assert result["schema_version"] == 2
    assert '"passage_id": "a-1"' in documents
    assert "first passage" not in documents


def test_vector_v1_manifest_is_explicitly_incompatible(tmp_path: Path) -> None:
    store = VectorIndexStore(tmp_path)
    store.index_path.mkdir(parents=True)
    store.manifest_path.write_text('{"schema_version": 1}', encoding="utf-8")
    store.documents_path.write_text("", encoding="utf-8")

    assert store.status()["code"] == "index_incompatible"
