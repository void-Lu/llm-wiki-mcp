from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WINDOWS_RESERVED_CHARS = set('<>:"|?*')
WINDOWS_RESERVED_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"}
WINDOWS_RESERVED_DEVICE_PREFIXES = ("COM", "LPT")
WINDOWS_RESERVED_DEVICE_SUFFIXES = set("123456789¹²³")

TOP_LEVEL_DIRS = (
    Path("raw/projects"),
    Path("raw/sources"),
    Path("raw/sources/file"),
    Path("raw/sources/references"),
    Path("raw/sources/chat"),
    Path("raw/assets"),
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/chatlog"),
    Path("wiki/sources"),
    Path("wiki/queries"),
    Path("wiki/comparisons"),
    Path("wiki/maintenance"),
    Path(".obsidian"),
    Path(".llm-wiki/ingest-cache"),
    Path(".llm-wiki/graph-index"),
    Path(".llm-wiki/relation-candidates"),
)


DEFAULT_SCHEMA_TEXT = """# Schema

本文件是 LLM Wiki 的维护规约。Agent 在摄入资料、生成页面、回答问题、合并页面或执行维护工具前，应先遵守这里的目录、frontmatter、来源追踪和安全规则。

## LLM Wiki 维护原则

1. `raw/` 是来源事实层：保存经过脱敏的 source snapshot、manifest、项目原始资料、会话原文或 CodeGraph 输出；除 rescan/delete 等生命周期工具外，不把它当成普通可编辑笔记。
2. `wiki/` 是知识编译层：页面可以总结、关联、比较、综合，但必须能通过 `sources` 字段追溯到 raw snapshot、外部搜索结果或人工 note。
3. `purpose.md` 描述当前 vault 的研究范围；`schema.md` 描述维护规则；`wiki/index.md` 是内容目录；`wiki/log.md` 是时间线。
4. 优先维护可读 Markdown、YAML frontmatter 和 `[[wikilink]]` 图谱；不要把 embedding/vector DB 作为主路径。
5. 生成内容要小步、可审计：先准备 prompt，再由调用方确认/传回 LLM 输出，最后 apply 写入。

## 页面类型与目录

| frontmatter `type` | 位置 | 说明 | generated |
| --- | --- | --- | --- |
| `source_index` | CodeGraph: `wiki/projects/<project>/sources/`；LLM: `wiki/sources/<target>/<project>/`；会话: `wiki/sources/chatlog/<yyyy>/<mm>/<dd>/` | 索引溯源页：frontmatter + 一句话摘要 + raw source 路径 + wikilinks，不承载知识内容 | `true` |
| `spec` | `wiki/projects/<project>/specs/` | 模型生成的规格文档 | `true` 或 `false` |
| `plan` | `wiki/projects/<project>/plans/` | 模型生成的实施计划 | `true` 或 `false` |
| `architecture` | `wiki/projects/<project>/architecture/` | 长期稳定的项目架构说明 | `true` 或 `false` |
| `pipeline` | `wiki/projects/<project>/pipelines/` | 业务流程、SuiteScript 调用链、数据流、任务链路 | `true` |
| `troubleshooting` | `wiki/projects/<project>/troubleshooting/` | 人工排障经验 | `false` |
| `researches` | `wiki/projects/<project>/researches/` | 项目调查结果、代码阅读结论、专题研究沉淀 | `true` 或 `false` |
| `chatlog` | `wiki/chatlog/<yyyy>/<mm>/<dd>/` | 会话摘要入口 | `true` |
| `concept` / `knowledge` | `wiki/concepts/<domain-or-project>/` | 领域知识、API 参考、场景实践 | `true` 或 `false` |
| `query` | `wiki/queries/` | 外部研究或一次问题综合后的归档页 | `true` |
| `comparison` | `wiki/comparisons/` | 方案、对象、实现路径的对比 | `true` 或 `false` |
| `maintenance` | `wiki/maintenance/` | 断链、孤儿页、重复页、候选关联、迁移审计等治理报告 | `true` |
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
    - raw/projects/project-a/codegraph/main/graph.json
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
- `sources` 中引用 `raw/...` 时，路径必须真实存在；删除 source 时应通过 `wiki_delete_source` 级联清理。
- 新页面标题和摘要应能让 `wiki/index.md` 成为有效导航入口。

## 写入与覆盖规则

1. 只能写入固定结构：`wiki/projects/<project>/{specs,plans,architecture,pipelines,troubleshooting,researches,sources}/`、`wiki/concepts/`、`wiki/chatlog/`、`wiki/sources/`、`wiki/queries/`、`wiki/comparisons/`、`wiki/maintenance/`。
2. 工具生成页只能覆盖已有 `generated: true` 页面；遇到 `generated: false` 必须停止并报告。
3. 页面合并时保留锁定字段：`type`、`title`、`created`、人工维护字段；数组字段采用去重合并。
4. 文件名和路径段必须是 Windows 安全的单段名称：不得包含 `<>:"|?*`、控制字符、ADS 冒号、保留设备名、尾随点或空格。
5. 写入前必须脱敏手机号、邮箱、API key、token 等敏感信息。

## Ingest 工作流

### CodeGraph ingest

```text
wiki_ingest_codegraph
    -> raw/projects/<project>/codegraph/<source_name>/
    -> wiki/projects/<project>/sources/<source_name>.md (索引页)
    -> wiki/projects/<project>/architecture/ 或 pipelines/ (可读总结页)
    -> refresh wiki/index.md + wiki/overview.md
    -> append wiki/log.md
```

### LLM staged ingest

```text
wiki_ingest_llm(stage="prepare")
    -> raw/sources/<source_type>/<project>/<source_name>/
    -> source_type="chat" 时，写 raw/sources/chat/<yyyy>/<mm>/<dd>/<source_name>/
    -> 返回合并 prompt
wiki_ingest_llm(stage="apply")
    -> wiki/concepts/ 或 wiki/projects/ 下的知识页面
    -> wiki/sources/<target_dir>/<project>/<source_name>.md (索引页)
    -> source_type="chat" 且生成 chatlog 时，写 wiki/sources/chatlog/<yyyy>/<mm>/<dd>/<source_name>.md (索引页)
    -> refresh index/overview/log/cache
```

## Query 与归档规则

1. 回答问题时优先使用 `wiki_query` 获取带编号引用的 context pack，再基于 `[1]`、`[2]` 等引用回答。
2. `wiki_query` 默认搜索 `wiki/**`，必要时可启用 `include_raw_sources` 查看 raw snapshot。
3. 重要的比较、研究结论或跨页洞察，不应只留在聊天记录里；应通过 `wiki_research`、`wiki_write_note` 或后续 research 工具归档到 `wiki/queries/` / `wiki/projects/<project>/researches/`。
4. 本地 Markdown 的宽泛检索可搭配 qmd 等外部工具，但不要把 qmd/embedding 设为本 MCP 的默认运行依赖。

## 维护工作流

定期执行：

1. `wiki_lint`：检查结构、frontmatter、断链、孤儿页、source traceability 和 cache manifest。
2. `wiki_enrich`：为页面补充指向已有页面的 `[[wikilink]]`。
3. `wiki_dedup`：检测并合并重复页面。
4. `wiki_insights`：查看孤儿、桥接节点、跨类型连接和社区结构。
5. `wiki_changelog`：查看最近 ingest/query/research/note 记录。

语义维护建议：

- 发现新资料与旧结论冲突时，在相关页面显式写出“差异/冲突/取舍”，不要静默覆盖旧结论。
- 发现高频概念但没有页面时，创建或建议创建 concept 页，并从相关页面补 wikilink。
- 发现陈旧 claim 时，优先追加来源和更新时间，再考虑合并或替换正文。

## 日志约定

`wiki/log.md` 是 append-only 时间线。每次 ingest、research、note、delete 或维护写入都应追加结构化条目，格式类似：

```text
## [2026-05-27T00:00:00Z] ingest | Source Title
- project: project-a
- status: ok
- paths:
    - wiki/sources/example.md
- sources:
    - raw/sources/file/project-a/docs/source.md
```

## 硬阻断

- 可能覆盖 `generated: false` 人工页。
- 页面路径逃逸 vault root 或落入未确认目录。
- 生成页缺少可追溯来源且不是结构页。
- 写入内容包含未脱敏敏感信息。
- 引入 Chroma、sentence-transformers、`.rag-index/`、`.models/` 或 embedding 主路径。
"""


DEFAULT_FILES = {
    Path("purpose.md"): "# Purpose\n\n描述这个知识库的目标、关键问题和研究范围。\n",
    Path("schema.md"): DEFAULT_SCHEMA_TEXT,
    Path("wiki/index.md"): "---\ntype: index\ngenerated: true\n---\n\n# Index\n\n",
    Path("wiki/log.md"): "# Log\n\n",
    Path("wiki/overview.md"): "---\ntype: overview\ngenerated: true\n---\n\n# Overview\n\n",
}


class WikiPathError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WikiPaths:
    root: Path

    def project_root(self, project: str) -> Path:
        return self.root / "wiki" / "projects" / safe_segment(project)

    def raw_project_root(self, project: str) -> Path:
        return self.root / "raw" / "projects" / safe_segment(project)

    def raw_project_requirements_dir(self, project: str) -> Path:
        return self.raw_project_root(project) / "requirements"

    def raw_project_codegraph_dir(self, project: str) -> Path:
        return self.raw_project_root(project) / "codegraph"

    def raw_project_chat_dir(self, project: str) -> Path:
        return self.raw_project_root(project) / "chat"

    def raw_project_assets_dir(self, project: str) -> Path:
        return self.raw_project_root(project) / "assets"

    def project_specs_dir(self, project: str) -> Path:
        return self.project_root(project) / "specs"

    def project_plans_dir(self, project: str) -> Path:
        return self.project_root(project) / "plans"

    def project_architecture_dir(self, project: str) -> Path:
        return self.project_root(project) / "architecture"

    def project_pipelines_dir(self, project: str) -> Path:
        return self.project_root(project) / "pipelines"

    def project_troubleshooting_dir(self, project: str) -> Path:
        return self.project_root(project) / "troubleshooting"

    def project_researches_dir(self, project: str) -> Path:
        return self.project_root(project) / "researches"

    def project_sources_dir(self, project: str) -> Path:
        return self.project_root(project) / "sources"

    def concepts_dir(self) -> Path:
        return self.root / "wiki" / "concepts"

    def chatlog_dir(self) -> Path:
        return self.root / "wiki" / "chatlog"

    def sources_dir(self) -> Path:
        return self.root / "wiki" / "sources"

    def queries_dir(self) -> Path:
        return self.root / "wiki" / "queries"

    def comparisons_dir(self) -> Path:
        return self.root / "wiki" / "comparisons"

    def maintenance_dir(self) -> Path:
        return self.root / "wiki" / "maintenance"


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


def create_wiki_root(vault_root: str | Path) -> WikiPaths:
    root = Path(vault_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative_dir in TOP_LEVEL_DIRS:
        (root / relative_dir).mkdir(parents=True, exist_ok=True)
    for relative_file, default_text in DEFAULT_FILES.items():
        target = root / relative_file
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(default_text, encoding="utf-8")
    return WikiPaths(root=root)


def slug(value: str) -> str:
    text = value.strip()
    text = re.sub(r"[\s\W]+", "-", text, flags=re.UNICODE).strip("-").lower()
    return text[:80].rstrip("-") or "page"


def _has_windows_reserved_character(value: str) -> bool:
    return any(char in WINDOWS_RESERVED_CHARS or ord(char) < 32 for char in value)


def _is_windows_reserved_device_name(value: str) -> bool:
    base = value.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED_DEVICE_NAMES:
        return True
    return len(base) == 4 and base[:3] in WINDOWS_RESERVED_DEVICE_PREFIXES and base[3] in WINDOWS_RESERVED_DEVICE_SUFFIXES
