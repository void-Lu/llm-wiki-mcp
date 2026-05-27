# NetSuite LLM Wiki MCP

A local MCP (Model Context Protocol) server that gives LLM coding agents full read/write access to an Obsidian-based knowledge wiki. Code facts come from CodeGraph; everything else is ingested, queried, and maintained through MCP tools.

No embeddings, no vector DB, no Chroma. Just Markdown, YAML frontmatter, and `[[wikilinks]]`.

## Install

```bash
python -m pip install -e ".[dev]"
```

## Run

```bash
# Start the MCP server
netsuite-llm-wiki-mcp-server

# Or via the CLI / module
netsuite-llm-wiki-mcp server
python -m netsuite_llm_wiki_mcp.server
```

### CLI

```bash
netsuite-llm-wiki-mcp init --vault <name> --root <path> --default
netsuite-llm-wiki-mcp status
```

## Configuration

The server resolves the wiki root (vault) in this order:

1. Tool parameter `vault_root`
2. Environment variable `NETSUITE_LLM_WIKI_VAULT_ROOT`
3. Global config `config.yaml` → `default_vault`

`config.yaml` lives in the platform user config directory for `netsuite-llm-wiki-mcp`:

- Windows: `%APPDATA%\\netsuite-llm-wiki-mcp\\config.yaml`
- macOS: `~/Library/Application Support/netsuite-llm-wiki-mcp/config.yaml`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/netsuite-llm-wiki-mcp/config.yaml`

For development and tests, `NETSUITE_LLM_WIKI_CONFIG_DIR` and `NETSUITE_LLM_WIKI_USER_DATA_DIR` can override config/data directories.

### MCP Client Setup

Add to your MCP client config (e.g. Claude Code `settings.json`):

```json
{
  "mcpServers": {
    "netsuite-wiki": {
      "command": "netsuite-llm-wiki-mcp-server"
    }
  }
}
```

## Tools

### Ingest

| Tool | Description |
|------|-------------|
| `wiki_init` | Create the wiki directory structure in an Obsidian vault |
| `wiki_ingest` | Ingest a CodeGraph source into wiki pages |
| `wiki_ingest_llm` | Three-stage LLM ingest: `prepare_analysis` → `prepare_generation` → `apply_generation` |
| `wiki_rescan` | Re-scan a source; skip if unchanged (SHA256), refresh raw snapshot if changed |
| `wiki_ingest_batch` | Persistent ingest queue: enqueue / next / complete / fail / retry / cancel / clear_done |

### Query

| Tool | Description |
|------|-------------|
| `wiki_query` | Keyword + CJK bigram search → graph expansion → context-budgeted output |
| `wiki_query_debug` | Same as query but returns per-result scores and graph expansion reasons |

### Maintenance

| Tool | Description |
|------|-------------|
| `wiki_lint` | Structural health check: frontmatter, broken links, source traceability, cache integrity, orphan pages |
| `wiki_enrich` | Two-stage wikilink enrichment: prepare (returns LLM prompt) → apply (inserts links) |
| `wiki_page_merge` | Merge pages: frontmatter union + locked field protection + optional LLM body merge |
| `wiki_dedup` | Duplicate detection and merge: detect → confirm → merge (three stages) |
| `wiki_insights` | Graph insights: orphan pages, bridge nodes, surprising cross-type connections, Louvain communities |
| `wiki_delete_source` | Delete a source with cascade cleanup: derived pages, cross-references, cache |
| `wiki_changelog` | Recent wiki log entries |

### Research & Notes

| Tool | Description |
|------|-------------|
| `wiki_research` | Deep research synthesis: search results → LLM synthesis → `wiki/queries/` page |
| `wiki_write_note` | Write a human-curated wiki note; replaces the old `save_obsidian_note` public tool name |

## Wiki Structure

```
vault_root/
├── purpose.md              # Research scope and key questions
├── schema.md               # Page types, frontmatter spec, maintenance rules
├── raw/
│   ├── sources/            # Immutable source snapshots (LLM read-only)
│   └── assets/             # Binary assets
├── wiki/
│   ├── index.md            # Content directory, LLM navigation entry
│   ├── log.md              # Append-only operation log
│   ├── overview.md         # Auto-generated summary
│   ├── projects/<project>/ # Project-scoped pages
│   │   ├── index.md
│   │   ├── code/           # CodeGraph-derived facts
│   │   ├── decisions/
│   │   ├── troubleshooting/
│   │   └── requirements/
│   ├── concepts/           # Domain knowledge (by domain subdirectory)
│   ├── sources/            # Source summary pages
│   ├── queries/            # Research synthesis pages
│   ├── synthesis/          # Cross-cutting analysis
│   └── comparisons/        # Side-by-side comparisons
├── .obsidian/              # Obsidian app config
└── .llm-wiki/              # Runtime state (ingest cache, queue)
```

## Data Flow

### CodeGraph Ingest

```
CodeGraph CLI → raw/sources/codegraph/<project>/<source_name>/
             → wiki/sources/ (source summary)
             → wiki/projects/<project>/code/ (code fact pages)
             → index + overview + log update
```

### LLM Staged Ingest

```
prepare_analysis  → returns analysis prompt (agent sends to LLM)
prepare_generation → returns generation prompt (agent sends to LLM)
apply_generation  → writes wiki pages from LLM JSON output
```

Cache: `.llm-wiki/ingest-cache/<project>/<source_name>.json` (skips unchanged sources by SHA256).

### Query Pipeline

```
Keywords / CJK bigrams → candidate pages
  → graph expansion (wikilink, shared source, common neighbor, same type)
  → context budget allocation
  → numbered-reference context pack
```

## Development

```bash
# Run all tests
pytest

# Single test file
pytest tests/test_wiki_query.py

# Single test function
pytest tests/test_wiki_query.py::test_function_name -v
```

### Conventions

- Python 3.11+, `src/` layout, minimal dependencies (`mcp` + `PyYAML`)
- Generated pages may only overwrite pages with `generated: true` in frontmatter
- Human-authored pages are never silently overwritten
- All writes are confined to the vault root; paths under `wiki/concepts/` and `wiki/projects/` follow fixed structure
- Sensitive data (phone, email, tokens) is redacted before writing
- Windows path safety: illegal chars, ADS colons, reserved device names, control chars, trailing dots/spaces
- 不引入 Chroma、sentence-transformers 或 embedding 模型
- 不创建 `.rag-index/` 或 `.models/`

## License

MIT
