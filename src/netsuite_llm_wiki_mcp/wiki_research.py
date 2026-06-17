"""Deep research synthesis: web search results → wiki page.

The MCP tool accepts pre-collected search results (the caller is
responsible for the actual web search), synthesizes them via an LLM
prompt, and writes the result to wiki/queries/.

Two-stage design:
  stage="prepare": accepts search results, returns synthesis prompt.
  stage="apply": accepts LLM synthesis, writes wiki page.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry
from netsuite_llm_wiki_mcp.wiki_paths import safe_segment


def wiki_research(
    vault_root: str | Path,
    topic: str,
    stage: str = "prepare",
    search_results: list[dict[str, str]] | None = None,
    synthesis: str | None = None,
    language: str = "zh-CN",
    project: str | None = None,
) -> dict[str, Any]:
    """Two-stage research synthesis.

    stage="prepare": build synthesis prompt from search results.
    stage="apply": write synthesis to wiki/queries/.
    """
    root = Path(vault_root).expanduser().resolve()

    if stage == "prepare":
        return _prepare(root, topic, search_results or [], language)
    elif stage == "apply":
        return _apply(root, topic, synthesis, language, project)
    else:
        return {"ok": False, "code": "invalid_stage", "error": "stage must be 'prepare' or 'apply'"}


def _prepare(
    root: Path,
    topic: str,
    search_results: list[dict[str, str]],
    language: str,
) -> dict[str, Any]:
    if not search_results:
        return {"ok": False, "code": "no_results", "error": "search_results is empty"}

    index_content = ""
    index_path = root / "wiki" / "index.md"
    if index_path.exists():
        index_content = index_path.read_text(encoding="utf-8")
    purpose_content = ""
    purpose_path = root / "purpose.md"
    if purpose_path.exists():
        purpose_content = purpose_path.read_text(encoding="utf-8")
    overview_content = ""
    overview_path = root / "wiki" / "overview.md"
    if overview_path.exists():
        overview_content = overview_path.read_text(encoding="utf-8")

    prompt = _build_synthesis_prompt(topic, search_results, index_content, purpose_content, overview_content, language)

    return {
        "ok": True,
        "stage": "prepare",
        "topic": topic,
        "result_count": len(search_results),
        "prompt": prompt,
        "instruction": "Feed this prompt to an LLM. Pass the response back via stage='apply' with synthesis parameter.",
    }


def _apply(
    root: Path,
    topic: str,
    synthesis: str | None,
    language: str,
    project: str | None,
) -> dict[str, Any]:
    if not synthesis or not synthesis.strip():
        return {"ok": False, "code": "empty_synthesis", "error": "synthesis content is empty"}

    cleaned = _strip_thinking_blocks(synthesis)
    today = date.today().isoformat()
    year, month, day = today.split("-")
    slug = _topic_to_slug(topic)
    filename = f"research-{slug}-{today}.md"

    queries_dir = root / "wiki" / "queries" / year / month / day / slug
    queries_dir.mkdir(parents=True, exist_ok=True)
    target = queries_dir / filename

    frontmatter = {
        "type": "query",
        "title": f"Research: {topic}",
        "created": today,
        "origin": "deep-research",
        "generated": True,
        "tags": ["research"],
        "language": language,
    }
    if project:
        frontmatter["project"] = project

    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    content = f"---\n{yaml_text}\n---\n\n# Research: {topic}\n\n{cleaned.strip()}\n"
    target.write_text(content, encoding="utf-8")

    rel_path = target.relative_to(root).as_posix()
    refresh_indexes(root)
    append_log_entry(root, WikiLogEntry(
        operation="research",
        title=f"Research: {topic}",
        paths=[rel_path],
        project=project or "",
        status="ok",
    ))

    return {
        "ok": True,
        "stage": "apply",
        "path": rel_path,
        "topic": topic,
    }


def _build_synthesis_prompt(
    topic: str,
    search_results: list[dict[str, str]],
    index_content: str,
    purpose_content: str,
    overview_content: str,
    language: str,
) -> str:
    results_text = "\n\n".join(
        f"[{i + 1}] **{r.get('title', 'Untitled')}** ({r.get('url', r.get('source', ''))})\n{r.get('snippet', r.get('content', ''))}"
        for i, r in enumerate(search_results)
    )

    purpose_section = f"\n\n## purpose.md\n{purpose_content}" if purpose_content else ""
    overview_section = f"\n\n## wiki/overview.md\n{overview_content}" if overview_content else ""
    index_section = f"\n\n## Existing Wiki Index (link to these pages with [[wikilink]])\n{index_content}" if index_content else ""

    return (
        "You are a research assistant. Synthesize the search results into a comprehensive wiki page.\n\n"
        f"Language: {language}\n\n"
        "## Cross-referencing\n"
        "- When your synthesis mentions an entity or concept that exists in the wiki index below, use [[wikilink]] syntax.\n\n"
        "## Writing Rules\n"
        "- Organize into clear sections with headings\n"
        "- Cite sources using [N] notation\n"
        "- Note contradictions or gaps\n"
        "- Neutral, encyclopedic tone\n"
        "- Output ONLY the body content (no frontmatter)\n"
        f"{purpose_section}"
        f"{overview_section}"
        f"{index_section}\n\n"
        f"## Research Topic: {topic}\n\n"
        f"## Search Results\n\n{results_text}\n\n"
        "Synthesize into a wiki page body now:"
    )


def _topic_to_slug(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9\s-]", "", topic.lower()).strip()
    slug = re.sub(r"\s+", "-", slug)[:50]
    try:
        safe_segment(slug)
    except ValueError:
        slug = "research"
    return slug or "research"


def _strip_thinking_blocks(text: str) -> str:
    text = re.sub(r"<think(?:ing)?>\s*[\s\S]*?</think(?:ing)?>\s*", "", text)
    text = re.sub(r"<think(?:ing)?>\s*[\s\S]*$", "", text)
    return text.lstrip()
