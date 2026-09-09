---
status: accepted
date: 2026-08-12
---

# 所有单文件文本产物统一使用崩溃安全写入原语

页面、chat source、日志卷、生成导航和 overview 的单文件 durable replacement 统一复用 `wiki.atomic_file` 的 `atomic_write_text`/`atomic_write_bytes`。该原语负责同目录临时文件、flush/fsync、replace、故障屏障和临时文件清理；调用方不再各自实现 temp + replace 或裸 `write_text`。

## 选择理由

现有页面写入已经有 fsync 和故障分类，但日志卷、chat source、`index.md` 与 `overview.md` 存在较弱或裸写路径。崩溃后的截断内容可能触发人工页保护，令导航长期无法重新生成。统一原语把崩溃安全和故障注入的复杂度集中在一个 owner，且不改变页面事实与派生投影的分离。

## 后果

- 单个目标文件的替换具有一致的 crash-safety 和测试行为。
- 多文件日志轮转、导航刷新和 overview 刷新仍不是跨文件事务；若其中一项失败，由既有 projection journal/repair 语义恢复，不对外宣称全量原子提交。
- 任何新增文本产物必须通过该原语；直接 `write_text` 仅允许在测试 fixture 或明确的非 durable 临时文件中使用。
- 该决策不改变人工页保护、页面提交成功后 `repair_pending` 或索引重建边界。

## 附注（2026-08-15）：fault 注入的 contextvar 传播

故障注入的唯一 owner 仍是 `wiki.atomic_file`，但注入值通过显式导出的
`fault_context(fault)` context manager 放入当前执行上下文。`atomic_write_text`、
页面投影阶段和归档状态转换从该 contextvar 读取 barrier；生产函数签名不再逐层携带
`fault`/`fault_at` 参数，也不保留可变的 profile 或 fault registry。context manager 使用
token 恢复嵌套上下文，未注入时继续使用 no-op `fault_barrier`；执行 registry 启动线程时
复制调用方 context，避免故障注入跨请求泄漏或在线程边界丢失。该传播方式不改变原子写入
安全语义、归档阶段词汇或稳定错误码。
