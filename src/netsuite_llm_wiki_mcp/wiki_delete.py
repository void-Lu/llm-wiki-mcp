"""Source deletion with cascade cleanup.

When a source is removed, this module:
1. Deletes derived wiki pages that trace back to that source.
2. Removes references from index.md and other pages' wikilinks.
3. Cleans up the raw snapshot directory.
4. Updates the ingest cache.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter
from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry
from netsuite_llm_wiki_mcp.wikilinks import wikilink_targets



def wiki_delete_source(
    vault_root: str | Path,
    project: str,
    source_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a source and cascade-clean derived wiki pages and references."""
    root = Path(vault_root).expanduser().resolve()

    raw_dir = root / "raw" / "sources" / "codegraph" / project / source_name
    if not raw_dir.exists():
        raw_dir = root / "raw" / "sources" / "file" / project / source_name
    if not raw_dir.exists():
        for source_type_dir in (root / "raw" / "sources").iterdir():
            candidate = source_type_dir / project / source_name
            if candidate.exists():
                raw_dir = candidate
                break

    derived_pages, source_pruned_pages = _find_derived_pages(root, project, source_name)
    slugs_to_remove = {p.stem for p in derived_pages}
    affected_refs = _find_affected_references(root, slugs_to_remove)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "raw_dir": raw_dir.relative_to(root).as_posix() if raw_dir.exists() else None,
            "derived_pages": [p.relative_to(root).as_posix() for p in derived_pages],
            "source_pruned_pages": [p.relative_to(root).as_posix() for p, _ in source_pruned_pages],
            "affected_references": affected_refs,
        }

    deleted_pages: list[str] = []
    pruned_pages: list[str] = []
    for page, remaining_sources in source_pruned_pages:
        if page.exists():
            _rewrite_sources(page, remaining_sources)
            pruned_pages.append(page.relative_to(root).as_posix())

    for page in derived_pages:
        if page.exists():
            rel = page.relative_to(root).as_posix()
            page.unlink()
            deleted_pages.append(rel)

    rewritten = _remove_references(root, slugs_to_remove)

    raw_deleted = False
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
        raw_deleted = True

    _clean_ingest_cache(root, project, source_name)

    source_summary = root / "wiki" / "sources" / f"{source_name}.md"
    if source_summary.exists():
        fm, _ = split_frontmatter(source_summary.read_text(encoding="utf-8"))
        if fm.get("generated") is True:
            source_summary.unlink()
            deleted_pages.append(source_summary.relative_to(root).as_posix())

    refresh_indexes(root)

    append_log_entry(root, WikiLogEntry(
        operation="delete_source",
        title=f"Deleted source: {project}/{source_name}",
        paths=deleted_pages,
        project=project,
        status="ok",
    ))

    return {
        "ok": True,
        "project": project,
        "source_name": source_name,
        "raw_deleted": raw_deleted,
        "pages_deleted": deleted_pages,
        "pages_pruned_sources": pruned_pages,
        "references_rewritten": rewritten,
    }


def _find_derived_pages(root: Path, project: str, source_name: str) -> tuple[list[Path], list[tuple[Path, list[str]]]]:
    """Find wiki pages whose frontmatter sources reference this source_name."""
    pages_to_delete: list[Path] = []
    pages_to_prune: list[tuple[Path, list[str]]] = []
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return pages_to_delete, pages_to_prune

    for path in sorted(wiki_dir.rglob("*.md")):
        if path.name in ("index.md", "log.md", "overview.md"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm, _ = split_frontmatter(text)
        if fm.get("generated") is not True:
            continue
        sources = fm.get("sources", [])
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        source_strs = [str(s) for s in sources]
        matching = [source for source in source_strs if _source_matches(source, source_name)]
        if matching and (not fm.get("project") or fm.get("project") == project):
            remaining = [source for source in source_strs if not _source_matches(source, source_name)]
            if remaining:
                pages_to_prune.append((path, remaining))
            else:
                pages_to_delete.append(path)

    return pages_to_delete, pages_to_prune


def _source_matches(source: str, source_name: str) -> bool:
    normalized = source.replace("\\", "/").casefold().strip("/")
    target = source_name.casefold().strip("/")
    if normalized == target:
        return True
    return normalized.startswith(f"{target}/") or f"/{target}/" in normalized or normalized.endswith(f"/{target}")


def _rewrite_sources(path: Path, remaining_sources: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    fm["sources"] = remaining_sources
    yaml_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n", encoding="utf-8")


def _find_affected_references(root: Path, slugs: set[str]) -> list[str]:
    """Find pages that reference any of the slugs being removed."""
    affected: list[str] = []
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return affected

    for path in sorted(wiki_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for target in wikilink_targets(text):
            stem = Path(target).stem.casefold()
            if stem in {s.casefold() for s in slugs}:
                affected.append(path.relative_to(root).as_posix())
                break

    return affected


def _remove_references(root: Path, slugs: set[str]) -> int:
    """Remove wikilinks to deleted slugs from all wiki pages."""
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return 0

    count = 0
    normalized_slugs = {s.casefold() for s in slugs}

    for path in sorted(wiki_dir.rglob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        new_content = content
        for slug in slugs:
            escaped = re.escape(slug)
            pattern = re.compile(r"\[\[" + escaped + r"((?:\\\||\|)([^\]]+))?\]\]")
            new_content = pattern.sub(lambda m: m.group(2) or slug, new_content)

        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            count += 1

    return count


def _clean_ingest_cache(root: Path, project: str, source_name: str) -> None:
    """Remove ingest cache entry for this source."""
    cache_dir = root / ".llm-wiki" / "ingest-cache" / project
    cache_file = cache_dir / f"{source_name}.json"
    if cache_file.exists():
        cache_file.unlink()
