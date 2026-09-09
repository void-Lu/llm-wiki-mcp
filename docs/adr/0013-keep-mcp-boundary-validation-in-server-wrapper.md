---
status: accepted
date: 2026-08-17
---

# 将 MCP 边界校验保留在 server wrapper

## 决策

拒绝把 `wiki_query` wrapper 的 `scope` 校验前移或删除。MCP server wrapper
继续在调用查询引擎前校验 scope；非法值返回稳定的错误 dict
`{"ok": false, "code": "invalid_scope"}`。查询引擎的 `_effective_scope` 仍
校验同一组允许值，非法值抛出 `ValueError`。

server 层错误 dict 与引擎层 `ValueError` 是刻意的纵深防御，不是待消除的
重复：前者保持 MCP 公共错误契约，后者保护直接调用 engine 的非 MCP 入口。

## 背景

架构评审建议把 `wiki_query` wrapper 校验前移，以减少 server 与 engine 的
重复逻辑。2026-08-17 的评审定案保留两道 scope 校验，因为
`run_query_v2` 也会被评测 adapter、CLI 等非 MCP 路径直接或间接调用；如果只
保留 wrapper 校验，这些入口将失去同一边界的第二道防线。

## 后果

MCP 调用继续得到结构化错误 dict，直接 engine 调用继续得到
`ValueError`；公共行为不变。两处允许值集合需要同步维护，这是为覆盖非 MCP
入口而接受的成本。任何后续 scope 变更都必须同时更新 wrapper 与引擎校验。
