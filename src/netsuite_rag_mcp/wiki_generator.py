from __future__ import annotations

import fnmatch
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.indexer import index_sources as run_index_sources
from netsuite_rag_mcp.models import SourceConfig, SourceDocument
from netsuite_rag_mcp.parser import parse_code_file
from netsuite_rag_mcp.parser_xml_json import parse_json_config, parse_xml_file
from netsuite_rag_mcp.redaction import redact_sensitive_text
from netsuite_rag_mcp.runtime_config import RuntimeConfigError, resolve_runtime_config

DEFAULT_LIBRARY_EXCLUDE_PATTERNS = (
    "src/FileCabinet/SuiteScripts/tools/crypto-js.js",
    "src/FileCabinet/SuiteScripts/tools/moment.js",
    "src/FileCabinet/SuiteScripts/tools/papaparse.js",
    "src/FileCabinet/SuiteScripts/tools/ramda.min.js",
)

CODE_EXTENSIONS = {".js", ".ts"}
CONFIG_EXTENSIONS = {".xml", ".json"}


@dataclass(frozen=True)
class WikiPage:
    relative_path: Path
    frontmatter: dict[str, Any]
    title: str
    body: str


@dataclass(frozen=True)
class ParsedWikiSource:
    document: SourceDocument
    relative_path: str
    page_kind: str
    script_type: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    slug = value.strip()
    punctuation = re.escape(string.punctuation)
    slug = re.sub(rf"[\s{punctuation}]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    return slug[:80].rstrip("-") or "wiki-page"


def _source_relative_path(file_path: Path, source: SourceConfig) -> str:
    return file_path.resolve().relative_to(source.root.resolve()).as_posix()


def _try_source_relative_path(file_path: Path, source: SourceConfig) -> str | None:
    try:
        return _source_relative_path(file_path, source)
    except ValueError:
        return None


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _is_utility_allowlisted(relative_path: str, source: SourceConfig) -> bool:
    normalized = _normalized_path(relative_path)
    return normalized in {_normalized_path(item) for item in source.utility_allowlist}


def _matches_pattern(relative_path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    normalized = _normalized_path(relative_path)
    name = Path(relative_path).name.casefold()
    for pattern in patterns:
        pattern_text = _normalized_path(pattern)
        if fnmatch.fnmatch(normalized, pattern_text) or fnmatch.fnmatch(name, pattern_text):
            return True
    return False


def _is_utility_file(file_path: Path, source: SourceConfig) -> bool:
    relative_path = _try_source_relative_path(file_path, source)
    if relative_path is None:
        return False
    parts = Path(relative_path).parts
    if "tools" not in {part.casefold() for part in parts}:
        return False
    if _is_utility_allowlisted(relative_path, source):
        return True
    return file_path.suffix.lower() in CODE_EXTENSIONS


def _is_library_file(file_path: Path, source: SourceConfig) -> bool:
    relative_path = _try_source_relative_path(file_path, source)
    if relative_path is None:
        return True
    if _is_utility_allowlisted(relative_path, source):
        return False
    patterns = list(DEFAULT_LIBRARY_EXCLUDE_PATTERNS) + list(source.library_exclude_patterns)
    return _matches_pattern(relative_path, patterns)


def _should_exclude_by_component(file_path: Path, base_path: Path, exclude_names: set[str]) -> bool:
    try:
        relative = file_path.relative_to(base_path)
    except ValueError:
        return True
    normalized_exclude_names = {name.casefold() for name in exclude_names}
    return any(part.casefold() in normalized_exclude_names for part in relative.parts)


def _collect_wiki_source_files(source: SourceConfig) -> list[Path]:
    if not source.root.exists():
        return []

    include_dirs: list[Path] = []
    for include in source.include:
        include_path = source.root / include
        if include_path.exists():
            include_dirs.append(include_path)

    if not include_dirs:
        return []

    extensions = {f".{item.lstrip('.').lower()}" for item in source.file_types}
    exclude_names = set(source.exclude)
    collected: list[Path] = []

    for include_dir in include_dirs:
        for extension in extensions:
            for candidate in include_dir.rglob(f"*{extension}"):
                if _should_exclude_by_component(candidate, include_dir, exclude_names):
                    continue
                if _is_library_file(candidate, source):
                    continue
                collected.append(candidate)

    return sorted(set(collected))


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _script_type(document: SourceDocument, file_path: Path, source: SourceConfig) -> str:
    if _is_utility_file(file_path, source):
        return "utility"
    raw = str(document.frontmatter.get("script_type", "script")).strip()
    if not raw:
        return "script"
    return raw.replace("Script", "").replace(" ", "").lower()


def _parse_wiki_source(file_path: Path, source: SourceConfig) -> ParsedWikiSource | None:
    suffix = file_path.suffix.lower()
    if suffix in CODE_EXTENSIONS:
        document = parse_code_file(file_path, source_name=source.source_name, repo_root=source.root)
        if document is None:
            return None
        return ParsedWikiSource(
            document=document,
            relative_path=_source_relative_path(file_path, source),
            page_kind="script",
            script_type=_script_type(document, file_path, source),
        )
    if suffix == ".xml":
        document = parse_xml_file(file_path, source_name=source.source_name, repo_root=source.root)
        if document is None:
            return None
        return ParsedWikiSource(
            document=document,
            relative_path=_source_relative_path(file_path, source),
            page_kind="object",
            script_type="",
        )
    if suffix == ".json":
        document = parse_json_config(file_path, source_name=source.source_name, repo_root=source.root)
        if document is None:
            return None
        return ParsedWikiSource(
            document=document,
            relative_path=_source_relative_path(file_path, source),
            page_kind="object",
            script_type="",
        )
    return None


def _page_name(relative_path: str) -> str:
    path = Path(relative_path)
    return f"{_slug(path.stem)}.md"


def _base_frontmatter(project: str, source_name: str, generated_at: str) -> dict[str, Any]:
    return {
        "type": "generated_wiki",
        "project": project,
        "source_kind": "code",
        "generated": True,
        "generated_at": generated_at,
        "source_repo": source_name,
        "do_not_edit": True,
        "archived": False,
        "tags": ["netsuite", "generated-wiki", project],
    }


def _render_markdown(page: WikiPage) -> str:
    frontmatter = yaml.safe_dump(page.frontmatter, allow_unicode=True, sort_keys=False).strip()
    body = redact_sensitive_text(page.body).strip()
    return f"---\n{frontmatter}\n---\n\n# {page.title}\n\n{body}\n"


def _render_script_page(parsed: ParsedWikiSource, project: str, source_name: str, generated_at: str) -> WikiPage:
    doc = parsed.document
    fm = _base_frontmatter(project, source_name, generated_at)
    functions = doc.frontmatter.get("functions", [])
    dependencies = doc.frontmatter.get("dependencies", [])
    fm.update(
        {
            "source_path": parsed.relative_path,
            "script_type": parsed.script_type,
            "script_id": doc.frontmatter.get("script_id", ""),
            "deployment_id": doc.frontmatter.get("deployment_id", ""),
            "related_objects": [],
            "related_scripts": [],
            "confidence": "fact",
        }
    )
    rows = []
    for fn in functions:
        rows.append(
            f"| `{fn.get('name', '')}` | {fn.get('start_line', '')}-{fn.get('end_line', '')} | "
            f"{bool(fn.get('entry_point', False))} |"
        )
    function_table = "\n".join(rows) if rows else "| 未识别 |  | False |"
    dependency_text = "\n".join(f"- `{item}`" for item in dependencies) if dependencies else "- 无"
    code_fence = "```javascript" if doc.language == "javascript" else "```"
    body = "\n".join(
        [
            "## 代码事实",
            f"- 源码路径：`{parsed.relative_path}`",
            f"- 脚本类型：`{parsed.script_type}`",
            f"- 语言：`{doc.language}`",
            f"- 文件哈希：`{doc.file_hash}`",
            "",
            "## 依赖模块",
            dependency_text,
            "",
            "## 函数与入口",
            "| 函数 | 行号 | 是否入口 |",
            "| --- | --- | --- |",
            function_table,
            "",
            "## 证据",
            f"- `source_path={parsed.relative_path}`",
            "",
            "## 源码摘录",
            code_fence,
            doc.body,
            "```",
        ]
    )
    return WikiPage(
        Path("projects") / project / "wiki" / "scripts" / _page_name(parsed.relative_path),
        fm,
        Path(parsed.relative_path).name,
        body,
    )


def _render_object_page(parsed: ParsedWikiSource, project: str, source_name: str, generated_at: str) -> WikiPage:
    doc = parsed.document
    fm = _base_frontmatter(project, source_name, generated_at)
    object_type = str(doc.frontmatter.get("record_type", Path(parsed.relative_path).suffix.lstrip("."))).lower()
    related_scripts = [doc.frontmatter.get("script_id", "")] if doc.frontmatter.get("script_id") else []
    fm.update(
        {
            "source_path": parsed.relative_path,
            "object_type": object_type,
            "script_id": doc.frontmatter.get("script_id", ""),
            "deployment_id": doc.frontmatter.get("deployment_id", ""),
            "related_objects": [],
            "related_scripts": related_scripts,
            "confidence": "fact",
        }
    )
    body = "\n".join(
        [
            "## 配置事实",
            f"- 源码路径：`{parsed.relative_path}`",
            f"- Object 类型：`{object_type}`",
            f"- Script ID：`{doc.frontmatter.get('script_id', '')}`",
            f"- Deployment ID：`{doc.frontmatter.get('deployment_id', '')}`",
            f"- 名称：`{doc.frontmatter.get('name', '')}`",
            "",
            "## 证据",
            f"- `source_path={parsed.relative_path}`",
        ]
    )
    return WikiPage(
        Path("projects") / project / "wiki" / "objects" / _page_name(parsed.relative_path),
        fm,
        Path(parsed.relative_path).name,
        body,
    )


def _render_index_page(
    project: str,
    source_name: str,
    generated_at: str,
    scripts: list[ParsedWikiSource],
    objects: list[ParsedWikiSource],
) -> WikiPage:
    fm = _base_frontmatter(project, source_name, generated_at)
    fm["confidence"] = "fact"
    script_lines = "\n".join(
        f"- [[scripts/{_page_name(item.relative_path)}|{Path(item.relative_path).name}]]" for item in scripts
    ) or "- 无"
    object_lines = "\n".join(
        f"- [[objects/{_page_name(item.relative_path)}|{Path(item.relative_path).name}]]" for item in objects
    ) or "- 无"
    body = "\n".join(
        [
            "## 项目总览",
            f"- 项目：`{project}`",
            f"- 代码来源：`{source_name}`",
            f"- 脚本数量：`{len(scripts)}`",
            f"- 配置数量：`{len(objects)}`",
            "",
            "## 脚本清单",
            script_lines,
            "",
            "## Object/Deployment 清单",
            object_lines,
            "",
            "## 流程入口",
            "- [[flows/inferred-relationships.md|推测关联]]",
        ]
    )
    return WikiPage(Path("projects") / project / "wiki" / "index.md", fm, f"{project} Wiki", body)


def _render_flow_page(
    project: str,
    source_name: str,
    generated_at: str,
    scripts: list[ParsedWikiSource],
    objects: list[ParsedWikiSource],
) -> WikiPage:
    fm = _base_frontmatter(project, source_name, generated_at)
    fm["confidence"] = "inferred"
    rows = []
    for obj in objects:
        script_id = str(obj.document.frontmatter.get("script_id", ""))
        if not script_id:
            continue
        matched = [script for script in scripts if script_id and script_id in script.relative_path]
        target = Path(matched[0].relative_path).name if matched else "未定位脚本文件"
        rows.append(f"| `{script_id}` | `{Path(obj.relative_path).name}` | `{target}` | inferred |")
    relation_table = "\n".join(rows) if rows else "| 未识别 |  |  | unknown |"
    body = "\n".join(
        [
            "## 推测关联",
            "这些关系由配置 ID、文件名或路径命名推断，不代表业务设计原因。",
            "",
            "| Script ID | 配置文件 | 候选脚本 | 置信度 |",
            "| --- | --- | --- | --- |",
            relation_table,
        ]
    )
    return WikiPage(
        Path("projects") / project / "wiki" / "flows" / "inferred-relationships.md",
        fm,
        "推测关联",
        body,
    )


def generate_suitecloud_wiki(
    vault_root: str | Path,
    project: str,
    source_name: str,
    auto_index: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    try:
        runtime = resolve_runtime_config(vault_root_arg=vault_root, require_sources_config=True)
    except RuntimeConfigError as exc:
        return _error(exc.code, str(exc))

    config = load_config(runtime.vault_root, runtime_config=runtime)
    source = next((item for item in config.sources if item.source_name == source_name and item.source_kind == "code"), None)
    if source is None:
        return _error("missing_code_source", f"code source not found: {source_name}")
    if not source.root.exists():
        return _error("missing_source_root", f"source root does not exist: {source.root}")

    files = _collect_wiki_source_files(source)
    if not files:
        return _error("empty_source", f"source has no matching files: {source.source_name}")

    timestamp = generated_at or _utc_now()
    parsed_items: list[ParsedWikiSource] = []
    errors: list[dict[str, str]] = []
    for file_path in files:
        parsed = _parse_wiki_source(file_path, source)
        if parsed is None:
            errors.append({"file": _source_relative_path(file_path, source), "error": "parse_failed"})
            continue
        parsed_items.append(parsed)

    scripts = [item for item in parsed_items if item.page_kind == "script"]
    objects = [item for item in parsed_items if item.page_kind == "object"]
    pages: list[WikiPage] = [_render_index_page(project, source.source_name, timestamp, scripts, objects)]
    pages.extend(_render_script_page(item, project, source.source_name, timestamp) for item in scripts)
    pages.extend(_render_object_page(item, project, source.source_name, timestamp) for item in objects)
    pages.append(_render_flow_page(project, source.source_name, timestamp, scripts, objects))

    written_paths: list[str] = []
    for page in pages:
        target = runtime.vault_root / page.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_markdown(page), encoding="utf-8")
        written_paths.append(page.relative_path.as_posix())

    indexed: dict[str, Any] | None = None
    if auto_index:
        indexed = run_index_sources(runtime.vault_root, source_names=["obsidian"], mode="incremental", config=config)

    return {
        "ok": True,
        "project": project,
        "source_name": source.source_name,
        "written": len(written_paths),
        "archived": 0,
        "paths": written_paths,
        "errors": errors,
        "indexed": indexed,
    }
