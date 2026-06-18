# 批量知识摄入防污染与质量门禁设计

## 背景

当前 LLM Wiki 摄入流程已经拆成 `prepare` 和 `apply` 两段：`prepare` 生成模型提示词，`apply` 接收模型生成的 JSON 并写入 wiki 页面。批量入口 `wiki_ingest_batch` 负责持久化队列，并提供 `prepare_all`、`apply_all` 等批处理动作。

在大批量文档摄入场景下，调用方容易把每条任务的 prompt、schema、generation、错误输出都留在同一模型会话中。随着任务数增加，历史样本会污染下一条任务的输出，表现为页面质量下降、JSON 结构漂移、字段缺失、schema 不再被严格遵守。服务端当前只做了有限校验，坏输出仍可能进入落盘路径。

## 目标

1. 降低批量摄入时当前会话上下文膨胀和历史输出污染。
2. 在写入 wiki 前强制校验模型 generation 的结构和最低质量。
3. 将 schema 错误与低质量输出隔离在队列状态中，避免进入 wiki 文件和索引。
4. 保持现有 staged ingest API 的兼容性，优先做小步、可回滚的修复。

## 非目标

1. 不在本阶段实现完全独立的模型调用运行器。
2. 不引入新依赖。
3. 不改变 CodeGraph 直接摄入流程。
4. 不重写 wiki 页面生成模型或整体信息架构。

## 推荐方案

采用 “A 快速止血 + C 质量治理” 的两阶段设计。

第一阶段先减少批量接口返回的大 payload，并在 `apply` 前加硬校验。第二阶段补齐 validate/repair/verify 状态链，让失败输出可诊断、可重试、不会污染下一轮生成。

## 阶段一：快速止血

### 批量响应瘦身

`prepare_all` 继续把 prompt 和 `expected_response_schema` 写入持久队列，但批量调用结果只返回精简摘要：

- `task_id`
- `status`
- `source_hash`
- `prompt_ref` 或 `has_prompt`
- 错误摘要

批量接口不返回整批 prompt 正文。调用方需要处理模型生成时，通过单任务动作读取一条已准备任务，例如新增 `next_prepared` 或 `get_prepared`。

这样同一轮工具响应不会把几十或几百条 prompt 注入当前对话上下文，调用方也会自然形成 “一次取一条、一次生成一条、一次提交一条” 的节奏。

### 单任务读取动作

新增 batch action：

```text
next_prepared
set_generation
apply_one
```

返回最早一条 `status == "prepared"` 且包含 prompt 的任务：

```json
{
  "ok": true,
  "task": {
    "id": "string",
    "project": "string",
    "source_name": "string",
    "source_type": "file",
    "prompt": "string",
    "expected_response_schema": {}
  }
}
```

没有任务时返回 `ok: true` 和空任务，不视为错误。

`set_generation` 接收 `task_id` 和单条模型 generation，只更新该任务的 `result.generation`，不改变任务终态。`apply_one` 对一条 prepared 且已有 generation 的任务执行 apply。这样避免复用现有 `complete`，因为 `complete` 当前语义是把任务直接标记为 `done`。

## 阶段二：硬校验与质量治理

### Generation 结构校验

在 `_apply_generation` 写文件前新增纯函数：

```python
validate_generation_payload(payload, manifest_sources, project, source_type) -> ValidationResult
```

校验规则：

- generation 必须是 JSON object。
- `source_summary` 必须是 object 或非空字符串。
- `pages` 必须是 list；允许为空，但必须仍能生成 source index。
- 每个 page 必须是 object。
- page 必须包含非空 `path`、`title`、`type`、`summary`、`body`。
- `sources` 必须非空，且全部来自 prepared manifest。
- `path` 必须通过现有 `_safe_generated_page_path` 约束。
- `type` 必须在允许集合内，初始集合为 `concept`、`spec`、`plan`、`research`、`troubleshooting`、`chatlog`、`source_index`。
- `body` 去除空白后必须达到最低长度，初始阈值建议 80 字符。
- `summary` 必须是短文本，不能包含多段正文。

校验失败时 `_apply_generation` 返回：

```json
{
  "ok": false,
  "code": "generation_schema_invalid",
  "error": "generation failed validation",
  "errors": [
    {"path": "pages[0].body", "code": "empty_body", "message": "body is required"}
  ]
}
```

失败时不写页面、不刷新索引、不追加 ingest log。

### 错误隔离

`apply_all` 捕获 `generation_schema_invalid` 后，把任务标记为失败并记录：

- `error_stage: "validate"`
- `validation_errors`
- `generation_hash`
- 精简错误摘要

默认 `status` 和批量响应不返回完整坏 generation。坏 generation 可以继续保留在任务结果中供调试，但不应出现在批量摘要响应里。

### Repair 后续扩展

本设计保留 repair 状态，但第一轮实现不强制调用模型自动修复。

后续可增加：

```text
prepare_repair
apply_repair
```

repair prompt 只包含原始 schema、精简错误列表、当前任务源文件引用和坏 generation 的局部片段，不包含整批历史任务。

### Verify 后续扩展

写入后可复用现有 `wiki_verify` 思路，增加自动验证入口：

- 页面 frontmatter 可解析。
- sources 路径存在。
- wikilink 目标规范。
- 生成页面与 source manifest 有可追踪关系。

验证失败时页面可以进入 `needs_review` 状态或任务进入 `verification_failed`。

## 数据流

```text
enqueue
  -> prepare_all
  -> next_prepared
  -> model generation outside batch response
  -> set_generation for one task
  -> apply_one or apply_all
  -> validate_generation_payload
  -> apply pages only if valid
  -> optional verify
```

关键约束：模型每次只看到当前任务的 prompt、schema 和必要 wiki context，不看到整批任务历史。

## 兼容性

现有 `wiki_ingest_llm(stage="prepare")` 和 `wiki_ingest_llm(stage="apply")` 保持不变。

`prepare_all` 仍然会在队列内部保存 prompt，避免破坏已有批处理缓存。变化只发生在 batch action 的返回摘要中。如果已有调用方依赖 `prepare_all` 返回 prompt，应迁移到 `next_prepared`。

## 测试策略

新增或调整测试覆盖：

1. `prepare_all` 不在 `results[]` 返回 prompt 正文。
2. `next_prepared` 返回单条 prepared 任务和 prompt。
3. `_apply_generation` 拒绝缺字段页面且不写文件。
4. `_apply_generation` 拒绝非法 sources。
5. `_apply_generation` 接受合法 payload 并保持现有写入行为。
6. `apply_all` 对 validation failure 记录 `error_stage == "validate"`。
7. 队列 status 不返回大段 prompt/generation。

## 风险与缓解

最主要风险是已有调用方依赖 `prepare_all` 直接拿到 prompt。缓解方式是保留队列内部字段，只改变批量摘要响应，并提供明确的 `next_prepared` 替代路径。

第二个风险是校验规则过严导致历史可接受输出被拒绝。缓解方式是先采用低门槛规则，只拦截明显不符合 schema 或无法追踪 source 的输出。

第三个风险是错误状态增加后队列生命周期复杂。缓解方式是第一轮复用现有 `failed` 状态，通过 `error_stage` 区分 validate/apply，而不是立即引入多个新终态。

## 完成标准

1. 大批量 `prepare_all` 响应不会把整批 prompt 注入当前会话上下文。
2. 单条 prepared 任务可以被显式读取并生成。
3. 不符合 schema 或最低质量规则的 generation 不会写入 wiki。
4. validation failure 有结构化错误，便于重试或 repair。
5. 现有 staged ingest 正常路径继续通过测试。
