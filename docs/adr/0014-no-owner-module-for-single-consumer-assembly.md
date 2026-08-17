---
status: accepted
date: 2026-08-18
---

# 单消费方装配不建 owner 模块

## 决策

对只有一个消费方的装配/投影逻辑，不为其建立独立 owner 模块（如曾提议的
`wiki/vault_status.py` 健康快照装配 owner）。装配逻辑以**模块级纯函数**留在
消费方所在模块内：函数只收数据、只返回结果（`assemble_vault_status(base_status,
config_status, archive_status, execution_status, detail) -> dict` 形态），
副作用取数留在 wrapper。只有当第二个真实消费方出现时，才把该纯函数升级为
独立模块--seam 从 hypothetical 变为 real 时才建。

## 背景

2026-08-18 架构评审曾建议把 `wiki_status` 的健康快照装配（vault 结构、
config 快照、ArchiveStatusReader、query registry 状态、`DETAIL_FIELDS`
字段裁剪，共约 30 行）抽成 `wiki/vault_status.py` owner，理由是可测试性。
grilling 发现该装配只有 MCP `wiki_status` 一个消费方：CLI `status` 的
`_runtime_payload` 是运行时诊断投影（storage/config 路径），CLI archive
status 是原始归档状态的透传，二者都不消费健康快照。按"one adapter =
hypothetical seam, two = real"纪律，单消费方下建模块需要发明注入协议，
interface 复杂度逼近 implementation，恰是浅模块。纯函数同样满足原始诉求：
测试可直调 seam，无需 MCP fixture。

CLI archive status 维持原始 `ArchiveStatusReader` 透传，不为凑第二个
adapter 而改其输出。

## 后果

装配逻辑获得直接 test surface（直调纯函数），但 `server.py` 继续持有
`assemble_vault_status` 与 `DETAIL_FIELDS`。未来若出现第二个消费方
（例如 CLI 需要同一健康快照），应将纯函数连同常量升级为独立模块并建立
注入协议，届时本 ADR 的约束解除。评审流程不应再对单消费方装配提议
owner 模块。
