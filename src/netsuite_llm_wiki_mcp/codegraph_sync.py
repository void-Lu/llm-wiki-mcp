"""Synchronise an external CodeGraph SQLite snapshot into the Wiki.

The importer deliberately treats CodeGraph as an input-only analyser.  It
does not start CodeGraph, query it during retrieval, or copy source files into
the vault.  The only durable inputs are the structured facts read from the
SQLite snapshot and the deterministic Markdown/JSON projections produced
here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from netsuite_llm_wiki_mcp.codegraph_policy import is_codegraph_frontmatter
from netsuite_llm_wiki_mcp.git_utils import get_git_revision
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter
from netsuite_llm_wiki_mcp.wiki_paths import safe_segment


CODEGRAPH_SOURCE_NAME = "codegraph"
IMPORT_SCHEMA_VERSION = 1
MAX_SUPPORTED_CODEGRAPH_SCHEMA_VERSION = 8
SUPPORTED_LANGUAGES = {"python", "javascript", "typescript", "js", "ts", "node", "suiteScript".casefold()}
ENTRYPOINT_NAMES = {"main", "run", "handler", "execute", "entrypoint"}
# Fixed SuiteScript 2.x/2.1 entry points. Custom Tool methods are defined by
# the tool JSON schema, so they intentionally cannot be represented by this
# name set and must be resolved from schema-aware CodeGraph metadata instead.
SUITESCRIPT_ENTRYPOINT_NAMES = {
    # RESTlet
    "get",
    "post",
    "put",
    "delete",
    # Map/Reduce
    "getinputdata",
    "map",
    "reduce",
    "summarize",
    # Suitelet
    "onRequest".casefold(),
    # User Event
    "beforeload",
    "beforesubmit",
    "aftersubmit",
    # Client
    "pageinit",
    "fieldChanged".casefold(),
    "lineInit".casefold(),
    "localizationContextEnter".casefold(),
    "localizationContextExit".casefold(),
    "postSourcing".casefold(),
    "saveRecord".casefold(),
    "sublistChanged".casefold(),
    "validateDelete".casefold(),
    "validateField".casefold(),
    "validateInsert".casefold(),
    "validateLine".casefold(),
    # Scheduled, Mass Update, Portlet, and Workflow Action
    "execute",
    "each",
    "render",
    "onAction".casefold(),
    # Bundle Installation
    "afterInstall".casefold(),
    "afterUpdate".casefold(),
    "beforeInstall".casefold(),
    "beforeUninstall".casefold(),
    "beforeUpdate".casefold(),
    # SDF Installation and SPA scripts
    "run",
    "initializeSpa".casefold(),
}
TRAVERSAL_EDGE_KINDS = {"calls", "instantiates"}
NODE_FIELDS = (
    "id",
    "kind",
    "name",
    "qualified_name",
    "file_path",
    "language",
    "start_line",
    "end_line",
    "start_column",
    "end_column",
    "docstring",
    "signature",
    "visibility",
    "is_exported",
    "is_async",
    "is_static",
    "is_abstract",
    "decorators",
    "type_parameters",
    "return_type",
)
FILE_FIELDS = ("path", "content_hash", "language", "size", "node_count", "errors")
EDGE_FIELDS = ("source", "target", "kind", "metadata", "line", "col", "provenance")
UNRESOLVED_FIELDS = ("from_node_id", "reference_name", "reference_kind", "line", "col", "candidates", "file_path", "language")


class CodeGraphSyncError(RuntimeError):
    """A stable, user-facing failure from the CodeGraph sync boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GraphSnapshot:
    project: str
    codegraph_version: str
    extraction_version: str
    schema_version: int
    files: tuple[dict[str, Any], ...]
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    unresolved_refs: tuple[dict[str, Any], ...]
    graph_hash: str
    revision: str


@dataclass(frozen=True)
class PipelineSnapshot:
    pipeline_id: str
    page_path: str
    entrypoint: dict[str, Any]
    node_ids: tuple[str, ...]
    member_files: tuple[str, ...]
    edge_keys: tuple[tuple[str, str, str], ...]
    external_edge_keys: tuple[tuple[str, str, str], ...]
    unresolved_count: int
    pipeline_status: str


def sync_codegraph(vault_root: str | Path, *, workspace_root: str | Path | None = None) -> dict[str, Any]:
    """Read the current workspace CodeGraph database and atomically sync it."""

    vault = Path(vault_root).expanduser().resolve()
    workspace = Path(workspace_root or Path.cwd()).expanduser().resolve()
    project = workspace.name.lower()
    try:
        safe_segment(project)
    except ValueError as exc:
        raise CodeGraphSyncError("invalid_project", str(exc)) from exc

    snapshot_data = _read_snapshot(workspace, project)
    pipelines = _extract_pipelines(snapshot_data)
    desired = _desired_state(snapshot_data, pipelines)
    return _commit_desired_state(vault, project, desired)


def _read_snapshot(workspace: Path, project: str) -> GraphSnapshot:
    db_path = workspace / ".codegraph" / "codegraph.db"
    if not db_path.is_file():
        raise CodeGraphSyncError("codegraph_db_missing", f"CodeGraph database not found: {db_path}")

    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise CodeGraphSyncError("codegraph_db_unreadable", "CodeGraph database could not be opened read-only") from exc

    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise CodeGraphSyncError("codegraph_db_corrupt", "CodeGraph database integrity check failed")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required_tables = {"files", "nodes", "edges"}
        if not required_tables <= tables:
            missing = ", ".join(sorted(required_tables - tables))
            raise CodeGraphSyncError("codegraph_schema_unsupported", f"CodeGraph database is missing required table(s): {missing}")
        required_columns = {
            "files": {"path", "content_hash", "language", "size"},
            "nodes": {"id", "kind", "name", "qualified_name", "file_path", "language", "start_line", "end_line"},
            "edges": {"source", "target", "kind"},
        }
        for table, columns in required_columns.items():
            actual = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if not columns <= actual:
                missing = ", ".join(sorted(columns - actual))
                raise CodeGraphSyncError("codegraph_schema_unsupported", f"CodeGraph table {table} is missing column(s): {missing}")

        metadata = _metadata(connection, "project_metadata") if "project_metadata" in tables else {}
        version = str(metadata.get("indexed_with_version") or "unknown")
        extraction_version = str(metadata.get("indexed_with_extraction_version") or "unknown")
        schema_version = _schema_version(connection, tables)
        if schema_version < 1 or schema_version > MAX_SUPPORTED_CODEGRAPH_SCHEMA_VERSION:
            raise CodeGraphSyncError("codegraph_schema_unsupported", f"unsupported CodeGraph schema version: {schema_version}")
        files = _read_files(connection, workspace)
        nodes = _read_nodes(connection)
        edges = _read_edges(connection)
        unresolved = _read_unresolved(connection, tables)
    except CodeGraphSyncError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise CodeGraphSyncError("codegraph_db_unreadable", "CodeGraph database could not be read") from exc
    finally:
        connection.close()

    graph_without_revision = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "project": project,
        "source_name": CODEGRAPH_SOURCE_NAME,
        "codegraph_version": version,
        "codegraph_schema_version": schema_version,
        "files": files,
        "nodes": nodes,
        "edges": edges,
        "unresolved_refs": unresolved,
    }
    graph_bytes = _canonical_json(graph_without_revision)
    git_revision = get_git_revision(workspace)
    revision = git_revision if re.fullmatch(r"[0-9a-fA-F]{40,64}", git_revision or "") else hashlib.sha256(graph_bytes).hexdigest()
    graph_with_revision = {**graph_without_revision, "revision": revision}
    graph_hash = hashlib.sha256(_canonical_json(graph_with_revision)).hexdigest()
    return GraphSnapshot(
        project=project,
        codegraph_version=version,
        extraction_version=extraction_version,
        schema_version=schema_version,
        files=tuple(files),
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved_refs=tuple(unresolved),
        graph_hash=graph_hash,
        revision=revision,
    )


def _metadata(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    try:
        return {str(row[0]): row[1] for row in connection.execute(f"SELECT key, value FROM {table}").fetchall()}
    except sqlite3.Error as exc:
        raise CodeGraphSyncError("codegraph_schema_unsupported", "CodeGraph metadata table is unreadable") from exc


def _schema_version(connection: sqlite3.Connection, tables: set[str]) -> int:
    if "schema_versions" not in tables:
        return 0
    try:
        values = [int(row[0]) for row in connection.execute("SELECT version FROM schema_versions").fetchall()]
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise CodeGraphSyncError("codegraph_schema_unsupported", "CodeGraph schema version table is unreadable") from exc
    return max(values, default=0)


def _read_files(connection: sqlite3.Connection, workspace: Path) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM files ORDER BY path").fetchall()
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        value = dict(row)
        path = _relative_source_path(value.get("path"), "files.path")
        if path in seen:
            raise CodeGraphSyncError("codegraph_schema_invalid", f"duplicate CodeGraph file path: {path}")
        seen.add(path)
        expected = str(value.get("content_hash") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CodeGraphSyncError("codegraph_hash_missing", f"CodeGraph file hash is missing or invalid: {path}")
        source = (workspace / PurePosixPath(path)).resolve()
        if not source.is_relative_to(workspace) or not source.is_file():
            raise CodeGraphSyncError("codegraph_source_missing", f"CodeGraph source file is missing: {path}")
        actual = _sha256_file(source)
        if actual != expected:
            raise CodeGraphSyncError("codegraph_hash_mismatch", f"CodeGraph source hash mismatch: {path}")
        item = {
            "path": path,
            "language": str(value.get("language") or "unknown"),
            "content_hash": expected,
            "size": int(value.get("size") or source.stat().st_size),
            "node_count": int(value.get("node_count") or 0),
        }
        if value.get("errors") not in (None, ""):
            item["errors"] = str(value["errors"])
        files.append(item)
    return files


def _read_nodes(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        value = dict(row)
        node_id = str(value.get("id") or "")
        if not node_id or node_id in seen:
            raise CodeGraphSyncError("codegraph_schema_invalid", "CodeGraph node ids must be unique and non-empty")
        seen.add(node_id)
        item: dict[str, Any] = {}
        for field in NODE_FIELDS:
            current = value.get(field)
            if field == "id":
                current = node_id
            elif field == "file_path" and current:
                current = _relative_source_path(current, "nodes.file_path")
            elif field.startswith("is_"):
                current = bool(current)
            elif current is not None and not isinstance(current, (str, int, float, bool, list, dict)):
                current = str(current)
            if current is not None and current != "":
                item[field] = _json_value(current)
        nodes.append(item)
    return nodes


def _read_edges(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM edges ORDER BY source, target, kind, line, col, id").fetchall()
    edges: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        item: dict[str, Any] = {}
        for field in EDGE_FIELDS:
            current = value.get(field)
            if current in (None, ""):
                continue
            if field in {"metadata", "provenance"}:
                current = _json_value(current)
            item[field] = current
        edges.append(item)
    return edges


def _read_unresolved(connection: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    if "unresolved_refs" not in tables:
        return []
    rows = connection.execute("SELECT * FROM unresolved_refs ORDER BY file_path, line, col, reference_name, id").fetchall()
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        item: dict[str, Any] = {}
        for field in UNRESOLVED_FIELDS:
            current = value.get(field)
            if current in (None, ""):
                continue
            if field == "file_path":
                current = _relative_source_path(current, "unresolved_refs.file_path")
            elif field == "candidates":
                current = _json_value(current)
            item[field] = current
        unresolved.append(item)
    return unresolved


def _relative_source_path(value: object, field: str) -> str:
    text = str(value or "").replace("\\", "/")
    if not text or re.match(r"^[A-Za-z]:", text) or text.startswith("/"):
        raise CodeGraphSyncError("codegraph_path_invalid", f"{field} must be a relative repository path")
    path = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in path.parts) or not path.parts:
        raise CodeGraphSyncError("codegraph_path_invalid", f"{field} contains an unsafe path")
    return path.as_posix()


def _json_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise CodeGraphSyncError("codegraph_source_unreadable", f"CodeGraph source file could not be read: {path}") from exc
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _extract_pipelines(snapshot: GraphSnapshot) -> list[PipelineSnapshot]:
    by_id = {str(node["id"]): node for node in snapshot.nodes}
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in snapshot.edges:
        if str(edge.get("kind") or "").casefold() in TRAVERSAL_EDGE_KINDS:
            outgoing[str(edge.get("source") or "")].append(edge)
    for values in outgoing.values():
        values.sort(key=lambda edge: (str(edge.get("target") or ""), str(edge.get("kind") or ""), int(edge.get("line") or 0), int(edge.get("col") or 0)))
    unresolved_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in snapshot.unresolved_refs:
        unresolved_by_node[str(item.get("from_node_id") or "")].append(item)

    roots = [node for node in snapshot.nodes if _is_trusted_entrypoint(node)]
    roots.sort(key=lambda node: (str(node.get("file_path") or ""), int(node.get("start_line") or 0), str(node.get("qualified_name") or node.get("name") or ""), str(node.get("id") or "")))
    raw_pipelines: list[dict[str, Any]] = []
    for root in roots:
        root_id = str(root["id"])
        reachable: set[str] = {root_id}
        queue: deque[str] = deque([root_id])
        traversed_edges: list[dict[str, Any]] = []
        external_edges: list[dict[str, Any]] = []
        while queue:
            source_id = queue.popleft()
            for edge in outgoing.get(source_id, []):
                target_id = str(edge.get("target") or "")
                if target_id not in by_id:
                    external_edges.append(edge)
                    continue
                traversed_edges.append(edge)
                if target_id not in reachable:
                    reachable.add(target_id)
                    queue.append(target_id)
        member_files = sorted({str(by_id[node_id].get("file_path") or "") for node_id in reachable if by_id[node_id].get("file_path")})
        unresolved_count = sum(len(unresolved_by_node.get(node_id, ())) for node_id in reachable) + len(external_edges)
        pipeline_id = f"{root.get('file_path') or 'unknown'}::{root.get('qualified_name') or root.get('name') or root_id}"
        raw_pipelines.append(
            {
                "pipeline_id": pipeline_id,
                "entrypoint": dict(root),
                "node_ids": tuple(sorted(reachable)),
                "member_files": tuple(member_files),
                "edge_keys": tuple(sorted((str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("kind") or "")) for edge in traversed_edges)),
                "external_edge_keys": tuple(sorted((str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("kind") or "")) for edge in external_edges)),
                "unresolved_count": unresolved_count,
                "pipeline_status": "partial" if unresolved_count else "complete",
            }
        )

    slug_counts: dict[str, int] = defaultdict(int)
    for item in raw_pipelines:
        slug_counts[_pipeline_slug(str(item["pipeline_id"]))] += 1
    result: list[PipelineSnapshot] = []
    for item in raw_pipelines:
        pipeline_id = str(item["pipeline_id"])
        slug = _pipeline_slug(pipeline_id)
        if slug_counts[slug] > 1:
            slug = f"{slug}-{hashlib.sha256(pipeline_id.encode('utf-8')).hexdigest()[:8]}"
        page_path = f"wiki/projects/{snapshot.project}/architecture/pipelines/{slug}.md"
        result.append(PipelineSnapshot(page_path=page_path, **item))
    return result


def _is_trusted_entrypoint(node: Mapping[str, Any]) -> bool:
    kind = str(node.get("kind") or "").casefold()
    if kind not in {"function", "method"}:
        return False
    name = str(node.get("name") or "").casefold()
    language = str(node.get("language") or "").casefold()
    exported = bool(node.get("is_exported"))
    decorators = str(node.get("decorators") or "").casefold()
    if language == "python":
        return name == "main"
    if language in {"suitescript", "suite_script", "suite-script"}:
        return exported and name in SUITESCRIPT_ENTRYPOINT_NAMES
    if language in {"javascript", "typescript", "js", "ts", "node"}:
        if exported and name in ENTRYPOINT_NAMES:
            return True
        return exported and (name in SUITESCRIPT_ENTRYPOINT_NAMES or "define" in decorators) and name in SUITESCRIPT_ENTRYPOINT_NAMES
    return False


def _pipeline_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.replace("/", "-"))
    slug = re.sub(r"-+", "-", slug).strip("-._").lower()
    return slug[:120] or "pipeline"


def _desired_state(snapshot: GraphSnapshot, pipelines: list[PipelineSnapshot]) -> dict[str, Any]:
    raw_root = f"raw/sources/projects/{snapshot.project}/codegraph"
    graph = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "project": snapshot.project,
        "source_name": CODEGRAPH_SOURCE_NAME,
        "codegraph_version": snapshot.codegraph_version,
        "codegraph_extraction_version": snapshot.extraction_version,
        "codegraph_schema_version": snapshot.schema_version,
        "revision": snapshot.revision,
        "files": list(snapshot.files),
        "nodes": list(snapshot.nodes),
        "edges": list(snapshot.edges),
        "unresolved_refs": list(snapshot.unresolved_refs),
    }
    graph_bytes = _canonical_json(graph)
    graph_hash = hashlib.sha256(graph_bytes).hexdigest()
    file_page_map = {
        str(item["path"]): f"wiki/projects/{snapshot.project}/architecture/code-facts/{item['path']}.md"
        for item in snapshot.files
    }
    manifest_files = [
        {"source_path": str(item["path"]), "source_hash": str(item["content_hash"]), "page_path": file_page_map[str(item["path"])]}
        for item in snapshot.files
    ]
    manifest_pipelines = [
        {
            "pipeline_id": item.pipeline_id,
            "page_path": item.page_path,
            "entrypoint": f"{item.entrypoint.get('file_path') or ''}::{item.entrypoint.get('qualified_name') or item.entrypoint.get('name') or ''}",
            "member_files": list(item.member_files),
            "pipeline_status": item.pipeline_status,
        }
        for item in pipelines
    ]
    manifest = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "project": snapshot.project,
        "source_name": CODEGRAPH_SOURCE_NAME,
        "codegraph_version": snapshot.codegraph_version,
        "codegraph_extraction_version": snapshot.extraction_version,
        "codegraph_schema_version": snapshot.schema_version,
        "revision": snapshot.revision,
        "graph_hash": graph_hash,
        "files": manifest_files,
        "pipelines": manifest_pipelines,
        "overview_page": f"wiki/projects/{snapshot.project}/architecture/code-overview.md",
    }
    manifest_bytes = _canonical_json(manifest)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    raw_sources = [f"{raw_root}/graph.json", f"{raw_root}/manifest.json"]
    source_hashes = [graph_hash, manifest_hash]

    pages: dict[str, str] = {}
    pipeline_by_file: dict[str, list[PipelineSnapshot]] = defaultdict(list)
    for pipeline in pipelines:
        for path in pipeline.member_files:
            pipeline_by_file[path].append(pipeline)
    nodes_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in snapshot.nodes:
        file_path = str(node.get("file_path") or "")
        if file_path:
            nodes_by_file[file_path].append(node)
    edges_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    node_by_id = {str(node["id"]): node for node in snapshot.nodes}
    for edge in snapshot.edges:
        source_file = str(node_by_id.get(str(edge.get("source") or ""), {}).get("file_path") or "")
        target_file = str(node_by_id.get(str(edge.get("target") or ""), {}).get("file_path") or "")
        for file_path in {source_file, target_file} - {""}:
            edges_by_file[file_path].append(edge)

    for file_item in snapshot.files:
        source_path = str(file_item["path"])
        body = _render_file_body(source_path, file_item, nodes_by_file.get(source_path, ()), edges_by_file.get(source_path, ()), pipeline_by_file.get(source_path, ()), file_page_map)
        pages[file_page_map[source_path]] = _render_page(
            title=source_path,
            body=body,
            frontmatter=_base_frontmatter(snapshot, "file", source_path, raw_sources, source_hashes)
            | {"source_path": source_path, "source_hash": str(file_item["content_hash"]), "language": str(file_item.get("language") or "unknown")},
        )
    for pipeline in pipelines:
        body = _render_pipeline_body(pipeline, node_by_id, snapshot.edges, snapshot.unresolved_refs, file_page_map)
        pages[pipeline.page_path] = _render_page(
            title=pipeline.pipeline_id,
            body=body,
            frontmatter=_base_frontmatter(snapshot, "pipeline", pipeline.pipeline_id, raw_sources, source_hashes)
            | {"pipeline_id": pipeline.pipeline_id, "pipeline_status": pipeline.pipeline_status, "member_files": list(pipeline.member_files)},
        )
    overview_path = str(manifest["overview_page"])
    pages[overview_path] = _render_page(
        title=f"{snapshot.project} Code Overview",
        body=_render_overview_body(snapshot, pipelines, file_page_map),
        frontmatter=_base_frontmatter(snapshot, "overview", "overview", raw_sources, source_hashes),
    )
    raw = {f"{raw_root}/graph.json": graph_bytes, f"{raw_root}/manifest.json": manifest_bytes}
    return {"raw": raw, "pages": pages, "snapshot": snapshot, "pipelines": pipelines}


def _base_frontmatter(snapshot: GraphSnapshot, artifact_kind: str, managed_key: str, raw_sources: list[str], source_hashes: list[str]) -> dict[str, Any]:
    return {
        "type": "code_fact",
        "artifact_kind": artifact_kind,
        "generated": True,
        "managed_by": CODEGRAPH_SOURCE_NAME,
        "managed_key": managed_key,
        "retrieval_scope": "project_code",
        "project": snapshot.project,
        "source_name": CODEGRAPH_SOURCE_NAME,
        "codegraph_version": snapshot.codegraph_version,
        "codegraph_extraction_version": snapshot.extraction_version,
        "revision": snapshot.revision,
        "sources": raw_sources,
        "source_hashes": source_hashes,
    }


def _render_page(*, title: str, body: str, frontmatter: Mapping[str, Any]) -> str:
    values = dict(frontmatter)
    values["title"] = title
    values["render_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    yaml_text = yaml.safe_dump(values, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{yaml_text}\n---\n\n# {title}\n\n{body.strip()}\n"


def _render_file_body(source_path: str, file_item: Mapping[str, Any], nodes: Iterable[Mapping[str, Any]], edges: Iterable[Mapping[str, Any]], pipelines: Iterable[PipelineSnapshot], file_page_map: Mapping[str, str]) -> str:
    lines = [
        "## 文件事实",
        f"- 源码路径：`{source_path}`",
        f"- 语言：`{file_item.get('language') or 'unknown'}`",
        f"- 源码 SHA-256：`{file_item.get('content_hash') or ''}`",
        f"- 文件大小：`{file_item.get('size') or 0}` 字节",
        "",
        "## 符号",
    ]
    node_values = sorted(nodes, key=lambda item: (int(item.get("start_line") or 0), str(item.get("kind") or ""), str(item.get("name") or ""), str(item.get("id") or "")))
    if not node_values:
        lines.append("- 无结构化符号记录。")
    for node in node_values:
        name = str(node.get("qualified_name") or node.get("name") or node.get("id") or "")
        span = f"{node.get('start_line', '?')}-{node.get('end_line', '?')}"
        signature = str(node.get("signature") or "").strip()
        suffix = f"：`{signature}`" if signature else ""
        lines.append(f"- `{node.get('kind') or 'symbol'}` `{name}`（行 {span}）{suffix}")
    lines.extend(["", "## 结构关系"])
    edge_values = sorted(edges, key=lambda item: (str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("kind") or ""), int(item.get("line") or 0)))
    if not edge_values:
        lines.append("- 无跨符号关系记录。")
    for edge in edge_values:
        lines.append(f"- `{edge.get('kind') or 'relation'}`：`{edge.get('source') or ''}` → `{edge.get('target') or ''}`")
    lines.extend(["", "## 所属技术 pipeline"])
    pipeline_values = sorted(pipelines, key=lambda item: item.pipeline_id)
    if not pipeline_values:
        lines.append("- 未被识别为可信入口的技术 pipeline 成员。")
    for pipeline in pipeline_values:
        lines.append(f"- [[{pipeline.page_path[:-3]}|{pipeline.pipeline_id}]]")
    return "\n".join(lines)


def _render_pipeline_body(pipeline: PipelineSnapshot, node_by_id: Mapping[str, Mapping[str, Any]], edges: Iterable[Mapping[str, Any]], unresolved: Iterable[Mapping[str, Any]], file_page_map: Mapping[str, str]) -> str:
    lines = [
        "## Pipeline 状态",
        f"- 状态：`{pipeline.pipeline_status}`",
        f"- 入口：`{pipeline.entrypoint.get('file_path') or ''}::{pipeline.entrypoint.get('qualified_name') or pipeline.entrypoint.get('name') or ''}`",
        f"- 可达节点：`{len(pipeline.node_ids)}`",
        f"- 成员文件：`{len(pipeline.member_files)}`",
        "",
        "## 成员文件",
    ]
    for path in pipeline.member_files:
        target = file_page_map.get(path)
        lines.append(f"- [[{target[:-3] if target else path}|{path}]]" if target else f"- `{path}`")
    lines.extend(["", "## 执行关系"])
    pipeline_edge_keys = set(pipeline.edge_keys) | set(pipeline.external_edge_keys)
    selected_edges = [edge for edge in edges if (str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("kind") or "")) in pipeline_edge_keys]
    if not selected_edges:
        lines.append("- 入口没有已解析的 calls/instantiates 关系。")
    for edge in sorted(selected_edges, key=lambda item: (str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("kind") or ""))):
        source = node_by_id.get(str(edge.get("source") or ""), {})
        target = node_by_id.get(str(edge.get("target") or ""), {})
        source_name = str(source.get("qualified_name") or source.get("name") or edge.get("source") or "")
        target_name = str(target.get("qualified_name") or target.get("name") or edge.get("target") or "")
        lines.append(f"- `{edge.get('kind') or ''}`：`{source_name}` → `{target_name}`")
    if pipeline.external_edge_keys:
        lines.extend(["", "## 外部关系边界"])
        for source, target, kind in pipeline.external_edge_keys:
            lines.append(f"- `{kind}`：`{source}` → 外部/未解析目标 `{target}`")
    selected_unresolved = [item for item in unresolved if str(item.get("from_node_id") or "") in set(pipeline.node_ids)]
    if selected_unresolved:
        lines.extend(["", "## 未解析或外部边界"])
        for item in sorted(selected_unresolved, key=lambda value: (str(value.get("file_path") or ""), int(value.get("line") or 0), str(value.get("reference_name") or ""))):
            lines.append(f"- `{item.get('reference_kind') or 'reference'}` `{item.get('reference_name') or ''}`（{item.get('file_path') or ''}:{item.get('line') or '?'}）")
    return "\n".join(lines)


def _render_overview_body(snapshot: GraphSnapshot, pipelines: Iterable[PipelineSnapshot], file_page_map: Mapping[str, str]) -> str:
    lines = [
        "## 同步摘要",
        f"- 项目：`{snapshot.project}`",
        f"- CodeGraph 版本：`{snapshot.codegraph_version}`",
        f"- Revision：`{snapshot.revision}`",
        f"- 文件：`{len(snapshot.files)}`",
        f"- 节点：`{len(snapshot.nodes)}`",
        f"- 关系：`{len(snapshot.edges)}`",
        "",
        "## Pipeline",
    ]
    pipeline_values = sorted(pipelines, key=lambda item: item.pipeline_id)
    if not pipeline_values:
        lines.append("- 未识别到可信技术入口。")
    for pipeline in pipeline_values:
        lines.append(f"- [[{pipeline.page_path[:-3]}|{pipeline.pipeline_id}]]（`{pipeline.pipeline_status}`）")
    lines.extend(["", "## 文件事实"])
    for source_path in sorted(file_page_map):
        target = file_page_map[source_path]
        lines.append(f"- [[{target[:-3]}|{source_path}]]")
    return "\n".join(lines)


def _commit_desired_state(vault: Path, project: str, desired: Mapping[str, Any]) -> dict[str, Any]:
    pages = {str(path): str(content) for path, content in dict(desired["pages"]).items()}
    raw = {str(path): bytes(content) for path, content in dict(desired["raw"]).items()}
    existing_managed = _existing_managed_pages(vault, project)
    for relative, content in pages.items():
        target = vault / PurePosixPath(relative)
        if target.exists():
            if not target.is_file():
                raise CodeGraphSyncError("codegraph_page_conflict", f"CodeGraph page target is not a file: {relative}")
            frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8"))
            if not is_codegraph_frontmatter(frontmatter, project=project):
                raise CodeGraphSyncError("codegraph_managed_page", f"refusing to overwrite a non-CodeGraph page: {relative}")
    created = sum(1 for path in pages if not (vault / PurePosixPath(path)).exists())
    updated = sum(1 for path, content in pages.items() if (vault / PurePosixPath(path)).is_file() and (vault / PurePosixPath(path)).read_text(encoding="utf-8") != content)
    unchanged = len(pages) - created - updated
    stale = sorted(existing_managed - set(pages))

    vault.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".codegraph-sync-", dir=vault))
    replacements: list[tuple[Path, Path, bool]] = []
    try:
        _stage_raw(stage, raw)
        _stage_wiki(vault, stage, project, pages)
        _apply_replacements(vault, stage, project, pages, raw, replacements)
        index_result = _stage_and_swap_index(vault, stage, replacements)
        result = {
            "ok": True,
            "project": project,
            "source_name": CODEGRAPH_SOURCE_NAME,
            "revision": desired["snapshot"].revision,
            "codegraph_version": desired["snapshot"].codegraph_version,
            "created": created,
            "updated": updated,
            "deleted": len(stale),
            "unchanged": unchanged,
            "index_updated": bool(index_result.get("ok")),
            "warnings": _warnings(desired),
        }
        return result
    except CodeGraphSyncError:
        _rollback_replacements(replacements)
        raise
    except Exception as exc:  # noqa: BLE001 - every sync failure must roll back the staged replacement
        _rollback_replacements(replacements)
        raise CodeGraphSyncError("codegraph_sync_failed", "CodeGraph sync could not be committed") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _warnings(desired: Mapping[str, Any]) -> list[str]:
    partial = sum(1 for item in desired["pipelines"] if item.pipeline_status == "partial")
    return [f"pipeline_partial:{partial}"] if partial else []


def _existing_managed_pages(vault: Path, project: str) -> set[str]:
    architecture = vault / "wiki" / "projects" / project / "architecture"
    paths: set[str] = set()
    if not architecture.exists():
        return paths
    candidates = [architecture / "code-overview.md", *((architecture / "code-facts").rglob("*.md") if (architecture / "code-facts").exists() else []), *((architecture / "pipelines").rglob("*.md") if (architecture / "pipelines").exists() else [])]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if is_codegraph_frontmatter(frontmatter, project=project):
            paths.add(path.relative_to(vault).as_posix())
    return paths


def _stage_raw(stage: Path, raw: Mapping[str, bytes]) -> None:
    for relative, content in raw.items():
        target = stage / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _stage_wiki(vault: Path, stage: Path, project: str, pages: Mapping[str, str]) -> None:
    for subdir in ("code-facts", "pipelines"):
        target = vault / "wiki" / "projects" / project / "architecture" / subdir
        staged = stage / "wiki" / "projects" / project / "architecture" / subdir
        if target.exists():
            shutil.copytree(target, staged)
        else:
            staged.mkdir(parents=True, exist_ok=True)
        for path in sorted(staged.rglob("*.md")):
            try:
                frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if is_codegraph_frontmatter(frontmatter, project=project):
                path.unlink()
    for relative, content in pages.items():
        staged = stage / PurePosixPath(relative)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(content, encoding="utf-8")


def _apply_replacements(vault: Path, stage: Path, project: str, pages: Mapping[str, str], raw: Mapping[str, bytes], replacements: list[tuple[Path, Path, bool]]) -> None:
    targets = [
        (vault / "raw" / "sources" / "projects" / project / "codegraph", stage / "raw" / "sources" / "projects" / project / "codegraph"),
        (vault / "wiki" / "projects" / project / "architecture" / "code-facts", stage / "wiki" / "projects" / project / "architecture" / "code-facts"),
        (vault / "wiki" / "projects" / project / "architecture" / "pipelines", stage / "wiki" / "projects" / project / "architecture" / "pipelines"),
        (vault / "wiki" / "projects" / project / "architecture" / "code-overview.md", stage / "wiki" / "projects" / project / "architecture" / "code-overview.md"),
    ]
    backup_root = stage / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    for index, (target, staged) in enumerate(targets):
        if not staged.exists():
            continue
        old = backup_root / str(index)
        had_old = target.exists()
        if had_old:
            old.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, old)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
        except Exception:
            if had_old and old.exists():
                os.replace(old, target)
            raise
        replacements.append((target, old, had_old))


def _stage_and_swap_index(vault: Path, stage: Path, replacements: list[tuple[Path, Path, bool]]) -> dict[str, Any]:
    index_stage = stage / "active-retrieval.sqlite3"
    source_store = RetrievalIndexStore(vault, scope="active")
    staged_store = RetrievalIndexStore(vault, scope="active", path=index_stage)
    build = staged_store.build(source_store.iter_vault_pages())
    if not build.get("ok"):
        raise CodeGraphSyncError("index_build_failed", "active retrieval index build failed")
    target = vault / ".llm-wiki" / "retrieval.sqlite3"
    old = stage / "backups" / "active-retrieval.sqlite3"
    had_old = target.exists()
    if had_old:
        old.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, old)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(index_stage, target)
    except Exception:
        if had_old and old.exists():
            os.replace(old, target)
        raise
    replacements.append((target, old, had_old))
    return build


def _rollback_replacements(replacements: list[tuple[Path, Path, bool]]) -> None:
    for target, old, had_old in reversed(replacements):
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            if had_old and old.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, target)
        except OSError:
            # Preserve the original sync error.  The caller still receives a
            # deterministic failure code; the rollback issue is visible in
            # the filesystem and can be repaired by a subsequent sync.
            continue
