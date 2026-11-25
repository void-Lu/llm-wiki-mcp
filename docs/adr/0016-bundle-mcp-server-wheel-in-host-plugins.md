---
status: accepted
date: 2026-11-25
---

# 宿主插件内置 MCP 服务 wheel 并通过 uvx 启动

本决策取代 `.trellis/tasks/11-25-cross-harness-wiki-plugin/design.md` 中“插件复用外部已安装的 `llm-wiki-mcp-server`”的原约定。

## 决策

1. **自包含打包**：每个宿主插件目录自带 `server/llm_wiki_mcp-<version>-py3-none-any.whl` 与由 `uv.lock` 导出的 `server/constraints.txt`（不含 `dev` 与可选 `vector` 依赖）。构建时先出 sdist 再由 sdist 构建 wheel，并对 wheel 顶层条目执行 `src/` 白名单校验，杜绝残留构建产物（如已移除的 `codegraph`）混入。
2. **统一可移植启动**：各宿主 MCP 配置统一改写为 `uvx --python ">=3.11" --with-requirements "<ROOT>/server/constraints.txt" --from "<ROOT>/server/<wheel>" llm-wiki-mcp-server`（Claude 使用 `${CLAUDE_PLUGIN_ROOT}`，Codex/Copilot/Antigravity/DSH 使用 `${PLUGIN_ROOT}`；DSH 由 `index.mjs` + `adapter.mjs` 在加载时按插件目录展开）。采用 `--with-requirements` 而非 `--constraints`，因为 `uv` 的 `--constraints` 在 Windows 下会对含空格路径按空白拆分；不传 `--no-python-downloads`，允许 `uvx` 首次自动准备 Python 3.11+。
3. **移除外部模式与绝对路径兜底**：删除构建脚本的 `--server-command` / `--server-arg`；任何宿主均不接受写死本机绝对路径的配置文件或兜底指引。无法以原生可移植方式暴露插件内置 MCP 的宿主入口（当前为 Google Antigravity for VS Code 扩展）暂缓支持，交由后续流程跟进。
4. **构建门禁与目录裁剪**：构建默认拒绝脏 Git 工作区，仅显式传 `--allow-dirty` 放行。按宿主裁剪多余文件：`session_hook.py` 仅进入需要 Python hook 的宿主，`session-guidance.md` 仅进入 DSH，`skills/shared-spec-vault/agents/openai.yaml` 仅进入 Codex；各宿主附带面向插件用户的简短 `README.md`，仓库 `README.md` 与 `CHANGELOG.md` 不进包。

## 后果

- 插件包可跨机器独立安装，不再依赖克隆仓库或手工 `uv sync`，vault 配置仍存放在平台用户配置目录。
- 首次使用前需联网并建议通过内置 wheel 执行一次 `llm-wiki-mcp init`（或 `status`）完成依赖预热，避免首次拉起超过宿主 MCP 启动超时。
- 离线安装依赖打包、插件模式 `vector` 检索及 Antigravity for VS Code 原生 MCP 暴露均留待后续需求处理。
