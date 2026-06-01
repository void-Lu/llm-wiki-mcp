"""Knowledge graph insights: orphans, bridges, surprising connections, communities.

Analyzes the wikilink graph structure to surface:
- Isolated pages (no or very few connections)
- Bridge pages (connect multiple clusters)
- Surprising cross-type connections
- Louvain communities with cohesion scoring
- Sparse communities (low internal density)
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.louvain import LouvainResult, community_cohesion, louvain
from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page, split_frontmatter

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_STRUCTURAL_PAGES = {"index", "log", "overview"}


def wiki_insights(
    vault_root: str | Path,
    project: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Analyze wiki graph and return structural insights."""
    root = Path(vault_root).expanduser().resolve()
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return {"ok": True, "insights": [], "message": "no wiki directory"}

    nodes, edges = _build_graph(root, project)
    if len(nodes) < 3:
        return {"ok": True, "insights": [], "message": "too few pages for analysis"}

    edge_tuples = [(e["source"], e["target"]) for e in edges]
    louvain_result = louvain(set(nodes.keys()), edge_tuples)

    insights: list[dict[str, Any]] = []
    insights.extend(_find_orphans(nodes, edges, limit=limit))
    insights.extend(_find_bridges_louvain(nodes, edges, louvain_result, limit=3))
    insights.extend(_find_surprising_connections_louvain(nodes, edges, louvain_result, limit=3))
    insights.extend(_find_sparse_communities(nodes, edge_tuples, louvain_result, limit=3))

    community_info = _community_summary(nodes, edge_tuples, louvain_result)

    return {
        "ok": True,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "modularity": louvain_result.modularity,
        "communities": community_info,
        "insights": insights[:limit],
    }


def _build_graph(
    root: Path, project: str | None
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    wiki_dir = root / "wiki"
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    by_stem: dict[str, list[str]] = defaultdict(list)

    for path in sorted(wiki_dir.rglob("*.md")):
        if path.name == "log.md":
            continue
        rel = path.relative_to(root).as_posix()
        slug = path.stem
        if slug in _STRUCTURAL_PAGES:
            continue
        if project and not _in_scope(rel, project):
            continue
        try:
            page = read_markdown_page(path, root)
        except (OSError, UnicodeDecodeError):
            continue
        page_type = str(page.frontmatter.get("type") or _infer_type(rel))
        nodes[rel] = {
            "slug": slug,
            "title": page.title,
            "type": page_type,
            "path": rel,
            "degree": 0,
        }
        by_stem[slug.casefold()].append(rel)

    by_rel_set = set(nodes.keys())
    for rel in list(nodes.keys()):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        _, body = split_frontmatter(text)
        for target_text in _WIKILINK_RE.findall(body):
            target_rel = _resolve_target(target_text, path, root, by_rel_set, by_stem)
            if target_rel and target_rel != rel and target_rel in nodes:
                edges.append({"source": rel, "target": target_rel})
                nodes[rel]["degree"] = nodes[rel].get("degree", 0) + 1
                nodes[target_rel]["degree"] = nodes[target_rel].get("degree", 0) + 1

    return nodes, edges


def _find_orphans(
    nodes: dict[str, dict[str, Any]],
    _edges: list[dict[str, str]],
    limit: int,
) -> list[dict[str, Any]]:
    orphans = [n for n in nodes.values() if n["degree"] <= 1]
    orphans.sort(key=lambda n: (n["degree"], n["slug"]))
    if not orphans:
        return []

    top = orphans[:limit]
    return [{
        "type": "orphan_pages",
        "severity": "warning",
        "title": f"{len(orphans)} isolated page{'s' if len(orphans) != 1 else ''}",
        "pages": [{"path": n["path"], "title": n["title"], "degree": n["degree"]} for n in top],
        "suggestion": "Add [[wikilinks]] to connect these pages, or research to expand their content.",
    }]


def _find_bridges_louvain(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, str]],
    louvain_result: LouvainResult,
    limit: int,
) -> list[dict[str, Any]]:
    neighbors: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        neighbors[edge["source"]].add(edge["target"])
        neighbors[edge["target"]].add(edge["source"])

    bridge_scores: list[tuple[str, int]] = []
    for rel in nodes:
        if rel not in neighbors:
            continue
        neighbor_comms = {
            louvain_result.community_map.get(n)
            for n in neighbors[rel]
            if n in louvain_result.community_map
        }
        neighbor_comms.discard(None)
        own_comm = louvain_result.community_map.get(rel)
        neighbor_comms.discard(own_comm)
        if neighbor_comms:
            bridge_scores.append((rel, len(neighbor_comms) + 1))

    bridge_scores.sort(key=lambda x: -x[1])
    results: list[dict[str, Any]] = []
    for rel, comm_count in bridge_scores[:limit]:
        node = nodes[rel]
        results.append({
            "type": "bridge_page",
            "severity": "info",
            "title": f"Key bridge: {node['title']}",
            "path": rel,
            "communities_connected": comm_count,
            "suggestion": f"This page connects {comm_count} knowledge clusters. Keep it well-maintained.",
        })
    return results


def _find_surprising_connections_louvain(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, str]],
    louvain_result: LouvainResult,
    limit: int,
) -> list[dict[str, Any]]:
    scored: list[tuple[dict[str, str], float, list[str]]] = []
    for edge in edges:
        src = nodes.get(edge["source"])
        tgt = nodes.get(edge["target"])
        if not src or not tgt:
            continue
        score = 0.0
        reasons: list[str] = []
        src_comm = louvain_result.community_map.get(edge["source"])
        tgt_comm = louvain_result.community_map.get(edge["target"])
        if src_comm is not None and tgt_comm is not None and src_comm != tgt_comm:
            score += 3.0
            reasons.append("crosses community boundary")
        if src["type"] != tgt["type"]:
            score += 1.5
            reasons.append(f"connects {src['type']} to {tgt['type']}")
        if score >= 3.0:
            scored.append((edge, score, reasons))

    scored.sort(key=lambda x: -x[1])
    results: list[dict[str, Any]] = []
    for edge, score, reasons in scored[:limit]:
        src = nodes[edge["source"]]
        tgt = nodes[edge["target"]]
        results.append({
            "type": "surprising_connection",
            "severity": "info",
            "title": f"{src['title']} ↔ {tgt['title']}",
            "source_path": edge["source"],
            "target_path": edge["target"],
            "score": score,
            "reasons": reasons,
        })
    return results


def _find_sparse_communities(
    nodes: dict[str, dict[str, Any]],
    edge_tuples: list[tuple[str, str]],
    louvain_result: LouvainResult,
    limit: int,
) -> list[dict[str, Any]]:
    """Find communities with low internal cohesion (< 0.15)."""
    results: list[dict[str, Any]] = []
    for comm_id, members in enumerate(louvain_result.communities):
        if len(members) < 3:
            continue
        cohesion = community_cohesion(members, edge_tuples)
        if cohesion < 0.15:
            member_titles = sorted(
                nodes[m]["title"] for m in members if m in nodes
            )[:5]
            results.append({
                "type": "sparse_community",
                "severity": "warning",
                "title": f"Sparse cluster ({len(members)} pages, cohesion {cohesion:.2f})",
                "community_id": comm_id,
                "size": len(members),
                "cohesion": cohesion,
                "sample_members": member_titles,
                "suggestion": "This cluster has few internal links. Add [[wikilinks]] between related pages or split into separate topics.",
            })
    results.sort(key=lambda x: x["cohesion"])
    return results[:limit]


def _community_summary(
    nodes: dict[str, dict[str, Any]],
    edge_tuples: list[tuple[str, str]],
    louvain_result: LouvainResult,
) -> list[dict[str, Any]]:
    """Build summary info for each community."""
    summaries: list[dict[str, Any]] = []
    for comm_id, members in enumerate(louvain_result.communities):
        cohesion = community_cohesion(members, edge_tuples)
        member_titles = sorted(nodes[m]["title"] for m in members if m in nodes)
        summaries.append({
            "id": comm_id,
            "size": len(members),
            "cohesion": round(cohesion, 3),
            "members": member_titles[:10],
        })
    summaries.sort(key=lambda x: -x["size"])
    return summaries




def _resolve_target(
    target_text: str,
    source_path: Path,
    root: Path,
    by_rel: set[str],
    by_stem: dict[str, list[str]],
) -> str | None:
    target_path = Path(target_text)
    if target_path.suffix != ".md":
        target_path = target_path.with_suffix(".md")
    candidates = [
        (source_path.parent / target_path).resolve(),
        (root / "wiki" / target_path).resolve(),
        (root / target_path).resolve(),
    ]
    for candidate in candidates:
        try:
            rel = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in by_rel:
            return rel
    stem_matches = by_stem.get(Path(target_text).stem.casefold(), [])
    if len(stem_matches) == 1:
        return stem_matches[0]
    return None


def _in_scope(rel: str, project: str) -> bool:
    if rel.startswith(f"wiki/projects/{project}/"):
        return True
    if rel.startswith(("wiki/concepts/", "wiki/sources/", "wiki/synthesis/")):
        return True
    return False


def _infer_type(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "wiki":
        if parts[1] == "projects" and len(parts) >= 4:
            return parts[3]
        return parts[1]
    return "page"
