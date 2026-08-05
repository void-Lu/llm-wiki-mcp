from __future__ import annotations

import json
from pathlib import Path

from runtime.runtime_provenance import RUNTIME_PROVENANCE
from wiki.wiki_files import wiki_status
from wiki.wiki_paths import create_wiki_root


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
    assert result["version"] == RUNTIME_PROVENANCE.package_version
    assert result["runtime"] == RUNTIME_PROVENANCE.to_public_dict()
    assert set(result["runtime"]) == {
        "package_version",
        "revision",
        "dirty",
        "revision_source",
        "started_at",
        "provenance_incomplete",
        "warnings",
    }
    assert "path" not in result["runtime"]
    assert "pid" not in result["runtime"]


def test_wiki_status_reports_missing_structure_without_creating_it(tmp_path: Path):
    root = tmp_path / "missing"

    result = wiki_status(root)

    assert result["ok"] is True
    assert result["initialized"] is False
    assert "wiki/index.md" in result["missing_required_paths"]
    assert all(not path.startswith("wiki/sources") for path in result["missing_required_paths"])
    assert not root.exists()

