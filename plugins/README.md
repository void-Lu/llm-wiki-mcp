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

## First-time setup (register vault & warm cache)

Run `init` once against the bundled wheel. This registers your Obsidian wiki
vault in your user config directory and pre-warms the `uv` cache so the host
starts the MCP server quickly without hitting startup timeouts:

```powershell
uvx --python ">=3.11" --with-requirements "<plugin-dir>/server/constraints.txt" `
    --from "<plugin-dir>/server/{{WHEEL_NAME}}" `
    llm-wiki-mcp init --vault my-wiki --root 'C:\path\to\wiki' --default
```

Replace `<plugin-dir>` with this installed plugin directory (and adjust quotes
for your shell on macOS/Linux). If you already have a registered `default_vault`,
you can warm the cache with `llm-wiki-mcp status` instead.

## Verify

1. Open a new session in your host after installing or upgrading this plugin.
2. Call `wiki_status` and confirm the expected logical vault is reported.
3. Ask a question covered by your wiki to confirm `wiki_query` runs, or invoke
   `shared-spec-vault` explicitly when maintaining shared specs.

## Upgrade & uninstall

- After upgrading to a newer plugin version, restart or open a new host session.
- Uninstalling the plugin removes only the host plugin files; your vault data
  and user-level `config.yaml` remain untouched.
