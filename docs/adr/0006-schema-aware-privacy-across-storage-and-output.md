---
status: accepted
date: 2026-08-10
---

# 隐私策略覆盖落盘、投影与公共响应

新写入的 display 字段统一按照逻辑知识库的 `PrivacySettings` 在落盘、派生投影和公共响应中处理；locator 与 integrity 字段必须保持精确身份，只做格式和敏感边界验证，不得被通用文本正则改写。公共 MCP 永不返回绝对路径、内部配置路径、可执行文件路径、原始异常文本或 traceback。

## 后果

- title、summary、tags、chat metadata 和正文使用同一字段级策略；自动文件名必须从处理后的安全标题生成，并防止碰撞。
- project、domain、filename、sources 等 locator 若违反隐私或路径策略则以稳定错误拒绝，不能把脱敏占位符写进路径；hash、plan ID、revision 等 integrity 字段保持原值。
- 所有工具结果经过统一 public-result projector；公开错误使用稳定 code、安全短消息和 correlation ID，不拼接 `str(exc)`。
- 历史页面不会静默批量重写。Privacy audit 通过 CLI/admin 的 plan/apply、页面 CAS 和可回滚记录显式迁移；检索投影可从处理后的事实页重建。
