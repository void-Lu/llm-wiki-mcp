from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.wiki_io import read_markdown_page

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def wiki_query(
    vault_root: str | Path,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    include_content: bool = True,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    tokens = _tokens(question)
    candidates = _candidate_pages(root)
    scored: list[tuple[float, Path, str]] = []
    for path in candidates:
        rel = path.relative_to(root).as_posix()
        text = _search_text(path, root)
        score = _score(text, tokens)
        if project and rel.startswith(f"wiki/projects/{project}/"):
            score += 5
        if score > 0:
            scored.append((score, path, text))
    scored.sort(key=lambda item: (-item[0], item[1].as_posix()))

    selected_paths: list[Path] = []
    for _, path, _ in scored[:top_k]:
        if path not in selected_paths:
            selected_paths.append(path)
        for linked in _linked_pages(path, root):
            if linked not in selected_paths and linked.exists():
                selected_paths.append(linked)
                if len(selected_paths) >= top_k:
                    break
        if len(selected_paths) >= top_k:
            break

    results = []
    context = []
    for index, path in enumerate(selected_paths[:top_k], 1):
        page = read_markdown_page(path, root)
        rel = path.relative_to(root).as_posix()
        snippet = _snippet(page.body, tokens)
        results.append({
            "path": rel,
            "title": page.title,
            "snippet": snippet,
            "score": next((score for score, scored_path, _ in scored if scored_path == path), 0),
            "frontmatter": page.frontmatter,
        })
        context.append({
            "citation": f"[{index}] {rel}",
            "path": rel,
            "title": page.title,
            "content": page.body if include_content else snippet,
            "frontmatter": page.frontmatter,
        })
    return {
        "ok": True,
        "question": question,
        "project": project or "",
        "results": results,
        "context": context,
        "policy": "Answer from wiki pages and cite returned citation paths.",
    }


def _candidate_pages(root: Path) -> list[Path]:
    wiki = root / "wiki"
    if not wiki.exists():
        return []
    return [path for path in sorted(wiki.rglob("*.md")) if path.name != "log.md"]


def _search_text(path: Path, root: Path) -> str:
    page = read_markdown_page(path, root)
    return "\n".join([path.relative_to(root).as_posix(), page.title, str(page.frontmatter), page.body]).casefold()


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    words = re.findall(r"[a-z0-9_]+", lowered)
    cjk_runs = re.findall(r"[一-鿿]+", lowered)
    cjk_tokens: list[str] = []
    for run in cjk_runs:
        cjk_tokens.append(run)
        cjk_tokens.extend(run[index : index + 2] for index in range(max(len(run) - 1, 0)))
    return [token for token in words + cjk_tokens if token]


def _score(text: str, tokens: list[str]) -> float:
    return float(sum(text.count(token) for token in tokens))


def _linked_pages(path: Path, root: Path) -> list[Path]:
    page = read_markdown_page(path, root)
    base_dir = path.parent
    linked = []
    for target in _WIKILINK_RE.findall(page.body):
        target_path = Path(target)
        if target_path.suffix != ".md":
            target_path = target_path.with_suffix(".md")
        resolved = (base_dir / target_path).resolve()
        if resolved.is_relative_to(root):
            linked.append(resolved)
    return linked


def _snippet(body: str, tokens: list[str], length: int = 180) -> str:
    folded = body.casefold()
    first = min((folded.find(token) for token in tokens if token in folded), default=-1)
    if first < 0:
        return body[:length]
    start = max(0, first - 40)
    return body[start : start + length]
