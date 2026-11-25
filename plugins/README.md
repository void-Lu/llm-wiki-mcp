# LLM Wiki Plugin (v{{VERSION}})

Self-contained local plugin for the `llm-wiki` MCP server, session workflow hook,
and `shared-spec-vault` skill.

## Included components

- `server/{{WHEEL_NAME}}` — bundled `llm-wiki-mcp` Python package (lexical &
  wikilink retrieval; excludes optional `vector` / `sentence-transformers`
  dependencies)
- `server/constraints.txt` — pinned runtime dependencies exported from `uv.lock`
- `session_hook.py` — `SessionStart` workflow guidance hook
- `skills/shared-spec-vault/` — explicit skill for shared spec vault maintenance

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) available on `PATH`
- Network access on first run so `uvx` can download pinned dependencies (and
  Python 3.11+ if not already installed locally)

## Zero-config vault & optional setup

- **Fresh machine (no manual `init` required)**: when the MCP server starts with
  neither `config.yaml` (`default_vault`) nor `LLM_WIKI_VAULT_ROOT`, it
  automatically creates `%USERPROFILE%\Documents\llm-wiki-vault` (or
  `~/Documents/llm-wiki-vault` on macOS/Linux), registers it in user
  `config.yaml`, and on Windows persists the User environment variable
  `LLM_WIKI_VAULT_ROOT`.
- **Existing vault respected**: if you already configured `default_vault` in
  `config.yaml` or set `LLM_WIKI_VAULT_ROOT`, the server uses your existing
  configuration without creating the default directory.
- **Runtime state stays inside your vault**: SQLite state, search indexes,
  caches, and archives are stored inside `<vault>/.llm-wiki/` and
  `<vault>/archives/`, never inside the plugin directory.

Optional one-click `init` (also pre-warms the `uv` cache to avoid first-launch
timeouts; missing target directories are created automatically):

```powershell
# Zero-arg default init (or add --vault <name> --root '<path>' --default)
uvx --python ">=3.11" --with-requirements "<plugin-dir>/server/constraints.txt" `
    --from "<plugin-dir>/server/{{WHEEL_NAME}}" `
    llm-wiki-mcp init

# Rename the local vault folder, config.yaml entry, and LLM_WIKI_VAULT_ROOT
uvx --python ">=3.11" --with-requirements "<plugin-dir>/server/constraints.txt" `
    --from "<plugin-dir>/server/{{WHEEL_NAME}}" `
    llm-wiki-mcp config rename-vault --to my-wiki
```

Pass `--keep-dir` to `rename-vault` if you only want to rename the logical name
in `config.yaml` without renaming the folder on disk.

## Using alongside `cc-switch`

Install this plugin natively in each host for MCP + `SessionStart` hook +
`shared-spec-vault`, and use `cc-switch` only for switching API/model providers.
Do **not** add a duplicate `llm-wiki` entry in `cc-switch`'s MCP panel.

## Verify

1. Open a new session in your host after installing or upgrading this plugin.
2. Call `wiki_status` and confirm the expected logical vault is reported.
3. Ask a question covered by your wiki to confirm `wiki_query` runs, or invoke
   `shared-spec-vault` explicitly when maintaining shared specs.

## Upgrade & uninstall

- After upgrading to a newer plugin version, restart or open a new host session.
- Uninstalling the plugin removes only the host plugin files; your vault data
  and user-level `config.yaml` remain untouched.
