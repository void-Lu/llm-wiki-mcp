"""wiki_verify: two-stage grounding check for generated wiki pages against raw sources."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def wiki_verify(
    vault_root: str | Path,
    stage: str,
    project: str | None = None,
    page_path: str | None = None,
    verification_result: dict[str, Any] | str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)

    if stage == "prepare":
        return _prepare_verify(root, project, page_path, language)
    if stage == "apply":
        if verification_result is None:
            return {"ok": False, "code": "missing_verification_result", "error": "verification_result is required for apply"}
        return _apply_verify(root, verification_result)
    return {"ok": False, "code": "unsupported_stage", "error": f"unsupported stage: {stage}"}


def _prepare_verify(root: Path, project: str | None, page_path: str | None, language: str) -> dict[str, Any]:
    index_pages = _collect_index_pages(root, project, page_path)
    if not index_pages:
        return {"ok": False, "code": "no_pages", "error": "no source index pages found to verify"}

    verification_items: list[dict[str, Any]] = []
    for index_rel_path in index_pages:
        index_file = root / index_rel_path
        if not index_file.is_file():
            continue
        index_page = read_markdown_page(index_file)
        if not index_page.frontmatter.get("generated"):
            continue

        raw_sources = index_page.frontmatter.get("sources", [])
        source_texts: list[dict[str, str]] = []
        for src_path in raw_sources:
            src_file = root / src_path
            if src_file.is_file():
                source_texts.append({
                    "path": src_path,
                    "content": src_file.read_text(encoding="utf-8", errors="ignore")[:8000],
                })
        if not source_texts:
            continue

        linked_pages = _resolve_wikilinks(root, index_page.body)
        page_contents: list[dict[str, str]] = []
        for linked_path in linked_pages:
            linked_file = root / linked_path
            if not linked_file.is_file():
                continue
            linked = read_markdown_page(linked_file)
            if not linked.frontmatter.get("generated"):
                continue
            page_contents.append({
                "path": linked_path,
                "title": linked.title,
                "summary": str(linked.frontmatter.get("summary", "")),
                "body": linked.body[:4000],
            })

        if not page_contents:
            continue

        verification_items.append({
            "index_path": index_rel_path,
            "sources": source_texts,
            "pages": page_contents,
        })

    if not verification_items:
        return {"ok": False, "code": "no_verifiable_pages", "error": "no source index pages with accessible sources and linked pages found"}

    prompt = _verify_prompt(verification_items, language)
    return {
        "ok": True,
        "stage": "prepare",
        "status": "needs_model",
        "pages_to_verify": sum(len(item["pages"]) for item in verification_items),
        "prompt": prompt,
        "expected_response_schema": {
            "results": [{
                "page_path": "string",
                "faithful": True,
                "score": 0.95,
                "issues": [{"claim": "string", "source_evidence": "string", "severity": "minor|major|hallucination"}],
            }],
        },
        "next_call": {"tool": "wiki_verify", "stage": "apply", "required": ["verification_result"]},
    }


def _apply_verify(root: Path, verification_result: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(verification_result, str):
        try:
            verification_result = json.loads(verification_result)
        except json.JSONDecodeError:
            return {"ok": False, "code": "invalid_verification_result", "error": "verification_result must be JSON"}
    if not isinstance(verification_result, dict):
        return {"ok": False, "code": "invalid_verification_result", "error": "verification_result must be a JSON object"}

    results = verification_result.get("results", [])
    if not results:
        return {"ok": False, "code": "empty_results", "error": "verification_result.results is empty"}

    summary: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        page_path = str(item.get("page_path", ""))
        faithful = bool(item.get("faithful", True))
        score = item.get("score", 1.0)
        issues = item.get("issues", [])
        summary.append({
            "page_path": page_path,
            "faithful": faithful,
            "score": score,
            "issue_count": len(issues),
            "issues": issues,
        })

    unfaithful = [s for s in summary if not s["faithful"]]
    return {
        "ok": True,
        "stage": "apply",
        "verified": len(summary),
        "faithful": len(summary) - len(unfaithful),
        "unfaithful": len(unfaithful),
        "results": summary,
    }


def _collect_index_pages(root: Path, project: str | None, page_path: str | None) -> list[str]:
    if page_path:
        return [page_path]
    sources_dir = root / "wiki" / "sources"
    if not sources_dir.is_dir():
        return []
    pages: list[str] = []
    if project:
        for subdir in ("concepts", "projects"):
            d = sources_dir / subdir / project
            if d.is_dir():
                for f in sorted(d.rglob("*.md")):
                    pages.append(f.relative_to(root).as_posix())
    else:
        for f in sorted(sources_dir.rglob("*.md")):
            pages.append(f.relative_to(root).as_posix())
    return pages


def _resolve_wikilinks(root: Path, body: str) -> list[str]:
    wiki_dir = root / "wiki"
    if not wiki_dir.is_dir():
        return []
    link_names = _WIKILINK_RE.findall(body)
    resolved: list[str] = []
    for name in link_names:
        matches = list(wiki_dir.rglob(f"{name}.md"))
        if matches:
            resolved.append(matches[0].relative_to(root).as_posix())
    return resolved


def _verify_prompt(items: list[dict[str, Any]], language: str) -> str:
    parts = [
        f"Verify the faithfulness of generated wiki pages against their raw sources. Respond in {language}.",
        "For each page, check whether claims in the page body are supported by the source material.",
        "Return JSON matching expected_response_schema.",
        "",
    ]
    for i, item in enumerate(items, 1):
        parts.append(f"## Source group {i}: {item['index_path']}")
        parts.append("")
        for src in item["sources"]:
            parts.append(f"### Raw source: {src['path']}")
            parts.append(src["content"])
            parts.append("")
        for page in item["pages"]:
            parts.append(f"### Generated page: {page['path']}")
            parts.append(f"Title: {page['title']}")
            parts.append(f"Summary: {page['summary']}")
            parts.append(f"Body (truncated):\n{page['body']}")
            parts.append("")
    return "\n".join(parts)
