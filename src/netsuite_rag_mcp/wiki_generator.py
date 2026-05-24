from __future__ import annotations

import fnmatch
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from netsuite_rag_mcp.models import SourceConfig, SourceDocument
from netsuite_rag_mcp.parser import parse_code_file
from netsuite_rag_mcp.parser_xml_json import parse_json_config, parse_xml_file
from netsuite_rag_mcp.redaction import redact_sensitive_text

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


def _matches_pattern(relative_path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    normalized = relative_path.replace("\\", "/").casefold()
    name = Path(relative_path).name.casefold()
    for pattern in patterns:
        pattern_text = pattern.replace("\\", "/").casefold()
        if fnmatch.fnmatch(normalized, pattern_text) or fnmatch.fnmatch(name, pattern_text):
            return True
    return False


def _is_utility_file(file_path: Path, source: SourceConfig) -> bool:
    relative_path = _source_relative_path(file_path, source)
    parts = Path(relative_path).parts
    if "tools" not in {part.casefold() for part in parts}:
        return False
    if relative_path in source.utility_allowlist:
        return True
    return file_path.suffix.lower() in CODE_EXTENSIONS


def _is_library_file(file_path: Path, source: SourceConfig) -> bool:
    relative_path = _source_relative_path(file_path, source)
    if relative_path in source.utility_allowlist:
        return False
    patterns = list(DEFAULT_LIBRARY_EXCLUDE_PATTERNS) + list(source.library_exclude_patterns)
    return _matches_pattern(relative_path, patterns)


def _should_exclude_by_component(file_path: Path, base_path: Path, exclude_names: set[str]) -> bool:
    try:
        relative = file_path.relative_to(base_path)
    except ValueError:
        return True
    return any(part in exclude_names for part in relative.parts)


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
