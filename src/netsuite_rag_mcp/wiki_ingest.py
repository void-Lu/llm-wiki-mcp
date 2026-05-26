from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from netsuite_rag_mcp.codegraph_client import CodeGraphClient
from netsuite_rag_mcp.redaction import redact_sensitive_text
from netsuite_rag_mcp.wiki_index import refresh_indexes
from netsuite_rag_mcp.wiki_io import write_wiki_page
from netsuite_rag_mcp.wiki_log import append_log_entry
from netsuite_rag_mcp.wiki_models import WikiLogEntry, WikiPage
from netsuite_rag_mcp.wiki_overview import refresh_overview
from netsuite_rag_mcp.wiki_paths import create_wiki_root, safe_segment, slug


_MAX_SOURCE_FILES = 200
_MAX_SOURCE_BYTES = 5_000_000
_ALLOWED_SOURCE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
_DENIED_SOURCE_NAMES = {".env", "credentials.json", "token.json", "secrets.json"}
_DENIED_SOURCE_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


class CodeGraphLike(Protocol):
    def status(self) -> dict[str, Any]: ...
    def files(self) -> dict[str, Any]: ...
    def context(self, query: str) -> dict[str, Any]: ...
    def impact(self, symbol: str) -> dict[str, Any]: ...


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

    if stage == "prepare_analysis":
        return _prepare_analysis(root, project_value, source_value, source_path, source_type_value, language)
    if stage == "prepare_generation":
        if analysis is None:
            return {"ok": False, "code": "missing_analysis", "error": "analysis is required for prepare_generation"}
        return _prepare_generation(root, project_value, source_value, language, analysis)
    if stage == "apply_generation":
        if generation is None:
            return {"ok": False, "code": "missing_generation", "error": "generation is required for apply_generation"}
        return _apply_generation(root, project_value, source_value, language, generation)
    return {"ok": False, "code": "unsupported_stage", "error": f"unsupported staged ingest stage: {stage}"}


def ingest_codegraph(
    vault_root: str | Path,
    project: str,
    source_name: str,
    query: str = "project code overview",
    codegraph_project_path: str | Path | None = None,
    client: CodeGraphLike | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)
    project_value = safe_segment(project)
    source_value = safe_segment(source_name)
    cg = client or CodeGraphClient(codegraph_project_path or Path.cwd())

    status = cg.status()
    if not status.get("ok"):
        return status
    files = cg.files()
    if not files.get("ok"):
        return files
    context = cg.context(query)
    if not context.get("ok"):
        return context

    snapshot_dir = root / "raw" / "sources" / "codegraph" / project_value / source_value
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshots = {
        "status.json": status.get("data", {}),
        "files.json": files.get("data", {}),
        "context.json": context.get("data", {}),
    }
    written_paths: list[str] = []
    for name, data in snapshots.items():
        target = snapshot_dir / name
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        written_paths.append(target.relative_to(root).as_posix())

    source_page_path = Path("wiki") / "sources" / f"codegraph-{project_value}-{source_value}.md"
    source_page = WikiPage(
        relative_path=source_page_path,
        frontmatter={
            "type": "source_summary",
            "generated": True,
            "project": project_value,
            "source_name": source_value,
            "sources": written_paths,
            "summary": f"CodeGraph snapshot for {project_value}/{source_value}",
        },
        title=f"CodeGraph {project_value}/{source_value}",
        body="\n".join([
            "## Source Snapshot",
            f"- Project: `{project_value}`",
            f"- Source: `{source_value}`",
            f"- Query: `{query}`",
            "- Snapshot: `raw/sources/codegraph/{}/{}/context.json`".format(project_value, source_value),
        ]),
    )
    write_wiki_page(root, source_page)
    written_paths.append(source_page_path.as_posix())

    code_pages = _code_pages_from_context(context.get("data", {}), project_value, source_value, written_paths[2])
    for page in code_pages:
        write_wiki_page(root, page)
        written_paths.append(page.relative_path.as_posix())
        symbol = str(page.frontmatter.get("symbol", ""))
        if symbol:
            impact = cg.impact(symbol)
            if impact.get("ok"):
                impact_path = snapshot_dir / f"impact-{slug(symbol)}.json"
                impact_path.write_text(json.dumps(impact.get("data", {}), ensure_ascii=False, indent=2), encoding="utf-8")
                written_paths.append(impact_path.relative_to(root).as_posix())

    refresh_indexes(root)
    refresh_overview(root)
    append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title=f"CodeGraph {project_value}/{source_value}",
            paths=written_paths,
            sources=[f"raw/sources/codegraph/{project_value}/{source_value}/context.json"],
            project=project_value,
            status="ok",
        ),
    )
    return {
        "ok": True,
        "project": project_value,
        "source_name": source_value,
        "written": len(written_paths),
        "paths": written_paths,
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
    source_root = Path(source_path).expanduser().resolve()
    if not source_root.exists():
        return {"ok": False, "code": "source_not_found", "error": f"source_path does not exist: {source_path}"}
    sources = _collect_sources(source_root)
    source_error = _source_limit_error(sources)
    if source_error is not None:
        return source_error
    source_hash = _sources_hash(sources, source_root)
    cache_path = _cache_path(root, project, source_name)
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
    _write_cache(root, project, source_name, {"source_hash": source_hash, "manifest": manifest, "status": "prepared"})
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
        "next_call": {"tool": "wiki_ingest", "stage": "prepare_generation", "required": ["analysis"]},
    }


def _prepare_generation(root: Path, project: str, source_name: str, language: str, analysis: dict[str, Any] | str) -> dict[str, Any]:
    cache = _read_cache(root, project, source_name)
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
        "next_call": {"tool": "wiki_ingest", "stage": "apply_generation", "required": ["generation"]},
    }


def _apply_generation(root: Path, project: str, source_name: str, language: str, generation: dict[str, Any] | str) -> dict[str, Any]:
    cache = _read_cache(root, project, source_name)
    if not cache:
        return {"ok": False, "code": "missing_prepared_source", "error": "run prepare_analysis before apply_generation"}
    payload = _generation_payload(generation)
    if payload is None:
        return {"ok": False, "code": "invalid_generation", "error": "generation must be a JSON object or JSON string"}
    manifest_sources = [item["path"] for item in cache.get("manifest", []) if isinstance(item, dict) and item.get("path")]
    written_paths: list[str] = []

    summary = payload.get("source_summary") if isinstance(payload.get("source_summary"), dict) else {}
    source_page = WikiPage(
        relative_path=Path("wiki") / "sources" / f"{project}-{source_name}.md",
        frontmatter={
            "type": "source_summary",
            "generated": True,
            "project": project,
            "source_name": source_name,
            "source_hash": cache.get("source_hash", ""),
            "language": language,
            "sources": manifest_sources,
            "summary": str(summary.get("summary") or f"Source summary for {project}/{source_name}"),
        },
        title=str(summary.get("title") or f"{project}/{source_name}"),
        body=str(summary.get("body") or summary.get("summary") or "资料摘要由 apply 阶段兜底生成。"),
    )
    write_wiki_page(root, source_page)
    written_paths.append(source_page.relative_path.as_posix())

    for item in payload.get("pages", []):
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
            body=str(item.get("body") or item.get("summary") or ""),
        )
        write_wiki_page(root, page)
        written_paths.append(path.as_posix())

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
    _write_cache(root, project, source_name, {**cache, "status": "applied", "written_paths": written_paths})
    return {"ok": True, "stage": "apply_generation", "project": project, "source_name": source_name, "written": len(written_paths), "paths": written_paths}


def _collect_sources(source_root: Path) -> list[Path]:
    if source_root.is_file():
        return [source_root] if _is_allowed_source_file(source_root) else []
    return [path for path in sorted(source_root.rglob("*")) if path.is_file() and _is_allowed_source_file(path)]


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


def _cache_path(root: Path, project: str, source_name: str) -> Path:
    return root / ".llm-wiki" / "ingest-cache" / project / f"{source_name}.json"


def _read_cache(root: Path, project: str, source_name: str) -> dict[str, Any]:
    path = _cache_path(root, project, source_name)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_cache(root: Path, project: str, source_name: str, data: dict[str, Any]) -> None:
    path = _cache_path(root, project, source_name)
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
        "Analysis:",
        json.dumps(analysis, ensure_ascii=False, indent=2) if not isinstance(analysis, str) else analysis,
        "Existing wiki context:",
        json.dumps(wiki_context, ensure_ascii=False, indent=2),
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


def _code_pages_from_context(data: dict[str, Any], project: str, source_name: str, context_snapshot: str) -> list[WikiPage]:
    nodes = _extract_nodes(data)
    pages = []
    for index, node in enumerate(nodes):
        symbol = str(node.get("symbol") or node.get("name") or node.get("title") or f"node-{index + 1}")
        source_path = str(node.get("source_path") or node.get("path") or node.get("file") or "")
        line_start = node.get("line_start") or node.get("start_line") or ""
        line_end = node.get("line_end") or node.get("end_line") or ""
        snippet = str(node.get("snippet") or node.get("code") or node.get("text") or "")
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
                body="\n".join([
                    "## CodeGraph Fact",
                    f"- Symbol: `{symbol}`",
                    f"- Source path: `{source_path}`" if source_path else "- Source path: 未识别",
                    f"- Lines: `{line_start}-{line_end}`" if line_start or line_end else "- Lines: 未识别",
                    "",
                    "## Snippet",
                    "```",
                    snippet,
                    "```",
                ]),
            )
        )
    if pages:
        return pages
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
                "summary": "CodeGraph context result",
            },
            title="CodeGraph Context",
            body=json.dumps(data, ensure_ascii=False, indent=2),
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
