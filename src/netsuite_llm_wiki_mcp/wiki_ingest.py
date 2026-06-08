from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Protocol

from netsuite_llm_wiki_mcp.codegraph_client import CodeGraphClient
from netsuite_llm_wiki_mcp.redaction import redact_sensitive_text
from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry, WikiPage
from netsuite_llm_wiki_mcp.wiki_overview import refresh_overview
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root, safe_segment, slug
from netsuite_llm_wiki_mcp.wikilinks import normalize_wikilink_targets



_MAX_SOURCE_FILES = 200
_MAX_SOURCE_BYTES = 5_000_000
_ALLOWED_SOURCE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
_DENIED_SOURCE_NAMES = {".env", "credentials.json", "token.json", "secrets.json"}
_DENIED_SOURCE_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def _resolve_source_path(root: Path, source_path: str | Path) -> Path:
    p = Path(source_path).expanduser()
    if not p.is_absolute():
        candidate = (root / p).resolve()
        if candidate.exists():
            return candidate
    return p.resolve()


class CodeGraphLike(Protocol):
    def status(self) -> dict[str, Any]: ...
    def files(self) -> dict[str, Any]: ...
    def context(self, query: str) -> dict[str, Any]: ...
    def impact(self, symbol: str) -> dict[str, Any]: ...
    def graph_snapshot(self) -> dict[str, Any]: ...
    def callers(self, symbol: str) -> dict[str, Any]: ...
    def callees(self, symbol: str) -> dict[str, Any]: ...


def ingest_source(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"ok": False, "code": "unsupported_source_type", "error": "only codegraph ingest is implemented"}


def staged_wiki_ingest(
    vault_root: str | Path,
    stage: str,
    project: str,
    source_name: str,
    source_path: str | Path | None = None,
    source_type: str = "file",
    language: str = "zh-CN",
    analysis: dict[str, Any] | str | None = None,
    generation: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)
    try:
        project_value = safe_segment(project)
        source_value = safe_segment(source_name)
        source_type_value = safe_segment(source_type)
    except ValueError as exc:
        return {"ok": False, "code": getattr(exc, "code", "invalid_path_component"), "error": str(exc)}

    if stage == "prepare":
        return _prepare_combined(root, project_value, source_value, source_path, source_type_value, language)
    if stage == "prepare_analysis":
        return _prepare_analysis(root, project_value, source_value, source_path, source_type_value, language)
    if stage == "prepare_generation":
        if analysis is None:
            return {"ok": False, "code": "missing_analysis", "error": "analysis is required for prepare_generation"}
        return _prepare_generation(root, project_value, source_value, language, analysis, source_type_value)
    if stage == "apply_generation" or stage == "apply":
        if generation is None:
            return {"ok": False, "code": "missing_generation", "error": "generation is required for apply_generation"}
        return _apply_generation(root, project_value, source_value, language, generation, source_type_value)
    return {"ok": False, "code": "unsupported_stage", "error": f"unsupported staged ingest stage: {stage}"}



def rescan_source(
    vault_root: str | Path,
    project: str,
    source_name: str,
    source_path: str | Path,
    source_type: str = "file",
    language: str = "zh-CN",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)
    try:
        project_value = safe_segment(project)
        source_value = safe_segment(source_name)
        source_type_value = safe_segment(source_type)
    except ValueError as exc:
        return {"ok": False, "code": getattr(exc, "code", "invalid_path_component"), "error": str(exc)}

    source_root = _resolve_source_path(root, source_path)
    if not source_root.exists():
        return {"ok": False, "code": "source_not_found", "error": f"source_path does not exist: {source_path}"}

    sources = _collect_sources(source_root)
    source_error = _source_limit_error(sources)
    if source_error is not None:
        return source_error

    source_hash = _sources_hash(sources, source_root)
    cache_path = _cache_path(root, project_value, source_value, source_type_value)
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("source_type") == source_type_value and cache.get("source_hash") == source_hash:
            return {
                "ok": True,
                "stage": "rescan",
                "status": "unchanged",
                "project": project_value,
                "source_name": source_value,
                "source_hash": source_hash,
                "paths": [],
                "message": "source hash unchanged; reuse previous generated wiki pages",
            }

    raw_dir = root / "raw" / "sources" / source_type_value / project_value / source_value
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = _write_source_snapshots(raw_dir, root, sources, source_root)
    _write_cache(
        root,
        project_value,
        source_value,
        {"source_hash": source_hash, "source_type": source_type_value, "manifest": manifest, "status": "prepared"},
        source_type=source_type_value,
    )

    paths = [item["path"] for item in manifest]
    paths.append((raw_dir / "manifest.json").relative_to(root).as_posix())
    classification_context = [item["relative_path"] for item in manifest]
    return {
        "ok": True,
        "stage": "rescan",
        "status": "changed",
        "project": project_value,
        "source_name": source_value,
        "source_hash": source_hash,
        "classification_context": classification_context,
        "context": {"sources": manifest, "language": language},
        "paths": paths,
        "prompt": _analysis_prompt(project_value, source_value, language, manifest),
        "expected_response_schema": {
            "key_entities": ["string"],
            "concepts": ["string"],
            "tensions": ["string"],
            "suggested_pages": [{"path": "wiki/...", "title": "string", "type": "string", "summary": "string"}],
        },
        "next_call": {"tool": "wiki_ingest_llm", "stage": "prepare_generation", "required": ["analysis"]},
    }



def ingest_codegraph(
    vault_root: str | Path,
    project: str,
    source_name: str,
    query: str = "project code overview",
    codegraph_project_path: str | Path | None = None,
    client: CodeGraphLike | None = None,
    include_extensions: list[str] | None = None,
    profile: str = "generic",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)
    project_value = safe_segment(project)
    source_value = safe_segment(source_name)
    profile_value = safe_segment(profile or "generic")

    if client is None:
        cg_path = Path(codegraph_project_path).expanduser().resolve() if codegraph_project_path else Path.cwd()
        if not cg_path.is_dir():
            return {
                "ok": False,
                "code": "codegraph_path_not_found",
                "error": f"codegraph_project_path does not exist or is not a directory: {cg_path}",
            }
        cg = CodeGraphClient(cg_path)
    else:
        cg = client

    status = cg.status()
    if not status.get("ok"):
        return status
    files = cg.files()
    if not files.get("ok"):
        return files
    context = cg.context(query)
    if not context.get("ok"):
        # Graceful degradation: context CLI subcommand may not exist in older
        # CodeGraph versions.  Fall back to an empty context dict — the full
        # graph snapshot from graph_snapshot() is sufficient for code-page
        # generation.
        context = {"ok": True, "data": {}}
    graph = cg.graph_snapshot()
    has_full_graph = bool(graph.get("ok"))
    if not graph.get("ok"):
        graph = {"ok": True, "data": _context_to_graph_snapshot(context.get("data", {}), files.get("data", {}))}

    if include_extensions:
        files["data"] = _filter_files_by_extensions(files.get("data", {}), include_extensions)
        if has_full_graph:
            graph["data"] = _filter_graph_by_extensions(graph["data"], include_extensions)

    context_json = json.dumps({
        "context": context.get("data", {}),
        "graph": graph.get("data", {}),
        "include_extensions": include_extensions or [],
        "profile": profile_value,
    }, ensure_ascii=False, sort_keys=True)
    context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    cache_path = _cache_path(root, project_value, source_value, "codegraph")
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("codegraph_context_hash") == context_hash:
            return {
                "ok": True,
                "status": "unchanged",
                "project": project_value,
                "source_name": source_value,
                "profile": profile_value,
                "context_hash": context_hash,
                "message": "codegraph context unchanged; skipping rewrite",
            }

    # 清理旧 code 页面，确保删除的源文件不会残留
    _clean_code_pages(root, project_value)

    snapshot_dir = root / "raw" / "sources" / "codegraph" / project_value
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshots = {
        "status.json": status.get("data", {}),
        "files.json": files.get("data", {}),
        "context.json": context.get("data", {}),
        "graph.json": graph.get("data", {}),
    }
    written_paths: list[str] = []
    for name, data in snapshots.items():
        target = snapshot_dir / name
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        written_paths.append(target.relative_to(root).as_posix())

    source_page_path = Path("wiki") / "sources" / "projects" / project_value / f"{source_value}.md"
    code_page_paths: list[str] = []

    graph_snapshot_path = next((p for p in written_paths if p.endswith("/graph.json")), written_paths[2] if len(written_paths) > 2 else "")
    if has_full_graph:
        code_pages = _code_pages_from_graph(graph.get("data", {}), project_value, source_value, graph_snapshot_path)
    else:
        code_pages = _code_pages_from_context(context.get("data", {}), project_value, source_value, written_paths[2] if len(written_paths) > 2 else "")
    for page in code_pages:
        write_wiki_page(root, page)
        written_paths.append(page.relative_path.as_posix())
        code_page_paths.append(page.relative_path.as_posix())
        if not has_full_graph:
            symbol = str(page.frontmatter.get("symbol", ""))
            if symbol:
                impact = cg.impact(symbol)
                if impact.get("ok"):
                    impact_path = snapshot_dir / f"impact-{slug(symbol)}.json"
                    impact_path.write_text(json.dumps(impact.get("data", {}), ensure_ascii=False, indent=2), encoding="utf-8")
                    written_paths.append(impact_path.relative_to(root).as_posix())

    # SuiteScript pipeline detection is profile-specific and only runs when full graph is available.
    if has_full_graph and profile_value == "suitescript":
        from netsuite_llm_wiki_mcp.pipeline_detector import PipelineDetector
        cg_project_path = Path(codegraph_project_path) if codegraph_project_path else None
        detector = PipelineDetector(graph.get("data", {}), cg_project_path)
        pipelines = detector.detect()
        for pipeline in pipelines:
            page = _pipeline_page(pipeline, project_value, source_value, graph_snapshot_path)
            write_wiki_page(root, page)
            written_paths.append(page.relative_path.as_posix())
            code_page_paths.append(page.relative_path.as_posix())
        if pipelines:
            cg_page = _call_graph_page(pipelines, project_value, source_value, graph_snapshot_path)
            write_wiki_page(root, cg_page)
            written_paths.append(cg_page.relative_path.as_posix())
            code_page_paths.append(cg_page.relative_path.as_posix())
            # Regenerate overview with pipeline grouping
            data = graph.get("data", {})
            overview = _project_overview_page(
                _extract_graph_files(data),
                _extract_nodes(data),
                _extract_edges(data),
                {str(n.get("id")): n for n in _extract_nodes(data) if n.get("id")},
                project_value, source_value, graph_snapshot_path,
                pipelines=pipelines,
            )
            write_wiki_page(root, overview)

    wikilinks = "\n".join(f"- [[{p[5:-3]}]]" for p in code_page_paths) if code_page_paths else "(no code pages)"
    raw_sources = [p for p in written_paths if p.startswith("raw/")]
    source_page = WikiPage(
        relative_path=source_page_path,
        frontmatter={
            "type": "source_index",
            "generated": True,
            "project": project_value,
            "source_name": source_value,
            "source_type": "codegraph",
            "sources": raw_sources,
            "profile": profile_value,
            "summary": f"CodeGraph snapshot for {project_value}/{source_value}",
        },
        title=f"CodeGraph {project_value}/{source_value}",
        body=f"CodeGraph snapshot for {project_value}/{source_value}\n\n## Raw sources\n\n"
             + "\n".join(f"- `{s}`" for s in raw_sources)
             + f"\n\n## Generated pages\n\n{wikilinks}",
    )
    write_wiki_page(root, source_page)
    written_paths.append(source_page_path.as_posix())

    refresh_indexes(root)
    refresh_overview(root)
    append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title=f"CodeGraph {project_value}/{source_value}",
            paths=written_paths,
            sources=[f"raw/sources/codegraph/{project_value}/graph.json"],
            project=project_value,
            status="ok",
        ),
    )
    _write_cache(root, project_value, source_value, {"codegraph_context_hash": context_hash, "status": "ingested", "profile": profile_value}, source_type="codegraph")
    return {
        "ok": True,
        "project": project_value,
        "source_name": source_value,
        "profile": profile_value,
        "written": len(written_paths),
        "paths": written_paths,
    }


def _prepare_combined(
    root: Path,
    project: str,
    source_name: str,
    source_path: str | Path | None,
    source_type: str,
    language: str,
) -> dict[str, Any]:
    if source_path is None:
        return {"ok": False, "code": "missing_source_path", "error": "source_path is required for prepare"}
    source_root = _resolve_source_path(root, source_path)
    if not source_root.exists():
        return {"ok": False, "code": "source_not_found", "error": f"source_path does not exist: {source_path}"}
    sources = _collect_sources(source_root)
    source_error = _source_limit_error(sources)
    if source_error is not None:
        return source_error
    source_hash = _sources_hash(sources, source_root)
    cache_path = _cache_path(root, project, source_name, source_type)
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("source_hash") == source_hash:
            return {
                "ok": True,
                "stage": "prepare",
                "status": "skipped",
                "code": "source_unchanged",
                "project": project,
                "source_name": source_name,
                "source_hash": source_hash,
                "message": "source hash unchanged; reuse previous generated wiki pages",
            }

    raw_dir = root / "raw" / "sources" / source_type / project / source_name
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = _write_source_snapshots(raw_dir, root, sources, source_root)
    _write_cache(root, project, source_name, {"source_hash": source_hash, "manifest": manifest, "status": "prepared"}, source_type=source_type)

    wiki_context = {
        "purpose": _read_optional(root / "purpose.md"),
        "schema": _read_optional(root / "schema.md"),
        "index": _read_optional(root / "wiki" / "index.md"),
    }
    prompt = _combined_prompt(project, source_name, language, manifest, wiki_context)
    return {
        "ok": True,
        "stage": "prepare",
        "status": "needs_model",
        "project": project,
        "source_name": source_name,
        "source_hash": source_hash,
        "prompt": prompt,
        "expected_response_schema": {
            "source_summary": {"title": "string", "summary": "string", "body": "markdown"},
            "pages": [{"path": "wiki/...", "title": "string", "type": "string", "summary": "string", "body": "markdown", "sources": ["raw/..."]}],
        },
        "next_call": {"tool": "wiki_ingest_llm", "stage": "apply", "required": ["generation"]},
    }


def _prepare_analysis(
    root: Path,
    project: str,
    source_name: str,
    source_path: str | Path | None,
    source_type: str,
    language: str,
) -> dict[str, Any]:
    if source_path is None:
        return {"ok": False, "code": "missing_source_path", "error": "source_path is required for prepare_analysis"}
    source_root = _resolve_source_path(root, source_path)
    if not source_root.exists():
        return {"ok": False, "code": "source_not_found", "error": f"source_path does not exist: {source_path}"}
    sources = _collect_sources(source_root)
    source_error = _source_limit_error(sources)
    if source_error is not None:
        return source_error
    source_hash = _sources_hash(sources, source_root)
    cache_path = _cache_path(root, project, source_name, source_type)
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("source_hash") == source_hash:
            return {
                "ok": True,
                "stage": "prepare_analysis",
                "status": "skipped",
                "code": "source_unchanged",
                "project": project,
                "source_name": source_name,
                "source_hash": source_hash,
                "message": "source hash unchanged; reuse previous generated wiki pages",
            }

    raw_dir = root / "raw" / "sources" / source_type / project / source_name
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = _write_source_snapshots(raw_dir, root, sources, source_root)
    _write_cache(root, project, source_name, {"source_hash": source_hash, "manifest": manifest, "status": "prepared"}, source_type=source_type)
    classification_context = [item["relative_path"] for item in manifest]
    return {
        "ok": True,
        "stage": "prepare_analysis",
        "status": "needs_model",
        "project": project,
        "source_name": source_name,
        "source_hash": source_hash,
        "classification_context": classification_context,
        "context": {"sources": manifest, "language": language},
        "prompt": _analysis_prompt(project, source_name, language, manifest),
        "expected_response_schema": {
            "key_entities": ["string"],
            "concepts": ["string"],
            "tensions": ["string"],
            "suggested_pages": [{"path": "wiki/...", "title": "string", "type": "string", "summary": "string"}],
        },
        "next_call": {"tool": "wiki_ingest_llm", "stage": "prepare_generation", "required": ["analysis"]},
    }


def _prepare_generation(root: Path, project: str, source_name: str, language: str, analysis: dict[str, Any] | str, source_type: str = "file") -> dict[str, Any]:
    cache = _read_cache(root, project, source_name, source_type)
    if not cache:
        return {"ok": False, "code": "missing_prepared_source", "error": "run prepare_analysis before prepare_generation"}
    wiki_context = {
        "purpose": _read_optional(root / "purpose.md"),
        "schema": _read_optional(root / "schema.md"),
        "index": _read_optional(root / "wiki" / "index.md"),
    }
    return {
        "ok": True,
        "stage": "prepare_generation",
        "status": "needs_model",
        "project": project,
        "source_name": source_name,
        "source_hash": cache.get("source_hash", ""),
        "context": {"analysis": analysis, "wiki": wiki_context, "manifest": cache.get("manifest", [])},
        "prompt": _generation_prompt(project, source_name, language, analysis, wiki_context),
        "expected_response_schema": {
            "source_summary": {"title": "string", "summary": "string", "body": "markdown"},
            "pages": [{"path": "wiki/...", "title": "string", "type": "string", "summary": "string", "body": "markdown", "sources": ["raw/..."]}],
        },
        "next_call": {"tool": "wiki_ingest_llm", "stage": "apply_generation", "required": ["generation"]},
    }




def _apply_generation(root: Path, project: str, source_name: str, language: str, generation: dict[str, Any] | str, source_type: str = "file") -> dict[str, Any]:
    cache = _read_cache(root, project, source_name, source_type)
    if not cache:
        return {"ok": False, "code": "missing_prepared_source", "error": "run prepare_analysis before apply_generation"}
    payload = _generation_payload(generation)
    if payload is None:
        return {"ok": False, "code": "invalid_generation", "error": "generation must be a JSON object or JSON string"}
    manifest_sources = [item["path"] for item in cache.get("manifest", []) if isinstance(item, dict) and item.get("path")]
    written_paths: list[str] = []

    raw_summary = payload.get("source_summary")
    if isinstance(raw_summary, dict):
        summary = raw_summary
    elif isinstance(raw_summary, str) and raw_summary.strip():
        summary = {"summary": raw_summary}
    else:
        summary = {}
    one_line_summary = str(summary.get("summary") or f"Source index for {project}/{source_name}")

    pages = list(payload.get("pages") or [])
    for key in ("concept", "concepts"):
        extra = payload.get(key)
        if isinstance(extra, dict):
            pages.append(extra)
        elif isinstance(extra, list):
            pages.extend(item for item in extra if isinstance(item, dict))

    for item in pages:
        if not isinstance(item, dict):
            continue
        try:
            path = _safe_generated_page_path(item, project)
        except ValueError as exc:
            return {"ok": False, "code": "invalid_generated_path", "error": str(exc)}
        page_sources = [str(value) for value in item.get("sources", [])] or manifest_sources
        page = WikiPage(
            relative_path=path,
            frontmatter={
                "type": str(item.get("type") or "concept"),
                "generated": True,
                "project": project,
                "source_name": source_name,
                "source_hash": cache.get("source_hash", ""),
                "language": language,
                "sources": page_sources,
                "summary": str(item.get("summary") or ""),
            },
            title=str(item.get("title") or path.stem),
            body=normalize_wikilink_targets(str(item.get("body") or item.get("summary") or "")),
        )
        write_wiki_page(root, page)
        written_paths.append(path.as_posix())

    index_paths = _write_source_index_pages(
        root, project, source_name, source_type, language,
        cache.get("source_hash", ""), manifest_sources, one_line_summary, written_paths,
    )
    written_paths.extend(index_paths)

    refresh_indexes(root)
    refresh_overview(root)
    append_log_entry(
        root,
        WikiLogEntry(
            operation="llm_ingest",
            title=f"{project}/{source_name}",
            paths=written_paths,
            sources=manifest_sources,
            project=project,
            status="ok",
        ),
    )
    _write_cache(root, project, source_name, {**cache, "status": "applied", "written_paths": written_paths}, source_type=source_type)
    return {"ok": True, "stage": "apply_generation", "project": project, "source_name": source_name, "written": len(written_paths), "paths": written_paths}


def _collect_sources(source_root: Path) -> list[Path]:
    if source_root.is_file():
        return [source_root] if _is_allowed_source_file(source_root) else []
    return [path for path in sorted(source_root.rglob("*")) if path.is_file() and _is_allowed_source_file(path)]


def _write_source_index_pages(
    root: Path,
    project: str,
    source_name: str,
    source_type: str,
    language: str,
    source_hash: str,
    manifest_sources: list[str],
    summary: str,
    page_paths: list[str],
) -> list[str]:
    groups: dict[str, list[str]] = {}
    for p in page_paths:
        parts = p.split("/")
        if len(parts) >= 2 and parts[0] == "wiki":
            target_dir = parts[1]
        else:
            target_dir = "other"
        groups.setdefault(target_dir, []).append(p)

    if not groups:
        groups["concepts"] = []

    written: list[str] = []
    for target_dir, linked_pages in groups.items():
        index_path = Path("wiki") / "sources" / target_dir / project / f"{source_name}.md"
        wikilinks = "\n".join(f"- [[{Path(p).stem}]]" for p in linked_pages) if linked_pages else "(no pages generated)"
        body = f"{summary}\n\n## Raw sources\n\n"
        body += "\n".join(f"- `{s}`" for s in manifest_sources)
        body += f"\n\n## Generated pages\n\n{wikilinks}"
        index_page = WikiPage(
            relative_path=index_path,
            frontmatter={
                "type": "source_index",
                "generated": True,
                "project": project,
                "source_name": source_name,
                "source_type": source_type,
                "source_hash": source_hash,
                "language": language,
                "sources": manifest_sources,
                "summary": summary,
            },
            title=f"{project}/{source_name}",
            body=body,
        )
        write_wiki_page(root, index_page)
        written.append(index_path.as_posix())
    return written
def _is_allowed_source_file(path: Path) -> bool:
    lowered_parts = {part.casefold() for part in path.parts}
    if lowered_parts & _DENIED_SOURCE_PARTS:
        return False
    name = path.name.casefold()
    if name in _DENIED_SOURCE_NAMES or name.startswith(".env"):
        return False
    return path.suffix.lower() in _ALLOWED_SOURCE_SUFFIXES


def _source_limit_error(sources: list[Path]) -> dict[str, Any] | None:
    if not sources:
        return {"ok": False, "code": "no_supported_sources", "error": "no supported text sources found"}
    if len(sources) > _MAX_SOURCE_FILES:
        return {"ok": False, "code": "too_many_sources", "error": f"source file count exceeds {_MAX_SOURCE_FILES}"}
    total_bytes = sum(path.stat().st_size for path in sources)
    if total_bytes > _MAX_SOURCE_BYTES:
        return {"ok": False, "code": "sources_too_large", "error": f"source bytes exceed {_MAX_SOURCE_BYTES}"}
    return None


def _sources_hash(sources: list[Path], source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sources:
        rel = path.relative_to(source_root.parent if source_root.is_file() else source_root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_source_snapshots(raw_dir: Path, root: Path, sources: list[Path], source_root: Path) -> list[dict[str, Any]]:
    manifest = []
    base = source_root.parent if source_root.is_file() else source_root
    for path in sources:
        rel = path.relative_to(base)
        target = raw_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        redacted_data = data
        if path.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}:
            redacted_text = redact_sensitive_text(data.decode("utf-8", errors="ignore"))
            target.write_text(redacted_text, encoding="utf-8")
            redacted_data = redacted_text.encode("utf-8")
        else:
            target.write_bytes(data)
        manifest.append({
            "relative_path": rel.as_posix(),
            "path": target.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "stored_sha256": hashlib.sha256(redacted_data).hexdigest(),
            "bytes": len(data),
        })
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _cache_path(root: Path, project: str, source_name: str, source_type: str = "file") -> Path:
    return root / ".llm-wiki" / "ingest-cache" / source_type / project / f"{source_name}.json"


def _read_cache(root: Path, project: str, source_name: str, source_type: str = "file") -> dict[str, Any]:
    path = _cache_path(root, project, source_name, source_type)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_cache(root: Path, project: str, source_name: str, data: dict[str, Any], source_type: str = "file") -> None:
    path = _cache_path(root, project, source_name, source_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _analysis_prompt(project: str, source_name: str, language: str, manifest: list[dict[str, Any]]) -> str:
    return "\n".join([
        f"You are analyzing source '{source_name}' for project '{project}' before generating an LLM Wiki.",
        f"Respond in {language}.",
        "Extract key entities, concepts, tensions, source traceability, and suggested wiki pages.",
        "Do not write files. Return JSON matching expected_response_schema.",
        "Sources:",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    ])


def _generation_prompt(project: str, source_name: str, language: str, analysis: dict[str, Any] | str, wiki_context: dict[str, str]) -> str:
    return "\n".join([
        f"Generate LLM Wiki pages for project '{project}' and source '{source_name}'.",
        f"Respond in {language}.",
        "Use the analysis and existing wiki context. Return JSON matching expected_response_schema.",
        "Every page must include source traceability via sources[].",
        "All [[wikilink]] targets must be all-lowercase kebab-case (e.g. [[user-event-script]], not [[User-Event-Script]]).",
        "Analysis:",
        json.dumps(analysis, ensure_ascii=False, indent=2) if not isinstance(analysis, str) else analysis,
        "Existing wiki context:",
        json.dumps(wiki_context, ensure_ascii=False, indent=2),
    ])


def _combined_prompt(project: str, source_name: str, language: str, manifest: list[dict[str, Any]], wiki_context: dict[str, str]) -> str:
    source_listing = "\n".join(f"- {item.get('relative_path', item.get('path', ''))}" for item in manifest)
    index_summary = wiki_context.get("index", "")[:2000]
    return "\n".join([
        f"Analyze source '{source_name}' for project '{project}' and generate LLM Wiki pages.",
        f"Respond in {language}. Return JSON matching expected_response_schema.",
        "",
        "## Source files",
        source_listing,
        "",
        "## Wiki purpose",
        wiki_context.get("purpose", "") or "(not set)",
        "",
        "## Wiki schema",
        wiki_context.get("schema", "") or "(not set)",
        "",
        "## Existing wiki index (truncated)",
        index_summary or "(empty)",
        "",
        "## Instructions",
        "1. Analyze the source files: extract key entities, concepts, and relationships.",
        "2. Generate a source_summary with a short title and a ONE-LINE summary (no body needed — index pages are auto-generated).",
        "3. Generate additional wiki pages (concepts, decisions, etc.) with full body content.",
        "4. Every page must include source traceability via sources[] referencing raw/ paths.",
        "5. All [[wikilink]] targets must be all-lowercase kebab-case (e.g. [[user-event-script]], not [[User-Event-Script]]).",
    ])


def _generation_payload(generation: dict[str, Any] | str) -> dict[str, Any] | None:
    if isinstance(generation, dict):
        return generation
    try:
        loaded = json.loads(generation)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _safe_generated_page_path(item: dict[str, Any], project: str) -> Path:
    raw_path = str(item.get("path") or "")
    if raw_path.startswith("wiki/") and raw_path.endswith(".md"):
        path = Path(raw_path)
        if _is_allowed_generated_path(path, project):
            return path
        raise ValueError(f"generated page path is outside the allowed project structure: {raw_path}")
    page_type = safe_segment(str(item.get("type") or "concept"))
    title = str(item.get("title") or item.get("summary") or "page")
    if page_type in {"code", "decision", "troubleshooting", "requirement"}:
        subdir = {"decision": "decisions", "requirement": "requirements"}.get(page_type, page_type)
        return Path("wiki") / "projects" / project / subdir / f"{slug(title)}.md"
    return Path("wiki") / "concepts" / project / f"{slug(title)}.md"


def _is_allowed_generated_path(path: Path, project: str) -> bool:
    parts = path.parts
    if len(parts) >= 5 and parts[0] == "wiki" and parts[1] == "projects" and parts[2] == project:
        return parts[3] in {"code", "decisions", "troubleshooting", "requirements"}
    if len(parts) >= 4 and parts[0] == "wiki" and parts[1] == "concepts" and parts[2] == project:
        return True
    return False


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _context_to_graph_snapshot(context_data: dict[str, Any], files_data: Any) -> dict[str, Any]:
    files = files_data.get("files", files_data) if isinstance(files_data, dict) else files_data
    return {
        "files": files if isinstance(files, list) else [],
        "nodes": _extract_nodes(context_data),
        "edges": _extract_edges(context_data),
    }


def _filter_graph_by_extensions(data: dict[str, Any], extensions: list[str]) -> dict[str, Any]:
    """Filter graph snapshot to only include files matching the given extensions."""
    ext_set = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}

    def _matches(file_path: str) -> bool:
        return Path(file_path).suffix.lower() in ext_set

    files = [f for f in (data.get("files") or []) if _matches(str(f.get("path", "")))]
    kept_paths = {str(f.get("path", "")) for f in files}
    nodes = [n for n in (data.get("nodes") or []) if str(n.get("filePath") or n.get("file_path") or "") in kept_paths]
    kept_ids = {str(n.get("id")) for n in nodes if n.get("id")}
    edges = [e for e in (data.get("edges") or []) if str(e.get("source", "")) in kept_ids or str(e.get("target", "")) in kept_ids]
    return {"files": files, "nodes": nodes, "edges": edges}


def _filter_files_by_extensions(data: Any, extensions: list[str]) -> Any:
    """Filter CodeGraph files() output to only include files matching extensions."""
    ext_set = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}

    def _matches(item: Any) -> bool:
        if isinstance(item, dict):
            file_path = str(item.get("path") or "")
        else:
            file_path = str(item or "")
        return Path(file_path).suffix.lower() in ext_set

    if isinstance(data, dict):
        value = data.get("files")
        if isinstance(value, list):
            return {**data, "files": [item for item in value if _matches(item)]}
        return data
    if isinstance(data, list):
        return [item for item in data if _matches(item)]
    return data


def _code_pages_from_graph(data: dict[str, Any], project: str, source_name: str, graph_snapshot: str) -> list[WikiPage]:
    files = _extract_graph_files(data)
    nodes = _extract_nodes(data)
    edges = _extract_edges(data)
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    pages = [_project_overview_page(files, nodes, edges, node_by_id, project, source_name, graph_snapshot)]

    nodes_by_file: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        file_path = str(node.get("filePath") or node.get("source_path") or node.get("path") or node.get("file") or "")
        if file_path:
            nodes_by_file.setdefault(file_path, []).append(node)

    file_paths = [str(item.get("path")) for item in files if isinstance(item, dict) and item.get("path")]
    for file_path in sorted(set(file_paths) | set(nodes_by_file)):
        if _is_vendored_file(file_path, nodes_by_file):
            continue
        file_info = next((item for item in files if isinstance(item, dict) and item.get("path") == file_path), {"path": file_path})
        pages.append(_file_code_fact_page(file_info, nodes_by_file.get(file_path, []), edges, node_by_id, project, source_name, graph_snapshot))
    return pages or _code_pages_from_context(data, project, source_name, graph_snapshot)


def _extract_graph_files(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    value = data.get("files") or []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _project_overview_page(
    files: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    project: str,
    source_name: str,
    graph_snapshot: str,
    pipelines: list[Any] | None = None,
) -> WikiPage:
    src_files = [item for item in files if item.get("path")]
    node_kinds: dict[str, int] = {}
    nodes_by_file: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        kind = str(node.get("kind") or "unknown")
        node_kinds[kind] = node_kinds.get(kind, 0) + 1
        fp = str(node.get("filePath") or node.get("source_path") or node.get("path") or node.get("file") or "")
        if fp:
            nodes_by_file.setdefault(fp, []).append(node)
    file_lines = [f"- `{item.get('path')}` ({item.get('language', 'unknown')}, nodes: {item.get('nodeCount', item.get('node_count', 0))})" for item in src_files]
    kind_lines = [f"- {kind}: {count}" for kind, count in sorted(node_kinds.items())]
    logic_lines = _global_logic_lines(edges, node_by_id, nodes_by_file=nodes_by_file)
    module_lines = _module_summary_lines(src_files, nodes)
    pipeline_section = _pipeline_overview_lines(pipelines) if pipelines else []
    return WikiPage(
        relative_path=Path("wiki") / "projects" / project / "code" / "overview.md",
        frontmatter={
            "type": "code_fact",
            "generated": True,
            "project": project,
            "source_name": source_name,
            "sources": [graph_snapshot],
            "codegraph_tool": "graph_snapshot",
            "symbol": "code-overview",
            "summary": f"Full CodeGraph project overview and logic chain for {project}",
        },
        title="Code Overview",
        body="\n".join([
            "## Global Code Framework",
            f"- Source files: {len(src_files)}",
            f"- Symbols: {len(nodes)}",
            f"- Relationships: {len(edges)}",
            *(([f"- Pipelines: {len(pipelines)}"] if pipelines else [])),
            "",
            *(["## Business Pipelines", *pipeline_section, ""] if pipeline_section else []),
            "## Module Layout",
            *(module_lines or ["(no modules)"]),
            "",
            "## Global Logic Chain",
            *(logic_lines or ["(no call relationships)"]),
            "",
            "## Symbol Kinds",
            *(kind_lines or ["(no symbols)"]),
            "",
            "## Source Files",
            *(file_lines or ["(no src files)"]),
        ]),
    )


def _file_code_fact_page(
    file_info: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    project: str,
    source_name: str,
    graph_snapshot: str,
) -> WikiPage:
    file_path = str(file_info.get("path") or "unknown")
    relative_path = _code_fact_relative_path(project, file_path)
    symbols = [_symbol_line(node) for node in sorted(nodes, key=lambda item: (item.get("startLine") or 0, str(item.get("name") or "")))]
    outgoing = _file_relationships(nodes, edges, node_by_id, direction="outgoing")
    incoming = _file_relationships(nodes, edges, node_by_id, direction="incoming")
    return WikiPage(
        relative_path=relative_path,
        frontmatter={
            "type": "code_fact",
            "generated": True,
            "project": project,
            "source_name": source_name,
            "sources": [graph_snapshot],
            "codegraph_tool": "graph_snapshot",
            "source_path": file_path,
            "symbol": file_path,
            "summary": f"CodeGraph file structure and relationships for {file_path}",
        },
        title=file_path,
        body="\n".join([
            "## File Fact",
            f"- Source path: `{file_path}`",
            f"- Language: `{file_info.get('language', 'unknown')}`",
            f"- Symbols: {len(nodes)}",
            "",
            "## Symbols",
            *(symbols or ["(no symbols)"]),
            "",
            "## Outgoing Relationships",
            *(outgoing or ["(no outgoing relationships)"]),
            "",
            "## Incoming Relationships",
            *(incoming or ["(no incoming relationships)"]),
        ]),
    )


def _code_fact_relative_path(project: str, file_path: str) -> Path:
    raw_parts = Path(file_path.replace("\\", "/")).parts
    if raw_parts and "." in raw_parts[-1]:
        raw_parts = (*raw_parts[:-1], Path(raw_parts[-1]).stem + ".md")
    safe_parts = [safe_segment(Path(part).stem) + Path(part).suffix if part.endswith(".md") else safe_segment(part) for part in raw_parts]
    return Path("wiki") / "projects" / project / "code" / Path(*safe_parts)


def _module_summary_lines(src_files: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for item in src_files:
        parts = Path(str(item.get("path", ""))).parts
        module = "/".join(parts[:2]) if len(parts) >= 2 else str(item.get("path", "unknown"))
        counts[module] = counts.get(module, 0) + 1
    node_counts: dict[str, int] = {}
    for node in nodes:
        file_path = str(node.get("filePath") or "")
        parts = Path(file_path).parts
        module = "/".join(parts[:2]) if len(parts) >= 2 else file_path
        node_counts[module] = node_counts.get(module, 0) + 1
    return [f"- `{module}`: {count} files, {node_counts.get(module, 0)} symbols" for module, count in sorted(counts.items())]


_VENDOR_PATH_PARTS = {"node_modules", "vendor", ".venv", "venv", "dist", "build", "__pycache__"}
_VENDOR_FILE_STEMS = {
    "papaparse", "lodash", "underscore", "jquery", "moment", "dayjs",
    "axios", "rxjs", "d3", "chart", "three", "pixi", "phaser",
}
_VENDOR_NODE_DENSITY_THRESHOLD = 120


def _is_project_source(file_path: str) -> bool:
    """Return True if file_path looks like project source code (not a vendored/third-party file)."""
    if not file_path:
        return False
    parts = file_path.replace("\\", "/").split("/")
    if any(part in _VENDOR_PATH_PARTS for part in parts):
        return False
    stem = Path(parts[-1]).stem.lower() if parts else ""
    stem_base = stem.removesuffix(".min")
    if stem_base in _VENDOR_FILE_STEMS:
        return False
    return True


def _is_vendored_file(file_path: str, nodes_by_file: dict[str, list[dict[str, Any]]]) -> bool:
    """Heuristic: a file with extremely high node density is likely a bundled/minified library."""
    if not _is_project_source(file_path):
        return True
    node_count = len(nodes_by_file.get(file_path, []))
    return node_count >= _VENDOR_NODE_DENSITY_THRESHOLD


def _global_logic_lines(
    edges: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    nodes_by_file: dict[str, list[dict[str, Any]]] | None = None,
    limit: int = 80,
) -> list[str]:
    if nodes_by_file is None:
        nodes_by_file = {}
    cross_file: list[str] = []
    intra_file: list[str] = []
    for edge in edges:
        if edge.get("kind") not in {"calls", "imports"}:
            continue
        source_node = node_by_id.get(str(edge.get("source") or ""), {})
        target_node = node_by_id.get(str(edge.get("target") or ""), {})
        source_file = str(source_node.get("filePath") or "")
        target_file = str(target_node.get("filePath") or "")
        if not _is_project_source(source_file) and not _is_project_source(target_file):
            continue
        if _is_vendored_file(source_file, nodes_by_file) or _is_vendored_file(target_file, nodes_by_file):
            continue
        source_name = str(source_node.get("name") or source_node.get("qualifiedName") or edge.get("source") or "unknown")
        target_name = str(target_node.get("name") or target_node.get("qualifiedName") or edge.get("target") or "unknown")
        line_num = edge.get("line")
        suffix = f" at line {line_num}" if line_num else ""
        files = f" ({source_file} → {target_file})" if source_file or target_file else ""
        entry = f"- `{source_name}` {edge.get('kind')} → `{target_name}`{suffix}{files}"
        if source_file and target_file and source_file != target_file:
            cross_file.append(entry)
        else:
            intra_file.append(entry)
        if len(cross_file) + len(intra_file) >= limit * 2:
            break
    lines = cross_file[:limit]
    remaining = limit - len(lines)
    if remaining > 0:
        lines.extend(intra_file[:remaining])
    if len(cross_file) + len(intra_file) > limit:
        lines.append(f"- ... truncated after {limit} relationships; see raw graph snapshot for full graph")
    return lines


def _pipeline_overview_lines(pipelines: list[Any]) -> list[str]:
    """Generate pipeline summary lines for the overview page."""
    lines: list[str] = []
    for p in pipelines:
        entry_str = ", ".join(p.entry_points[:3]) if p.entry_points else "none"
        lines.append(f"- **[[pipelines/{p.name}|{p.name}]]** ({len(p.files)} files, confidence: {p.confidence})")
        lines.append(f"  - Entry points: {entry_str}")
        if p.shared_records:
            lines.append(f"  - Shared records: {', '.join(p.shared_records[:5])}")
    return lines


def _pipeline_page(pipeline: Any, project: str, source_name: str, graph_snapshot: str) -> WikiPage:
    """Generate a wiki page for a single detected pipeline."""
    file_links = "\n".join(f"- [[{Path(f).stem}]] (`{f}`)" for f in pipeline.files)
    entry_lines = "\n".join(f"- `{e}`" for e in pipeline.entry_points) if pipeline.entry_points else "(none detected)"
    record_lines = "\n".join(f"- `{r}`" for r in pipeline.shared_records) if pipeline.shared_records else "(none)"
    implicit_lines: list[str] = []
    for src, target_id, ref_type in pipeline.implicit_edges[:20]:
        implicit_lines.append(f"- `{Path(src).stem}` → `{target_id}` ({ref_type})")
    implicit_section = "\n".join(implicit_lines) if implicit_lines else "(none detected)"
    return WikiPage(
        relative_path=Path("wiki") / "projects" / project / "code" / "pipelines" / f"{pipeline.name}.md",
        frontmatter={
            "type": "code_fact",
            "generated": True,
            "project": project,
            "source_name": source_name,
            "sources": [graph_snapshot],
            "pipeline": pipeline.name,
            "confidence": pipeline.confidence,
            "summary": f"Business pipeline: {pipeline.name} ({len(pipeline.files)} scripts)",
        },
        title=f"Pipeline: {pipeline.name}",
        body="\n".join([
            f"Confidence: {pipeline.confidence}",
            "",
            "## Scripts",
            file_links,
            "",
            "## Entry Points",
            entry_lines,
            "",
            "## Shared Records",
            record_lines,
            "",
            "## Implicit References",
            implicit_section,
        ]),
    )


def _call_graph_page(pipelines: list[Any], project: str, source_name: str, graph_snapshot: str) -> WikiPage:
    """Generate a call-graph summary page showing inter-pipeline relationships."""
    lines: list[str] = []
    all_implicit: list[str] = []
    for p in pipelines:
        lines.append(f"### {p.name}")
        lines.append(f"- Files: {len(p.files)}")
        lines.append(f"- Entry points: {len(p.entry_points)}")
        lines.append(f"- Shared records: {', '.join(p.shared_records[:5]) or 'none'}")
        lines.append("")
        for src, target_id, ref_type in p.implicit_edges[:10]:
            all_implicit.append(f"- `{Path(src).stem}` → `{target_id}` ({ref_type}) [pipeline: {p.name}]")
    implicit_section = "\n".join(all_implicit[:50]) if all_implicit else "(no implicit triggers detected)"
    return WikiPage(
        relative_path=Path("wiki") / "projects" / project / "code" / "call-graph.md",
        frontmatter={
            "type": "code_fact",
            "generated": True,
            "project": project,
            "source_name": source_name,
            "sources": [graph_snapshot],
            "summary": f"Cross-script call graph and implicit triggers for {project}",
        },
        title="Call Graph",
        body="\n".join([
            f"Total pipelines: {len(pipelines)}",
            f"Total implicit references: {sum(len(p.implicit_edges) for p in pipelines)}",
            "",
            "## Pipelines",
            *lines,
            "## Implicit Trigger Chain",
            implicit_section,
        ]),
    )


def _symbol_line(node: dict[str, Any]) -> str:
    name = str(node.get("name") or node.get("symbol") or node.get("qualifiedName") or "unknown")
    kind = str(node.get("kind") or "unknown")
    start = node.get("startLine") or node.get("line_start") or node.get("start_line") or "?"
    end = node.get("endLine") or node.get("line_end") or node.get("end_line") or "?"
    signature = str(node.get("signature") or "")
    return f"- `{name}` ({kind}, lines {start}-{end})" + (f": `{signature}`" if signature else "")


def _file_relationships(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], node_by_id: dict[str, dict[str, Any]], direction: str) -> list[str]:
    local_ids = {str(node.get("id")) for node in nodes if node.get("id")}
    lines: list[str] = []
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if direction == "outgoing" and source in local_ids:
            source_name = str(node_by_id.get(source, {}).get("name") or source)
            target_name = str(node_by_id.get(target, {}).get("name") or target)
            lines.append(f"- `{source_name}` {edge.get('kind', 'related')} → `{target_name}`" + (f" at line {edge.get('line')}" if edge.get("line") else ""))
        elif direction == "incoming" and target in local_ids:
            source_name = str(node_by_id.get(source, {}).get("name") or source)
            target_name = str(node_by_id.get(target, {}).get("name") or target)
            lines.append(f"- `{source_name}` {edge.get('kind', 'related')} → `{target_name}`" + (f" at line {edge.get('line')}" if edge.get("line") else ""))
    return lines


def _code_pages_from_context(data: dict[str, Any], project: str, source_name: str, context_snapshot: str) -> list[WikiPage]:
    nodes = _extract_nodes(data)
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    code_blocks = _extract_code_blocks(data)
    edges = _extract_edges(data)
    pages = []
    for index, node in enumerate(nodes):
        symbol = str(node.get("symbol") or node.get("name") or node.get("qualifiedName") or node.get("title") or f"node-{index + 1}")
        source_path = str(node.get("source_path") or node.get("filePath") or node.get("path") or node.get("file") or "")
        line_start = node.get("line_start") or node.get("startLine") or node.get("start_line") or ""
        line_end = node.get("line_end") or node.get("endLine") or node.get("end_line") or ""
        code_block = _match_code_block(node, code_blocks)
        source_code = str(code_block.get("content") or "") if code_block else ""
        signature = str(node.get("signature") or "")
        snippet = str(node.get("snippet") or node.get("code") or node.get("text") or signature or source_code)
        language = str(code_block.get("language") or node.get("language") or "") if code_block else str(node.get("language") or "")
        relationships = _relationships_for_node(node, edges, node_by_id)
        page_slug = slug(symbol)
        pages.append(
            WikiPage(
                relative_path=Path("wiki") / "projects" / project / "code" / f"{page_slug}.md",
                frontmatter={
                    "type": "code_fact",
                    "generated": True,
                    "project": project,
                    "source_name": source_name,
                    "sources": [context_snapshot],
                    "codegraph_tool": "context",
                    "source_path": source_path,
                    "symbol": symbol,
                    "line_start": line_start,
                    "line_end": line_end,
                    "summary": f"CodeGraph context for {symbol}",
                },
                title=symbol,
                body="\n".join(line for line in [
                    "## CodeGraph Fact",
                    f"- Symbol: `{symbol}`",
                    f"- Kind: `{node.get('kind', '')}`" if node.get("kind") else None,
                    f"- Source path: `{source_path}`" if source_path else "- Source path: 未识别",
                    f"- Lines: `{line_start}-{line_end}`" if line_start or line_end else "- Lines: 未识别",
                    "",
                    "## Signature" if node.get("signature") else "## Snippet",
                    f"```python",
                    snippet,
                    "```",
                    "",
                    "## Source Code",
                    f"```{language or 'text'}",
                    source_code,
                    "```",
                    "",
                    "## Relationships",
                    *(relationships or ["(no direct relationships in context)"]),
                ] if line is not None),
            )
        )
    if pages:
        return pages
    # Fallback: no structured nodes found; write a summary page with truncated content
    raw_json = json.dumps(data, ensure_ascii=False, indent=2)
    _MAX_FALLBACK_BODY = 4000
    if len(raw_json) > _MAX_FALLBACK_BODY:
        truncated_body = raw_json[:_MAX_FALLBACK_BODY] + "\n\n... (truncated, see raw snapshot)"
    else:
        truncated_body = raw_json
    return [
        WikiPage(
            relative_path=Path("wiki") / "projects" / project / "code" / "codegraph-context.md",
            frontmatter={
                "type": "code_fact",
                "generated": True,
                "project": project,
                "source_name": source_name,
                "sources": [context_snapshot],
                "codegraph_tool": "context",
                "summary": "CodeGraph context result (no structured nodes extracted)",
            },
            title="CodeGraph Context",
            body=f"No structured symbol nodes extracted from CodeGraph context.\n\nRaw snapshot: `{context_snapshot}`\n\n```json\n{truncated_body}\n```",
        )
    ]


def _extract_nodes(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("nodes", "results", "symbols", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in data for key in ("symbol", "name", "path", "file", "snippet")):
            return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _extract_code_blocks(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    value = data.get("codeBlocks") or data.get("code_blocks") or []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _extract_edges(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    value = data.get("edges") or data.get("relationships") or []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _match_code_block(node: dict[str, Any], code_blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    node_name = str(node.get("name") or node.get("symbol") or node.get("qualifiedName") or "")
    node_path = str(node.get("filePath") or node.get("source_path") or node.get("path") or node.get("file") or "")
    node_start = node.get("startLine") or node.get("line_start") or node.get("start_line")
    for block in code_blocks:
        block_name = str(block.get("nodeName") or block.get("name") or block.get("symbol") or "")
        block_path = str(block.get("filePath") or block.get("source_path") or block.get("path") or block.get("file") or "")
        block_start = block.get("startLine") or block.get("line_start") or block.get("start_line")
        if node_name and block_name == node_name and (not node_path or not block_path or block_path == node_path):
            return block
        if node_path and block_path == node_path and node_start and block_start == node_start:
            return block
    return None


def _relationships_for_node(node: dict[str, Any], edges: list[dict[str, Any]], node_by_id: dict[str, dict[str, Any]]) -> list[str]:
    node_id = str(node.get("id") or "")
    if not node_id:
        return []
    relationships: list[str] = []
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        kind = str(edge.get("kind") or "related")
        if source == node_id:
            target_node = node_by_id.get(target, {})
            target_name = str(target_node.get("name") or target_node.get("symbol") or target_node.get("qualifiedName") or target)
            line = edge.get("line")
            suffix = f" at line {line}" if line else ""
            relationships.append(f"- {kind} → `{target_name}`{suffix}")
        elif target == node_id:
            source_node = node_by_id.get(source, {})
            source_name = str(source_node.get("name") or source_node.get("symbol") or source_node.get("qualifiedName") or source)
            line = edge.get("line")
            suffix = f" at line {line}" if line else ""
            relationships.append(f"- `{source_name}` → {kind}{suffix}")
    return relationships


def _clean_code_pages(root: Path, project: str) -> None:
    """删除项目的所有旧 code 页面，确保同步时不会残留已删除源码的页面。"""
    code_dir = root / "wiki" / "projects" / project / "code"
    if not code_dir.is_dir():
        return
    for path in sorted(code_dir.rglob("*.md"), reverse=True):
        try:
            path.unlink()
        except OSError:
            pass
    # 清理因文件删除而残留的空目录
    for path in sorted(code_dir.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            try:
                path.rmdir()
            except OSError:
                pass
