from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from netsuite_rag_mcp.models import RagConfig, SourceConfig
from netsuite_rag_mcp.runtime_config import RuntimeConfig, RuntimeConfigError, resolve_runtime_config

DEFAULT_INCLUDE = ["projects", "knowledge"]
DEFAULT_EXCLUDE = [".git", ".obsidian", ".superpowers", ".rag-index"]
DEFAULT_CHROMA_PATH = ".rag-index/chroma"
DEFAULT_COLLECTION = "netsuite_knowledge"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_CACHE_PATH = ".models"


def _resolve_path(value: str | Path, base: Path) -> Path:
    """Resolve a path relative to base, handling '.' and absolute paths."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _migrate_v1_to_v2(raw: dict[str, Any], resolved_root: Path) -> dict[str, Any]:
    """Migrate v1 flat schema to v2 sources[] schema."""
    include = raw.get("include", list(DEFAULT_INCLUDE))
    exclude = raw.get("exclude", list(DEFAULT_EXCLUDE))
    collection = raw.get("collection_name", DEFAULT_COLLECTION)

    v2: dict[str, Any] = {
        "schema_version": 2,
        "workspace_root": raw.get("vault_root", "."),
        "index": {
            "chroma_path": raw.get("chroma_path", DEFAULT_CHROMA_PATH),
            "embedding_model": raw.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
            "embedding_cache_path": raw.get("embedding_cache_path", DEFAULT_EMBEDDING_CACHE_PATH),
            "collections": {"default": collection},
        },
        "sources": [
            {
                "source_name": "obsidian",
                "source_kind": "note",
                "root": raw.get("vault_root", "."),
                "include": include,
                "exclude": exclude,
                "file_types": ["md"],
                "parser": "markdown_frontmatter_h2",
                "collection": collection,
                "authority": "curated_note_source",
            }
        ],
    }
    return v2


def _parse_sources(sources: list[dict[str, Any]], resolved_root: Path) -> list[SourceConfig]:
    """Parse v2 sources[] list into list[SourceConfig]."""
    result: list[SourceConfig] = []
    for src in sources:
        root_path = _resolve_path(src.get("root", "."), resolved_root)
        result.append(
            SourceConfig(
                source_name=src["source_name"],
                source_kind=src["source_kind"],
                root=root_path,
                include=list(src.get("include", DEFAULT_INCLUDE)),
                exclude=list(src.get("exclude", DEFAULT_EXCLUDE)),
                file_types=list(src.get("file_types", ["md"])),
                parser=src.get("parser", "markdown_frontmatter_h2"),
                collection=src["collection"],
                authority=src.get("authority", "curated_note_source"),
                file_exclude_patterns=list(src.get("file_exclude_patterns", [])),
                library_exclude_patterns=list(src.get("library_exclude_patterns", [])),
                utility_allowlist=list(src.get("utility_allowlist", [])),
            )
        )
    return result


def load_config(
    vault_root: str | Path,
    config_path: str | Path | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> RagConfig:
    explicit_config_path = Path(config_path).expanduser() if config_path is not None else None
    explicit_config_exists = explicit_config_path is not None and explicit_config_path.exists()
    runtime = runtime_config or resolve_runtime_config(
        vault_root_arg=vault_root,
        require_sources_config=not explicit_config_exists,
    )
    root = runtime.vault_root
    path = explicit_config_path if explicit_config_path is not None else runtime.sources_config_path
    raw: dict[str, Any] = {}
    if not path.exists():
        raise RuntimeConfigError(
            f"Vault root {root} does not contain required source config {path}.",
            code="missing_sources_config",
        )
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = loaded if isinstance(loaded, dict) else {}

    # Detect schema version: v1 has no schema_version (or has vault_root at top level)
    is_v1 = "schema_version" not in raw

    if is_v1:
        # Auto-migrate v1 flat config to v2 sources[] schema
        migrated = _migrate_v1_to_v2(raw, root)
        raw = migrated

    # Parse v2 config
    workspace_root_value = raw.get("workspace_root", raw.get("vault_root", "."))
    resolved_root = _resolve_path(workspace_root_value, root)

    # Parse index section
    index = raw.get("index", {})
    chroma_path = runtime.chroma_path
    embedding_model = str(index.get("embedding_model", raw.get("embedding_model", DEFAULT_EMBEDDING_MODEL)))
    embedding_cache_path = runtime.embedding_cache_path
    collections = index.get("collections", {})
    default_collection = str(collections.get("default", raw.get("collection_name", DEFAULT_COLLECTION)))

    # Parse sources[]
    sources_raw = raw.get("sources", [])
    sources = _parse_sources(sources_raw, resolved_root)

    # Derive backward-compat flat fields from first source (if available)
    first_source = sources[0] if sources else None
    include_paths = [(resolved_root / p) for p in (first_source.include if first_source else DEFAULT_INCLUDE)]
    exclude_names = set(first_source.exclude if first_source else DEFAULT_EXCLUDE)
    collection_name = first_source.collection if first_source else default_collection

    return RagConfig(
        vault_root=resolved_root,
        include_paths=include_paths,
        exclude_names=exclude_names,
        chroma_path=chroma_path,
        collection_name=collection_name,
        embedding_model=embedding_model,
        embedding_cache_path=embedding_cache_path,
        manifest_path=runtime.manifest_path,
        sources=sources,
    )
