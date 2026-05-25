from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.chunker import chunk_document
from netsuite_rag_mcp.chunker_xml_json import chunk_json_config, chunk_xml_document
from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.manifest import (
    ManifestEntry,
    compute_file_hash,
    manifest_key,
    read_manifest,
    write_manifest,
)
from netsuite_rag_mcp.models import Chunk, RagConfig, SourceConfig
from netsuite_rag_mcp.parser import parse_code_file, parse_markdown_file
from netsuite_rag_mcp.parser_xml_json import parse_json_config, parse_xml_file
from netsuite_rag_mcp.vector_store import ChromaVectorStore, Embedder, SentenceTransformerEmbedder

# ── Parser/chunker routing table ─────────────────────────────────────────────
# Maps (parser_name, file_extension) → (parse_func_name, chunk_func_name)
_ROUTE_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    ("markdown_frontmatter_h2", ".md"): ("parse_markdown_file", "chunk_document"),
    ("suitescript_code_and_config", ".js"): ("parse_code_file", "chunk_code_document"),
    ("suitescript_code_and_config", ".ts"): ("parse_code_file", "chunk_code_document"),
    ("suitescript_code_and_config", ".xml"): ("parse_xml_file", "chunk_xml_document"),
    ("suitescript_code_and_config", ".json"): ("parse_json_config", "chunk_json_config"),
}


def _route_to_parser_chunker(parser: str, extension: str) -> tuple[str, str] | None:
    """Route a (parser_name, file_extension) pair to the correct parser and chunker function names.

    Args:
        parser: Parser name from SourceConfig (e.g. "markdown_frontmatter_h2").
        extension: Lowercase file extension including dot (e.g. ".md", ".js").

    Returns:
        Tuple of (parse_func_name, chunk_func_name) or None if no route exists.
    """
    return _ROUTE_TABLE.get((parser, extension))


def _collect_source_files(source: SourceConfig, vault_root: Path) -> list[Path]:
    """Collect all files for a source within its root, matching file_types and include/exclude rules.

    Args:
        source: SourceConfig defining root, include, exclude, file_types.
        vault_root: Resolved vault root path.

    Returns:
        Sorted list of file paths matching the source criteria.
    """
    source_root = source.root
    if not source_root.exists():
        return []

    # Build include directories
    include_dirs: list[Path] = []
    for inc in source.include:
        inc_path = source_root / inc
        if inc_path.exists():
            include_dirs.append(inc_path)

    # If no include dirs resolve, fall back to source_root itself
    if not include_dirs:
        include_dirs = [source_root] if source_root.exists() else []

    # Build exclude set
    exclude_names = set(source.exclude)

    collected: list[Path] = []
    extensions = {f".{ft.lstrip('.')}" for ft in source.file_types}

    for inc_dir in include_dirs:
        for ext in extensions:
            for f in inc_dir.rglob(f"*{ext}"):
                if _should_exclude(f, inc_dir, exclude_names):
                    continue
                collected.append(f)

    return sorted(collected)


def _parse_and_chunk(
    file_path: Path,
    source: SourceConfig,
    vault_root: Path,
) -> tuple[Any, list[Chunk]] | None:
    """Parse a file and chunk it using the correct parser/chunker pair based on routing.

    Args:
        file_path: Absolute path to the file to parse.
        source: SourceConfig providing parser name and routing info.
        vault_root: Resolved vault root path.

    Returns:
        Tuple of (SourceDocument, list[Chunk]), or None if the file cannot be parsed.
    """
    extension = file_path.suffix.lower()
    route = _route_to_parser_chunker(source.parser, extension)
    if route is None:
        return None

    parse_func_name, chunk_func_name = route

    # ── Parse ──
    if parse_func_name == "parse_markdown_file":
        document = parse_markdown_file(file_path, vault_root)
    elif parse_func_name == "parse_code_file":
        document = parse_code_file(file_path, source_name=source.source_name, repo_root=source.root)
    elif parse_func_name == "parse_xml_file":
        document = parse_xml_file(file_path, source_name=source.source_name, repo_root=source.root)
    elif parse_func_name == "parse_json_config":
        document = parse_json_config(file_path, source_name=source.source_name, repo_root=source.root)
    else:
        return None

    if document is None:
        return None

    # ── Inject source metadata from SourceConfig ──
    document = _inject_source_metadata(document, source)

    # ── Chunk ──
    if chunk_func_name == "chunk_document":
        chunks = chunk_document(document)
    elif chunk_func_name == "chunk_code_document":
        from netsuite_rag_mcp.chunker import chunk_code_document
        chunks = chunk_code_document(document)
    elif chunk_func_name == "chunk_xml_document":
        chunks = chunk_xml_document(document)
    elif chunk_func_name == "chunk_json_config":
        chunks = chunk_json_config(document)
    else:
        return None

    # ── Inject source metadata into chunks ──
    chunks = [_inject_chunk_metadata(c, source, document.file_hash) for c in chunks]

    return document, chunks


def _inject_source_metadata(document: Any, source: SourceConfig) -> Any:
    """Inject source_kind and source_name from SourceConfig into a SourceDocument.

    The parser may already set these (e.g., parse_code_file sets source_kind='code'),
    but SourceConfig is the authority.
    """
    return document.__class__(
        **{k: v for k, v in document.__dict__.items() if k != "source_kind" and k != "source_name"},
        source_kind=source.source_kind,
        source_name=source.source_name,
    )


def _inject_chunk_metadata(chunk: Chunk, source: SourceConfig, file_hash: str) -> Chunk:
    """Inject source_kind, source_name, and file_hash into a Chunk."""
    return Chunk(
        id=chunk.id,
        doc_id=chunk.doc_id,
        chunk_index=chunk.chunk_index,
        source_path=chunk.source_path,
        heading=chunk.heading,
        text=chunk.text,
        metadata=chunk.metadata,
        function_name=chunk.function_name,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        source_kind=source.source_kind,
        source_name=source.source_name,
        file_hash=file_hash,
    )


def _should_exclude(file_path: Path, base_path: Path, exclude_names: set[str]) -> bool:
    """Check if a file should be excluded based on path components."""
    try:
        relative = file_path.relative_to(base_path)
    except ValueError:
        return True
    for part in relative.parts:
        if part in exclude_names:
            return True
    return False


def _index_source(
    source: SourceConfig,
    vault_root: Path,
    manifest: dict[str, ManifestEntry],
    vector_store: ChromaVectorStore,
    mode: str,
) -> dict[str, Any]:
    """Index a single source.

    Args:
        source: SourceConfig for the source to index.
        vault_root: Resolved vault root.
        manifest: Manifest dict (mutable, updated in place).
        vector_store: ChromaVectorStore instance.
        mode: "full" or "incremental".

    Returns:
        Dict with indexed, skipped, deleted, errors, chunks counts.
    """
    indexed = 0
    skipped = 0
    deleted = 0
    chunk_count = 0
    errors: list[dict[str, str]] = []

    # Collect files for this source
    files = _collect_source_files(source, vault_root)
    total_files = len(files)

    for file_path in files:
        try:
            relative_path = file_path.resolve().relative_to(source.root.resolve()).as_posix()
            file_stat = file_path.stat()
            file_mtime = file_stat.st_mtime
            file_size = file_stat.st_size
            key = manifest_key(source.source_name, source.source_kind, relative_path)

            # ── Three-step incremental check ──
            existing = manifest.get(key)

            if mode == "incremental" and existing is not None:
                # Step 1: Fast check — compare mtime + size (skip hash if both match)
                if existing.mtime == file_mtime and existing.size == file_size:
                    skipped += 1
                    continue

                # Step 2: Slow check — compute hash only for candidates
                current_hash = compute_file_hash(file_path)
                if current_hash == existing.file_hash:
                    # False positive mtime/size change — file content unchanged
                    # Update mtime and size in manifest to avoid future false positives
                    manifest[key] = ManifestEntry(
                        doc_id=existing.doc_id,
                        source_name=existing.source_name,
                        source_kind=existing.source_kind,
                        relative_path=existing.relative_path,
                        mtime=file_mtime,
                        size=file_size,
                        file_hash=existing.file_hash,
                        chunk_count=existing.chunk_count,
                        indexed_at=existing.indexed_at,
                    )
                    skipped += 1
                    continue

            # Step 3: File changed or new, or full mode — compute hash and re-index
            file_hash = compute_file_hash(file_path)

            # ── Parse and chunk ──
            result = _parse_and_chunk(file_path, source, vault_root)
            if result is None:
                skipped += 1
                continue

            document, chunks = result

            # ── Delete old chunks and upsert new ones ──
            vector_store.delete_doc(document.doc_id)
            vector_store.upsert_chunks(chunks)

            # ── Update manifest ──
            manifest[key] = ManifestEntry(
                doc_id=document.doc_id,
                source_name=source.source_name,
                source_kind=source.source_kind,
                relative_path=relative_path,
                mtime=file_mtime,
                size=file_size,
                file_hash=file_hash,
                chunk_count=len(chunks),
                indexed_at=datetime.now(timezone.utc).isoformat(),
            )

            indexed += 1
            chunk_count += len(chunks)

        except Exception as exc:
            rel = ""
            try:
                rel = str(file_path.resolve().relative_to(vault_root.resolve()))
            except (ValueError, OSError):
                rel = str(file_path)
            errors.append({"file": rel, "error": str(exc)})

    # ── Handle deleted files (files in manifest but no longer on disk) ──
    if mode in ("full", "incremental"):
        source_prefix = f"{source.source_name}:{source.source_kind}:"
        current_files = {f.resolve().relative_to(source.root.resolve()).as_posix() for f in files}
        keys_to_delete: list[str] = []
        for key, entry in list(manifest.items()):
            if key.startswith(source_prefix) and entry.relative_path not in current_files:
                vector_store.delete_doc(entry.doc_id)
                keys_to_delete.append(key)
                deleted += 1
        for key in keys_to_delete:
            del manifest[key]

    return {
        "indexed": indexed,
        "skipped": skipped,
        "deleted": deleted,
        "chunks": chunk_count,
        "errors": errors,
        "total_files": total_files,
    }


def index_all(
    vault_root: str | Path,
    mode: str = "full",
    config: RagConfig | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Index all sources defined in config.

    Returns per-source statistics:
    {
        "sources": {
            "obsidian": {"indexed": 5, "skipped": 10, "deleted": 2, "errors": 0},
            "netsuite_repo": {"indexed": 20, "skipped": 5, "deleted": 1, "errors": 0}
        },
        "total_indexed": 25,
        "total_skipped": 15,
        "total_deleted": 3,
        "total_errors": 0
    }
    """
    # Validate mode before any I/O
    if mode not in ("full", "incremental"):
        raise ValueError(f"Invalid mode: {mode}. Must be 'full' or 'incremental'")

    vault_root = Path(vault_root).expanduser().resolve()

    # Load configuration
    if config is None:
        config = load_config(vault_root)

    # Create embedder if not provided
    if embedder is None:
        selected_embedder = SentenceTransformerEmbedder(
            config.embedding_model, cache_folder=config.embedding_cache_path
        )
    else:
        selected_embedder = embedder

    # Create vector store
    vector_store = ChromaVectorStore(
        config.chroma_path, config.collection_name, selected_embedder
    )

    # Initialize manifest
    manifest_path = config.manifest_path
    manifest: dict[str, ManifestEntry] = {}

    # Handle full vs incremental mode
    if mode == "full":
        vector_store.reset()
        manifest = {}
    elif mode == "incremental":
        manifest = read_manifest(manifest_path)

    # Index each source
    sources_stats: dict[str, dict[str, Any]] = {}
    total_indexed = 0
    total_skipped = 0
    total_deleted = 0
    total_errors = 0

    for source in config.sources:
        stats = _index_source(source, vault_root, manifest, vector_store, mode)
        sources_stats[source.source_name] = stats
        total_indexed += stats["indexed"]
        total_skipped += stats["skipped"]
        total_deleted += stats["deleted"]
        total_errors += len(stats["errors"])

    # Save manifest
    write_manifest(manifest_path, manifest)

    return {
        "sources": sources_stats,
        "total_indexed": total_indexed,
        "total_skipped": total_skipped,
        "total_deleted": total_deleted,
        "total_errors": total_errors,
    }


def index_sources(
    vault_root: str | Path,
    source_names: list[str] | None = None,
    source_kind: str | None = None,
    mode: str = "full",
    config: RagConfig | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Index specific sources by name or kind.

    Args:
        source_names: Optional list of source names to index (e.g., ["obsidian", "netsuite_repo"]).
        source_kind: Optional kind filter ("note" or "code").
        mode: "full" to reset and re-index, "incremental" for delta.
        config: Optional pre-loaded RagConfig.
        embedder: Optional embedder instance.

    Returns per-source statistics same as index_all.
    """
    if mode not in ("full", "incremental"):
        raise ValueError(f"Invalid mode: {mode}. Must be 'full' or 'incremental'")

    vault_root = Path(vault_root).expanduser().resolve()

    # Load configuration
    if config is None:
        config = load_config(vault_root)

    has_filters = source_names is not None or source_kind is not None
    if not has_filters:
        return index_all(vault_root, mode=mode, config=config, embedder=embedder)

    # Filter sources
    filtered_sources: list[SourceConfig] = []
    for source in config.sources:
        if source_names is not None and source.source_name not in source_names:
            continue
        if source_kind is not None and source.source_kind != source_kind:
            continue
        filtered_sources.append(source)

    if mode == "full":
        if embedder is None:
            selected_embedder = SentenceTransformerEmbedder(
                config.embedding_model, cache_folder=config.embedding_cache_path
            )
        else:
            selected_embedder = embedder

        vector_store = ChromaVectorStore(
            config.chroma_path, config.collection_name, selected_embedder
        )
        manifest_path = config.manifest_path
        manifest = read_manifest(manifest_path)

        sources_stats: dict[str, dict[str, Any]] = {}
        total_indexed = 0
        total_skipped = 0
        total_deleted = 0
        total_errors = 0

        for source in filtered_sources:
            stats = _index_source(source, vault_root, manifest, vector_store, mode)
            sources_stats[source.source_name] = stats
            total_indexed += stats["indexed"]
            total_skipped += stats["skipped"]
            total_deleted += stats["deleted"]
            total_errors += len(stats["errors"])

        write_manifest(manifest_path, manifest)

        return {
            "sources": sources_stats,
            "total_indexed": total_indexed,
            "total_skipped": total_skipped,
            "total_deleted": total_deleted,
            "total_errors": total_errors,
        }

    # Delegate to index_all with a modified config containing only filtered sources
    filtered_config = RagConfig(
        vault_root=config.vault_root,
        include_paths=config.include_paths,
        exclude_names=config.exclude_names,
        chroma_path=config.chroma_path,
        collection_name=config.collection_name,
        embedding_model=config.embedding_model,
        embedding_cache_path=config.embedding_cache_path,
        manifest_path=config.manifest_path,
        sources=filtered_sources,
    )

    return index_all(vault_root, mode=mode, config=filtered_config, embedder=embedder)


def index_vault(
    vault_root: str | Path, mode: str = "incremental", embedder: Embedder | None = None
) -> dict[str, Any]:
    """Index Obsidian vault with full or incremental mode.

    Backward-compatible wrapper around index_all. Returns legacy format
    with indexed_files, skipped_files, etc.

    Args:
        vault_root: Root path of the Obsidian vault.
        mode: Either "full" or "incremental".
        embedder: Optional embedder instance.

    Returns:
        Dictionary with keys: mode, indexed_files, skipped_files, indexed_chunks, errors, collection_count
    """
    # Validate mode early before any I/O
    if mode not in ("full", "incremental"):
        raise ValueError(f"Invalid mode: {mode}. Must be 'full' or 'incremental'")

    vault_root = Path(vault_root).expanduser().resolve()

    # Load configuration
    config = load_config(vault_root)

    # Create embedder for index_all and collection count
    if embedder is None:
        selected_embedder = SentenceTransformerEmbedder(
            config.embedding_model, cache_folder=config.embedding_cache_path
        )
    else:
        selected_embedder = embedder

    # Delegate to index_all
    all_result = index_all(vault_root, mode=mode, config=config, embedder=selected_embedder)

    # Get collection count
    vector_store = ChromaVectorStore(
        config.chroma_path, config.collection_name, selected_embedder
    )

    # Build legacy-style report
    total_indexed = all_result["total_indexed"]
    total_skipped = all_result["total_skipped"]
    total_errors = all_result["total_errors"]
    total_chunks = sum(s.get("chunks", 0) for s in all_result["sources"].values())

    # Collect all errors
    all_errors: list[dict[str, str]] = []
    for source_stats in all_result["sources"].values():
        all_errors.extend(source_stats.get("errors", []))

    return {
        "mode": mode,
        "indexed_files": total_indexed,
        "skipped_files": total_skipped,
        "indexed_chunks": total_chunks,
        "errors": all_errors,
        "collection_count": vector_store.count(),
    }


def _collect_markdown_files(config: RagConfig) -> list[Path]:
    """Collect all markdown files under include paths, excluding specified names.

    Retained for backward compatibility; new code uses _collect_source_files.
    """
    markdown_files: list[Path] = []

    for include_path in config.include_paths:
        if not include_path.exists():
            continue

        for md_file in include_path.rglob("*.md"):
            if _should_exclude(md_file, include_path, config.exclude_names):
                continue
            markdown_files.append(md_file)

    return sorted(markdown_files)