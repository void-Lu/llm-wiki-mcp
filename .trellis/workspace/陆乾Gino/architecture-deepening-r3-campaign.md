# architecture-deepening-r3 campaign 记忆

状态：已完成（2026-08-19）

## 范围

主任务 `08-19-architecture-deepening-r3` 按 G1 → G7 顺序完成。每个子任务均在独立的新 Codex 会话中实施，模型为 gpt-5.6-luna、最高思考强度；子会话不创建子代理、不提交 Git、不归档，由主会话完成验收、分组提交和归档。7 个子任务的 PRD/Implement 完成项均已勾选，子任务目录均已归档。

## 交付摘要

- G1：接通 calibrated quality gate runtime、评测 adapter snapshot、候选特征词汇和 report schema owner；补齐 CLAUDE/CHANGELOG。
- G2：统一 wiki update preview/apply 的 incoming preparation 顺序和纯函数；补齐 CLAUDE/CHANGELOG。
- G3：`KnowledgeDependencies.update_page(..., policy=policy)` 收拢 PagePolicy 投影；补齐 CLAUDE/CHANGELOG。
- G4：清扫 retrieval/wiki/server 死代码与重复 seam；补齐 CLAUDE/CHANGELOG。
- G5：收缩 note writer 参数并将页面脱敏责任归 `prepare_wiki_page`；补齐 CLAUDE/CHANGELOG。
- G6：统一 snapshot page eligibility owner；更新 CLAUDE，CHANGELOG 记录为无需更新（内部收敛、无公开行为变化）。
- G7：增加 journal projection stage parity 纯断言和漂移负例；CLAUDE/CHANGELOG 均无需更新（内部测试约束）。

## 验收

- 主会话默认沙箱全量：921 passed、2 skipped、5 个既有失败；失败为 3 个 PyPI 网络套接字 `os error 10013` 和 2 个已有 README/.gitignore/CLAUDE 文档断言，未出现 campaign 新增失败。
- 受控高权限 `tests/test_build_backend.py`：3 passed，确认 build 失败来自网络沙箱。
- 高权限全量：922 passed、2 skipped；文档断言 2 个仍在，另有 archive migration 与 log rotation 的 Windows 文件锁失败。archive 单测重跑通过，log rotation 的 `os.replace` `WinError 5` 仍复现，未归因于本 campaign。
- `uv run ruff check src/`：通过。
- `git diff --check`：通过。
- 最终工作树仅保留用户已有、未触碰的未跟踪目录 `.netsuite-mcp/`。

## 候选 6

候选 6（`QueryExecutionContext.execute()`、冻结请求值对象、outcome 冻结前门禁投影、server 统一 telemetry terminal 语义、QueryTelemetry DDL 惰性化）明确暂缓，另行立项。原因是它跨执行入口、冻结边界、遥测终态和 DDL 生命周期，改动爆炸半径高于本 campaign，需基于 G1 后现状单独写规格和验收。

## 环境报错与处理

- CodeGraph/ `trellis mem` 在低权限沙箱访问 `C:\Users\26327` 报 `EPERM lstat`；使用受控高权限只读调用。`trellis mem` 另提示当前版本 OpenCode reader 不可用，但 Codex 历史可读。
- pytest 默认 AppData 临时根报 `WinError 5`；使用高权限或工作区显式 `--basetemp`。
- pytest cache 报 `WinError 183`；使用 `-p no:cacheprovider`。
- SQLite 临时 vault 清理报 `WinError 32`；延后清理。
- Git `.git/index.lock` 写入、归档自动 `git add` 和用户级 `.config/git/ignore` 读取受限；仅对明确提交使用受控高权限，未纳入 `.netsuite-mcp/`。
- uv build 访问 PyPI 报套接字 `os error 10013`；高权限重跑通过。
- log rotation 原子替换偶发/复现 `os.replace` `WinError 5`；记录为 Windows 文件锁环境问题，留待单独问题处理。

## 主会话分组提交

`7d710db`、`ec01a00`、`339125d`、`af154c4`、`7041d70`、`1e532f5`、`65fa800`、`84c0766`、`5fc1933`、`b0b89b9`、`3b610e6`、`e78612c`、`4c66965`、`e421ea2`、`1bcd48e`、`4ca4499`、`ffdd13e`、`57c2a7d`。
