from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ARRAY_METADATA_FIELDS = {
    "related_objects",
    "related_scripts",
    "related_deployments",
    "deployment_ids",
    "script_parameters",
    "field_ids",
    "config_paths",
    "tags",
    "zentao_urls",
}


@dataclass(frozen=True)
class SourceConfig:
    source_name: str
    source_kind: str
    root: Path
    include: list[str]
    exclude: list[str]
    file_types: list[str]
    parser: str
    collection: str
    authority: str
    file_exclude_patterns: list[str] = field(default_factory=list)
    library_exclude_patterns: list[str] = field(default_factory=list)
    utility_allowlist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RagConfig:
    vault_root: Path
    include_paths: list[Path]
    exclude_names: set[str]
    chroma_path: Path
    collection_name: str
    embedding_model: str
    embedding_cache_path: Path
    manifest_path: Path
    sources: list[SourceConfig] = field(default_factory=list)


@dataclass(frozen=True)
class SourceDocument:
    doc_id: str
    source_path: str
    absolute_path: Path
    frontmatter: dict[str, Any]
    body: str
    updated_at: str
    source_kind: str = "note"
    source_name: str = ""
    file_hash: str = ""
    repo_root: str = ""
    repo_relative_path: str = ""
    language: str = ""


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    chunk_index: int
    source_path: str
    heading: str
    text: str
    metadata: dict[str, Any]
    function_name: str = ""
    line_start: int = 0
    line_end: int = 0
    source_kind: str = "note"
    source_name: str = ""
    file_hash: str = ""


@dataclass(frozen=True)
class RoutingResult:
    """Result of query routing analysis."""

    kind: str  # "note_led", "code_led", or "mixed"
    source_filter: dict  # Effective metadata filter for this routing
    boost_factor: float  # Weight multiplier for preferred source
    explanation: str  # Human-readable explanation of routing decision


@dataclass(frozen=True)
class SearchResult:
    citation_id: str
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    distance: float | None
    source_kind: str = "note"
    git_commit: str = ""


@dataclass(frozen=True)
class ConflictReport:
    """Report of a conflict between note and code sources for the same entity."""

    conflict_type: str     # "implementation", "business", "staleness", "unclassified"
    entity: str            # What entity/fact is conflicting (script_id, function_name, path)
    note_source: dict      # Citation info from note source
    code_source: dict      # Citation info from code source
    winning_source: str    # "code", "note", or "both"
    explanation: str       # Why this source wins
    uncertainty: bool      # True if conflict cannot be clearly resolved