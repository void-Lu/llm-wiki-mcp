"""Manifest management for tracking indexed files.

v2 manifest key format: {source_name}:{source_kind}:{relative_path}
v1 manifest key format: relative_path (backward compatible — auto-migrated)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ManifestEntry:
    """A single file's indexing record in the manifest."""

    doc_id: str
    source_name: str
    source_kind: str  # "note" or "code"
    relative_path: str
    mtime: float
    size: int
    file_hash: str  # SHA-256 hex digest
    chunk_count: int
    indexed_at: str  # ISO 8601 timestamp
    # Code-specific fields (empty/default for notes)
    git_commit: str = ""
    git_branch: str = ""
    dirty: bool = False


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of file content.

    Args:
        file_path: Absolute path to the file.

    Returns:
        Hex digest string (64 chars).
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def read_manifest(manifest_path: Path) -> dict[str, ManifestEntry]:
    """Read manifest from JSON file.

    Auto-detects v1 format (keys without ':') and migrates to v2.

    Args:
        manifest_path: Path to the manifest JSON file.

    Returns:
        Dict keyed by v2 manifest keys ({source_name}:{source_kind}:{relative_path}).
    """
    if not manifest_path.exists():
        return {}

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return {}

    if not isinstance(raw, dict):
        return {}

    # Detect if v1 format (keys are just relative paths, no ':')
    is_v1 = any(":" not in key for key in raw)
    if is_v1:
        return migrate_manifest(raw)

    # v2 format: parse ManifestEntry objects
    manifest: dict[str, ManifestEntry] = {}
    for key, entry_data in raw.items():
        if isinstance(entry_data, dict):
            manifest[key] = ManifestEntry(**entry_data)
    return manifest


def write_manifest(manifest_path: Path, manifest: dict[str, ManifestEntry]) -> None:
    """Write manifest to JSON file.

    Args:
        manifest_path: Path to the manifest JSON file.
        manifest: Dict of ManifestEntry objects keyed by v2 manifest keys.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {key: asdict(entry) for key, entry in manifest.items()}
    manifest_path.write_text(
        json.dumps(serializable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def migrate_manifest(old_manifest: dict) -> dict[str, ManifestEntry]:
    """Migrate v1 manifest (relative_path keys) to v2 format.

    v1 entries are assumed to be note source with source_name='obsidian'.
    v2 entries (keys containing ':') pass through unchanged.

    Args:
        old_manifest: Raw dict from manifest JSON (v1 or v2 format).

    Returns:
        Dict of ManifestEntry objects keyed by v2 manifest keys.
    """
    result: dict[str, ManifestEntry] = {}

    for key, entry_data in old_manifest.items():
        if not isinstance(entry_data, dict):
            continue

        if ":" in key:
            # v2 key — already in proper format
            result[key] = ManifestEntry(**entry_data)
        else:
            # v1 key — relative_path only, default to obsidian/note
            relative_path = key
            result[manifest_key("obsidian", "note", relative_path)] = ManifestEntry(
                doc_id=entry_data.get("doc_id", ""),
                source_name="obsidian",
                source_kind="note",
                relative_path=relative_path,
                mtime=entry_data.get("mtime", 0.0),
                size=entry_data.get("size", 0),
                file_hash=entry_data.get("file_hash", ""),
                chunk_count=entry_data.get("chunk_count", 0),
                indexed_at=entry_data.get("indexed_at", ""),
                git_commit=entry_data.get("git_commit", ""),
                git_branch=entry_data.get("git_branch", ""),
                dirty=entry_data.get("dirty", False),
            )

    return result


def manifest_key(source_name: str, source_kind: str, relative_path: str) -> str:
    """Generate v2 manifest key: {source_name}:{source_kind}:{relative_path}.

    Args:
        source_name: Name of the source (e.g., "obsidian", "netsuite_repo").
        source_kind: Kind of source ("note" or "code").
        relative_path: Relative file path within the source root.

    Returns:
        v2 manifest key string.
    """
    return f"{source_name}:{source_kind}:{relative_path}"