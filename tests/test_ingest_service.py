from pathlib import Path

from netsuite_llm_wiki_mcp.ingest_service import ingest_file
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore


def test_single_file_ingest_handles_new_unchanged_and_modified_chat(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = tmp_path / "chat.md"
    source.write_text("invoice token=sk-abcdefghijklmno", encoding="utf-8")

    first = ingest_file(vault_root=root, source_path=source, source_name="session-1", project="finance", source_type="chat")
    second = ingest_file(vault_root=root, source_path=source, source_name="session-1", project="finance", source_type="chat")
    source.write_text("invoice changed", encoding="utf-8")
    third = ingest_file(vault_root=root, source_path=source, source_name="session-1", project="finance", source_type="chat")

    assert [first["operation"], second["operation"], third["operation"]] == ["new", "unchanged", "modified"]
    assert RetrievalIndexStore(root).search_fts("changed")


def test_single_file_ingest_rejects_a_directory(tmp_path: Path) -> None:
    result = ingest_file(vault_root=tmp_path / "vault", source_path=tmp_path, source_name="nope")
    assert result["code"] == "source_not_file"


def test_single_file_ingest_indexes_non_chat_sources_in_the_raw_store(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = tmp_path / "manual.txt"
    source.write_text("raw-only invoice approval field", encoding="utf-8")

    result = ingest_file(vault_root=root, source_path=source, source_name="manual", project="finance")

    assert result["index_scope"] == "raw"
    assert RetrievalIndexStore(root, scope="raw").search_fts("raw-only invoice")
    assert not RetrievalIndexStore(root).search_fts("raw-only invoice")


def test_single_file_ingest_stores_binary_office_and_script_sources_as_assets(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    knowledge = tmp_path / "knowledge.txt"
    knowledge.write_text("indexed knowledge", encoding="utf-8")
    ingest_file(vault_root=root, source_path=knowledge, source_name="knowledge", project="finance")
    raw_store = RetrievalIndexStore(root, scope="raw")
    before = raw_store.status()
    for name, content in (("guide.pdf", b"%PDF-1.7\x00binary"), ("handler.py", b"def main(): pass")):
        source = tmp_path / name
        source.write_bytes(content)
        result = ingest_file(vault_root=root, source_path=source, source_name="blocked", project="finance")
        assert result["ok"] is True
        assert result["storage_kind"] == "asset"
        assert result["semantic_indexed"] is False
        assert result["index"]["code"] == "asset_not_indexed"
        assert result["source"] == f"raw/assets/finance/blocked/{name}"
        assert (root / result["source"]).read_bytes() == content
        assert raw_store.status()["fingerprint"] == before["fingerprint"]
        assert raw_store.search_fts("main") == []


def test_single_file_ingest_stores_invalid_utf8_text_as_asset(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = tmp_path / "broken.md"
    source.write_bytes(b"\xff\xfe")

    result = ingest_file(vault_root=root, source_path=source, source_name="broken", project="finance")

    assert result["ok"] is True
    assert result["storage_kind"] == "asset"
    assert result["source"] == "raw/assets/finance/broken/broken.md"
    assert (root / result["source"]).read_bytes() == b"\xff\xfe"
