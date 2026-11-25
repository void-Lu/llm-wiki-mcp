# LLM Wiki 宿主插件

插件在宿主会话开始时加载知识库工作流，并内置 `llm-wiki-mcp` 服务端 wheel。助手可按任务需要查询；写入和归档使用当前任务已明确授权的范围，调用既有 MCP 工具。任务准备交付时，有具体内容、来源和保存价值，或有明确旧页及退役依据，才提示候选。`shared-spec-vault` 仍通过显式 skill 调用维护公共规范和项目镜像。

## 前提与范围

- 本机 `PATH` 上已安装 [`uv`](https://docs.astral.sh/uv/)。
- 首次启动可联网下载锁定版本的 Python 运行时依赖（如本机缺少 Python 3.11+，`uvx` 会自动下载）。
- 插件内置服务默认只含关键词与 wikilink 检索，不包含可选 `vector`（`sentence-transformers`）依赖；离线依赖打包不在当前范围。

## 构建插件包

构建脚本默认要求 Git 工作区干净，从源码先构建 sdist 再生成 wheel，导出 `uv.lock` 对应的 `constraints.txt`，校验 wheel 顶层白名单，并同步各宿主 manifest 与本地市场版本号：

```powershell
uv run python scripts/build_wiki_plugins.py --output-dir dist/wiki-plugins
$wikiPluginBundle = (Resolve-Path dist/wiki-plugins).Path
```

如需在未提交改动时做本地开发构建，可显式传入 `--allow-dirty`。

输出包含 `codex`、`claude`、`copilot`、`antigravity`、`dsh` 五个自包含安装目录以及本地市场清单。每个宿主目录自带 `server/llm_wiki_mcp-<version>-py3-none-any.whl`、`server/constraints.txt` 与简短的 `README.md`，MCP 配置均通过 `uvx` 与宿主插件目录变量启动，不含本机绝对路径。构建新版本时请使用新的输出目录，升级后重开宿主会话。建议在宿主中只保留一个 `llm-wiki` MCP 注册，避免插件与原手工配置同时暴露同一套工具。

## 首次注册 Vault 并预热缓存

安装插件后（或构建完成后），建议用内置 wheel 执行一次 `init`（已有 `default_vault` 时可执行 `status`）。这会在用户级配置目录写入 `config.yaml`，同时把依赖预热进 `uv` 缓存，避免宿主首次拉起 MCP 时因下载超时：

```powershell
$hostDir = Join-Path $wikiPluginBundle "codex"
$wheel = (Get-ChildItem (Join-Path $hostDir "server") -Filter "*.whl").FullName
$req = Join-Path $hostDir "server\constraints.txt"
uvx --python ">=3.11" --with-requirements $req --from $wheel llm-wiki-mcp init --vault my-wiki --root 'C:\path with spaces\wiki' --default
uvx --python ">=3.11" --with-requirements $req --from $wheel llm-wiki-mcp status
```

## 安装入口

| 入口 | 状态 | 安装或载入 | 启用和检查 |
| --- | --- | --- | --- |
| Codex 桌面 / CLI | 支持 | 从输出根目录注册本地市场：`codex plugin marketplace add "$wikiPluginBundle"` 与 `codex plugin add llm-wiki@llm-wiki-local`。重新打开桌面端后进入本地市场。 | `codex mcp list --json` 可确认 `${PLUGIN_ROOT}` 已展开为插件缓存路径；在桌面端审核并信任 hook 后重开新会话。[OpenAI 打包文档](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks) |
| Claude Code CLI | 支持 | 使用支持 hook 参数数组的 Claude Code 2.1.139 或以上版本。临时试用：`claude --plugin-dir "$wikiPluginBundle/claude"`。持久安装：先运行 `claude plugin marketplace add "$wikiPluginBundle"`，再运行 `claude plugin install llm-wiki@llm-wiki-local`。 | 通过 `claude plugin validate` 或 `/plugin` 查看插件，使用插件命名空间中的 `shared-spec-vault` skill。[Claude Code 插件](https://code.claude.com/docs/en/plugins/overview) |
| GitHub Copilot CLI | 支持 | `copilot --plugin-dir "$wikiPluginBundle/copilot"` 临时载入；支持本地路径安装的版本可执行 `copilot plugin install "$wikiPluginBundle/copilot"`。 | 在同一 CLI 会话确认插件和 MCP。[Copilot CLI 插件参考](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference) |
| VS Code Copilot | 支持 | 选 Copilot harness。运行 **Chat: Install Plugin from Source**，选择输出目录中的 `copilot`。 | 在 Agent Customizations 查看插件和捆绑组件，在实际 Copilot 会话验证 hooks。[VS Code 插件安装](https://code.visualstudio.com/docs/agent-customization/agent-plugins#install-a-plugin-from-source) |
| Google Antigravity CLI | 支持 | `agy plugin install "$wikiPluginBundle/antigravity"`。 | 用 `agy plugin validate --strict`、`agy plugin list` 和新会话的 `/hooks` 查看已载入配置。[Antigravity 插件](https://antigravity.google/docs/plugins) |
| Google Antigravity for VS Code | 暂缓支持 | — | 扩展 1.13.0 实测虽能加载 hook 与 skill，但未向模型暴露插件内置 MCP 工具；本方案不采用写死绝对路径的用户级配置兜底，该入口交由后续流程跟进。 |
| DeepSeek Harness | 支持 | 新建 profile 时，先用 `dsh --profile wiki --from-default-profile web --dump-config` 从 Web 模板初始化；执行 `dsh plugin --profile wiki add "$wikiPluginBundle/dsh"`，再用 `dsh --profile wiki` 启动。已有 `mcpManager` 与 `skills` 的应用 profile 可直接添加。 | 用 `dsh --profile wiki --dump-config` 核对配置层。版本范围与 `dsh.cmd` 路径见生成包中的 `dsh/README.md`。[DSH profile bundle](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md) |

## 验证一次实际会话

1. 在各入口开启新会话，调用 `wiki_status`，核对逻辑 vault 名；再问一个已知知识库能回答的问题，查看是否实际调用 `wiki_query`。
2. 显式调用 `shared-spec-vault`，确认加载该 skill 和参考资料。涉及写入或变更时沿用当前任务授权。
3. 用临时 vault 测试候选和落库。先完成一个能产出可复用结论的示例任务，检查候选和 vault 状态。再明确请求保存相同结论，核对项目页、概念页或实体页及其引用。归档用一篇可复核的旧页测试 plan/apply 及恢复。
4. 原始会话应使用完整、角色标记的可见记录。当前宿主无法提供完整记录时，可导出该会话后显式交由 `wiki_write_note` 保存。普通原始文件使用 `wiki_ingest`。

## 故障排查与卸载

- 若宿主启动 MCP 失败或超时，先确认 `uv` / `uvx` 在宿主进程的 `PATH` 中可见，再按上方命令用内置 wheel 执行一次 `llm-wiki-mcp status` 预热缓存并核对 vault 配置。
- 若 hook 未加载，检查插件启用状态、宿主信任确认以及是否已重开新会话。
- 各宿主均可通过原生插件管理界面停用或卸载；卸载只移除插件文件，用户 vault 数据与平台用户配置目录中的 `config.yaml` 会完整保留。
