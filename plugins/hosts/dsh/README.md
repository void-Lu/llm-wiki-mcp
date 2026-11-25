# DSH bundle: llm-wiki (v{{VERSION}})

This is a DeepSeek Harness profile bundle. The shared assembler supplies
`session-guidance.md`, `server/{{WHEEL_NAME}}`, `server/constraints.txt`, and
the complete `skills/shared-spec-vault/` tree at this package root. DSH reads
the guidance file directly; it does not ship or execute the Python hook.

## Prerequisites and vault setup

`uv` must be available on `PATH`, with network access on first run so `uvx` can
download pinned runtime dependencies (and Python 3.11+ if not already present).
The bundled server includes lexical and wikilink retrieval only (no optional
`vector` extra).

On a fresh machine with no `config.yaml` and no `LLM_WIKI_VAULT_ROOT`, starting
the bundled MCP server automatically creates `~/Documents/llm-wiki-vault`
(`%USERPROFILE%\Documents\llm-wiki-vault` on Windows), writes user-level
`config.yaml`, and persists `LLM_WIKI_VAULT_ROOT` on Windows. Existing user
configuration is always respected. All runtime state and caches stay inside
`<vault>/.llm-wiki/`.

You can also run `init` (zero-arg or custom `--vault`/`--root`, auto-creating
missing folders) to warm the `uv` cache before opening DSH, or rename an
existing vault with `config rename-vault`:

```sh
uvx --python ">=3.11" --with-requirements "/path/to/assembled/dsh/server/constraints.txt" \
  --from "/path/to/assembled/dsh/server/{{WHEEL_NAME}}" \
  llm-wiki-mcp init

uvx --python ">=3.11" --with-requirements "/path/to/assembled/dsh/server/constraints.txt" \
  --from "/path/to/assembled/dsh/server/{{WHEEL_NAME}}" \
  llm-wiki-mcp config rename-vault --to my-wiki
```

## Install in an isolated profile

Use the assembled DSH output directory as the local package path. To create
a fresh profile, start from the built-in `web` template, which supplies the
MCP manager and local credentials service.

```sh
dsh --profile wiki-check --from-default-profile web --dump-config
dsh plugin --profile wiki-check add /path/to/assembled/dsh
dsh --profile wiki-check
```

For an existing app profile that provides `mcpManager` and `skills`, use the
`plugin add` step directly. The default `tui` and `headless` templates in
`0.2.2-rc.5` omit the MCP manager. A custom profile can provide it using the
same credentials and MCP manager rows as `dsh --profile web --dump-default-config`.
On Windows, if `dsh` is absent from `PATH`, the desktop installer includes
`resources/runtime/cli/bin/dsh.cmd` under its installation directory.

The plugin registers the local stdio MCP server named `llm-wiki` and adds the
bundled skill root to DSH's filesystem skill registry. At load time `index.mjs`
expands `${PLUGIN_ROOT}` in `mcp.json` to the installed bundle directory and
launches the bundled wheel via `uvx` without machine-specific paths in the
manifest.

Remove it from the isolated profile with:

```sh
dsh plugin --profile wiki-check remove dsh-llm-wiki
```

Then remove the temporary profile using the DSH profile management UI or CLI.
The profile is separate from the user's default profile, and the vault remains
owned by the existing LLM Wiki service.

## Lifecycle boundary

The bundle listens only for `agent/created`. It registers the shared guidance
through that agent's `agent.ctx.systemPrompt.section()`, so the guidance is an
Agent scoped system prompt rather than a role=user message. DSH documents that
the event is awaited before queued input is released and that system prompt
sections render as system-role messages for model requests. A WeakSet makes a
duplicate event for the same live agent a no-op. The lifecycle listener does
not inspect conversation history, access the Wiki vault, or perform
filesystem/network operations. The guidance explicitly says it grants no user
authorization; the assistant decides whether to use MCP based on the current
user request.

The first-party callback signature is
`agent/created(this: Scoped<Agent>, payload: { agent: Agent; source: SessionStartSource; signal?: AbortSignal }): undefined | Promise<undefined>`.
The agent API exposes `agent.ctx`; system prompt registration is
`section(section: PromptSection): () => void`. Registration through the
agent-scoped context owns the section for that agent, and the system-prompt
package renders assembled sections as a system-role message. See the DSH
[agent runtime event types](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent/src/runtime-types.ts#L1509-L1533),
[system prompt API and role contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/system-prompt/README.md).
The bundle and profile format follow DSH's
[plugin publishing guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md#L178-L219).

## Runtime compatibility

The DSH engine and peers accept `0.2.2-rc.5` and `0.2.3-alpha.2`. Run them with
matching peer versions from the same DSH release. The former requires Node
24.9.0 or newer. The latter requires Node 24.22.3 or newer in the 24.x line,
or 26.13.1 or newer.

### Local verification evidence (2026-11-25)

The desktop-bundled CLI reports `0.2.2-rc.5`. A disposable profile accepted the
bundle. In a native session that enabled the official credentials and MCP
manager services, the plugin applied and ran its `agent/created` callback.
The isolated model request stopped at `MISSING_CREDENTIAL`. A full model
turn with Wiki tools and a native run on `0.2.3-alpha.2` remain open.
