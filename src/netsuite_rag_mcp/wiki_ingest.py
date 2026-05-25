from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from netsuite_rag_mcp.codegraph_client import CodeGraphClient
from netsuite_rag_mcp.wiki_index import refresh_indexes
from netsuite_rag_mcp.wiki_io import write_wiki_page
from netsuite_rag_mcp.wiki_log import append_log_entry
from netsuite_rag_mcp.wiki_models import WikiLogEntry, WikiPage
from netsuite_rag_mcp.wiki_overview import refresh_overview
from netsuite_rag_mcp.wiki_paths import create_wiki_root, safe_segment, slug


class CodeGraphLike(Protocol):
    def status(self) -> dict[str, Any]: ...
    def files(self) -> dict[str, Any]: ...
    def context(self, query: str) -> dict[str, Any]: ...
    def impact(self, symbol: str) -> dict[str, Any]: ...


def ingest_source(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"ok": False, "code": "unsupported_source_type", "error": "only codegraph ingest is implemented"}


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
