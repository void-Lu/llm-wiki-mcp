from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from netsuite_rag_mcp.models import ARRAY_METADATA_FIELDS, SourceDocument

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# ── SuiteScript / JavaScript annotation patterns ──

NS_ANNOTATION_RE = re.compile(r"@NScriptType\s+(\S+)", re.IGNORECASE)
NS_API_VERSION_RE = re.compile(r"@NApiVersion\s+(\S+)", re.IGNORECASE)
NS_MODULE_SCOPE_RE = re.compile(r"@NModuleScope\s+(\S+)", re.IGNORECASE)

JSDOC_BLOCK_RE = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)
BLOCK_COMMENT_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)

DEFINE_DEPS_RE = re.compile(
    r"define\s*\(\s*\[([^\]]*)\]",
    re.DOTALL,
)

# ── Function boundary patterns ──

NAMED_FUNCTION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(",
    re.MULTILINE,
)
METHOD_DEF_RE = re.compile(
    r"^\s*(\w+)\s*:\s*function\s*\(",
    re.MULTILINE,
)
ARROW_FN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>",
    re.MULTILINE,
)

NETSUITE_ENTRY_POINTS: dict[str, set[str]] = {
    "Restlet": {"get", "post", "put", "delete", "doGet", "doPost", "doPut", "doDelete"},
    "UserEvent": {"beforeLoad", "beforeSubmit", "afterSubmit"},
    "MapReduce": {"getInputData", "map", "reduce", "summarize"},
    "Suitelet": {"onRequest"},
    "ClientScript": {
        "pageInit", "fieldChanged", "postSourcing", "sublistChanged",
        "lineInit", "validateField", "validateLine", "validateInsert",
        "validateDelete", "saveRecord",
    },
    "Scheduled": {"execute"},
    "Portlet": {"render"},
}

CODE_EXTENSIONS = {".js", ".ts"}
MD_EXTENSIONS = {".md"}
XML_EXTENSION = {".xml"}
JSON_EXTENSION = {".json"}


def parse_markdown_file(path: Path, vault_root: Path) -> SourceDocument:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    relative_path = path.resolve().relative_to(vault_root.resolve()).as_posix()
    doc_id = hashlib.sha1(relative_path.lower().encode("utf-8")).hexdigest()
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    return SourceDocument(
        doc_id=doc_id,
        source_path=relative_path,
        absolute_path=path,
        frontmatter=_normalize_frontmatter(frontmatter),
        body=body.strip(),
        updated_at=updated_at,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {"type": "unknown"}, text

    raw = yaml.safe_load(match.group(1))
    frontmatter = raw if isinstance(raw, dict) else {"type": "unknown"}
    body = text[match.end() :]
    return frontmatter, body


def _normalize_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(frontmatter)
    for key in ARRAY_METADATA_FIELDS:
        value = normalized.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            normalized[key] = [str(item).strip() for item in value if str(item).strip()]
        elif isinstance(value, str):
            normalized[key] = [value.strip()] if value.strip() else []
    return normalized


# ── SuiteScript / JavaScript / TypeScript parser ──


def parse_code_file(
    path: Path,
    source_name: str = "",
    repo_root: Path | None = None,
) -> SourceDocument | None:
    """Parse a .js/.ts code file into a SourceDocument with source_kind='code'."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None

    suffix = path.suffix.lower()
    language = "typescript" if suffix == ".ts" else "javascript"
    file_hash = hashlib.sha256(raw).hexdigest()
    frontmatter: dict[str, Any] = {"language": language, "file_hash": file_hash}

    # ── Extract @NScriptType / @NApiVersion / @NModuleScope ──
    ns_match = NS_ANNOTATION_RE.search(text)
    if ns_match:
        frontmatter["script_type"] = ns_match.group(1)

    api_match = NS_API_VERSION_RE.search(text)
    if api_match:
        frontmatter["api_version"] = api_match.group(1)

    scope_match = NS_MODULE_SCOPE_RE.search(text)
    if scope_match:
        frontmatter["module_scope"] = scope_match.group(1)

    # ── Extract define() dependencies ──
    deps_match = DEFINE_DEPS_RE.search(text)
    if deps_match:
        deps_raw = deps_match.group(1)
        frontmatter["dependencies"] = _parse_define_deps(deps_raw)

    # ── Extract description from first JSDoc or block comment ──
    frontmatter["description"] = _extract_description(text)

    # ── Detect function boundaries ──
    script_type = frontmatter.get("script_type", "")
    entry_points = NETSUITE_ENTRY_POINTS.get(script_type, set())
    frontmatter["functions"] = _detect_function_boundaries(text, entry_points)

    # ── Static analysis: related objects & scripts ──
    frontmatter["related_objects"] = _extract_related_objects(text)
    frontmatter["related_scripts"] = _extract_related_scripts(text)

    # ── Build doc_id, paths, timestamps ──
    vault_root = repo_root or path.parent
    relative_path = path.resolve().relative_to(vault_root.resolve()).as_posix()
    doc_id = hashlib.sha1(f"{source_name}:{relative_path}".lower().encode("utf-8")).hexdigest()
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    return SourceDocument(
        doc_id=doc_id,
        source_path=relative_path,
        absolute_path=path,
        frontmatter=frontmatter,
        body=text,
        updated_at=updated_at,
        source_kind="code",
        source_name=source_name,
        file_hash=file_hash,
        repo_root=str(vault_root),
        repo_relative_path=relative_path,
        language=language,
    )


def _parse_define_deps(raw: str) -> list[str]:
    """Parse a comma-separated list of quoted module paths from define([...])."""
    deps: list[str] = []
    for part in raw.split(","):
        stripped = part.strip().strip("'\"")
        if stripped:
            deps.append(stripped)
    return deps


def _extract_description(text: str) -> str:
    """Return the first sentence/line from the first JSDoc or block comment."""
    jsdoc_match = JSDOC_BLOCK_RE.search(text)
    if jsdoc_match:
        content = jsdoc_match.group(1).strip()
        lines = content.splitlines()
        # Collect non-annotation lines until we hit an @tag
        desc_lines: list[str] = []
        for line in lines:
            stripped = line.strip().lstrip("*").strip()
            if stripped.startswith("@") and not desc_lines:
                continue
            if stripped.startswith("@"):
                break
            if stripped:
                desc_lines.append(stripped)
        if desc_lines:
            return " ".join(desc_lines)

    block_match = BLOCK_COMMENT_RE.search(text)
    if block_match:
        content = block_match.group(1).strip()
        lines = content.splitlines()
        desc_lines = [l.strip() for l in lines if l.strip()]
        if desc_lines:
            return " ".join(desc_lines)

    return ""


def _detect_function_boundaries(
    text: str,
    entry_points: set[str],
) -> list[dict[str, Any]]:
    """Detect function boundaries and mark known NetSuite entry points."""
    lines = text.splitlines()
    functions: list[dict[str, Any]] = []

    # Gather all function start positions
    raw_matches: list[tuple[str, int]] = []

    for m in NAMED_FUNCTION_RE.finditer(text):
        raw_matches.append((m.group(1), _line_from_offset(text, m.start())))

    for m in METHOD_DEF_RE.finditer(text):
        raw_matches.append((m.group(1), _line_from_offset(text, m.start())))

    for m in ARROW_FN_RE.finditer(text):
        raw_matches.append((m.group(1), _line_from_offset(text, m.start())))

    # Sort by start line
    raw_matches.sort(key=lambda x: x[1])

    # Compute end lines: each function ends at the line before the next function
    for idx, (name, start_line) in enumerate(raw_matches):
        if idx + 1 < len(raw_matches):
            end_line = raw_matches[idx + 1][1] - 1
        else:
            end_line = len(lines)

        functions.append({
            "name": name,
            "start_line": start_line + 1,  # 1-based
            "end_line": end_line + 1,      # 1-based, inclusive
            "entry_point": name in entry_points,
        })

    return functions


def _line_from_offset(text: str, offset: int) -> int:
    """Return 0-based line number for a character offset in *text*."""
    return text[:offset].count("\n")


# ── Static analysis: related objects & scripts ──

# Patterns for record type references (custom records, lists, etc.)
_RECORD_TYPE_RE = re.compile(
    r"""(?:type\s*[:=]\s*|\.type\s*[:=]\s*)['"]?(customrecord_\w+|customlist_\w+|customsearch_\w+)['"]?""",
    re.IGNORECASE,
)
_RECORD_TYPE_STR_RE = re.compile(
    r"""['"](?:customrecord_\w+|customlist_\w+|customsearch_\w+)['"]""",
    re.IGNORECASE,
)
# search.load({id: 'customsearch_xxx'})
_SEARCH_LOAD_RE = re.compile(
    r"""search\.load\s*\(\s*\{[^}]*id\s*:\s*['"](\w+)['"]""",
    re.DOTALL,
)
# task.create with scriptId
_TASK_SCRIPT_RE = re.compile(
    r"""scriptId\s*[:=]\s*['"]?(customscript_\w+|customdeploy_\w+)['"]?""",
    re.IGNORECASE,
)
# url.resolveScript / url.resolveRecord with scriptId
_URL_SCRIPT_RE = re.compile(
    r"""(?:resolveScript|resolveRecord)\s*\(\s*\{[^}]*scriptId\s*:\s*['"](\w+)['"]""",
    re.DOTALL,
)
# Generic customscript_ / customdeploy_ references in string literals
_SCRIPT_REF_RE = re.compile(
    r"""['"](?:customscript_\w+|customdeploy_\w+)['"]""",
    re.IGNORECASE,
)


def _extract_related_objects(text: str) -> list[str]:
    """Extract custom record/list/search IDs referenced in code."""
    found: set[str] = set()
    for m in _RECORD_TYPE_RE.finditer(text):
        found.add(m.group(1).lower())
    for m in _RECORD_TYPE_STR_RE.finditer(text):
        found.add(m.group(0).strip("'\"").lower())
    for m in _SEARCH_LOAD_RE.finditer(text):
        val = m.group(1).lower()
        if val.startswith("custom"):
            found.add(val)
    return sorted(found)


def _extract_related_scripts(text: str) -> list[str]:
    """Extract customscript_/customdeploy_ IDs referenced in code."""
    found: set[str] = set()
    for m in _TASK_SCRIPT_RE.finditer(text):
        found.add(m.group(1).lower())
    for m in _URL_SCRIPT_RE.finditer(text):
        val = m.group(1).lower()
        if val.startswith("custom"):
            found.add(val)
    for m in _SCRIPT_REF_RE.finditer(text):
        found.add(m.group(0).strip("'\"").lower())
    return sorted(found)


# ── Unified file dispatcher ──


def parse_file(
    path: Path,
    vault_root: Path,
    source_name: str = "",
    repo_root: Path | None = None,
) -> SourceDocument | None:
    """Route to the correct parser based on file extension."""
    suffix = path.suffix.lower()

    if suffix in MD_EXTENSIONS:
        return parse_markdown_file(path, vault_root)

    if suffix in CODE_EXTENSIONS:
        effective_repo = repo_root or vault_root
        return parse_code_file(path, source_name=source_name, repo_root=effective_repo)

    if suffix in XML_EXTENSION:
        from netsuite_rag_mcp.parser_xml_json import parse_xml_file
        effective_repo = repo_root or vault_root
        return parse_xml_file(path, source_name=source_name, repo_root=effective_repo)

    if suffix in JSON_EXTENSION:
        from netsuite_rag_mcp.parser_xml_json import parse_json_config
        effective_repo = repo_root or vault_root
        return parse_json_config(path, source_name=source_name, repo_root=effective_repo)

    return None