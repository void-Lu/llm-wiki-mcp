"""Shared ownership checks for CodeGraph-managed Wiki artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

CODEGRAPH_SOURCE_NAME = "codegraph"
CODEGRAPH_RETRIEVAL_SCOPE = "project_code"


def is_codegraph_raw_path(relative_path: str | Path) -> bool:
    """Return whether a raw path belongs to the latest CodeGraph snapshot."""

    normalized = str(relative_path).replace("\\", "/").strip("/")
    parts = normalized.split("/")
    return len(parts) >= 6 and parts[:3] == ["raw", "sources", "projects"] and parts[4] == "codegraph"


def is_project_code_page(frontmatter: Mapping[str, Any], *, project: str | None = None) -> bool:
    """Identify a generated CodeGraph page at the retrieval boundary."""

    if frontmatter.get("retrieval_scope") != CODEGRAPH_RETRIEVAL_SCOPE:
        return False
    return project is None or str(frontmatter.get("project") or "").casefold() == project.casefold()


def codegraph_architecture_path(relative_path: str | Path) -> bool:
    """Return whether ``relative_path`` is in the canonical managed subtree."""

    normalized = Path(str(relative_path).replace("\\", "/"))
    parts = normalized.parts
    if len(parts) < 5 or parts[:2] != ("wiki", "projects") or parts[3] != "architecture":
        return False
    return parts[4] in {"code-facts", "pipelines"} or parts[4] == "code-overview.md"


def is_codegraph_frontmatter(frontmatter: Mapping[str, Any], *, project: str | None = None) -> bool:
    """Check the complete ownership marker, not merely ``generated``."""

    if frontmatter.get("generated") is not True:
        return False
    if frontmatter.get("managed_by") != CODEGRAPH_SOURCE_NAME:
        return False
    if frontmatter.get("retrieval_scope") != CODEGRAPH_RETRIEVAL_SCOPE:
        return False
    if frontmatter.get("source_name") != CODEGRAPH_SOURCE_NAME:
        return False
    return project is None or str(frontmatter.get("project") or "") == project


def is_codegraph_managed_page(
    vault_root: str | Path,
    relative_path: str | Path,
    *,
    project: str | None = None,
) -> bool:
    """Read only the candidate page and identify a protected CodeGraph page."""

    relative = Path(str(relative_path).replace("\\", "/"))
    if not codegraph_architecture_path(relative):
        return False
    root = Path(vault_root).expanduser().resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return False
    try:
        from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter

        frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8"))
    except OSError:
        return False
    return is_codegraph_frontmatter(frontmatter, project=project)


def is_codegraph_managed_path(relative_path: str | Path, frontmatter: Mapping[str, Any]) -> bool:
    """Ownership predicate used by stale reconciliation and write guards."""

    return codegraph_architecture_path(relative_path) and is_codegraph_frontmatter(frontmatter)
