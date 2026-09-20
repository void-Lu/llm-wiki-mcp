---
name: shared-spec-vault
description: 将项目规范目录与 Wiki vault 镜像，或在公共规范与项目规范之间执行逐项审核的提取、导入。来源目录由本 skill 声明，不假设固定名称。
---

# Shared Spec Vault

这是一个用户显式触发的 skill。每次只执行一个模式：`sync`、`extract` 或 `import`。不创建 Trellis task，不安装自身，也不保存审核状态或旧正文。

## 项目规范来源

工具只接受调用方传入的 `source_root`，脚本内不假设任何目录名；具体来源写在本 skill 里，安装后按项目增改：

| project | source_root |
| --- | --- |
| <project> | <项目规范目录的绝对路径> |

解析顺序：上表命中 > 用户本次指定的路径 > 项目 agent 配置（`AGENTS.md`/`CLAUDE.md`）声明的规范目录 > 探测 `.trellis/spec/`。都无法确定时先问用户，不要猜。

路径必须是绝对路径且落在当前项目内；一个项目只声明一个规范目录，避免同一批规则出现两个镜像。

## 共同约束

- 每次调用工具时都显式传入「项目规范来源」解析出的绝对路径和明确的 `project` 名称。
- 公共规范固定为 `wiki/entities/shared-specs/<file>.md`。目录扁平，项目和技术栈通过 frontmatter 的 `applies_to.languages`、`applies_to.platforms`、`applies_to.frameworks` 隔离；`conditions` 保存不能结构化的适用条件。
- 规则正文保留原文，只添加或维护公共页的 frontmatter 和必要的标题封装；不要总结、改写或合并正文。
- 所有会写入 vault 的操作先 preview，再把完整差异和影响告诉用户，得到确认后才 apply。每次导入都重新审核，不复用上次选择。
- 预览后遇到 `source_changed` 或 `target_changed`，停止 apply，重新 preview；不强行覆盖。

## `sync`：项目镜像

1. 调用 `wiki_sync_specs(source_root=<项目规范来源的绝对路径>, project=<project>, action="preview")`。
2. 展示 create、update、delete、unchanged 和每个页面路径；确认后用同一 `plan_id` 调用 `action="apply"`。
3. 这是当前目录的完全镜像：源目录已删除的规则会删除 `wiki/projects/<project>/specs/` 中对应页面，不保留旧经验或历史副本。
4. 项目镜像删除不会删除 `wiki/entities/shared-specs/`。若公共页的 `derived_from` 指向被删除的项目规则，逐页提示用户复核：保留时用 `upsert` 保留正文并移除失效来源，更新时重新提取候选，删除时用 `delete`；每个选择都先 preview 再 apply。

## `extract`：提取公共规范

1. 读取 `<项目规范来源>/**/*.md`，按 heading 或明确规则边界拆成单条候选，逐条判断它是否能跨项目复用；不要因为文件名或目录名自动推广。
2. 为每个候选选择扁平目标 `wiki/entities/shared-specs/<safe-name>.md`，填写 `applies_to`、`conditions`，并把每个来源记录为 `{project, path, rule}`。同一公共页可有多个来源项目，追加来源而不是覆盖来源集合。
3. 先用 `wiki_list` 枚举已有公共页，按稳定路径、`derived_from`、标题和正文分别匹配；不要把不同页面仅凭相似标题合并。对命中的公共页保留已有来源并追加当前来源。
4. 对每个候选分别调用 `wiki_manage_shared_spec(operation="upsert", action="preview", ...)`，展示正文差异、适用范围和来源差异；让用户逐项选择“新建、更新、舍弃”。
5. 只对选择新建或更新的候选用返回的 `plan_id` apply；选择舍弃的候选用同一工具的 `action="discard"` 删除临时计划，不写入 vault，也不留下审核记录。apply 前重新读取候选正文和来源页，若内容变化就重新 preview。

## `import`：公共规范回写项目

1. 先确认当前项目的语言、平台、框架、版本和架构前提，以及要组合的规范组；只推荐所有非空 `applies_to` 标签都匹配的公共页，`conditions` 由 skill 在审核时解释并展示依据。
2. 用 `wiki_list` 找到 `wiki/entities/shared-specs/` 下的公共页，再用每项的 `content_ref` 调 `wiki_get(include_body=true)` 读取正文和 frontmatter；不要用模糊检索代替完整枚举。
3. 默认从 `derived_from` 的项目路径推导 `<项目规范来源>/<relative>`；没有唯一来源时先要求用户指定目标路径。
4. 将公共页正文与本地目标逐项比较：缺失是“新建”，内容相同是“重复/unchanged”，不同且规则互斥是“冲突”，不同但可并存是“相关”；每项展示统一 diff、分类理由和目标路径，逐项询问“新建、更新、舍弃”。
5. 用户确认后重新读取公共页和本地目标，若任一版本改变就重新分类和 preview；仅将仍获选择的项写入本地项目规范目录，使用临时文件替换保证单文件原子写入。正文保持公共页原文，不改写其他本地规则，也不把审核结果写入 vault 或项目历史。
6. 完成后重新读取已写入文件核对内容；若项目有本地 spec index，再按项目现有约定更新它。导入不会反向删除公共页。

## 工具边界

- `wiki_sync_specs` 只负责项目树的 preview/apply 镜像。
- `wiki_manage_shared_spec` 只负责一个公共页的 upsert/delete preview/apply，以及路径、适用范围、来源和 CAS 校验。
- skill 负责读取本地文件、提取候选、枚举公共页、逐项询问和回写项目规范目录；不要把批量审核状态塞进 MCP 计划。

完成标准：用户确认的每个页面都已写入，未确认的页面没有副作用；任一漂移或投影失败都返回明确错误并停在可重试状态。
