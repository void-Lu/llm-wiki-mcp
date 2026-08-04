from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from netsuite_llm_wiki_mcp.codegraph_sync import (
    SUITESCRIPT_ENTRYPOINT_NAMES,
    CodeGraphSyncError,
    _is_trusted_entrypoint,
    sync_codegraph,
)
from netsuite_llm_wiki_mcp.query_pipeline import run_query_v2
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _make_codegraph(workspace: Path, *, broken_hash: bool = False, unresolved: bool = False) -> None:
    source_dir = workspace / "src"
    source_dir.mkdir(parents=True)
    main = b"def main():\n    return helper()\n"
    helper = b"def helper():\n    return 'ok'\n"
    (source_dir / "main.py").write_bytes(main)
    (source_dir / "helper.py").write_bytes(helper)
    graph_dir = workspace / ".codegraph"
    graph_dir.mkdir()
    connection = sqlite3.connect(graph_dir / "codegraph.db")
    connection.executescript(
        """
        CREATE TABLE project_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL, description TEXT);
        CREATE TABLE files(path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, language TEXT NOT NULL, size INTEGER NOT NULL, modified_at REAL NOT NULL, indexed_at INTEGER NOT NULL, node_count INTEGER DEFAULT 0, errors TEXT);
        CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL, file_path TEXT NOT NULL, language TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, start_column INTEGER NOT NULL DEFAULT 0, end_column INTEGER NOT NULL DEFAULT 0, docstring TEXT, signature TEXT, visibility TEXT, is_exported INTEGER DEFAULT 0, is_async INTEGER DEFAULT 0, is_static INTEGER DEFAULT 0, is_abstract INTEGER DEFAULT 0, decorators TEXT, type_parameters TEXT, return_type TEXT, updated_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT);
        CREATE TABLE unresolved_refs(id INTEGER PRIMARY KEY, from_node_id TEXT NOT NULL, reference_name TEXT NOT NULL, reference_kind TEXT NOT NULL, line INTEGER NOT NULL, col INTEGER NOT NULL, candidates TEXT, file_path TEXT NOT NULL, language TEXT NOT NULL);
        """
    )
    connection.executemany("INSERT INTO project_metadata VALUES (?, ?, ?)", [("indexed_with_version", "test-1", 1), ("indexed_with_extraction_version", "7", 1)])
    connection.execute("INSERT INTO schema_versions VALUES (6, 1, 'test')")
    connection.executemany(
        "INSERT INTO files(path, content_hash, language, size, modified_at, indexed_at, node_count, errors) VALUES (?, ?, ?, ?, 0, 0, ?, NULL)",
        [
            ("src/main.py", "0" * 64 if broken_hash else _hash(main), "python", len(main), 1),
            ("src/helper.py", _hash(helper), "python", len(helper), 1),
        ],
    )
    connection.executemany(
        "INSERT INTO nodes(id, kind, name, qualified_name, file_path, language, start_line, end_line, signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("main", "function", "main", "main", "src/main.py", "python", 1, 2, "()"),
            ("helper", "function", "helper", "helper", "src/helper.py", "python", 1, 2, "()"),
        ],
    )
    connection.execute("INSERT INTO edges(source, target, kind, line, col) VALUES ('main', 'helper', 'calls', 2, 11)")
    if unresolved:
        connection.execute("INSERT INTO unresolved_refs(from_node_id, reference_name, reference_kind, line, col, file_path, language) VALUES ('main', 'external', 'calls', 2, 1, 'src/main.py', 'python')")
    connection.commit()
    connection.close()


def test_js_ts_and_suitescript_entrypoints_are_recognized() -> None:
    assert _is_trusted_entrypoint({"kind": "function", "name": "main", "language": "javascript", "is_exported": 1})
    assert _is_trusted_entrypoint({"kind": "method", "name": "run", "language": "typescript", "is_exported": 1})
    assert _is_trusted_entrypoint({"kind": "function", "name": "afterSubmit", "language": "suitescript", "is_exported": 1})
    assert not _is_trusted_entrypoint({"kind": "function", "name": "afterSubmit", "language": "suitescript", "is_exported": 0})


def test_all_fixed_suitescript_entrypoints_are_trusted_roots() -> None:
    expected = {
        "get",
        "post",
        "put",
        "delete",
        "getinputdata",
        "map",
        "reduce",
        "summarize",
        "onrequest",
        "beforeload",
        "beforesubmit",
        "aftersubmit",
        "pageinit",
        "fieldchanged",
        "lineinit",
        "localizationcontextenter",
        "localizationcontextexit",
        "postsourcing",
        "saverecord",
        "sublistchanged",
        "validatedelete",
        "validatefield",
        "validateinsert",
        "validateline",
        "execute",
        "each",
        "render",
        "onaction",
        "afterinstall",
        "afterupdate",
        "beforeinstall",
        "beforeuninstall",
        "beforeupdate",
        "run",
        "initializespa",
    }
    assert SUITESCRIPT_ENTRYPOINT_NAMES == expected
    for name in expected:
        assert _is_trusted_entrypoint({"kind": "function", "name": name, "language": "suitescript", "is_exported": 1})
        assert _is_trusted_entrypoint({"kind": "function", "name": name, "language": "javascript", "is_exported": 1})


def test_sync_rejects_missing_database_before_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    vault = tmp_path / "vault"

    try:
        sync_codegraph(vault, workspace_root=workspace)
    except CodeGraphSyncError as exc:
        assert exc.code == "codegraph_db_missing"
    else:
        raise AssertionError("expected a missing database error")
    assert not vault.exists()


def test_sync_rejects_unsupported_schema_before_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace)
    connection = sqlite3.connect(workspace / ".codegraph" / "codegraph.db")
    connection.execute("UPDATE schema_versions SET version = 99")
    connection.commit()
    connection.close()
    vault = tmp_path / "vault"

    try:
        sync_codegraph(vault, workspace_root=workspace)
    except CodeGraphSyncError as exc:
        assert exc.code == "codegraph_schema_unsupported"
    else:
        raise AssertionError("expected an unsupported schema error")
    assert not vault.exists()


def test_sync_writes_latest_raw_pages_pipeline_and_active_index(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace)
    vault = tmp_path / "vault"
    first = sync_codegraph(vault, workspace_root=workspace)

    assert first["ok"] is True
    assert first["project"] == "demoproject"
    assert first["created"] == 4
    assert (vault / "raw/sources/projects/demoproject/codegraph/graph.json").is_file()
    assert (vault / "raw/sources/projects/demoproject/codegraph/manifest.json").is_file()
    assert list((vault / "wiki/projects/demoproject/architecture/code-facts").rglob("*.md"))
    assert list((vault / "wiki/projects/demoproject/architecture/pipelines").rglob("*.md"))
    assert (vault / "wiki/projects/demoproject/architecture/code-overview.md").is_file()
    graph = json.loads((vault / "raw/sources/projects/demoproject/codegraph/graph.json").read_text(encoding="utf-8"))
    assert "def main" not in json.dumps(graph, ensure_ascii=False)

    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in [
            *[item for item in (vault / "raw/sources/projects/demoproject/codegraph").rglob("*") if item.is_file()],
            *(vault / "wiki/projects/demoproject/architecture").rglob("*.md"),
        ]
    }
    second = sync_codegraph(vault, workspace_root=workspace)
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 4
    after = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in [
            *[item for item in (vault / "raw/sources/projects/demoproject/codegraph").rglob("*") if item.is_file()],
            *(vault / "wiki/projects/demoproject/architecture").rglob("*.md"),
        ]
    }
    assert after == before

    raw_store = RetrievalIndexStore(vault, scope="raw")
    raw_store.build(raw_store.iter_vault_pages())
    assert raw_store.search_fts("graph") == []


def test_sync_hash_failure_does_not_create_partial_state(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace, broken_hash=True)
    vault = tmp_path / "vault"

    try:
        sync_codegraph(vault, workspace_root=workspace)
    except CodeGraphSyncError as exc:
        assert exc.code == "codegraph_hash_mismatch"
    else:
        raise AssertionError("expected a hash mismatch")
    assert not (vault / "raw").exists()
    assert not (vault / "wiki").exists()


def test_partial_pipeline_is_rendered_with_boundary_warning(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace, unresolved=True)
    vault = tmp_path / "vault"

    result = sync_codegraph(vault, workspace_root=workspace)
    assert result["warnings"] == ["pipeline_partial:1"]
    pipeline = next((vault / "wiki/projects/demoproject/architecture/pipelines").glob("*.md")).read_text(encoding="utf-8")
    assert "pipeline_status: partial" in pipeline
    assert "未解析或外部边界" in pipeline


def test_sync_removes_only_stale_managed_pages(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace)
    vault = tmp_path / "vault"
    sync_codegraph(vault, workspace_root=workspace)

    stale = vault / "wiki/projects/demoproject/architecture/code-facts/stale.md"
    stale.write_text(
        "---\n"
        "type: code_fact\n"
        "artifact_kind: file\n"
        "generated: true\n"
        "managed_by: codegraph\n"
        "managed_key: stale.py\n"
        "retrieval_scope: project_code\n"
        "project: demoproject\n"
        "source_name: codegraph\n"
        "---\n\n# stale\n\nold\n",
        encoding="utf-8",
    )
    manual = vault / "wiki/projects/demoproject/architecture/code-facts/manual.md"
    manual.write_text("---\ntype: note\ngenerated: false\n---\n\n# Manual\n", encoding="utf-8")
    old_layout = vault / "wiki/projects/demoproject/code/legacy.md"
    old_layout.parent.mkdir(parents=True, exist_ok=True)
    old_layout.write_text("# Legacy\n", encoding="utf-8")

    result = sync_codegraph(vault, workspace_root=workspace)

    assert result["deleted"] == 1
    assert not stale.exists()
    assert manual.exists()
    assert old_layout.exists()


def test_sync_refuses_to_overwrite_manual_target(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace)
    vault = tmp_path / "vault"
    target = vault / "wiki/projects/demoproject/architecture/code-facts/src/main.py.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntype: note\ngenerated: false\n---\n\n# Manual\n", encoding="utf-8")

    try:
        sync_codegraph(vault, workspace_root=workspace)
    except CodeGraphSyncError as exc:
        assert exc.code == "codegraph_managed_page"
    else:
        raise AssertionError("expected a manual page conflict")
    assert not (vault / "raw").exists()
    assert "# Manual" in target.read_text(encoding="utf-8")


def test_project_code_pages_are_excluded_without_explicit_project(tmp_path: Path) -> None:
    workspace = tmp_path / "DemoProject"
    workspace.mkdir()
    _make_codegraph(workspace)
    vault = tmp_path / "vault"
    sync_codegraph(vault, workspace_root=workspace)
    general = vault / "wiki/concepts/general.md"
    general.parent.mkdir(parents=True)
    general.write_text("---\ntype: knowledge\nproject: ''\ntitle: General\n---\n\n# General\n\nshared guidance\n", encoding="utf-8")
    RetrievalIndexStore(vault).build(RetrievalIndexStore(vault).iter_vault_pages())

    default_result = run_query_v2(vault, "helper", retrieval_mode="lexical")
    assert all(item["path"] != "wiki/projects/demoproject/architecture/code-facts/src/helper.py.md" for item in default_result["results"])
    project_result = run_query_v2(vault, "helper", project="DEMOPROJECT", retrieval_mode="lexical")
    assert any("architecture/code-facts" in item["path"] for item in project_result["results"])
