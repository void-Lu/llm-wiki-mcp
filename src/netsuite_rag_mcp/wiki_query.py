from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.wiki_io import read_markdown_page, split_frontmatter

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


@dataclass
class QueryCandidate:
    path: Path
    rel: str
    title: str
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    source_kind: str = "wiki"
    keyword_score: float = 0.0
    vector_score: float = 0.0
    graph_score: float = 0.0

    @property
    def total_score(self) -> float:
        return self.keyword_score + self.vector_score + self.graph_score


@dataclass(frozen=True)
class Graph:
    neighbors: dict[str, set[str]]
    sources: dict[str, set[str]]
    types: dict[str, str]


def wiki_query(
    vault_root: str | Path,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    include_content: bool = True,
    context_window_tokens: int = 16_000,
    include_context_pack: bool = True,
    chat_history: list[dict[str, str]] | None = None,
    language: str = "zh-CN",
    enable_vector: bool = False,
    vector_config: dict[str, Any] | None = None,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    tokens = _tokens(question)
    candidates = _candidate_pages(root, include_raw_sources=include_raw_sources)
    if project:
        candidates = [candidate for candidate in candidates if _in_project_scope(candidate.rel, project)]
    graph = _build_graph(root)

    scored: dict[str, QueryCandidate] = {}
    for candidate in candidates:
        score = _keyword_score(candidate, tokens)
        if project and candidate.rel.startswith(f"wiki/projects/{project}/"):
            score += 5
        if score > 0:
            candidate.keyword_score = score
            scored[candidate.rel] = candidate

    vector_warnings = _apply_optional_vector_stage(scored, enable_vector, vector_config)
    _apply_graph_expansion(scored, candidates, graph, max_graph_hops=max_graph_hops)

    selected = sorted(scored.values(), key=lambda item: (-item.total_score, item.rel))[:top_k]
    results = [_result_item(candidate, tokens) for candidate in selected]
    context = _legacy_context(selected, tokens, include_content)
    context_pack = _context_pack(root, selected, question, context_window_tokens, chat_history or [], language) if include_context_pack else None

    return {
        "ok": True,
        "question": question,
        "project": project or "",
        "results": results,
        "context": context,
        "context_pack": context_pack,
        "budget": context_pack.get("budget") if context_pack else {},
        "citations": context_pack.get("citations") if context_pack else [item["citation"] for item in context],
        "pipeline": {
            "stage_1_keyword_hits": sum(1 for item in scored.values() if item.keyword_score > 0),
            "stage_1_raw_sources_included": include_raw_sources,
            "stage_1_5_vector_enabled": enable_vector,
            "stage_1_5_vector_warnings": vector_warnings,
            "stage_2_graph_hops": max_graph_hops,
            "stage_2_graph_hits": sum(1 for item in scored.values() if item.graph_score > 0),
            "stage_3_context_window_tokens": context_window_tokens,
            "stage_4_context_pack_enabled": include_context_pack,
        },
        "policy": "Answer from the numbered context pages and cite sources as [1], [2], etc.",
    }


def wiki_query_debug(
    vault_root: str | Path,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
) -> dict[str, Any]:
    result = wiki_query(
        vault_root=vault_root,
        question=question,
        project=project,
        top_k=top_k,
        include_content=False,
        include_context_pack=False,
        max_graph_hops=max_graph_hops,
        include_raw_sources=include_raw_sources,
    )
    root = Path(vault_root).expanduser().resolve()
    graph = _build_graph(root)
    graph_reasons: dict[str, list[dict[str, Any]]] = {}
    selected_paths = [item["path"] for item in result["results"]]
    seed_paths = [item["path"] for item in result["results"] if item["scores"]["keyword"] > 0]
    for path in selected_paths:
        reasons: list[dict[str, Any]] = []
        for seed in seed_paths:
            if seed == path:
                continue
            reasons.extend(_relationship_reasons(seed, path, graph))
        if reasons:
            graph_reasons[path] = reasons
    return {**result, "graph_reasons": graph_reasons}


def _relationship_reasons(left: str, right: str, graph: Graph) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if right in graph.neighbors.get(left, set()):
        reasons.append({"kind": "direct_wikilink", "source": left, "target": right, "score": 3.0})
    shared_sources = sorted(graph.sources.get(left, set()) & graph.sources.get(right, set()))
    for source in shared_sources:
        reasons.append({"kind": "shared_source", "source": left, "target": right, "value": source, "score": 4.0})
    common = sorted(graph.neighbors.get(left, set()) & graph.neighbors.get(right, set()))
    for neighbor in common:
        degree = len(graph.neighbors.get(neighbor, set()))
        if degree > 1:
            reasons.append({"kind": "common_neighbor", "source": left, "target": right, "value": neighbor, "score": 1.5 / math.log(degree + 1)})
    if graph.types.get(left) and graph.types.get(left) == graph.types.get(right):
        reasons.append({"kind": "same_type", "source": left, "target": right, "value": graph.types[left], "score": 1.0})
    return reasons


def _candidate_pages(root: Path, include_raw_sources: bool = False) -> list[QueryCandidate]:
    candidates: list[QueryCandidate] = []
    wiki = root / "wiki"
    if wiki.exists():
        for path in sorted(wiki.rglob("*.md")):
            if path.name == "log.md":
                continue
            candidates.append(_wiki_candidate(path, root))
    raw_sources = root / "raw" / "sources"
    if include_raw_sources and raw_sources.exists():
        for path in sorted(raw_sources.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}:
                candidates.append(_raw_candidate(path, root))
    return candidates


def _in_project_scope(rel: str, project: str) -> bool:
    parts = Path(rel).parts
    if rel.startswith(f"wiki/projects/{project}/"):
        return True
    if rel.startswith(("wiki/concepts/", "wiki/sources/", "wiki/synthesis/", "wiki/comparisons/")):
        return True
    if len(parts) >= 5 and parts[0] == "raw" and parts[1] == "sources" and parts[3] == project:
        return True
    return False


def _wiki_candidate(path: Path, root: Path) -> QueryCandidate:
    page = read_markdown_page(path, root)
    return QueryCandidate(
        path=path,
        rel=path.relative_to(root).as_posix(),
        title=page.title,
        body=page.body,
        frontmatter=page.frontmatter,
        source_kind="wiki",
    )


def _raw_candidate(path: Path, root: Path) -> QueryCandidate:
    text = path.read_text(encoding="utf-8", errors="ignore")
    title = path.stem
    if path.suffix.lower() == ".md":
        frontmatter, body = split_frontmatter(text)
        title = str(frontmatter.get("title") or _first_heading(body) or path.stem)
        text = body
    else:
        frontmatter = {}
    return QueryCandidate(
        path=path,
        rel=path.relative_to(root).as_posix(),
        title=title,
        body=text,
        frontmatter=frontmatter,
        source_kind="raw",
    )


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    words = [word for word in re.findall(r"[a-z0-9_]+", lowered) if word not in _STOPWORDS]
    cjk_runs = re.findall(r"[一-鿿]+", lowered)
    cjk_tokens: list[str] = []
    for run in cjk_runs:
        if len(run) == 1:
            cjk_tokens.append(run)
        else:
            cjk_tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return [token for token in words + cjk_tokens if token]


def _keyword_score(candidate: QueryCandidate, tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    text = _search_text(candidate)
    title = candidate.title.casefold()
    score = float(sum(text.count(token) for token in tokens))
    if any(token in title for token in tokens):
        score += 10
    return score


def _search_text(candidate: QueryCandidate) -> str:
    return "\n".join([candidate.rel, candidate.title, str(candidate.frontmatter), candidate.body]).casefold()


def _apply_optional_vector_stage(scored: dict[str, QueryCandidate], enable_vector: bool, vector_config: dict[str, Any] | None) -> list[dict[str, str]]:
    if not enable_vector:
        return []
    if not vector_config:
        return [{"code": "vector_config_missing", "message": "vector search was requested but vector_config was not provided"}]
    return [{"code": "vector_backend_not_configured", "message": "optional vector search is not initialized in this MCP server yet; keyword and graph stages were used"}]


def _build_graph(root: Path) -> Graph:
    wiki = root / "wiki"
    pages = [path for path in sorted(wiki.rglob("*.md")) if path.name != "log.md"] if wiki.exists() else []
    by_rel = {path.relative_to(root).as_posix(): path for path in pages}
    by_stem: dict[str, list[str]] = {}
    for rel, path in by_rel.items():
        by_stem.setdefault(path.stem.casefold(), []).append(rel)

    neighbors = {rel: set() for rel in by_rel}
    sources: dict[str, set[str]] = {}
    types: dict[str, str] = {}
    for rel, path in by_rel.items():
        page = read_markdown_page(path, root)
        sources[rel] = {str(item) for item in _as_list(page.frontmatter.get("sources"))}
        types[rel] = str(page.frontmatter.get("type") or _path_type(rel))
        for target in _wikilink_targets(page.body, path, root, by_rel, by_stem):
            neighbors[rel].add(target)
            neighbors.setdefault(target, set()).add(rel)
    return Graph(neighbors=neighbors, sources=sources, types=types)


def _wikilink_targets(body: str, path: Path, root: Path, by_rel: dict[str, Path], by_stem: dict[str, list[str]]) -> list[str]:
    targets = []
    for target in _WIKILINK_RE.findall(body):
        target_path = Path(target)
        candidates: list[Path] = []
        if target_path.suffix != ".md":
            target_path = target_path.with_suffix(".md")
        candidates.extend([(path.parent / target_path).resolve(), (root / "wiki" / target_path).resolve(), (root / target_path).resolve()])
        matched = ""
        for candidate in candidates:
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in by_rel:
                matched = rel
                break
        if not matched:
            stem_matches = by_stem.get(Path(target).stem.casefold(), [])
            if len(stem_matches) == 1:
                matched = stem_matches[0]
        if matched:
            targets.append(matched)
    return targets


def _apply_graph_expansion(scored: dict[str, QueryCandidate], all_candidates: list[QueryCandidate], graph: Graph, max_graph_hops: int) -> None:
    candidates_by_rel = {candidate.rel: candidate for candidate in all_candidates}
    seeds = [rel for rel, candidate in scored.items() if candidate.source_kind == "wiki"]
    for seed in seeds:
        frontier = {seed}
        visited = {seed}
        for hop in range(1, max_graph_hops + 1):
            next_frontier: set[str] = set()
            for rel in frontier:
                next_frontier.update(graph.neighbors.get(rel, set()) - visited)
            decay = 1 / hop
            for rel in next_frontier:
                candidate = candidates_by_rel.get(rel)
                if candidate is None:
                    continue
                candidate.graph_score += _relationship_score(seed, rel, graph) * decay
                scored.setdefault(rel, candidate)
            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break


def _relationship_score(left: str, right: str, graph: Graph) -> float:
    score = 0.0
    if right in graph.neighbors.get(left, set()):
        score += 3.0
    shared_sources = graph.sources.get(left, set()) & graph.sources.get(right, set())
    if shared_sources:
        score += 4.0 * len(shared_sources)
    common = graph.neighbors.get(left, set()) & graph.neighbors.get(right, set())
    for neighbor in common:
        degree = len(graph.neighbors.get(neighbor, set()))
        if degree > 1:
            score += 1.5 / math.log(degree + 1)
    if graph.types.get(left) and graph.types.get(left) == graph.types.get(right):
        score += 1.0
    return score


def _result_item(candidate: QueryCandidate, tokens: list[str]) -> dict[str, Any]:
    return {
        "path": candidate.rel,
        "title": candidate.title,
        "snippet": _snippet(candidate.body, tokens),
        "score": candidate.total_score,
        "scores": {
            "keyword": candidate.keyword_score,
            "vector": candidate.vector_score,
            "graph": candidate.graph_score,
        },
        "frontmatter": candidate.frontmatter,
        "source_kind": candidate.source_kind,
    }


def _legacy_context(candidates: list[QueryCandidate], tokens: list[str], include_content: bool) -> list[dict[str, Any]]:
    context = []
    for index, candidate in enumerate(candidates, 1):
        snippet = _snippet(candidate.body, tokens)
        context.append({
            "citation": f"[{index}] {candidate.rel}",
            "path": candidate.rel,
            "title": candidate.title,
            "content": candidate.body if include_content else snippet,
            "frontmatter": candidate.frontmatter,
        })
    return context


def _context_pack(
    root: Path,
    candidates: list[QueryCandidate],
    question: str,
    context_window_tokens: int,
    chat_history: list[dict[str, str]],
    language: str,
) -> dict[str, Any]:
    total = max(4_000, min(1_000_000, context_window_tokens))
    budgets = {
        "wiki_pages": int(total * 0.60),
        "chat_history": int(total * 0.20),
        "index": int(total * 0.05),
        "system": total - int(total * 0.60) - int(total * 0.20) - int(total * 0.05),
    }
    system_text = _system_prompt(root, question, language)
    index_text = _read_optional(root / "wiki" / "index.md")
    chat_text = _chat_history_text(chat_history)

    used = {
        "system": _token_count(_truncate_tokens(system_text, budgets["system"])),
        "index": _token_count(_truncate_tokens(index_text, budgets["index"])),
        "chat_history": _token_count(_truncate_tokens(chat_text, budgets["chat_history"])),
        "wiki_pages": 0,
    }
    pages = []
    remaining = budgets["wiki_pages"]
    citations = []
    for index, candidate in enumerate(candidates, 1):
        text = _numbered_page(index, candidate)
        chunk = _truncate_tokens(text, remaining)
        if not chunk.strip():
            break
        page_tokens = _token_count(chunk)
        used["wiki_pages"] += page_tokens
        remaining -= page_tokens
        pages.append({"citation": f"[{index}]", "path": candidate.rel, "title": candidate.title, "content": chunk})
        citations.append({"citation": f"[{index}]", "path": candidate.rel, "title": candidate.title})
        if remaining <= 0:
            break

    return {
        "system": _truncate_tokens(system_text, budgets["system"]),
        "index": _truncate_tokens(index_text, budgets["index"]),
        "chat_history": _truncate_tokens(chat_text, budgets["chat_history"]),
        "pages": pages,
        "citations": citations,
        "budget": {"total": total, "allocated": budgets, "used": used},
    }


def _system_prompt(root: Path, question: str, language: str) -> str:
    purpose = _read_optional(root / "purpose.md")
    schema = _read_optional(root / "schema.md")
    return "\n".join([
        "You answer using the numbered LLM Wiki context pages.",
        f"Language: {language}",
        "Cite sources with [1], [2], etc. Do not cite pages that are not in the context pack.",
        f"Question: {question}",
        "\n# purpose.md",
        purpose,
        "\n# schema.md",
        schema,
    ])


def _numbered_page(index: int, candidate: QueryCandidate) -> str:
    return "\n".join([
        f"[{index}] {candidate.title}",
        f"Path: {candidate.rel}",
        f"Frontmatter: {candidate.frontmatter}",
        "",
        candidate.body,
    ])


def _chat_history_text(chat_history: list[dict[str, str]]) -> str:
    lines = []
    for message in chat_history:
        role = str(message.get("role", "message"))
        content = str(message.get("content", ""))
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _truncate_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    return text if len(text) <= max_chars else text[:max_chars].rstrip()


def _token_count(text: str) -> int:
    return math.ceil(len(text) / 4)


def _snippet(body: str, tokens: list[str], length: int = 180) -> str:
    folded = body.casefold()
    first = min((folded.find(token) for token in tokens if token in folded), default=-1)
    if first < 0:
        return body[:length]
    start = max(0, first - 40)
    return body[start : start + length]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _path_type(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "wiki":
        if parts[1] == "projects" and len(parts) >= 4:
            return parts[3]
        return parts[1]
    return "page"


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
