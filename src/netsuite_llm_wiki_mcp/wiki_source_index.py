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

    section_entries, interior_nodes = _build_tag_index_tree(entries, source_name)

    # 确定需要写 index 的节点集合：所有有章节条目落入的父路径并上所有 interior 节点。
    node_paths: set[tuple[str, ...]] = {parent for parent, _ in section_entries.keys()}
    node_paths |= interior_nodes
    # 根 () 始终写一份根 index
    node_paths.add(())

    written: list[str] = []
    group_summary: list[dict[str, Any]] = []
    for node_path in sorted(node_paths, key=lambda p: (len(p), [seg.casefold() for seg in p])):
        page_written = _write_node_index(
            root, target, target_rel, source_name, source_rel,
            node_path=node_path,
            section_entries=section_entries,
            interior_nodes=interior_nodes,
            page_size=page_size,
        )
        written.extend(page_written)
        total_at_node = sum(
            len(section_entries.get((node_path, leaf), []))
            for parent, leaf in section_entries.keys()
            if parent == node_path
        )
        group_summary.append({
            "tag_path": "/".join(node_path),
            "count": total_at_node,
        })

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
        "groups": group_summary,
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


def _child_sort_key(child: str) -> str:
    return child.casefold()


def _node_relative_path(node_path: tuple[str, ...]) -> str:
    return "/".join(node_path)


def _node_display_path(node_path: tuple[str, ...], source_name: str) -> str:
    return _node_relative_path(node_path) or source_name


def _write_node_index(
    root: Path,
    target: Path,
    target_rel: str,
    source_name: str,
    source_rel: str,
    node_path: tuple[str, ...],
    section_entries: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]],
    interior_nodes: set[tuple[str, ...]],
    page_size: int,
) -> list[str]:
    """Write the _entries.md (and pagination _entries-NN.md) for one tree node.

    `node_path` is an absolute tree path tuple (root node is ()).  Children
    are the unique direct child segments of `node_path`.  For each child we
    emit a `## {child}` section containing:
      - any raw entries whose (parent==node_path, leaf==child) landed here
      - a `-> [[child/.../_entries|child/.../_entries]]` navigation row when
        `(*node_path, child)` is itself an interior node.
    """
    children = {
        leaf for parent, leaf in section_entries.keys() if parent == node_path
    } | {
        path[len(node_path)] for path in interior_nodes
        if len(path) > len(node_path) and path[: len(node_path)] == node_path
    }
    children = sorted(children, key=_child_sort_key)

    node_rel_dir = "/".join((*Path(target_rel).parts, *node_path)) if node_path else target_rel
    section_blocks: list[tuple[str, list[dict[str, Any]], bool, str]] = []
    for child in children:
        direct_key = (node_path, child)
        child_entry_list = list(section_entries.get(direct_key, []))
        child_entry_list.sort(
            key=lambda entry: (
                _toc_sort_key(entry),
                str(entry["title"]).casefold(),
                str(entry["raw_path"]).casefold(),
            )
        )
        child_node_path = (*node_path, child)
        is_child_interior = child_node_path in interior_nodes
        child_index_rel = f"{node_rel_dir}/{child}/_entries"
        section_blocks.append((child, child_entry_list, is_child_interior, child_index_rel))

    if node_path == (_UNGROUPED_MARKER,):
        section_blocks = [
            (entry["title"], [entry], False, "")
            for entry in section_entries.get((node_path, _UNGROUPED_LEAF), [])
        ]
        section_blocks.sort(key=lambda block: block[0].casefold())

    # Build the flat grouped_entries and per-section [start, end) global offsets
    # into that flat list. Offsets are computed over the FINAL section_blocks
    # (i.e. after the ungrouped override above), so they match what will render.
    grouped_entries: list[dict[str, Any]] = []
    section_offsets: list[tuple[int, int]] = []
    running = 0
    for _, entries, _, _ in section_blocks:
        start = running
        grouped_entries.extend(entries)
        running += len(entries)
        section_offsets.append((start, running))

    chunk_count = max(1, (len(grouped_entries) + page_size - 1) // page_size)
    written: list[str] = []
    node_dir = target.joinpath(*node_path) if node_path else target
    if node_dir.resolve() != target.resolve() and not node_dir.resolve().is_relative_to(target.resolve()):
        raise WikiWriteError(f"node dir escapes target: {node_dir}")

    per_chunk = max(1, page_size) if page_size else len(grouped_entries)
    for chunk_index in range(1, chunk_count + 1):
        filename = "_entries.md" if chunk_index == 1 else f"_entries-{chunk_index:02d}.md"
        rel_path = f"{node_rel_dir}/{filename}"
        full_path = root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_start = (chunk_index - 1) * per_chunk
        chunk_end = min(chunk_start + per_chunk, len(grouped_entries))
        body = _node_index_body(
            source_name, source_rel, node_path,
            section_blocks, section_offsets, grouped_entries,
            chunk_start, chunk_end, chunk_index, chunk_count,
        )
        title_tag = _node_display_path(node_path, source_name)
        title = f"{source_name}: {title_tag}"
        if chunk_count > 1:
            title += f" ({chunk_index}/{chunk_count})"
        page = WikiPage(
            relative_path=Path(rel_path),
            title=title,
            frontmatter=_frontmatter(
                title=title,
                source_name=source_name,
                source_rel=source_rel,
                summary=f"Lightweight source index for {title_tag}.",
                indexed_count=chunk_end - chunk_start,
                total_group_count=len(grouped_entries),
                tag_path=_node_relative_path(node_path),
            ),
            body=body,
        )
        write_wiki_page(root, page)
        written.append(rel_path)
    return written


def _node_index_body(
    source_name: str,
    source_rel: str,
    node_path: tuple[str, ...],
    section_blocks: list[tuple[str, list[dict[str, Any]], bool, str]],
    section_offsets: list[tuple[int, int]],
    grouped_entries: list[dict[str, Any]],
    chunk_start: int,
    chunk_end: int,
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
        f"- Node tag path: `{_node_relative_path(node_path) or '(root)'}`",
        f"- Documents on this page: {chunk_end - chunk_start}",
        f"- Page chunk: {chunk_index}/{chunk_count}",
        "",
    ]
    for (child, child_entries, is_child_interior, child_index_rel), (section_start, section_end) in zip(section_blocks, section_offsets):
        child_lines: list[str] = [f"## {child}", ""]
        # Slice this child's contribution by intersecting its [section_start, section_end)
        # with the chunk's [chunk_start, chunk_end) range — index-based, NOT `in`.
        # This is the fix for the same-parent-sibling-mirror bug: an entry dict
        # mirrored into two sibling children is `==` to itself, so the old
        # `chunk_entries[cursor] in child_entries` allocation matched the wrong
        # sibling's list and swallowed the second sibling's section.
        slice_start = max(section_start, chunk_start)
        slice_end = min(section_end, chunk_end)
        chunk_child = grouped_entries[slice_start:slice_end] if slice_end > slice_start else []
        for index, entry in enumerate(chunk_child, 1):
            child_lines.extend(_entry_lines(index, entry))
        if is_child_interior:
            child_lines.append(f"- → [[{child_index_rel}|{child}/_entries]]")
            child_lines.append("")
        if len(child_lines) > 2:
            lines.extend(child_lines)
    return "\n".join(lines).rstrip()


def _frontmatter(
    title: str,
    source_name: str,
    source_rel: str,
    summary: str,
    indexed_count: int,
    total_group_count: int,
    tag_path: str = "",
) -> dict[str, Any]:
    return {
        "type": "source_index",
        "title": title,
        "generated": True,
        "source_name": source_name,
        "source_root": source_rel,
        "tag_path": tag_path,
        "index_kind": "lightweight_source_index",
        "indexed_count": indexed_count,
        "total_group_count": total_group_count,
        "summary": summary,
        "tags": ["netsuite", "source-index", "help-docs"],
        "sources": [source_rel],
    }


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
