---
status: accepted
date: 2026-08-14
---

# 退役清理工具完整移除

## 决策

用户已确认真实 vault 在 2026-08-14 完成 CodeGraph 与 `wiki/sources` 内容迁移，删除前提
成立。因此完整移除三项一次性/退役工具：`SupersedeRegistry`、`repair codegraph-removal`
清理命令，以及 `scripts/archive_wiki_sources.py` 与孤立的
`scripts/codegraph/suitescript-resolver.js`。不新增替代机制，也不再设置 sunset 时间表。

## 公共契约变化

- `wiki_ingest` 响应删除 `superseded_jobs`；`stale_pages` 与信息性 `generation` 字段保留。
- `wiki_status` 删除 `queue` 键；`detail="generation"` 仅保留状态、配置、版本和运行身份字段。
- CLI 删除 `repair codegraph-removal`；repair 家族仅保留 page-operation、provenance 和
  privacy-audit。

## 保留与边界

`KnowledgeDependencies` 的 raw freshness/stale 行为不变，`generation: {enabled: false,
reason: "raw_only"}` 仍作为信息性声明。活动 Wiki 继续通过具体 `raw/sources/**` 文件和
`source_hashes` 表达 provenance；不迁移、重命名或删除 vault 文件，仓库根 `.codegraph/`
也不在本决策范围内。

## Supersede 关系

本 ADR supersede ADR-0011 中关于 `repair codegraph-removal` 存量清理入口的清理条款，
也 supersede 归档任务 `.trellis/tasks/archive/2026-08/08-13-knowledge-compilation-cleanup-01`
的 PRD 中将 `wiki_ingest` 的 `superseded_jobs`、`wiki_status.queue` 和 SupersedeRegistry
列为行为闸门的条款。ADR-0011 关于移除 CodeGraph 摄入工具本身的决策不变；cleanup PRD
中的惰性迁移原则只随已删除登记机制一并失效，不构成新的迁移要求。

## 后果与回滚

这是一次明确的公共契约收缩：客户端必须停止读取已删除字段或调用已删除命令。三个实现
commit 可独立 revert；revert 不会触碰真实 vault。
