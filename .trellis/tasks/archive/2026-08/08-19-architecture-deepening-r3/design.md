# 设计:架构深化整改 r3(父任务)

## 领域边界

本 campaign 是 2026-08-19 架构评审 9 项候选的落地载体。父任务只负责顺序编排、跨子任务约束与收尾;所有代码改动在 7 个子任务内完成,各自持有 design.md/implement.md。候选 6(执行入口收拢)不在本 campaign 子任务清单内,G1 落地后独立立项。

## 子任务依赖图

```
G1 门禁接线(P0)
 ├─(硬依赖)─> G4 死代码清扫     # _query_case 死转口是 EngineQueryAdapter(root) 唯一调用方;
 │                                # 7 个私有导入/mcp_requires_lexical 收敛与 G1-D 同域
 ├─(软依赖)─> G6 资格判定收敛    # 同改 query_pipeline.py,先 G1 避免同文件冲突
 └─(后继)───> 候选 6 独立立项    # 执行入口收拢,基于 G1 后的新现状写规格

G2 preview/apply 合并 ── 独立(wiki_update.py)
G3 PagePolicy 穿透   ── 独立(knowledge_dependencies + 3 调用方)
G5 note_writer 收缩  ── 独立(note_writer.py/wiki_io.py)
G7 journal 断言      ── 独立(projection_profile.py)
```

## 执行顺序与并行规则

**默认串行序列**(campaign prd 已定):G1 -> G2 -> G3 -> G4 -> G5 -> G6 -> G7。

串行是保守默认,允许按以下规则并行:

- G2/G3/G5/G7 四个 wiki 侧子任务互相独立,且与 G1(retrieval/runtime 侧)文件不相交,可在 G1 之后任意并行。
- G4 必须等 G1 完成(硬依赖:删除 `EngineQueryAdapter(root)` 构造与 `_query_case` 死转口必须同批或后于 G1-B;`retrieval_eval.py` 的私有导入提升与 `mcp_requires_lexical` 收敛若 G1-D 已做则 G4 只剩删除)。
- G6 建议在 G1 之后(同改 `query_pipeline.py` 不同区域,串行避免冲突;G1-C 若已删 `evaluate_candidates`,G4 清单相应缩减)。
- 每个子任务独立提交边界(implement.md 已列);主会话按逻辑提交边界 squash,不跨子任务混提。

## 跨子任务约束

- 全程遵守 ADR-0013(MCP 边界双校验保留)与 ADR-0014(单消费方不建 owner 模块);G3 的 `update_page` 有 3 个真实消费方,seam 已 real。
- fail-open 语义(artifact 缺失/不匹配 -> keep + 诊断)已定案,任何子任务不得重议。
- 门禁字段、状态或介入位置变更须同步 `query-quality-gate` spec 与 Query V2/评测回归(G1 内执行)。
- 普通页面变更只做增量投影、全量建库仅显式 CLI/admin -- G1 的 per-query artifact 加载属读取文件,不触碰该红线。
- 文档同步:G1 落地后 CLAUDE.md 查询侧架构段补一行 artifact 接线;CONTEXT.md 术语已就位(校准产物/阈值视图),各子任务不重复改。

## 收尾标准

- 7 个子任务 status=completed,checkbox 全勾。
- 全量 `uv run python -m pytest` + `uv run ruff check src/` 已执行；以“无本 campaign 新增回归”为验收门槛，既有环境/文档失败需在 implement.md 留证，不扩大本 campaign 范围。
- 父任务 prd.md 定案表逐项核对无遗漏;候选 6 的立项票据(或明确记录「暂缓+理由」)。
- 记忆更新:`architecture-deepening-r3-campaign.md` 标记完成;campaign 目录归档至 `.trellis/tasks/archive/2026-08/`。
