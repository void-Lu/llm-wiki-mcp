from __future__ import annotations

import json
from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_files import wiki_list_files, wiki_read_file, wiki_status
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


def test_wiki_status_reports_structure_and_queue_counts(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    queue = root / ".llm-wiki" / "ingest-queue.json"
    queue.write_text(
        json.dumps([
            {"id": "a", "status": "pending"},
            {"id": "b", "status": "failed"},
            {"id": "c", "status": "done"},
        ]),
        encoding="utf-8",
    )

    result = wiki_status(root)

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["missing_required_paths"] == []
    assert result["queue"]["counts"] == {"pending": 1, "failed": 1, "done": 1}
    assert "version" in result


def test_wiki_status_reports_missing_structure_without_creating_it(tmp_path: Path):
    root = tmp_path / "missing"

    result = wiki_status(root)

    assert result["ok"] is True
    assert result["initialized"] is False
    assert "wiki/index.md" in result["missing_required_paths"]
    assert not root.exists()


def test_wiki_list_files_limits_to_public_roots(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/concepts/a.md").write_text("# A\n", encoding="utf-8")
    (root / "raw/sources/file/alpha/a.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "raw/sources/file/alpha/a.txt").write_text("raw", encoding="utf-8")
    (root / "raw/projects/alpha/codegraph/main/graph.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "raw/projects/alpha/codegraph/main/graph.json").write_text("{}", encoding="utf-8")
    (root / ".llm-wiki/private.json").write_text("{}", encoding="utf-8")

    result = wiki_list_files(root, root_name="all", recursive=True)

    assert result["ok"] is True
    paths = {item["path"] for item in result["files"]}
    assert "wiki/concepts/a.md" in paths
    assert "raw/sources/file/alpha/a.txt" in paths
    assert "raw/projects/alpha/codegraph/main/graph.json" in paths
    assert ".llm-wiki/private.json" not in paths


def test_wiki_list_files_clamps_max_files_and_reports_truncation(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(3):
        (root / f"wiki/concepts/{index}.md").write_text("# Page\n", encoding="utf-8")

    result = wiki_list_files(root, root_name="wiki", max_files=2)

    assert result["ok"] is True
    assert len(result["files"]) == 2
    assert result["truncated"] is True


def test_wiki_read_file_reads_public_text_file_and_truncates(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/concepts/a.md").write_text("0123456789abcdef", encoding="utf-8")

    result = wiki_read_file(root, "wiki/concepts/a.md", max_bytes=10)

    assert result["ok"] is True
    assert result["path"] == "wiki/concepts/a.md"
    assert result["content"] == "0123456789"
    assert result["truncated"] is True
    assert result["omitted_bytes"] == 6


def test_wiki_read_file_reads_raw_project_text_file(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    path = root / "raw/projects/alpha/codegraph/main/graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"nodes":[]}', encoding="utf-8")

    result = wiki_read_file(root, "raw/projects/alpha/codegraph/main/graph.json")

    assert result["ok"] is True
    assert result["path"] == "raw/projects/alpha/codegraph/main/graph.json"
    assert result["content"] == '{"nodes":[]}'


def test_wiki_read_file_truncates_without_splitting_utf8_characters(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/concepts/a.md").write_text("发票abc", encoding="utf-8")

    result = wiki_read_file(root, "wiki/concepts/a.md", max_bytes=4)

    assert result["ok"] is True
    assert result["content"] == "发"
    assert result["truncated"] is True
    assert result["omitted_bytes"] == len("票abc".encode("utf-8"))


def test_wiki_read_file_rejects_unsafe_paths(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    traversal = wiki_read_file(root, "../secret.md")
    private = wiki_read_file(root, ".llm-wiki/ingest-queue.json")
    binary = wiki_read_file(root, "raw/sources/book.pdf")

    assert traversal["ok"] is False
    assert traversal["code"] == "path_escape"
    assert private["ok"] is False
    assert private["code"] == "path_not_allowed"
    assert binary["ok"] is False
    assert binary["code"] == "unsupported_file_type"
