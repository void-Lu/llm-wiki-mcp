from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry, WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import slug


def wiki_synthesis(
    vault_root: str | Path,
    question: str,
    stage: str = "prepare",
    context_pages: list[dict[str, Any]] | None = None,
    synthesis: str | None = None,
    title: str | None = None,
    project: str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    pages = context_pages or []
    if stage == "prepare":
        return _prepare(root, question, pages, title, project, language)
    if stage == "apply":
        return _apply(root, question, pages, synthesis, title, project, language)
    return {"ok": False, "code": "invalid_stage", "error": "stage must be 'prepare' or 'apply'"}


def _prepare(
    root: Path,
    question: str,
    context_pages: list[dict[str, Any]],
    title: str | None,
    project: str | None,
    language: str,
) -> dict[str, Any]:
    prompt = _build_prompt(root, question, context_pages, title, project, language)
    return {
        "ok": True,
        "stage": "prepare",
        "question": question,
        "prompt": prompt,
        "expected_response_schema": {"body": "markdown synthesis body only, no frontmatter"},
        "next_call": {"tool": "wiki_synthesis", "stage": "apply", "required": ["synthesis"]},
    }


def _apply(
    root: Path,
    question: str,
    context_pages: list[dict[str, Any]],
    synthesis: str | None,
    title: str | None,
    project: str | None,
    language: str,
) -> dict[str, Any]:
    if not synthesis or not synthesis.strip():
        return {"ok": False, "code": "empty_synthesis", "error": "synthesis content is empty"}
    cleaned = _strip_thinking_blocks(synthesis).strip()
    today = date.today().isoformat()
    page_title = title or f"Synthesis: {question}"
    filename = f"synthesis-{slug(page_title)}-{today}.md"
    rel_path = Path("wiki") / "synthesis" / filename
    sources = [str(page.get("path")) for page in context_pages if page.get("path")]
    frontmatter: dict[str, Any] = {
        "type": "synthesis",
        "generated": True,
        "origin": "query-synthesis",
        "question": question,
        "created": today,
        "language": language,
        "sources": sources,
        "summary": f"Query synthesis for: {question}",
    }
    if project:
        frontmatter["project"] = project
    write_wiki_page(root, WikiPage(rel_path, frontmatter, page_title, cleaned))
    refresh_indexes(root)
    append_log_entry(root, WikiLogEntry(operation="synthesis", title=page_title, paths=[rel_path.as_posix()], sources=sources, project=project or "", status="ok"))
    return {"ok": True, "stage": "apply", "path": rel_path.as_posix(), "question": question}


def _build_prompt(root: Path, question: str, context_pages: list[dict[str, Any]], title: str | None, project: str | None, language: str) -> str:
    purpose = _read_optional(root / "purpose.md")
    schema = _read_optional(root / "schema.md")
    overview = _read_optional(root / "wiki" / "overview.md")
    index = _read_optional(root / "wiki" / "index.md")
    pages_text = "\n\n".join(
        "\n".join([
            f"[{idx}] {page.get('title') or page.get('path') or 'Context Page'}",
            f"Path: {page.get('path', '')}",
            str(page.get("content") or page.get("body") or page.get("snippet") or ""),
        ])
        for idx, page in enumerate(context_pages, 1)
    )
    return "\n".join([
        "You are writing a durable LLM Wiki synthesis page from a prior query.",
        f"Language: {language}",
        f"Project: {project or ''}",
        f"Title: {title or ''}",
        f"Question: {question}",
        "",
        "## purpose.md",
        purpose,
        "",
        "## schema.md",
        schema,
        "",
        "## wiki/overview.md",
        overview,
        "",
        "## wiki/index.md",
        index,
        "",
        "## Query Context Pages",
        pages_text,
        "",
        "Write a concise wiki page body. Preserve source traceability with [N] citations matching context pages. Note uncertainty, contradictions, follow-up questions, and durable decisions. Output body only; no frontmatter.",
    ])


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _strip_thinking_blocks(text: str) -> str:
    text = re.sub(r"<think(?:ing)?>\s*[\s\S]*?</think(?:ing)?>\s*", "", text)
    text = re.sub(r"<think(?:ing)?>\s*[\s\S]*$", "", text)
    return text.lstrip()
