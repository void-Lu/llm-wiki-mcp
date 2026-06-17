# Wiki Vault 目录结构调整规格

## 目标

将 LLM Wiki Vault 的 `raw/` 与 `wiki/` 目录硬切换到新的固定结构。实现完成后，工具、初始化、写入校验、索引、公开读取、lint、查询和测试都以新结构为唯一合法结构；不保留 `raw/projects/` 兼容路径。

实现不得在代码、测试或文档中硬编码个人 vault 绝对路径。运行时 vault root 仍由工具参数、环境变量或全局配置解析。

## 范围

包含：

- 初始化目录和默认文件结构。
- CodeGraph、LLM ingest、research、note、query、lint、read/list 等工具涉及的路径规则。
- `wiki/index.md` 及各子目录 `index.md` 的索引策略。
- `wiki/log.md` 与 `wiki/archives/log.md` 超过 200 条时的归档策略。
- 测试和 README / schema 文档中的路径说明。

不包含：

- 自动迁移已有 vault 中旧目录的文件。
- 保留 `raw/projects/`、`wiki/comparisons/`、`wiki/maintenance/` 或 `wiki/projects/<project>/sources/` 的兼容写入。
- 引入向量库、embedding 或新的外部运行依赖。

## 新目录结构

### Vault 根目录

```text
<vault-root>/
├── purpose.md
├── schema.md
├── raw/
│   ├── assets/
│   └── sources/
│       ├── projects/
│       ├── chat/
│       ├── file/
│       └── references/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── overview.md
│   ├── concepts/
│   │   └── index.md
│   ├── chatlog/
│   │   └── index.md
│   ├── projects/
│   ├── sources/
│   │   └── index.md
│   ├── queries/
│   │   └── index.md
│   ├── entities/
│   │   └── index.md
│   └── archives/
│       └── log.md
├── .obsidian/
└── .llm-wiki/
    ├── ingest-cache/
    ├── graph-index/
    └── relation-candidates/
```

说明：

- `raw/` 的直接子级只有 `assets/` 和 `sources/`。
- `raw/assets/` 初始化时无子级目录。
- `raw/sources/` 初始化时只有 `projects/`、`chat/`、`file/`、`references/` 四个子级目录。
- `wiki/` 的直接子级目录只有 `concepts/`、`chatlog/`、`projects/`、`sources/`、`queries/`、`entities/`、`archives/`。
- `wiki/` 的直接子级文件只有 `index.md`、`log.md`、`overview.md`。
- `.obsidian/` 和 `.llm-wiki/` 是运行支持目录，不属于 `raw/` 或 `wiki/` 内容结构。

### raw/sources/projects

```text
raw/sources/projects/
└── <project>/
    ├── requirements/
    ├── codegraph/
    │   ├── status.json
    │   ├── files.json
    │   ├── context.json
    │   ├── graph.json
    │   └── codefacts.json
    └── assets/
```

规则：

- 项目源目录统一位于 `raw/sources/projects/<project>/`。
- CodeGraph snapshot 统一写入 `raw/sources/projects/<project>/codegraph/`；不再在 `codegraph/` 下创建 `<source-name>` 层，因为 `<project>` 已表明项目来源。
- 项目需求原文或需求 snapshot 写入 `raw/sources/projects/<project>/requirements/`。
- 项目相关原始资产写入 `raw/sources/projects/<project>/assets/`。
- 旧路径 `raw/projects/<project>/...` 不再生成、不再作为合法读取路径、不再作为 lint 合法路径。

### raw/sources/chat

```text
raw/sources/chat/
└── <yyyy>/
    └── <mm>/
        └── <dd>/
            └── <session-id>/
                ├── session.md
                └── manifest.json
```

规则：

- 会话原文按日期和 session id 存放。
- 日期优先从 `source_name` / `session-id` 中解析；无法解析时使用当前日期。
- `session-id` 必须是单段 Windows 安全路径名。

### raw/sources/file 与 raw/sources/references

```text
raw/sources/file/
└── <document-structure>/

raw/sources/references/
└── <document-structure>/
```

规则：

- 初始化时不创建额外子级目录。
- 后续根据输入文档结构创建子目录。
- 不再强制插入 `<project>/<source-name>/` 作为所有文件类来源的固定层级；具体路径由摄入逻辑基于文档结构和 source namespace 生成，但必须保持在对应 source type 根目录内。

## wiki 结构

### wiki/index.md

`wiki/index.md` 只索引各一级内容入口，不展开具体文档；其中 `projects/` 展开到每个项目的 `index.md`。

```text
wiki/index.md
    -> [[concepts/index|Concepts]]
    -> [[chatlog/index|Chatlog]]
    -> [[projects/<project>/index|<project>]]
    -> [[sources/index|Sources]]
    -> [[queries/index|Queries]]
    -> [[entities/index|Entities]]
    -> [[archives/log|Archives Log]]
```

如果某一级目录没有可索引内容，显示 `- 无`。`wiki/archives/` 按要求不创建 `index.md`，因此 `wiki/index.md` 只链接 `wiki/archives/log.md` 作为归档入口。

### wiki/concepts

```text
wiki/concepts/
├── index.md
└── <domain>/
    ├── index.md
    └── <concept-page>.md
```

规则：

- `wiki/concepts/index.md` 索引每个 `<domain>/index.md`。
- 如果只有少量通用概念，也仍优先放入明确 `<domain>/`。
- 每个 `<domain>/index.md` 索引该 domain 内的概念页面。

### wiki/chatlog

```text
wiki/chatlog/
├── index.md
└── <yyyy>/
    └── <mm>/
        └── <dd>/
            └── <session-summary>.md
```

规则：

- `wiki/chatlog/index.md` 直接记录 chatlog 条目。
- `wiki/chatlog/<yyyy>/<mm>/<dd>/` 不生成 `index.md`。
- chatlog 页面必须可追溯到 `raw/sources/chat/<yyyy>/<mm>/<dd>/<session-id>/session.md`。
- 如果 `wiki/chatlog/index.md` 记录数量超过 200 条，将最旧的超出条目及其对应的历史 `wiki/chatlog/index.md` 片段一并归档到 `wiki/archives/<yyyy>/<mm>/<dd>/chatlog/`，当前 `wiki/chatlog/index.md` 只保留最新 200 条。

### wiki/projects

```text
wiki/projects/
└── <project>/
    ├── index.md
    ├── specs/
    ├── plans/
    ├── architecture/
    ├── pipelines/
    ├── troubleshooting/
    └── researches/
```

规则：

- 每个 `<project>` 必须有 `index.md`。
- 项目下只允许这些子目录：`specs/`、`plans/`、`architecture/`、`pipelines/`、`troubleshooting/`、`researches/`。
- 不再允许 `wiki/projects/<project>/sources/`、`objects/`、`code/` 等旧目录作为 wiki 生成目标。
- CodeGraph 可读页面写入 `architecture/` 或 `pipelines/`。
- CodeGraph 文件级事实如果仍需保留，应作为 raw JSON 写入 `raw/sources/projects/<project>/codegraph/codefacts.json`，不再生成 `wiki/projects/<project>/sources/codefacts/...` 页面。

### wiki/sources

```text
wiki/sources/
├── index.md
├── concepts/
│   └── <domain>/
│       └── <source-name>.md
├── chatlog/
│   └── <yyyy>/
│       └── <mm>/
│           └── <dd>/
│               └── <source-name>.md
├── projects/
│   └── <project>/
│       ├── specs/
│       ├── plans/
│       ├── architecture/
│       ├── pipelines/
│       ├── troubleshooting/
│       └── researches/
├── queries/
└── entities/
```

规则：

- `wiki/sources/` 是来源索引目录，镜像 `wiki/` 下除 `wiki/sources/` 和 `wiki/archives/` 以外的目标结构。
- `wiki/sources/archives/` 不创建、不写入；归档后的数据不建立新的 source index，已有历史 source index 应随对应历史内容一并归档。
- source index 页面只保存 frontmatter、一句话摘要、raw source 路径和指向生成页的 wikilinks，不承载知识正文。
- CodeGraph source index 从旧 `wiki/projects/<project>/sources/<source-name>.md` 改为 `wiki/sources/projects/<project>/architecture/codegraph.md` 或按实际生成页所属项目子目录分组写入 `codegraph.md`。
- LLM source index 按生成页目标路径分组写入镜像目录。

### wiki/queries

```text
wiki/queries/
├── index.md
└── <yyyy>/
    └── <mm>/
        └── <dd>/
            └── <query-id>/
                └── <query-page>.md
```

规则：

- `wiki_research` 生成内容写入日期和 query id 分层目录。
- `wiki/queries/index.md` 索引查询目录入口或具体 query id 入口，不直接展开所有历史页面。

### wiki/entities

```text
wiki/entities/
├── index.md
└── <entity>/
    └── <entity-page>.md
```

规则：

- `wiki/entities/` 存放构建完毕的不同实体。
- `wiki/entities/index.md` 直接索引各 `<entity>/` 内的实体页面。
- `wiki/entities/<entity>/` 内不生成 `index.md`。

### wiki/archives

```text
wiki/archives/
├── log.md
└── <yyyy>/
    └── <mm>/
        └── <dd>/
            ├── projects/
            │   └── <project>/
            │       └── ...
            ├── concepts/
            │   └── <domain>/
            │       └── ...
            ├── chatlog/
            │   └── ...
            ├── queries/
            │   └── ...
            └── entities/
                └── ...
```

规则：

- `wiki/archives/` 存放过时、废弃或超限归档的 wiki 文档。
- 归档日期目录内保留原 `wiki/` 下的相对结构，但不包含最外层 `wiki/` 前缀。
- 示例：`wiki/projects/demo/specs/a.md` 归档为 `wiki/archives/2026/06/16/projects/demo/specs/a.md`。
- 归档文档不需要索引。
- 被归档文档的 frontmatter 应增加归档标识，例如 `archived: true`，并可追加 `tags: [archived]`。
- `wiki/archives/log.md` 记录归档日志。

## 日志归档规则

`wiki/log.md` 和 `wiki/archives/log.md` 都以 200 条结构化条目为上限。

当追加新日志时：

1. 解析当前日志条目数量。
2. 如果追加后超过 200 条，将最旧的超出部分移动到当天归档路径。
3. `wiki/log.md` 保留最新 200 条。
4. 归档出来的日志片段放入 `wiki/archives/<yyyy>/<mm>/<dd>/log/<source-log-name>-<sequence>.md`。
5. `wiki/archives/log.md` 记录本次日志归档动作。
6. 如果 `wiki/archives/log.md` 也超过 200 条，按同样规则归档其旧条目。

## 工具路径行为

### wiki_init / wiki_status

- `wiki_init` 创建新结构中的必需目录和默认文件。
- `wiki_status` 只检查新结构必需路径。
- 旧目录存在时不自动删除，但 `wiki_lint` 应报告为 deprecated 或 invalid。

### wiki_ingest_codegraph

写入路径改为：

```text
raw/sources/projects/<project>/codegraph/status.json
raw/sources/projects/<project>/codegraph/files.json
raw/sources/projects/<project>/codegraph/context.json
raw/sources/projects/<project>/codegraph/graph.json
raw/sources/projects/<project>/codegraph/codefacts.json
wiki/projects/<project>/architecture/code-overview.md
wiki/projects/<project>/pipelines/<pipeline>.md
wiki/sources/projects/<project>/architecture/codegraph.md
wiki/sources/projects/<project>/pipelines/codegraph.md
```

规则：

- 不再写 `raw/projects/...`。
- 不再写 `wiki/projects/<project>/sources/...`。
- 文件级 code facts 默认折叠到 raw `codefacts.json`。
- 可读 wiki 页面只写入 `architecture/` 和 `pipelines/`。

### wiki_ingest_llm

- `stage="prepare"` 写入 `raw/sources/<source-type>/...`。
- `source_type="chat"` 写入 `raw/sources/chat/<yyyy>/<mm>/<dd>/<session-id>/`。
- `stage="apply"` 只允许生成新结构内的 wiki 页面。
- source index 写入 `wiki/sources/` 的镜像目标目录。

### wiki_research

- 从旧 `wiki/queries/<filename>.md` 改为 `wiki/queries/<yyyy>/<mm>/<dd>/<query-id>/<filename>.md`。
- 写入后刷新 `wiki/queries/index.md` 和 `wiki/index.md`。

### wiki_write_note

- `spec` 写入 `wiki/projects/<project>/specs/`。
- `plan` 写入 `wiki/projects/<project>/plans/`。
- `troubleshooting` 写入 `wiki/projects/<project>/troubleshooting/`。
- `researches` 写入 `wiki/projects/<project>/researches/`。
- `knowledge` 写入 `wiki/concepts/<domain>/`。
- 不新增其他 note type 的目录。

### wiki_list_files / wiki_read_file

- 只允许公开列举或读取 `wiki/` 和 `raw/sources/`。
- `raw/projects/` 不再是允许路径。
- 非文本文件仍拒绝读取。

### wiki_query / wiki_insights / wiki_lint

- 默认知识查询范围包含 `wiki/concepts/`、`wiki/chatlog/`、`wiki/projects/`、`wiki/sources/`、`wiki/queries/`、`wiki/entities/`。
- 默认不把 `wiki/archives/` 纳入查询结果。
- `wiki_lint` 检查旧目录并报告问题：`raw/projects/`、`wiki/comparisons/`、`wiki/maintenance/`、`wiki/projects/<project>/sources/`。

## 写入校验规则

允许的 wiki 写入前缀：

```text
wiki/projects/<project>/{specs,plans,architecture,pipelines,troubleshooting,researches}/
wiki/concepts/<domain>/
wiki/chatlog/<yyyy>/<mm>/<dd>/
wiki/sources/
wiki/queries/<yyyy>/<mm>/<dd>/<query-id>/
wiki/entities/<entity>/
wiki/archives/<yyyy>/<mm>/<dd>/
```

禁止写入：

```text
raw/projects/
wiki/comparisons/
wiki/maintenance/
wiki/projects/<project>/sources/
wiki/projects/<project>/objects/
wiki/projects/<project>/code/
wiki/requirements/
wiki/knowledge/
wiki/synthesis/
```

所有动态路径段继续使用 Windows 安全校验：不得包含 `<>:"|?*`、控制字符、ADS 冒号、保留设备名、尾随点或空格。

## 验收标准

- 初始化一个空 vault 后，只生成本规格列出的必需目录和默认文件。
- `wiki_status` 对新结构返回 initialized=true。
- CodeGraph ingest 不创建 `raw/projects/`、`raw/sources/projects/<project>/codegraph/<source-name>/` 或 `wiki/projects/<project>/sources/`。
- LLM ingest 的 source index 写入 `wiki/sources/` 镜像目录。
- `wiki/index.md` 只索引一级目录 index，不列出具体知识页。
- 项目、concept、query 目录生成对应 `index.md`；chatlog 只维护 `wiki/chatlog/index.md`；entity 子目录不生成 `index.md`。
- `wiki_list_files(root_name="sources")` 不返回 `raw/projects/`。
- `wiki_read_file("raw/projects/...")` 返回不允许路径错误。
- `wiki_query` 默认结果不包含 `wiki/archives/`。
- 日志超过 200 条时会归档旧条目，并保留最新 200 条。
- `wiki/chatlog/index.md` 超过 200 条时，归档最旧超出条目及对应历史索引片段，并保留最新 200 条。
- 相关测试和全量测试通过。
