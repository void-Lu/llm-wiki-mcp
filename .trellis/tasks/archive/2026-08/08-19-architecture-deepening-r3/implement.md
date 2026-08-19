# 实施清单:架构深化整改 r3(父任务)

## 前置

- [x] 评审报告(HTML)与 9 项候选 grilling 定案(2026-08-19)。
- [x] prd.md 定案表、7 个子任务 prd/design/implement 文档齐备。
- [x] CONTEXT.md 增「校准产物」「阈值视图」词条。

## 编排(按序执行;并行规则见 design.md)

- [x] **G1 质量门禁接线**(P0,先修行为正确性):artifact_path 配置+pipeline 接线、评测入口对等、特征提取收窄、report schema 单点化。
- [x] **G2 preview/apply 准备合并**:统一准备纯函数,错误顺序对齐 apply 现状。
- [x] **G3 PagePolicy 穿透 update_page**:`policy=` 一步迁移 3 个调用方。
- [x] **G4 死代码清扫**(硬依赖 G1 完成):10 项清单,含两条 G1 耦合点复核。
- [x] **G5 note_writer 最小收缩**:删死参数、脱敏单点化。
- [x] **G6 候选资格判定收敛**(建议后于 G1):先删 `_graph_expand` 复检(纯赢),再统一入口。
- [x] **G7 journal 阶段元组断言**:显式化隐式不变量,不动 schema。

G2/G3/G5/G7 互相独立且与 G1 文件不相交,可在 G1 后并行推进;G4、G6 必须后于 G1。

## 后继(不在本 campaign)

- [x] 候选 6(执行入口收拢)明确暂缓并独立立项:context 公开 `execute()` + 冻结请求值对象 + 门禁投影移到 outcome 冻结前(消除 freeze-then-thaw 往返)+ 遥测 terminal 语义归 server 一处 + `QueryTelemetry` DDL 惰性化。基于 G1 后新现状写规格；本 campaign 不实施，理由是它同时改变执行入口、请求冻结边界、遥测终态和 DDL 生命周期，爆炸半径明显大于本轮行为修复与纯收敛任务。

## 环境执行记录（2026-08-19）

- CodeGraph 在沙箱内访问 `C:\Users\26327` 时出现 `EPERM lstat`；已通过主会话一次性受控高权限只读探索解决，后续独立会话提前授权。
- pytest 默认临时目录 `C:\Users\26327\AppData\Local\Temp\pytest-of-26327` 出现 `WinError 5`；已改用高权限测试或工作区内显式 `--basetemp`，完成后清理临时目录。
- Git 写入 `.git/index.lock` 及归档脚本自动 `git add` 受沙箱权限限制；已对明确的分组提交使用受控高权限，未纳入未识别的 `.netsuite-mcp/`。
- Git 读取用户级 `.config/git/ignore` 出现 `Permission denied` warning；未改动用户级 Git 配置。
- pytest cache 创建出现 `WinError 183`（目标目录已存在）；验证命令使用 `-p no:cacheprovider` 或工作区独立临时根目录。
- SQLite 临时 vault 清理曾出现 `WinError 32`（文件仍被占用）；改为测试进程结束后清理，并保留显式工作区临时根目录。
- 构建测试在沙箱内访问 PyPI 出现套接字 `os error 10013`；受控高权限重跑 `tests/test_build_backend.py` 后 3 项通过，确认是网络沙箱限制。
- 高权限全量中 archive migration 与 log rotation 曾出现 `os.replace` 的 `WinError 5`；archive 单测重跑通过，log rotation 单测仍复现，记录为既有 Windows 文件锁/原子替换环境问题，未扩大本 campaign 修复范围。
- 低权限运行 `trellis mem` 同样触发 `EPERM lstat C:\Users\26327`；高权限读取成功，但工具提示 OpenCode platform reader 在当前版本暂不可用；Codex 历史检索正常。

## 收尾

- [x] 7 个子任务 completed；各子任务 PRD/Implement 完成项、顺序和提交边界逐项核对。
- [x] 全量 `uv run python -m pytest` 与 `uv run ruff check src/` 已执行并完成回归核对：默认沙箱全量为 921 passed、2 skipped、5 个既有失败（3 个 PyPI 套接字 10013、2 个文档断言）；高权限 build backend 3 passed，未发现 campaign 新增失败。高权限全量额外受 Windows 临时文件锁影响的 archive/log 单测已分别复核，archive 通过、log rotation 仍复现，均非本 campaign 改动。
- [x] CLAUDE.md 查询侧架构段补 artifact 接线一行(G1 产出)。
- [x] 已更新并标记完成记忆 `.trellis/workspace/陆乾Gino/architecture-deepening-r3-campaign.md`；campaign 目录待本次收尾脚本归档。

## 子任务文档维护核对（2026-08-19）

- [x] G1：更新根目录 `CLAUDE.md` 查询侧 artifact 接线；更新 `CHANGELOG.md` 校准门禁条目。
- [x] G2：更新根目录 `CLAUDE.md` wiki update 准备顺序；更新 `CHANGELOG.md`。
- [x] G3：更新根目录 `CLAUDE.md` PagePolicy 投影边界；更新 `CHANGELOG.md`。
- [x] G4：更新根目录 `CLAUDE.md` 查询/状态清扫说明；更新 `CHANGELOG.md`。
- [x] G5：更新根目录 `CLAUDE.md` note writer 脱敏边界；更新 `CHANGELOG.md`。
- [x] G6：更新根目录 `CLAUDE.md` snapshot candidate eligibility owner；内部收敛无公开行为变化，`CHANGELOG.md` 不需更新，已在子任务记录原因。
- [x] G7：仅增加内部纯断言与测试；`CLAUDE.md`、`CHANGELOG.md` 均不需更新，已在子任务记录原因。

## 逻辑提交边界

- 每个子任务按各自 implement.md 的提交边界独立成组;父任务本身无代码提交,只有 `.trellis` 文档与文档同步(CONTEXT.md/CLAUDE.md/spec)。
