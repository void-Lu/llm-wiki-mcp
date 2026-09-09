---
status: accepted
date: 2026-08-13
---

> 清理条款已由 ADR-0012 supersede；本 ADR 的 CodeGraph 摄入移除决策主体仍然有效。

# 移除 CodeGraph 摄入工具

## 决策

从下一发布起移除 `wiki_codegraph_import`、`src/codegraph/` 包及其所有权/检索隔离消费点；新增 `repair codegraph-removal plan|apply` 清理既有 vault 中带完整 CodeGraph 标记的页面和 raw 目录。项目 `wiki/projects/*/architecture/` 目录本身保留，供未来项目级代码分析笔记使用。

## 理由

- CodeGraph 摄入的实际价值低，用户已确认不再继续使用。
- `codegraph_sync` 维护第二套页面写入管线，与 ADR-0009 规定的统一崩溃安全写入和页面变更协调器边界冲突；移除比继续深化这套管线更简单、可恢复成本更低。
- 未来代码结构知识通过既有 note/update 工具写入项目级 `architecture/`，不再建立专用摄入入口或检索 corpus。

## 清理与兼容行为

- `repair codegraph-removal plan` 只读列出匹配的 architecture/archive 页面和 `raw/sources/projects/*/codegraph/` 目录；`apply` 幂等删除它们，保留 architecture 目录、手工页面和其他 raw 内容。
- 未运行清理命令的旧页面按普通 generated 页面处理，`wiki_update` 可以按既有 generated 覆盖规则更新；查询不再要求项目，也不再对 `project_code` 做专属过滤。
- 旧 raw 文件按既有 raw scope 的显式索引边界处理，不再经过 CodeGraph 专属排除；清理命令会删除匹配的 `raw/sources/projects/*/codegraph/` 目录。

## 后果

- core MCP 工具由 10 个减少为 9 个；`wiki_status` 不再返回 CodeGraph 字段。
- CodeGraph 摄入包和对应测试删除；公共文档、运行配置说明和 Trellis spec 不再把摄入作为可用能力。
- 本决策不删除仓库根 `.codegraph/`，它仍是 AI 代码导航工具自己的索引；也不自动迁移或重命名 vault 中的其他内容。
