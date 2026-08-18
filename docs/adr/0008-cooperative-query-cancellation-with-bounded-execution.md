---
status: accepted
date: 2026-08-10
---

# 查询采用协作式取消与有界执行容量

`wiki_query` 的 deadline 必须贯穿查询阶段，而不能只限制 MCP 调用方等待结果的时间。查询服务在阶段边界检查取消状态；外部 provider 使用其原生 timeout；不能被 Python 安全强停的同步阻塞调用进入容量受限的执行器。deadline 到达后停止启动后续阶段，并返回稳定的取消阶段信息，但不承诺终止已经运行的工作线程。

## 后果

- 查询上下文携带单调时钟 deadline/cancellation token，检索、fallback、图扩展、重排、上下文组装和 provider 调用在进入与返回时检查剩余预算。
- provider timeout 由剩余 deadline 收紧；无原生取消能力的同步调用只能在有界执行容量中运行，容量耗尽时快速返回稳定的 overload/busy 错误，不能无限堆积后台线程。
- 超时响应包含安全的 `cancelled_stage`、correlation ID 和稳定错误码；不暴露原始异常，也不能把“调用方停止等待”表述为“底层工作已被强制终止”。
- deadline 到达后不得继续安排图扩展、fallback 或普通 telemetry 等后续工作；已经运行且无法取消的调用可以自然收敛，但其结果不得重新进入已结束请求。
- 不为 MVP 引入每查询子进程。若以后出现必须强制终止的不可信计算，再以独立隔离执行 ADR 评估 Windows 启动成本、状态序列化和数据库连接所有权。
