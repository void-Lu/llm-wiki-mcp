# 架构深化整改 r3 - 2026-08-19 架构评审落地

## 背景

2026-08-19 架构评审（焦点:Query V2 质量门禁链路热点 + 写入路径/MCP 边界）产出 9 项候选，经两轮 grilling 全部定案。两项已核实的行为正确性问题优先:

1. **运行时门禁恒 keep-all**：calibration 加载侧（`CalibrationArtifactLoader`/`resolve_threshold_view` 等）在 `src/` 内零生产消费方，`run_query_v2` 门禁调用不传 threshold view；默认 vault `codingwork` 正在跑 shadow，抑制计数恒为 0。
2. **评测 engine 入口丢设置**：`EngineQueryAdapter(root)` 只取 root，`_execute_engine_query` 不传 `quality_gate`；现有测试断言 `status in {"proven","unproven"}` 两可通过，恰好掩盖断线。

## 定案摘要（grilling 记录）

| 决策 | 定案 |
|---|---|
| Q1 门禁接线方向 | (a) 接线:`QualityGateSettings` 增加 artifact 定位，fail-open 语义复用加载侧既有实现 |
| Q2 评测入口 | (b) 入口对等:两个 adapter 消费同一 runtime snapshot |
| Q3 note_writer | (a) 最小收缩:删死参数+归并双重脱敏;不做参数分组(单消费方,hypothetical seam) |
| Q4 preview/apply | (a) 共享准备函数留 `wiki_update.py`;校验顺序以 apply 现状为准(incoming 内容先于页面状态) |
| Q5 PagePolicy | (b) 直接改为只收 `policy=`,一步迁移 3 个调用方,不留双轨 |
| Q6 执行入口时机 | (a) 分开:先修行为正确性,候选 6 独立立项 |
| Q7 候选收敛拆分 | (a) 资格判定独立任务;(b) 特征提取收窄并入 G1 |
| Q8 journal 解耦 | (b) 暂缓 schema 调整,先补显式断言 |
| Q9 清扫打包 | (a) 独立 PR;遥测 terminal 语义归候选 6,report schema 双份归 G1 |
| Q10 artifact 路径 | (a) `quality_gate.artifact_path`,vault 相对解析,绝对路径也接受 |
| Q11 adapter 形状 | (a) `EngineQueryAdapter(snapshot)`,删除 root 便捷构造 |
| Q12 载体 | (a) 本 campaign 目录,子任务按序 G1->G7 |
| Q13 术语 | (c) CONTEXT.md 增「校准产物」「阈值视图」(已落地) |

## 子任务顺序

- **G1 质量门禁接线**（候选 1+2+7b + report schema 单点化）— 行为正确性,最优先
- **G2 preview/apply 准备合并**（候选 4）
- **G3 PagePolicy 穿透 update_page**（候选 5）
- **G4 死代码清扫 PR**（候选 9a）
- **G5 note_writer 最小收缩**（候选 3a）
- **G6 候选资格判定收敛**（候选 7a）
- **G7 journal 阶段元组断言**（候选 8b）

## 后续独立立项

**候选 6（执行入口收拢）**：`QueryExecutionContext` 公开单一执行入口 + 冻结请求值对象 + 门禁投影移到 outcome 冻结前（连带消除 freeze-then-thaw 往返）+ 遥测 terminal 语义归 server 一处 + 每请求 DDL 挪惰性初始化。依赖 G1 落地后基于新现状写规格。

## 约束

- 遵守 ADR-0013（MCP 边界双校验保留）、ADR-0014（单消费方不建 owner 模块;G3 的 update_page 有 3 个真实消费方,seam 已 real）。
- fail-open 已定案（quality-gate spec）;加载侧 policy_version/identity 不匹配 -> keep + 诊断,不需重议。
- 门禁字段、状态或介入位置变更需同步 `query-quality-gate` spec 与 Query V2/评测回归。
