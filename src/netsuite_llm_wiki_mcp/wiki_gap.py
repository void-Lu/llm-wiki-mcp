"""Wiki gap analysis: identify missing concepts, shallow pages, and suggest enrichment paths.

Three-stage workflow:
- analyze: scan wiki/concepts/ and wiki/projects/, compare against CodeGraph symbols,
  raw sources, and existing coverage → produce a gap report
- suggest: for each gap, recommend enrichment actions (ingest_url, ingest_llm, write_note)
- fill: execute suggested actions (delegates to existing tools)
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page, split_frontmatter

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_STRUCTURAL_PAGES = {"index", "log", "overview"}
_MIN_BODY_TOKENS = 80  # pages with fewer tokens are considered shallow


def wiki_gap(
    vault_root: str | Path,
    stage: str = "analyze",
    project: str | None = None,
    taxonomy: list[str] | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Main entry point for gap analysis.

    Args:
        vault_root: path to the Obsidian vault root
        stage: 'analyze' or 'suggest'
        project: optional project scope filter
        taxonomy: optional list of expected concept names to check coverage
        language: output language
    """
    root = Path(vault_root).expanduser().resolve()
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return {"ok": False, "code": "no_wiki", "error": "wiki directory not found"}

    if stage == "analyze":
        return _analyze(root, project, taxonomy, language)
    elif stage == "suggest":
        return _suggest(root, project, taxonomy, language)
    else:
        return {"ok": False, "code": "invalid_stage", "error": f"stage must be 'analyze' or 'suggest', got '{stage}'"}


# ---------------------------------------------------------------------------
# Analyze stage
# ---------------------------------------------------------------------------


def _analyze(
    root: Path,
    project: str | None,
    taxonomy: list[str] | None,
    language: str,
) -> dict[str, Any]:
    """Scan wiki and produce a gap report."""
    concepts_dir = root / "wiki" / "concepts"
    projects_dir = root / "wiki" / "projects"

    # Collect existing concept pages
    existing_concepts = _collect_pages(concepts_dir, root)

    # Collect project pages
    existing_project_pages = {}
    if project:
        proj_dir = projects_dir / project
        if proj_dir.exists():
            existing_project_pages = _collect_pages(proj_dir, root)
    else:
        if projects_dir.exists():
            existing_project_pages = _collect_pages(projects_dir, root)

    all_pages = {**existing_concepts, **existing_project_pages}

    # Find shallow pages (too little content)
    shallow_pages = _find_shallow_pages(all_pages)

    # Find orphan pages (no incoming wikilinks)
    orphan_pages = _find_orphan_pages(root, all_pages)

    # Find missing concepts from taxonomy
    missing_from_taxonomy = []
    if taxonomy:
        existing_stems = {info["stem"].lower() for info in all_pages.values()}
        for expected in taxonomy:
            if expected.lower() not in existing_stems:
                missing_from_taxonomy.append(expected)

    # Find concepts referenced in wikilinks but not existing as pages
    dangling_links = _find_dangling_links(root, project)

    # Find raw sources not yet ingested
    uningest_sources = _find_uningested_sources(root, project)

    report = {
        "ok": True,
        "stage": "analyze",
        "summary": {
            "total_pages": len(all_pages),
            "shallow_pages": len(shallow_pages),
            "orphan_pages": len(orphan_pages),
            "missing_from_taxonomy": len(missing_from_taxonomy),
            "dangling_links": len(dangling_links),
            "uningested_sources": len(uningest_sources),
        },
        "shallow_pages": shallow_pages[:20],
        "orphan_pages": orphan_pages[:20],
        "missing_from_taxonomy": missing_from_taxonomy[:30],
        "dangling_links": dangling_links[:20],
        "uningested_sources": uningest_sources[:20],
    }
    return report


def _collect_pages(directory: Path, root: Path) -> dict[str, dict[str, Any]]:
    """Collect all markdown pages in a directory tree, keyed by relative path."""
    pages: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return pages
    for path in sorted(directory.rglob("*.md")):
        stem = path.stem
        if stem in _STRUCTURAL_PAGES:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        frontmatter, body = split_frontmatter(text)
        wikilinks = set(_WIKILINK_RE.findall(body))
        pages[rel] = {
            "stem": stem,
            "path": path,
            "rel": rel,
            "frontmatter": frontmatter,
            "body": body,
            "wikilinks": wikilinks,
            "token_estimate": _estimate_tokens(body),
            "type": frontmatter.get("type", ""),
            "tags": frontmatter.get("tags", []),
        }
    return pages


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: CJK chars count as 1 token each, words count as 1."""
    cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    non_cjk = re.sub(r"[\u4e00-\u9fff]", "", text)
    word_count = len(non_cjk.split())
    return cjk_count + word_count


def _find_shallow_pages(pages: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Find pages with very little content."""
    shallow = []
    for rel, info in pages.items():
        tokens = info["token_estimate"]
        if tokens < _MIN_BODY_TOKENS:
            shallow.append({
                "path": rel,
                "title": info["stem"],
                "token_estimate": tokens,
                "reason": "content too short",
            })
    return sorted(shallow, key=lambda x: x["token_estimate"])


def _find_orphan_pages(root: Path, pages: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Find pages that no other page links to."""
    # Build set of all stems that are linked to
    linked_stems: set[str] = set()
    wiki_dir = root / "wiki"
    if wiki_dir.exists():
        for path in wiki_dir.rglob("*.md"):
            if path.stem in _STRUCTURAL_PAGES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            _, body = split_frontmatter(text)
            for link_target in _WIKILINK_RE.findall(body):
                linked_stems.add(link_target.lower())

    orphans = []
    for rel, info in pages.items():
        stem = info["stem"]
        if stem.lower() not in linked_stems:
            orphans.append({
                "path": rel,
                "title": stem,
                "reason": "no incoming wikilinks",
            })
    return orphans


def _find_dangling_links(root: Path, project: str | None) -> list[dict[str, Any]]:
    """Find wikilink targets that don't correspond to any existing page."""
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return []

    # Build set of all existing page stems
    existing_stems: set[str] = set()
    for path in wiki_dir.rglob("*.md"):
        existing_stems.add(path.stem.lower())

    # Collect all wikilink targets
    dangling: dict[str, list[str]] = defaultdict(list)
    scan_dir = wiki_dir
    if project:
        proj_dir = wiki_dir / "projects" / project
        if proj_dir.exists():
            scan_dir = proj_dir

    for path in scan_dir.rglob("*.md"):
        if path.stem in _STRUCTURAL_PAGES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        _, body = split_frontmatter(text)
        for link_target in _WIKILINK_RE.findall(body):
            if link_target.lower() not in existing_stems:
                dangling[link_target].append(path.relative_to(root).as_posix())

    result = []
    for target, sources in sorted(dangling.items(), key=lambda x: -len(x[1])):
        result.append({
            "missing_page": target,
            "referenced_by_count": len(sources),
            "referenced_by": sources[:5],
        })
    return result


def _find_uningested_sources(root: Path, project: str | None) -> list[dict[str, Any]]:
    """Find raw sources that have no corresponding wiki page."""
    raw_dir = root / "raw" / "sources"
    if not raw_dir.exists():
        return []

    # Build set of known source names from wiki/sources/ index pages
    known_sources: set[str] = set()
    sources_dir = root / "wiki" / "sources"
    if sources_dir.exists():
        for path in sources_dir.rglob("*.md"):
            known_sources.add(path.stem.lower())

    # Also check ingest cache
    cache_dir = root / ".llm-wiki" / "ingest-cache"
    if cache_dir.exists():
        for path in cache_dir.rglob("*.json"):
            known_sources.add(path.stem.lower())

    # Scan raw sources
    uningested = []
    for source_type_dir in sorted(raw_dir.iterdir()):
        if not source_type_dir.is_dir():
            continue
        source_type = source_type_dir.name
        for item in sorted(source_type_dir.iterdir()):
            if not item.is_dir():
                continue
            # item is project-level dir
            if project and item.name != project:
                continue
            for source_dir in sorted(item.iterdir()):
                if not source_dir.is_dir():
                    continue
                source_name = source_dir.name
                if source_name.lower() not in known_sources:
                    uningested.append({
                        "source_type": source_type,
                        "project": item.name,
                        "source_name": source_name,
                        "path": source_dir.relative_to(root).as_posix(),
                    })

    return uningested


# ---------------------------------------------------------------------------
# Suggest stage
# ---------------------------------------------------------------------------


def _suggest(
    root: Path,
    project: str | None,
    taxonomy: list[str] | None,
    language: str,
) -> dict[str, Any]:
    """Based on gap analysis, suggest concrete enrichment actions."""
    analysis = _analyze(root, project, taxonomy, language)
    if not analysis["ok"]:
        return analysis

    suggestions: list[dict[str, Any]] = []

    # Suggest ingesting uningested raw sources
    for source in analysis["uningested_sources"]:
        suggestions.append({
            "action": "wiki_ingest_llm",
            "priority": "high",
            "reason": f"raw source '{source['source_name']}' exists but has no wiki page",
            "params": {
                "stage": "prepare",
                "project": source["project"],
                "source_name": source["source_name"],
                "source_type": source["source_type"],
            },
        })

    # Suggest creating pages for dangling links (high reference count first)
    for dangling in analysis["dangling_links"]:
        if dangling["referenced_by_count"] >= 2:
            suggestions.append({
                "action": "wiki_write_note_or_research",
                "priority": "high",
                "reason": f"'{dangling['missing_page']}' is referenced by {dangling['referenced_by_count']} pages but doesn't exist",
                "missing_page": dangling["missing_page"],
                "options": [
                    {"tool": "wiki_write_note", "when": "you have domain knowledge to write it directly"},
                    {"tool": "wiki_research", "when": "you need to research the topic first"},
                    {"tool": "wiki_ingest_url", "when": "there's an authoritative URL for this topic"},
                ],
            })
        else:
            suggestions.append({
                "action": "wiki_write_note_or_research",
                "priority": "medium",
                "reason": f"'{dangling['missing_page']}' is referenced but doesn't exist",
                "missing_page": dangling["missing_page"],
                "options": [
                    {"tool": "wiki_write_note", "when": "you have domain knowledge"},
                    {"tool": "wiki_research", "when": "you need to research first"},
                ],
            })

    # Suggest enriching shallow pages
    for shallow in analysis["shallow_pages"]:
        suggestions.append({
            "action": "enrich_or_rewrite",
            "priority": "medium",
            "reason": f"'{shallow['title']}' has only ~{shallow['token_estimate']} tokens",
            "page_path": shallow["path"],
            "options": [
                {"tool": "wiki_enrich", "when": "page just needs more wikilinks"},
                {"tool": "wiki_page_merge", "when": "you have additional content to merge in"},
                {"tool": "wiki_ingest_url", "when": "there's a URL with more detail on this topic"},
            ],
        })

    # Suggest creating pages for missing taxonomy items
    for missing in analysis.get("missing_from_taxonomy", []):
        suggestions.append({
            "action": "create_concept",
            "priority": "medium",
            "reason": f"expected concept '{missing}' not found in wiki",
            "missing_concept": missing,
            "options": [
                {"tool": "wiki_write_note", "params": {"note_type": "knowledge", "title": missing}},
                {"tool": "wiki_research", "params": {"topic": missing}},
                {"tool": "wiki_ingest_url", "when": "there's a documentation URL"},
            ],
        })

    # Suggest linking orphan pages
    for orphan in analysis["orphan_pages"][:10]:
        suggestions.append({
            "action": "wiki_enrich",
            "priority": "low",
            "reason": f"'{orphan['title']}' has no incoming links — consider enriching related pages to link to it",
            "page_path": orphan["path"],
        })

    return {
        "ok": True,
        "stage": "suggest",
        "total_suggestions": len(suggestions),
        "suggestions": suggestions[:30],
        "analysis_summary": analysis["summary"],
    }
