from __future__ import annotations

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
from netsuite_rag_mcp.source_filters import (
    DEFAULT_FILE_EXCLUDE_PATTERNS,
    matches_path_pattern,
    normalized_path,
    should_exclude_by_component,
    should_exclude_by_file_pattern,
)

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


@dataclass(frozen=True)
class ScriptConfigLink:
    script_id: str
    deployment_ids: tuple[str, ...]
    script_parameters: tuple[str, ...]
    config_paths: tuple[str, ...]
    script_file: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_HEADER_MAX_LINES = 40


def _extract_header_excerpt(source_text: str) -> str:
    """Extract only the file header: leading comments + define()/require() imports.

    Returns at most ``_HEADER_MAX_LINES`` lines to keep wiki pages concise.
    The full source is already indexed separately by the RAG code source.
    """
    lines = source_text.splitlines()
    end = 0
    in_block_comment = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Track block comments
        if "/*" in stripped:
            in_block_comment = True
        if in_block_comment:
            end = i + 1
            if "*/" in stripped:
                in_block_comment = False
            continue
        # Single-line comments or blank lines at the top
        if stripped.startswith("//") or stripped.startswith("*") or stripped == "":
            end = i + 1
            continue
        # define() / require() opening (may span multiple lines)
        if re.match(r"^(define\s*\(|const\s+\w+\s*=\s*require|import\s)", stripped):
            # Capture until the opening callback body `=> {` or `function(`
            for j in range(i, min(len(lines), i + 30)):
                end = j + 1
                if re.search(r"(=>\s*\{|\)\s*\{)", lines[j]):
                    break
            break
        # If we hit actual code that isn't a comment/import, stop
        break

    # Ensure we don't exceed the max
    end = min(end, _HEADER_MAX_LINES)
    if end == 0:
        end = min(len(lines), _HEADER_MAX_LINES)

    excerpt = "\n".join(lines[:end])
    if end < len(lines):
        excerpt += "\n// ... (完整源码见源文件)"
    return excerpt


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
    return normalized_path(value)


def _is_utility_allowlisted(relative_path: str, source: SourceConfig) -> bool:
    normalized = _normalized_path(relative_path)
    return normalized in {_normalized_path(item) for item in source.utility_allowlist}


def _matches_pattern(relative_path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return matches_path_pattern(relative_path, patterns)


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
    return should_exclude_by_component(file_path, base_path, exclude_names)


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
                if should_exclude_by_file_pattern(
                    candidate,
                    source.root,
                    list(DEFAULT_FILE_EXCLUDE_PATTERNS) + list(source.file_exclude_patterns),
                ):
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


def _unique(items: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _bullet_list(items: list[str] | tuple[str, ...]) -> str:
    values = _unique(items)
    return "\n".join(f"- `{item}`" for item in values) if values else "- 无"


def _bullet_list_limited(items: list[str] | tuple[str, ...], limit: int = 20) -> str:
    values = _unique(items)
    if not values:
        return "- 无"
    shown = values[:limit]
    lines = [f"- `{item}`" for item in shown]
    if len(values) > limit:
        lines.append(f"- …（共 {len(values)} 个；完整列表见 frontmatter `deployment_ids`）")
    return "\n".join(lines)


def _inline_id_summary(items: list[str] | tuple[str, ...], limit: int = 5) -> str:
    values = _unique(items)
    if not values:
        return "未识别"
    shown = values[:limit]
    text = ", ".join(f"`{item}`" for item in shown)
    if len(values) > limit:
        text += f"，…（共 {len(values)} 个）"
    return text


def _table_or_empty(rows: list[str], empty_row: str) -> str:
    return "\n".join(rows) if rows else empty_row


def _dependency_category(dependency: str) -> str:
    normalized = dependency.replace("\\", "/").casefold()
    name = Path(normalized).name
    third_party_names = {"crypto-js.js", "moment.js", "papaparse.js", "ramda.min.js", "ramda.js"}
    if normalized.startswith("n/"):
        return "netsuite"
    if name in third_party_names:
        return "third_party"
    if "/tools/" in f"/{normalized}" or normalized.startswith("tools/"):
        return "utility"
    if normalized.startswith(".") or "suitescripts_" in normalized:
        return "internal"
    return "other"


def _render_dependencies(dependencies: list[str]) -> str:
    groups = {
        "netsuite": [],
        "utility": [],
        "internal": [],
        "third_party": [],
        "other": [],
    }
    for dependency in dependencies:
        groups[_dependency_category(str(dependency))].append(str(dependency))

    sections = [
        ("NetSuite 标准模块", groups["netsuite"]),
        ("项目公共工具", groups["utility"]),
        ("内部业务脚本", groups["internal"]),
        ("第三方库", groups["third_party"]),
        ("其他模块", groups["other"]),
    ]
    return "\n\n".join(f"### {title}\n{_bullet_list(items)}" for title, items in sections)


def _operation_rows(operations: list[dict[str, Any]], target_key: str) -> list[str]:
    rows: list[str] = []
    for operation in operations:
        rows.append(
            f"| `{operation.get('operation', '')}` | `{operation.get(target_key, '')}` | {operation.get('line', '')} |"
        )
    return rows


def _render_data_effects(record_operations: list[dict[str, Any]], search_operations: list[dict[str, Any]]) -> str:
    record_rows = _operation_rows(record_operations, "record_type")
    search_rows = _operation_rows(search_operations, "target")
    return "\n".join(
        [
            "## 数据与副作用",
            "### Record 操作",
            "| 操作 | 对象/类型 | 行号 |",
            "| --- | --- | --- |",
            _table_or_empty(record_rows, "| 未识别 |  |  |"),
            "",
            "### Search 操作",
            "| 操作 | 查询/对象 | 行号 |",
            "| --- | --- | --- |",
            _table_or_empty(search_rows, "| 未识别 |  |  |"),
        ]
    )


def _render_fields_and_parameters(script_parameters: list[str], field_ids: list[str]) -> str:
    return "\n".join(
        [
            "## 字段与参数",
            "### Script Parameters",
            _bullet_list(script_parameters),
            "",
            "### Field IDs",
            _bullet_list(field_ids),
        ]
    )


def _script_config_key(path: str) -> str:
    return _normalized_path(path)


def _merge_config_link(existing: ScriptConfigLink | None, parsed: ParsedWikiSource) -> ScriptConfigLink:
    fm = parsed.document.frontmatter
    script_file = str(fm.get("script_file", ""))
    script_id = str(fm.get("script_id", ""))
    deployment_ids = tuple(_unique(list(fm.get("deployment_ids", []))))
    script_parameters = tuple(_unique(list(fm.get("script_parameters", []))))
    config_paths = (parsed.relative_path,)
    if existing is None:
        return ScriptConfigLink(script_id, deployment_ids, script_parameters, config_paths, script_file)
    return ScriptConfigLink(
        existing.script_id or script_id,
        tuple(_unique(list(existing.deployment_ids) + list(deployment_ids))),
        tuple(_unique(list(existing.script_parameters) + list(script_parameters))),
        tuple(_unique(list(existing.config_paths) + list(config_paths))),
        existing.script_file or script_file,
    )


def _build_script_config_links(objects: list[ParsedWikiSource]) -> dict[str, ScriptConfigLink]:
    links: dict[str, ScriptConfigLink] = {}
    for obj in objects:
        script_file = str(obj.document.frontmatter.get("script_file", ""))
        if not script_file:
            continue
        key = _script_config_key(script_file)
        links[key] = _merge_config_link(links.get(key), obj)
    return links


def _render_config_link(config_link: ScriptConfigLink | None) -> str:
    if config_link is None:
        return "\n".join(["## 配置关联", "- Script ID：未识别", "- Deployment：未识别", "- Script Parameter：未识别", "- 配置文件：未识别"])
    return "\n".join(
        [
            "## 配置关联",
            f"- Script ID：`{config_link.script_id}`" if config_link.script_id else "- Script ID：未识别",
            f"- Script File：`{config_link.script_file}`" if config_link.script_file else "- Script File：未识别",
            "- Deployment：" + _inline_id_summary(config_link.deployment_ids),
            "- Script Parameter：" + _inline_id_summary(config_link.script_parameters),
            "- 配置文件：" + _inline_id_summary(config_link.config_paths),
        ]
    )


def _render_script_page(
    parsed: ParsedWikiSource,
    project: str,
    source_name: str,
    generated_at: str,
    config_link: ScriptConfigLink | None = None,
) -> WikiPage:
    doc = parsed.document
    fm = _base_frontmatter(project, source_name, generated_at)
    functions = doc.frontmatter.get("functions", [])
    dependencies = doc.frontmatter.get("dependencies", [])
    related_scripts = doc.frontmatter.get("related_scripts", [])
    related_deployments = doc.frontmatter.get("related_deployments", [])
    deployment_ids = list(config_link.deployment_ids) if config_link else []
    script_parameters = _unique(list(doc.frontmatter.get("script_parameters", [])) + (list(config_link.script_parameters) if config_link else []))
    field_ids = _unique(list(doc.frontmatter.get("field_ids", [])))
    record_operations = doc.frontmatter.get("record_operations", [])
    search_operations = doc.frontmatter.get("search_operations", [])
    script_id = str(doc.frontmatter.get("script_id", "") or (config_link.script_id if config_link else ""))
    fm.update(
        {
            "source_path": parsed.relative_path,
            "script_type": parsed.script_type,
            "script_id": script_id,
            "deployment_id": deployment_ids[0] if deployment_ids else doc.frontmatter.get("deployment_id", ""),
            "deployment_ids": deployment_ids,
            "related_objects": doc.frontmatter.get("related_objects", []),
            "related_scripts": related_scripts,
            "related_deployments": related_deployments,
            "script_parameters": script_parameters,
            "field_ids": field_ids,
            "config_paths": list(config_link.config_paths) if config_link else [],
            "confidence": "fact",
        }
    )
    rows = []
    for fn in functions:
        role = "NetSuite 入口" if bool(fn.get("entry_point", False)) else "普通函数"
        rows.append(
            f"| `{fn.get('name', '')}` | {fn.get('start_line', '')}-{fn.get('end_line', '')} | "
            f"{role} |"
        )
    function_table = "\n".join(rows) if rows else "| 未识别 |  | 普通函数 |"
    dependency_text = _render_dependencies(list(dependencies))
    related_objects = doc.frontmatter.get("related_objects", [])
    related_objects_text = _bullet_list(related_objects)
    related_scripts_text = _bullet_list(related_scripts)
    related_deployments_text = _bullet_list(related_deployments)
    deployment_ids_text = _bullet_list_limited(deployment_ids)
    code_fence = "```javascript" if doc.language == "javascript" else "```"
    header_excerpt = _extract_header_excerpt(doc.body)
    evidence_lines = [f"- `source_path={parsed.relative_path}`"]
    if config_link:
        evidence_lines.extend(f"- `config_path={item}`" for item in config_link.config_paths)
    evidence_lines.extend(
        f"- `{operation.get('operation', '')} line={operation.get('line', '')} target={operation.get('record_type', '')}`"
        for operation in record_operations
    )
    evidence_lines.extend(
        f"- `{operation.get('operation', '')} line={operation.get('line', '')} target={operation.get('target', '')}`"
        for operation in search_operations
    )

    body = "\n".join(
        [
            "## 代码事实",
            f"- 源码路径：`{parsed.relative_path}`",
            f"- 脚本类型：`{parsed.script_type}`",
            f"- 语言：`{doc.language}`",
            f"- 文件哈希：`{doc.file_hash}`",
            "",
            _render_config_link(config_link),
            "",
            "## 依赖模块",
            dependency_text,
            "",
            "## 关联对象",
            related_objects_text,
            "",
            "## 关联脚本",
            related_scripts_text,
            "",
            "## 关联部署",
            related_deployments_text,
            "",
            "## 本脚本部署",
            deployment_ids_text,
            "",
            "## 函数与入口",
            "| 函数 | 行号 | 角色 |",
            "| --- | --- | --- |",
            function_table,
            "",
            _render_data_effects(record_operations, search_operations),
            "",
            _render_fields_and_parameters(script_parameters, field_ids),
            "",
            "## 证据",
            "\n".join(evidence_lines),
            "",
            "## 源码摘录",
            code_fence,
            header_excerpt,
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
    deployment_ids = list(doc.frontmatter.get("deployment_ids", []))
    script_parameters = list(doc.frontmatter.get("script_parameters", []))
    script_file = str(doc.frontmatter.get("script_file", ""))
    fm.update(
        {
            "source_path": parsed.relative_path,
            "object_type": object_type,
            "script_id": doc.frontmatter.get("script_id", ""),
            "deployment_id": doc.frontmatter.get("deployment_id", ""),
            "deployment_ids": deployment_ids,
            "script_parameters": script_parameters,
            "script_file": script_file,
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
            f"- Script File：`{script_file}`",
            f"- 名称：`{doc.frontmatter.get('name', '')}`",
            "",
            "## Deployment 列表",
            _bullet_list_limited(deployment_ids),
            "",
            "## Script Parameters",
            _bullet_list(script_parameters),
            "",
            "## 证据",
            f"- `source_path={parsed.relative_path}`",
            f"- `script_file={script_file}`" if script_file else "- `script_file=未识别`",
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
    script_lines_by_type = _render_script_index_by_type(scripts)
    script_lines_by_dir = _render_script_index_by_directory(scripts)
    object_lines = _render_object_index(objects)
    body = "\n".join(
        [
            "## 项目总览",
            f"- 项目：`{project}`",
            f"- 代码来源：`{source_name}`",
            f"- 脚本数量：`{len(scripts)}`",
            f"- 配置数量：`{len(objects)}`",
            "",
            "## 脚本清单（按类型）",
            script_lines_by_type,
            "",
            "## 脚本清单（按目录）",
            script_lines_by_dir,
            "",
            "## Object/Deployment 清单",
            object_lines,
            "",
            "## 流程入口",
            "- [[flows/inferred-relationships.md|推测关联]]",
        ]
    )
    return WikiPage(Path("projects") / project / "wiki" / "index.md", fm, f"{project} Wiki", body)


def _script_link(item: ParsedWikiSource) -> str:
    return f"[[scripts/{_page_name(item.relative_path)}|{Path(item.relative_path).name}]]"


def _object_link(item: ParsedWikiSource) -> str:
    return f"[[objects/{_page_name(item.relative_path)}|{Path(item.relative_path).name}]]"


def _render_script_index_by_type(scripts: list[ParsedWikiSource]) -> str:
    if not scripts:
        return "- 无"
    grouped: dict[str, list[ParsedWikiSource]] = {}
    for script in scripts:
        grouped.setdefault(script.script_type or "script", []).append(script)
    sections: list[str] = []
    for script_type in sorted(grouped):
        lines = "\n".join(f"- {_script_link(item)}" for item in sorted(grouped[script_type], key=lambda x: x.relative_path))
        sections.append(f"### {script_type}\n{lines}")
    return "\n\n".join(sections)


def _render_script_index_by_directory(scripts: list[ParsedWikiSource]) -> str:
    if not scripts:
        return "- 无"
    grouped: dict[str, list[ParsedWikiSource]] = {}
    for script in scripts:
        parts = Path(script.relative_path).parts
        directory = parts[-2] if len(parts) >= 2 else "."
        grouped.setdefault(directory, []).append(script)
    sections: list[str] = []
    for directory in sorted(grouped):
        lines = "\n".join(f"- {_script_link(item)}" for item in sorted(grouped[directory], key=lambda x: x.relative_path))
        sections.append(f"### {directory}\n{lines}")
    return "\n\n".join(sections)


def _render_object_index(objects: list[ParsedWikiSource]) -> str:
    if not objects:
        return "- 无"
    grouped: dict[str, list[ParsedWikiSource]] = {}
    for obj in objects:
        object_type = str(obj.document.frontmatter.get("record_type", Path(obj.relative_path).suffix.lstrip("."))).lower()
        grouped.setdefault(object_type, []).append(obj)
    sections: list[str] = []
    for object_type in sorted(grouped):
        lines = "\n".join(f"- {_object_link(item)}" for item in sorted(grouped[object_type], key=lambda x: x.relative_path))
        sections.append(f"### {object_type}\n{lines}")
    return "\n\n".join(sections)


def _render_flow_page(
    project: str,
    source_name: str,
    generated_at: str,
    scripts: list[ParsedWikiSource],
    objects: list[ParsedWikiSource],
) -> WikiPage:
    fm = _base_frontmatter(project, source_name, generated_at)
    fm["confidence"] = "inferred"
    config_rows = []
    script_by_path = {_script_config_key(script.relative_path): script for script in scripts}
    object_by_script_id: dict[str, ParsedWikiSource] = {}
    object_by_deployment_id: dict[str, ParsedWikiSource] = {}
    for obj in objects:
        script_id = str(obj.document.frontmatter.get("script_id", ""))
        if script_id:
            object_by_script_id[script_id] = obj
        for deployment_id in obj.document.frontmatter.get("deployment_ids", []):
            object_by_deployment_id[str(deployment_id)] = obj
        script_file = str(obj.document.frontmatter.get("script_file", ""))
        if not script_file:
            continue
        matched = script_by_path.get(_script_config_key(script_file))
        target = Path(matched.relative_path).name if matched else "未定位脚本文件"
        deployment_text = _inline_id_summary(list(obj.document.frontmatter.get("deployment_ids", [])))
        config_rows.append(
            f"| `{Path(obj.relative_path).name}` | `{script_id}` | {deployment_text} | `{script_file}` | `{target}` | fact |"
        )

    call_rows = []
    for script in scripts:
        for script_id in script.document.frontmatter.get("related_scripts", []):
            obj = object_by_script_id.get(str(script_id))
            target = Path(obj.relative_path).name if obj else "未定位配置"
            call_rows.append(f"| `{Path(script.relative_path).name}` | script | `{script_id}` | `{target}` | inferred |")
        for deployment_id in script.document.frontmatter.get("related_deployments", []):
            obj = object_by_deployment_id.get(str(deployment_id))
            target = Path(obj.relative_path).name if obj else "未定位配置"
            call_rows.append(f"| `{Path(script.relative_path).name}` | deployment | `{deployment_id}` | `{target}` | inferred |")

    config_table = _table_or_empty(config_rows, "| 未识别 |  |  |  |  | unknown |")
    call_table = _table_or_empty(call_rows, "| 未识别 |  |  |  | unknown |")
    body = "\n".join(
        [
            "## 推测关联",
            "这些关系由配置 ID、文件名或路径命名推断，不代表业务设计原因。",
            "",
            "## 配置到脚本",
            "| 配置文件 | Script ID | Deployment | Script File | 候选脚本 | 置信度 |",
            "| --- | --- | --- | --- | --- | --- |",
            config_table,
            "",
            "## 代码调用关系",
            "| 来源脚本 | 引用类型 | 引用 ID | 候选配置 | 置信度 |",
            "| --- | --- | --- | --- | --- |",
            call_table,
        ]
    )
    return WikiPage(
        Path("projects") / project / "wiki" / "flows" / "inferred-relationships.md",
        fm,
        "推测关联",
        body,
    )


def _read_frontmatter(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
    except StopIteration:
        return {}
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    return loaded if isinstance(loaded, dict) else {}


def _is_generated_wiki_page(path: Path) -> bool:
    fm = _read_frontmatter(path)
    return fm.get("type") == "generated_wiki" and fm.get("generated") is True


def _ensure_can_write_pages(vault_root: Path, pages: list[WikiPage]) -> dict[str, Any] | None:
    for page in pages:
        target = vault_root / page.relative_path
        if target.exists() and not _is_generated_wiki_page(target):
            return _error(
                "manual_wiki_page_exists",
                f"refusing to overwrite non-generated wiki page: {page.relative_path.as_posix()}",
            )
    return None


def _unique_archive_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    index = 1
    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _archive_removed_script_pages(
    vault_root: Path,
    project: str,
    active_script_paths: set[str],
    archived_at: str,
) -> list[str]:
    scripts_dir = vault_root / "projects" / project / "wiki" / "scripts"
    if not scripts_dir.exists():
        return []

    archived_paths: list[str] = []
    for existing in sorted(scripts_dir.glob("*.md")):
        relative_existing = existing.relative_to(vault_root).as_posix()
        if relative_existing in active_script_paths:
            continue
        if not _is_generated_wiki_page(existing):
            continue
        fm = _read_frontmatter(existing)
        original_text = existing.read_text(encoding="utf-8")
        updated_frontmatter = dict(fm)
        updated_frontmatter["archived"] = True
        updated_frontmatter["archived_at"] = archived_at
        updated_frontmatter["archived_reason"] = "source_removed"
        updated_frontmatter["former_source_path"] = fm.get("source_path", "")
        yaml_text = yaml.safe_dump(updated_frontmatter, allow_unicode=True, sort_keys=False).strip()
        body = original_text.split("---", 2)[2].lstrip() if original_text.startswith("---") else original_text
        archive_target = _unique_archive_path(
            vault_root / "projects" / project / "wiki" / "archive" / "scripts" / existing.name
        )
        archive_target.parent.mkdir(parents=True, exist_ok=True)
        archive_target.write_text(f"---\n{yaml_text}\n---\n\n{body}", encoding="utf-8")
        existing.unlink()
        archived_paths.append(archive_target.relative_to(vault_root).as_posix())
    return archived_paths


def generate_suitecloud_wiki(
    vault_root: str | Path,
    project: str,
    source_name: str,
    auto_index: bool = True,
    generated_at: str | None = None,
    llm_summary: bool = False,
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
    config_links = _build_script_config_links(objects)
    pages: list[WikiPage] = [_render_index_page(project, source.source_name, timestamp, scripts, objects)]
    pages.extend(
        _render_script_page(
            item,
            project,
            source.source_name,
            timestamp,
            config_links.get(_script_config_key(item.relative_path)),
        )
        for item in scripts
    )
    pages.extend(_render_object_page(item, project, source.source_name, timestamp) for item in objects)
    pages.append(_render_flow_page(project, source.source_name, timestamp, scripts, objects))

    write_error = _ensure_can_write_pages(runtime.vault_root, pages)
    if write_error is not None:
        return write_error

    active_script_paths = {
        page.relative_path.as_posix()
        for page in pages
        if page.relative_path.parent.name == "scripts"
    }
    archived_paths = _archive_removed_script_pages(runtime.vault_root, project, active_script_paths, timestamp)

    written_paths: list[str] = []
    for page in pages:
        target = runtime.vault_root / page.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_markdown(page), encoding="utf-8")
        written_paths.append(page.relative_path.as_posix())

    indexed: dict[str, Any] | None = None
    if auto_index:
        indexed = run_index_sources(runtime.vault_root, source_names=["obsidian"], mode="incremental", config=config)

    result: dict[str, Any] = {
        "ok": True,
        "project": project,
        "source_name": source.source_name,
        "written": len(written_paths),
        "archived": len(archived_paths),
        "archived_paths": archived_paths,
        "paths": written_paths,
        "errors": errors,
        "indexed": indexed,
    }

    if llm_summary:
        result["summary_prompts"] = _build_summary_prompts(scripts, project)

    return result


def _build_summary_prompts(scripts: list[ParsedWikiSource], project: str) -> list[dict[str, Any]]:
    """Build per-script metadata prompts for the calling model to generate summaries."""
    prompts: list[dict[str, Any]] = []
    for parsed in scripts:
        doc = parsed.document
        functions = doc.frontmatter.get("functions", [])
        dependencies = doc.frontmatter.get("dependencies", [])
        related_objects = doc.frontmatter.get("related_objects", [])
        func_names = [f.get("name", "") for f in functions]
        header_excerpt = _extract_header_excerpt(doc.body)
        wiki_path = (Path("projects") / project / "wiki" / "scripts" / _page_name(parsed.relative_path)).as_posix()

        prompts.append({
            "wiki_path": wiki_path,
            "script_name": Path(parsed.relative_path).name,
            "script_type": parsed.script_type,
            "dependencies": dependencies,
            "functions": func_names,
            "related_objects": related_objects,
            "header_excerpt": header_excerpt,
        })
    return prompts


def write_wiki_summaries(
    vault_root: str | Path,
    project: str,
    summaries: list[dict[str, str]],
) -> dict[str, Any]:
    """Write LLM-generated business summaries into existing wiki pages.

    Each item in summaries must have:
        - wiki_path: relative path from vault root (e.g. projects/huideng/wiki/scripts/xxx.md)
        - summary: the generated business summary text
    """
    vault_root = Path(vault_root)
    written = 0
    errors: list[dict[str, str]] = []

    for item in summaries:
        wiki_path = item.get("wiki_path", "")
        summary = item.get("summary", "")
        if not wiki_path or not summary:
            errors.append({"wiki_path": wiki_path, "error": "missing wiki_path or summary"})
            continue

        target = vault_root / wiki_path
        if not target.exists():
            errors.append({"wiki_path": wiki_path, "error": "wiki page not found"})
            continue

        try:
            content = target.read_text(encoding="utf-8")
            # Insert summary section after "## 代码事实" block (after the blank line following file_hash)
            marker = "## 依赖模块"
            summary_section = f"## 业务语义摘要\n{summary}\n\n"
            if "## 业务语义摘要" in content:
                # Replace existing summary
                content = re.sub(
                    r"## 业务语义摘要\n.*?\n\n(?=## )",
                    summary_section,
                    content,
                    count=1,
                    flags=re.DOTALL,
                )
            elif marker in content:
                content = content.replace(marker, summary_section + marker, 1)
            else:
                errors.append({"wiki_path": wiki_path, "error": "cannot locate insertion point"})
                continue

            target.write_text(content, encoding="utf-8")
            written += 1
        except Exception as e:
            errors.append({"wiki_path": wiki_path, "error": str(e)})

    return {
        "ok": len(errors) == 0,
        "written": written,
        "errors": errors,
    }

