"""Own Wiki path policy and the templates used to initialize a vault.

This module centralizes logical path validation, per-surface path-error
translation, and physical vault-escape checks.  ``DEFAULT_SCHEMA_TEXT``,
``DEFAULT_FILES``, and ``create_wiki_root`` remain here as the owner of the
vault initialization template.
"""

from __future__ import annotations

import os
import re
import string
from pathlib import Path

WINDOWS_RESERVED_CHARS = set('<>:"|?*')
WINDOWS_RESERVED_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"}
WINDOWS_RESERVED_DEVICE_PREFIXES = ("COM", "LPT")
WINDOWS_RESERVED_DEVICE_SUFFIXES = set("123456789¹²³")


def filesystem_path(path: str | Path) -> Path:
    """Return a Windows long-path-safe representation at filesystem boundaries.

    Vault source trees can legitimately exceed ``MAX_PATH`` because their raw
    provenance preserves the source hierarchy.  Keep relative logical paths in
    index/metadata, but use the extended-length form for OS access.
    """
    resolved = Path(path).expanduser().resolve()
    if os.name != "nt":
        return resolved
    value = str(resolved)
    if value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


ARCHIVES_DIR = Path("archives")
ARCHIVES_LOG_DIR = ARCHIVES_DIR / "log"
ARCHIVES_LOG_PATH = ARCHIVES_DIR / "log.md"

STATE_DB = Path(".llm-wiki/state.sqlite3")
PAGE_STATE_DB = Path(".llm-wiki/page-state.sqlite3")
KNOWLEDGE_DEPENDENCIES_DB = Path(".llm-wiki/knowledge-dependencies.sqlite3")
RETRIEVAL_DB_BY_SCOPE = {
    "active": Path(".llm-wiki/retrieval.sqlite3"),
    "archive": Path(".llm-wiki/archive-index.sqlite3"),
    "raw": Path(".llm-wiki/raw-retrieval.sqlite3"),
}
VECTOR_INDEX = Path(".llm-wiki/vector-index")
VECTOR_INDEX_BY_CORPUS = {
    "active": VECTOR_INDEX,
    "archive": VECTOR_INDEX.with_name("archive-vector-index"),
}
ADMIN_PLANS_DIR = Path(".llm-wiki/admin-plans")
PRIVACY_AUDIT_DIR = Path(".llm-wiki/privacy-audit")
MIGRATIONS_DIR = Path(".llm-wiki/migrations")
LOG_OPERATION_INDEX = Path(".llm-wiki/log-operation-index.json")
LEGACY_ARCHIVE_MARKER = Path(".llm-wiki/legacy-archive-migration-v2.json")


TOP_LEVEL_DIRS = (
    Path("raw/sources"),
    Path("raw/sources/projects"),
    Path("raw/sources/file"),
    Path("raw/sources/references"),
    Path("raw/sources/chat"),
    Path("raw/assets"),
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/entities"),
    Path("archives/bundles"),
    Path("archives/.staging"),
    Path("archives/.pending"),
    Path(".obsidian"),
    Path(".llm-wiki/ingest-cache"),
    Path(".llm-wiki/graph-index"),
    Path(".llm-wiki/relation-candidates"),
)


_WIKI_PAGE_PREFIXES = (
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/entities"),
)
_WIKI_FORBIDDEN_PARTS = {"objects"}
_WIKI_FORBIDDEN_PREFIXES = (
    Path("wiki/code"),
    Path("wiki/decisions"),
    Path("wiki/troubleshooting"),
    Path("wiki/requirements"),
    Path("wiki/knowledge"),
    Path("wiki/synthesis"),
    Path("wiki/comparisons"),
    Path("wiki/maintenance"),
    Path("projects"),
)
_WIKI_PROJECT_SUBDIRS = {
    "specs",
    "plans",
    "architecture",
    "pipelines",
    "troubleshooting",
    "researches",
}
_WIKI_RESERVED_STRUCTURE_PARTS = {
    "wiki",
    "projects",
    "concepts",
    "entities",
    "archives",
    *_WIKI_PROJECT_SUBDIRS,
}


DEFAULT_SCHEMA_TEXT = """# Schema

本文件是 LLM Wiki 的维护规约。Agent 在摄入资料、生成页面、回答问题、受控更新或执行归档维护前，应先遵守这里的目录、frontmatter、来源追踪和安全规则。

## LLM Wiki 维护原则

1. `raw/` 是来源事实层：保存 source snapshot、manifest 和项目原始资料；除 ingest/update 生命周期外，不把它当成普通可编辑笔记。
2. `wiki/` 是知识编译层：页面可以总结、关联、比较、综合；声明来源时必须通过 `sources` / `source_hashes` 追溯到具体 raw snapshot，不使用来源索引或 capsule。
3. `purpose.md` 描述当前 vault 的研究范围；`schema.md` 描述维护规则；`wiki/index.md` 是内容目录；`wiki/log.md` 是时间线。
4. 优先维护可读 Markdown、YAML frontmatter 和 `[[wikilink]]` 图谱；不要把 embedding/vector DB 作为主路径。
5. 生成内容要小步、可审计：raw snapshot 先落盘并建立 raw 索引；正式 Wiki 页面只通过显式 note/update 或人工 review 工作流产生。

## 页面类型与目录

| frontmatter `type` | 位置 | 说明 | generated |
| --- | --- | --- | --- |
| `spec` | `wiki/projects/<project>/specs/` | 规格文档 | `true` 或 `false` |
| `plan` | `wiki/projects/<project>/plans/` | 实施计划 | `true` 或 `false` |
| `architecture` | `wiki/projects/<project>/architecture/` | 长期稳定的项目架构说明 | `true` 或 `false` |
| `troubleshooting` | `wiki/projects/<project>/troubleshooting/` | 人工排障经验 | `false` |
| `researches` | `wiki/projects/<project>/researches/` | 项目调查结果、代码阅读结论、专题研究沉淀 | `true` 或 `false` |
| `concept` / `knowledge` | `wiki/concepts/<domain>/` | 领域知识、API 参考、场景实践 | `true` 或 `false` |
| `entity` | `wiki/entities/<entity>/` | 构建完毕的实体页面 | `true` 或 `false` |
| `archive` | `archives/bundles/<yyyy>/<mm>/<archive-id>/` | 过时、废弃或超限归档的 wiki 文档；不参与活动索引 | `true` 或 `false` |
| `index` | `wiki/index.md` | 内容目录，按类别列出页面和摘要 | `true` |
| `project_index` | `wiki/projects/<project>/index.md` | 项目内目录 | `true` |
| `overview` | `wiki/overview.md` | 自动统计和最近日志摘要 | `true` |

## Frontmatter 规范

所有 `wiki/**/*.md` 页面（`wiki/log.md` 除外）应包含 YAML frontmatter。推荐字段：

```yaml
---
type: concept
title: 页面标题
generated: true
project: project-a        # 项目页必填；通用 concept 可为空
domain: suitescript       # knowledge/concept 可选
source_name: docs         # 来源命名空间，可选
source_hash: sha256...    # source snapshot hash，可选
sources:
    - raw/sources/file/project-a/docs/source.md
summary: 一句话摘要
tags:
    - netsuite
    - suitescript
---
```

要求：

- `type`、`title`、`generated` 是核心字段。
- `generated: true` 页面应尽量包含 `sources`；如果是 `index` / `overview` 这类结构页，可不包含来源。
- `generated: false` 表示人工页，不允许工具静默覆盖。
- `sources` 中引用 `raw/...` 时，路径必须真实存在；删除或归档 source 时应通过 `wiki_archive`/CLI admin 生命周期处理。
- 新页面标题和摘要应能让 `wiki/index.md` 成为有效导航入口。

## 参考来源

Wiki 页面正文可以在末尾追加固定的 `## 参考来源` 段落，用
`related_pages=[{"path": "wiki/...md", "title": "..."}]` 记录本次采纳的其他
Wiki 页面；工具会校验目标存在于 `wiki/` 下并写成去掉 `.md` 扩展名的
`[[wikilink|标题]]`。raw 文档不写入该段落，而应通过 frontmatter 的
`sources`（仅允许存在的 `raw/sources/**` 文件）追踪；chat 历史继续使用
`chat_derived` + `chat_sources`，不混入 Wiki 链接或 raw `sources`。

## 写入与覆盖规则

1. Wiki 页面只能写入固定结构：`wiki/projects/<project>/{specs,plans,architecture,troubleshooting,researches}/`、`wiki/concepts/<domain>/`、`wiki/entities/<entity>/`。不可变归档内容只能由归档生命周期写入 `archives/bundles/<yyyy>/<mm>/<archive-id>/`。
2. 工具生成页只能覆盖已有 `generated: true` 页面；遇到 `generated: false` 必须停止并报告。
3. 受控更新时保留锁定字段：`type`、`title`、`created`、来源和人工维护字段；数组字段采用去重合并。
4. 文件名和路径段必须是 Windows 安全的单段名称：不得包含 `<>:"|?*`、控制字符、ADS 冒号、保留设备名、尾随点或空格。
5. 写入前必须脱敏手机号、邮箱、API key、token 等敏感信息。

## Ingest 工作流

### 单文件 ingest

```text
wiki_ingest
    ├─ UTF-8 Markdown/纯文本 -> raw/sources/<source_type>/<project>/<source_name>/<file>
    │                         -> RetrievalIndexStore 增量更新
    │                         -> raw provenance 失效标记与 raw index 更新
    └─ 其他原文件            -> raw/assets/<project>/<source_name>/<file>
                              -> 仅保存原文件/hash，不建立语义索引
```

`wiki_ingest` 只接受一个已存在文件；目录、批量摄入和 reconcile 不再属于 MCP 工具职责。
非文本文件是 `wiki_ingest` 的 asset 分流场景，不会进入 raw FTS；不额外注册 `wiki_store_asset` MCP 工具。

## Query、更新与归档规则

1. 回答问题时优先使用 `wiki_query` 获取带编号引用的 context pack，再基于 `[1]`、`[2]` 等引用回答。
2. `wiki_query` 默认搜索 `wiki/**`，必要时可启用 `include_raw_sources` 查看 raw snapshot。
3. 重要的比较、研究结论或跨页洞察，应显式保存为 concept/entity 或 `wiki/projects/<project>/researches/`。
4. 本地 Markdown 的宽泛检索可搭配 qmd 等外部工具，但不要把 qmd/embedding 设为本 MCP 的默认运行依赖。
5. 使用 `wiki_update(preview|apply)` 保持页面编辑可审计；使用 `wiki_archive`/`wiki_restore` 管理生命周期，purge 只在 CLI/admin 边界。

## 维护工作流

定期执行：

1. 用 `wiki_query` 检查知识覆盖和检索质量。
2. 用 `wiki_update` 修正过期、错误或锁定字段冲突的页面。
3. 用 `wiki_archive`/`wiki_restore` 管理归档生命周期。
4. 用 CLI `vector/index status|build|update` 与 `retrieval-eval` 验证索引和检索回归。

语义维护建议：

- 发现新资料与旧结论冲突时，在相关页面显式写出“差异/冲突/取舍”，不要静默覆盖旧结论。
- 发现高频概念但没有页面时，创建或建议创建 concept 页，并从相关页面补 wikilink。
- 发现陈旧 claim 时，优先追加来源和更新时间，再考虑合并或替换正文。

## 日志约定

`wiki/log.md` 是 append-only 时间线。每次 ingest、update、note、archive 或 restore 都应追加结构化条目，格式类似：

```text
## [2026-05-27T00:00:00Z] ingest | Source Title
- project: project-a
- status: ok
- paths:
    - raw/sources/file/project-a/docs/source.md
- sources:
    - raw/sources/file/project-a/docs/source.md
```

## 硬阻断

- 可能覆盖 `generated: false` 人工页。
- 页面路径逃逸 vault root 或落入未确认目录。
- 生成页缺少可追溯 raw 来源且不是结构页。
- 写入内容包含未脱敏敏感信息。
- 引入 Chroma、sentence-transformers、`.rag-index/`、`.models/` 或 embedding 主路径。
"""


DEFAULT_FILES = {
    Path("purpose.md"): "# Purpose\n\n描述这个知识库的目标、关键问题和研究范围。\n",
    Path("schema.md"): DEFAULT_SCHEMA_TEXT,
    Path("wiki/index.md"): "---\ntype: index\ngenerated: true\n---\n\n# Index\n\n",
    Path("wiki/log.md"): "# Log\n\n",
    Path("wiki/overview.md"): "---\ntype: overview\ngenerated: true\n---\n\n# Overview\n\n",
    Path("wiki/concepts/index.md"): "---\ntype: index\ngenerated: true\n---\n\n# Concepts\n\n",
    Path("wiki/entities/index.md"): "---\ntype: index\ngenerated: true\n---\n\n# Entities\n\n",
    ARCHIVES_LOG_PATH: "# Archives Log\n\n",
}


class WikiPathError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_SURFACE_CODE_MAPS: dict[str, dict[str, str]] = {
    "update": {
        "navigation_index_forbidden": "update_path_not_allowed",
        "invalid_wiki_path": "update_path_not_allowed",
    },
    "note_filename": {
        "invalid_path_component": "invalid_filename",
        "empty_segment": "invalid_filename",
    },
    "note_segment": {
        "invalid_path_component": "invalid_path_component",
        "empty_segment": "invalid_path_component",
    },
    "reference": {
        "invalid_wiki_path": "path_not_allowed",
        "navigation_index_forbidden": "path_not_allowed",
    },
    "io": {"navigation_index_forbidden": "invalid_wiki_path"},
    "ingest": {},
    "mutation": {},
    "provenance": {},
}


def translate_path_error(code: str, surface: str) -> str:
    """Translate a path code for one public surface.

    Unknown surfaces and codes pass through unchanged, as do surfaces with an
    explicitly empty mapping.
    """

    return _SURFACE_CODE_MAPS.get(surface, {}).get(code, code)


def resolve_within_root(root: Path, relative: Path) -> Path:
    """Resolve *relative* below *root* or raise ``path_escape``."""

    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise WikiPathError("path_escape", f"resolved path escapes vault root: {relative}")
    return path


def is_navigation_index_path(value: str | Path) -> bool:
    """Return whether *value* is an index owned by navigation projection."""

    path = _logical_path(value)
    parts = path.parts
    if path.name.casefold() != "index.md" or not parts or parts[0].casefold() != "wiki":
        return False
    if path == Path("wiki/index.md"):
        return True
    if len(parts) >= 3 and parts[1].casefold() in {"concepts", "entities"}:
        return True
    return len(parts) == 4 and parts[1].casefold() == "projects"


def validate_wiki_page_path(
    value: str | Path,
    *,
    allow_navigation_index: bool = False,
) -> Path:
    """Validate one vault-relative Wiki page path.

    This is the sole owner of page-prefix, path-segment, Windows-safety and
    generated-navigation-index policy. Callers translate ``WikiPathError``
    into their own stable public error envelope.
    """

    normalized = _logical_path(value)
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        raise WikiPathError("path_escape", "page path must stay inside wiki root")
    if normalized.suffix.casefold() != ".md":
        raise WikiPathError("invalid_wiki_path", "wiki page must be a markdown file")

    navigation_index = is_navigation_index_path(normalized)
    if navigation_index and not allow_navigation_index:
        raise WikiPathError(
            "navigation_index_forbidden",
            "generated navigation indexes are writable only by navigation or admin boundaries",
        )
    if any(_starts_with(normalized, prefix) for prefix in _WIKI_FORBIDDEN_PREFIXES):
        raise WikiPathError("invalid_wiki_path", f"page path is outside the confirmed wiki structure: {normalized.as_posix()}")
    if any(part.casefold() in _WIKI_FORBIDDEN_PARTS for part in normalized.parts):
        raise WikiPathError("invalid_wiki_path", f"objects directories are not part of the confirmed wiki structure: {normalized.as_posix()}")
    if not navigation_index and not any(_starts_with(normalized, prefix) for prefix in _WIKI_PAGE_PREFIXES):
        raise WikiPathError("invalid_wiki_path", f"page path is outside the confirmed wiki structure: {normalized.as_posix()}")
    if _starts_with(normalized, Path("wiki/projects")) and not _is_valid_project_path(normalized):
        raise WikiPathError("invalid_wiki_path", f"project page path is outside the confirmed project structure: {normalized.as_posix()}")

    for part in normalized.parts:
        if part.casefold() in _WIKI_RESERVED_STRUCTURE_PARTS:
            continue
        stem = Path(part).stem if part.casefold().endswith(".md") else part
        try:
            safe_segment(stem)
        except WikiPathError:
            raise
        except ValueError as exc:
            raise WikiPathError("invalid_path_component", str(exc)) from exc
    return normalized


def admin_wiki_page_file(
    vault_root: str | Path,
    value: str | Path,
    *,
    allow_missing: bool = False,
) -> Path:
    """Resolve one admin Wiki page while preserving only physical boundaries.

    This is intentionally different from :func:`validate_wiki_page_path`:
    admin maintenance may inspect or repair legacy/maintenance prefixes and
    generated navigation indexes, so it does not apply the ordinary writer's
    forbidden-prefix or navigation-index policy.  It still requires a
    vault-relative ``wiki/`` Markdown path, validates every Windows-safe path
    segment, and rejects symlink/logical escapes from ``vault_root``.
    """

    root = Path(vault_root).expanduser().resolve()
    normalized = _logical_path(value)
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        raise WikiPathError("path_escape", "admin page path must stay inside wiki root")
    if not normalized.parts or normalized.parts[0].casefold() != "wiki":
        raise WikiPathError("invalid_wiki_path", "admin page path must be under wiki/")
    if normalized.suffix.casefold() != ".md":
        raise WikiPathError("invalid_wiki_path", "admin page must be a markdown file")
    for part in normalized.parts[1:]:
        stem = Path(part).stem if part.casefold().endswith(".md") else part
        safe_segment(stem)
    candidate = resolve_within_root(root, normalized)
    if not allow_missing and not candidate.is_file():
        raise WikiPathError("page_not_found", "admin page does not exist")
    return candidate


def _logical_path(value: str | Path) -> Path:
    if isinstance(value, Path):
        return Path(*value.parts)
    return Path(str(value).replace("\\", "/"))


def _starts_with(path: Path, prefix: Path) -> bool:
    return path.parts[: len(prefix.parts)] == prefix.parts


def _is_valid_project_path(path: Path) -> bool:
    parts = path.parts
    if len(parts) == 4 and parts[3].casefold() == "index.md":
        return True
    return len(parts) >= 5 and parts[3] in _WIKI_PROJECT_SUBDIRS


def safe_segment(value: str) -> str:
    if not value:
        raise WikiPathError("empty_segment", "path segment is required")
    path = Path(value)
    if (
        "/" in value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) != 1
    ):
        raise WikiPathError("path_escape", "value must be a single safe path segment")
    if _has_windows_reserved_character(value) or value.endswith((".", " ")) or _is_windows_reserved_device_name(value):
        raise WikiPathError("invalid_path_component", "value contains a Windows-invalid path component")
    return value


def create_wiki_root(vault_root: str | Path) -> None:
    root = Path(vault_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative_dir in TOP_LEVEL_DIRS:
        (root / relative_dir).mkdir(parents=True, exist_ok=True)
    for relative_file, default_text in DEFAULT_FILES.items():
        target = root / relative_file
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(default_text, encoding="utf-8")


def slug(
    value: str,
    *,
    lowercase: bool = True,
    fallback: str = "page",
    ascii_punctuation: bool = False,
) -> str:
    """Return the canonical filename/wikilink slug.

    The default mode is used for new canonical references: Unicode word
    characters are preserved, separators collapse to ``-``, and the result
    is lower-case with a deterministic ``page`` fallback.  ``lowercase``,
    ``fallback`` and ``ascii_punctuation`` are explicit compatibility knobs
    for legacy note filenames; they reuse this owner instead of introducing
    another slug policy.
    """
    text = value.strip()
    if ascii_punctuation:
        punctuation = re.escape(string.punctuation)
        text = re.sub(rf"[\s{punctuation}]+", "-", text)
    else:
        text = re.sub(r"[\s\W]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-+", "-", text).strip("-")
    if lowercase:
        text = text.lower()
    return text[:80].rstrip("-") or fallback


def _has_windows_reserved_character(value: str) -> bool:
    return any(char in WINDOWS_RESERVED_CHARS or ord(char) < 32 for char in value)


def _is_windows_reserved_device_name(value: str) -> bool:
    base = value.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED_DEVICE_NAMES:
        return True
    return len(base) == 4 and base[:3] in WINDOWS_RESERVED_DEVICE_PREFIXES and base[3] in WINDOWS_RESERVED_DEVICE_SUFFIXES
