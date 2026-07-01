from __future__ import annotations

import hashlib
import os
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import WikiWriteError, split_frontmatter, write_wiki_page
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry, WikiPage
from netsuite_llm_wiki_mcp.wiki_overview import refresh_overview
from netsuite_llm_wiki_mcp.wiki_paths import slug

DEFAULT_PAGE_SIZE = 80
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 250
DEFAULT_MAX_HEADINGS = 12
MAX_HEADING_SCAN_LINES = 1_200
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{1,}|[一-鿿]{2,}")
_STOPWORDS = {
    "about",
    "after",
    "and",
    "are",
    "before",
    "for",
    "from",
    "how",
    "into",
    "netsuite",
    "oracle",
    "the",
    "this",
    "to",
    "using",
    "with",
    "your",
}


def build_source_index(
    vault_root: str | Path,
    source_root: str | Path,
    source_name: str,
    target_dir: str | Path | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_headings: int = DEFAULT_MAX_HEADINGS,
    refresh: bool = True,
) -> dict[str, Any]:
    """Build queryable source_index pages for raw markdown without LLM analysis."""
    root = Path(vault_root).expanduser().resolve()
    if not source_name.strip():
        return {"ok": False, "code": "missing_source_name", "error": "source_name is required"}

    resolved_source = _resolve_source_root(root, source_root)
    if not resolved_source.get("ok"):
        return resolved_source
    source_dir: Path = resolved_source["absolute_path"]
    source_rel: str = resolved_source["relative_path"]

    resolved_target = _resolve_target_dir(root, target_dir, source_name)
    if not resolved_target.get("ok"):
        return resolved_target
    target: Path = resolved_target["absolute_path"]
    target_rel: str = resolved_target["relative_path"]

    page_size = _clamp(page_size, MIN_PAGE_SIZE, MAX_PAGE_SIZE)
    max_headings = _clamp(max_headings, 0, 50)
    toc = _load_toc_manifest(source_dir)
    entries = [_entry_for_file(root, source_dir, path, toc, max_headings) for path in sorted(source_dir.rglob("*.md"))]
    entries = [entry for entry in entries if entry is not None]
    if not entries:
        return {"ok": False, "code": "no_markdown_sources", "error": f"no markdown files found under {source_rel}"}

    clear_result = _clear_existing_generated_pages(root, target)
    if not clear_result.get("ok"):
        return clear_result

    groups = _group_entries(entries)
    written: list[str] = []
    written.extend(_write_group_pages(root, target, target_rel, source_name, source_rel, groups, page_size))
    catalog_path = _write_catalog_page(root, target, target_rel, source_name, source_rel, entries, groups, written)
    written.insert(0, catalog_path)

    index_result: dict[str, Any] | None = None
    overview_result: dict[str, Any] | None = None
    if refresh:
        index_result = refresh_indexes(root)
        if index_result.get("ok"):
            overview_result = refresh_overview(root)
        else:
            return index_result

    log_result = append_log_entry(
        root,
        WikiLogEntry(
            operation="source_index",
            title=f"{source_name} lightweight source index",
            project="",
            status="ok",
            paths=written,
            sources=_source_manifest_paths(source_dir, source_rel),
        ),
    )

    return {
        "ok": True,
        "source_name": source_name,
        "source_root": source_rel,
        "target_dir": target_rel,
        "indexed_count": len(entries),
        "page_count": len(written),
        "page_size": page_size,
        "groups": [
            {"title": title, "count": len(group_entries)}
            for title, group_entries in groups.items()
        ],
        "written": written,
        "cleared": clear_result.get("deleted", []),
        "index_result": index_result or {},
        "overview_result": overview_result or {},
        "log_result": log_result,
    }


def _resolve_source_root(root: Path, source_root: str | Path) -> dict[str, Any]:
    raw_sources = (root / "raw" / "sources").resolve()
    candidate = Path(source_root)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        return {"ok": False, "code": "source_not_found", "error": f"source_root is not a directory: {source_root}"}
    if not candidate.is_relative_to(raw_sources):
        return {"ok": False, "code": "source_root_not_allowed", "error": "source_root must be under raw/sources/"}
    return {"ok": True, "absolute_path": candidate, "relative_path": candidate.relative_to(root).as_posix()}


def _resolve_target_dir(root: Path, target_dir: str | Path | None, source_name: str) -> dict[str, Any]:
    relative = Path(target_dir) if target_dir else Path("wiki/sources/references") / slug(source_name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return {"ok": False, "code": "target_path_escape", "error": "target_dir must be relative and stay inside wiki/sources/"}
    relative = Path(*relative.parts)
    if not relative.is_relative_to(Path("wiki/sources")):
        return {"ok": False, "code": "target_not_allowed", "error": "target_dir must be under wiki/sources/"}
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        return {"ok": False, "code": "target_path_escape", "error": "resolved target_dir escapes vault root"}
    return {"ok": True, "absolute_path": target, "relative_path": relative.as_posix()}


def _load_toc_manifest(source_dir: Path) -> dict[str, dict[str, Any]]:
    path = source_dir / "_toc_manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tree = data.get("tree") if isinstance(data, dict) else None
    return tree if isinstance(tree, dict) else {}


def _entry_for_file(
    root: Path,
    source_dir: Path,
    path: Path,
    toc: dict[str, dict[str, Any]],
    max_headings: int,
) -> dict[str, Any] | None:
    try:
        text = _read_text(path)
    except OSError:
        return None
    frontmatter, body = split_frontmatter(text)
    source_url = str(frontmatter.get("source") or "").strip()
    toc_meta = toc.get(source_url, {}) if source_url else {}
    toc_path = _string_list(toc_meta.get("toc_path")) or _path_toc(source_dir, path)
    title = str(frontmatter.get("title") or toc_meta.get("title") or _first_heading(body) or path.stem).strip()
    headings = _headings(body, max_headings)
    rel = path.relative_to(root).as_posix()
    published = str(frontmatter.get("published") or toc_meta.get("published") or "").strip()
    doc_type = str(toc_meta.get("type") or _infer_type_from_file(path) or "").strip()
    depth = toc_meta.get("depth")
    tags = [str(item) for item in _as_list(frontmatter.get("tags")) if str(item).strip()]
    return {
        "title": title,
        "raw_path": rel,
        "source": source_url,
        "toc_path": toc_path,
        "type": doc_type,
        "depth": depth,
        "published": published,
        "headings": headings,
        "tags": tags,
        "hash": hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest(),
        "keywords": _keywords(title, toc_path, headings, tags),
    }


def _path_toc(source_dir: Path, path: Path) -> list[str]:
    relative = path.relative_to(source_dir)
    parts = list(relative.parts)
    if parts:
        parts[-1] = Path(parts[-1]).stem
    return [part.replace("_", " ") for part in parts]


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _headings(body: str, max_headings: int) -> list[str]:
    if max_headings <= 0:
        return []
    headings: list[str] = []
    for index, line in enumerate(body.splitlines()):
        if index > MAX_HEADING_SCAN_LINES:
            break
        match = _HEADING_RE.match(line)
        if not match:
            continue
        text = _clean_inline_markdown(match.group(2))
        if text and text not in headings:
            headings.append(text)
        if len(headings) >= max_headings:
            break
    return headings


def _clean_inline_markdown(value: str) -> str:
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value.strip(" #")


def _keywords(title: str, toc_path: list[str], headings: list[str], tags: list[str]) -> list[str]:
    text = " ".join([title, *toc_path, *headings, *tags])
    seen: set[str] = set()
    keywords: list[str] = []
    for match in _WORD_RE.findall(text):
        token = match.strip(".,;:()[]{}").casefold()
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(match.strip(".,;:()[]{}"))
        if len(keywords) >= 24:
            break
    return keywords


def _infer_type_from_file(path: Path) -> str:
    stem = path.stem
    return stem.split("_", 1)[0] if "_" in stem else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _group_entries(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        toc_path = entry.get("toc_path") or []
        title = str(toc_path[1] if len(toc_path) > 1 else toc_path[0] if toc_path else "Ungrouped")
        grouped[title].append(entry)
    return {
        title: sorted(items, key=lambda item: (_toc_sort_key(item), item["title"].casefold(), item["raw_path"].casefold()))
        for title, items in sorted(grouped.items(), key=lambda pair: pair[0].casefold())
    }


def _toc_sort_key(entry: dict[str, Any]) -> str:
    return " > ".join(str(item) for item in entry.get("toc_path") or [])


_UNGROUPED_MARKER = "_ungrouped"
_UNGROUPED_LEAF = "<ungrouped>"


def _resolve_tag_path(raw_tag: Any, source_name: str) -> tuple[str, ...] | None:
    """Normalize one frontmatter tag string into a tree path tuple.

    Splits on '/', slug-cleans each segment, drops dangerous/empty ones,
    strips a leading segment equal to source_name, and returns either a
    >=1 length tuple, the root-marker (slug(source_name),) when the tag
    equals source_name exactly, or None when no valid segment remains.
    """
    if not isinstance(raw_tag, str):
        return None
    parts: list[str] = []
    for seg in raw_tag.split("/"):
        stripped = seg.strip()
        if not stripped or stripped in {".", ".."} or "\\" in stripped:
            continue
        slug_seg = slug(stripped)
        if not slug_seg:
            continue
        parts.append(slug_seg)
    if not parts:
        return None
    source_slug = slug(source_name).casefold()
    if parts and parts[0].casefold() == source_slug:
        parts = parts[1:]
        if not parts:
            return (slug(source_name),)
    return tuple(parts)


def _entry_tag_paths(entry: dict[str, Any], source_name: str) -> list[tuple[str, ...]]:
    """Return all tag tree paths for an entry; mirrors across tags.

    No valid tag → returns [(_UNGROUPED_MARKER, _UNGROUPED_LEAF)] so that the
    parent ('_ungrouped',) becomes a standalone index carrying each ungrouped
    raw file as its own `## <title>` section similarly to multi-tag mirroring
    in tagged nodes.
    """
    raw_tags = entry.get("tags") or []
    paths: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for raw_tag in raw_tags:
        path = _resolve_tag_path(raw_tag, source_name)
        if path is None or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    if not paths:
        return [(_UNGROUPED_MARKER, _UNGROUPED_LEAF)]
    return paths


def _build_tag_index_tree(
    entries: list[dict[str, Any]],
    source_name: str,
) -> tuple[dict[tuple[tuple[str, ...], str], list[dict[str, Any]]], set[tuple[str, ...]]]:
    """Build the tag-path index tree.

    Returns:
        section_entries: maps (parent_path, leaf_segment) -> [entry, ...].
            Each (entry, path) pair lands in parent=path[:-1]'s index under
            section `## path[-1]`. Entries are deduped by raw_path per
            (parent_path, leaf_segment) key.
        interior_nodes: set of node paths that have at least one child
            subtree (i.e. appear as a strict prefix of some path), including
            the root (). Those nodes get a directory + index.md.
    """
    section_entries: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)
    all_nodes: set[tuple[str, ...]] = set()
    for entry in entries:
        for path in _entry_tag_paths(entry, source_name):
            for depth in range(len(path) + 1):
                all_nodes.add(path[:depth])
            parent = path[:-1]
            leaf = path[-1]
            key = (parent, leaf)
            if not any(e["raw_path"] == entry["raw_path"] for e in section_entries[key]):
                section_entries[key].append(entry)
    interior_nodes = {
        node for node in all_nodes
        if any(
            other != node
            and len(other) > len(node)
            and other[: len(node)] == node
            for other in all_nodes
        )
    }
    return section_entries, interior_nodes


def _write_group_pages(
    root: Path,
    target: Path,
    target_rel: str,
    source_name: str,
    source_rel: str,
    groups: dict[str, list[dict[str, Any]]],
    page_size: int,
) -> list[str]:
    written: list[str] = []
    for group_index, (group_title, entries) in enumerate(groups.items(), 1):
        chunks = [entries[index : index + page_size] for index in range(0, len(entries), page_size)]
        for chunk_index, chunk in enumerate(chunks, 1):
            suffix = f"-{chunk_index:02d}" if len(chunks) > 1 else ""
            filename = f"{group_index:02d}-{slug(group_title)}{suffix}.md"
            title = f"{source_name}: {group_title}"
            if len(chunks) > 1:
                title += f" ({chunk_index}/{len(chunks)})"
            rel_path = Path(target_rel) / filename
            body = _group_body(source_name, source_rel, group_title, chunk, len(entries), chunk_index, len(chunks))
            page = WikiPage(
                relative_path=rel_path,
                title=title,
                frontmatter=_frontmatter(
                    title=title,
                    source_name=source_name,
                    source_rel=source_rel,
                    summary=f"Lightweight source index for {len(chunk)} {group_title} documents.",
                    indexed_count=len(chunk),
                    total_group_count=len(entries),
                ),
                body=body,
            )
            write_wiki_page(root, page)
            written.append(rel_path.as_posix())
    return written


def _write_catalog_page(
    root: Path,
    target: Path,
    target_rel: str,
    source_name: str,
    source_rel: str,
    entries: list[dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
    written_pages: list[str],
) -> str:
    title = f"{source_name}: Source Catalog"
    rel_path = Path(target_rel) / "catalog.md"
    lines = [
        "This is a lightweight locator index. It records where each raw source document lives and what headings it contains; it does not summarize or replace the source documents.",
        "",
        "Use the raw paths below as inputs for targeted `wiki_ingest_llm` runs when a document needs deeper knowledge-page ingestion.",
        "",
        "## Coverage",
        "",
        f"- Source root: `{source_rel}`",
        f"- Indexed Markdown files: {len(entries)}",
        f"- Source index pages: {len(written_pages)}",
        "",
        "## Sections",
        "",
    ]
    by_title = _pages_by_group(written_pages)
    for group_title, group_entries in groups.items():
        lines.append(f"### {group_title}")
        lines.append("")
        lines.append(f"- Documents: {len(group_entries)}")
        for page in by_title.get(slug(group_title), []):
            lines.append(f"- Page: [[{Path(page).relative_to(Path(target_rel)).as_posix()}|{Path(page).stem}]]")
        lines.append("")
    page = WikiPage(
        relative_path=rel_path,
        title=title,
        frontmatter=_frontmatter(
            title=title,
            source_name=source_name,
            source_rel=source_rel,
            summary=f"Catalog for {len(entries)} lightweight raw source index entries.",
            indexed_count=len(entries),
            total_group_count=len(entries),
        ),
        body="\n".join(lines).rstrip(),
    )
    write_wiki_page(root, page)
    return rel_path.as_posix()


def _pages_by_group(written_pages: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for page in written_pages:
        name = Path(page).stem
        match = re.match(r"^\d+-(.+?)(?:-\d+)?$", name)
        key = match.group(1) if match else name
        grouped[key].append(page)
    return grouped


def _frontmatter(
    title: str,
    source_name: str,
    source_rel: str,
    summary: str,
    indexed_count: int,
    total_group_count: int,
) -> dict[str, Any]:
    return {
        "type": "source_index",
        "title": title,
        "generated": True,
        "source_name": source_name,
        "source_root": source_rel,
        "index_kind": "lightweight_source_index",
        "indexed_count": indexed_count,
        "total_group_count": total_group_count,
        "summary": summary,
        "tags": ["netsuite", "source-index", "help-docs"],
        "sources": [source_rel],
    }


def _group_body(
    source_name: str,
    source_rel: str,
    group_title: str,
    entries: list[dict[str, Any]],
    total_group_count: int,
    chunk_index: int,
    chunk_count: int,
) -> str:
    lines = [
        "This page is a lightweight source index for raw documentation. It is intended for query discovery and targeted follow-up ingestion.",
        "",
        "## Scope",
        "",
        f"- Source name: `{source_name}`",
        f"- Source root: `{source_rel}`",
        f"- Section: {group_title}",
        f"- Documents on this page: {len(entries)}",
        f"- Documents in section: {total_group_count}",
        f"- Page chunk: {chunk_index}/{chunk_count}",
        "",
        "## Documents",
        "",
    ]
    for index, entry in enumerate(entries, 1):
        lines.extend(_entry_lines(index, entry))
    return "\n".join(lines).rstrip()


def _entry_lines(index: int, entry: dict[str, Any]) -> list[str]:
    title = entry["title"]
    lines = [
        f"### {index}. {title}",
        "",
        f"- raw: `{entry['raw_path']}`",
    ]
    if entry.get("source"):
        lines.append(f"- url: {entry['source']}")
    if entry.get("toc_path"):
        lines.append(f"- toc: {' > '.join(entry['toc_path'])}")
    details = []
    if entry.get("type"):
        details.append(f"type={entry['type']}")
    if entry.get("depth") not in (None, ""):
        details.append(f"depth={entry['depth']}")
    if entry.get("published"):
        details.append(f"published={entry['published']}")
    details.append(f"sha256={entry['hash'][:16]}")
    lines.append(f"- metadata: {'; '.join(details)}")
    if entry.get("headings"):
        lines.append(f"- headings: {'; '.join(entry['headings'])}")
    if entry.get("keywords"):
        lines.append(f"- keywords: {', '.join(entry['keywords'])}")
    lines.append("")
    return lines


def _clear_existing_generated_pages(root: Path, target: Path) -> dict[str, Any]:
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "deleted": []}
    deleted: list[str] = []
    for path in sorted(target.rglob("*.md")):
        frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        if frontmatter.get("generated") is not True:
            return {
                "ok": False,
                "code": "manual_page_exists",
                "path": path.relative_to(root).as_posix(),
                "error": "refusing to overwrite non-generated source index page",
            }
        path.unlink()
        deleted.append(path.relative_to(root).as_posix())
    return {"ok": True, "deleted": deleted}


def _source_manifest_paths(source_dir: Path, source_rel: str) -> list[str]:
    paths = [source_rel]
    for name in ("_manifest.json", "_toc_manifest.json", "_path_aliases.json"):
        if (source_dir / name).exists():
            paths.append(f"{source_rel}/{name}")
    return paths


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def _read_text(path: Path) -> str:
    with open(_filesystem_path(path), encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _filesystem_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved
