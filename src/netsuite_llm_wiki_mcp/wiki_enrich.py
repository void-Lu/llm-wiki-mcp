"""Post-save wikilink enrichment.

Given a wiki page and the wiki index, returns a list of (term, target)
substitutions that should be wrapped in [[wikilink]] syntax. The actual
string replacement is done deterministically in code — the LLM only
identifies which terms map to which existing pages.

This module is designed to be called as an MCP tool. The LLM call is
the caller's responsibility (MCP returns a prompt; caller feeds it to
an LLM and passes the result back via apply stage).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page, split_frontmatter
from netsuite_llm_wiki_mcp.wikilinks import format_wikilink, is_markdown_table_row_at, wikilink_targets



def wiki_enrich(
    vault_root: str | Path,
    page_path: str,
    stage: str = "prepare",
    links: list[dict[str, str]] | str | None = None,
) -> dict[str, Any]:
    """Two-stage wikilink enrichment.

    stage="prepare": reads the page + index, returns a prompt for the LLM.
    stage="apply": applies the LLM's link suggestions to the page.
    """
    root = Path(vault_root).expanduser().resolve()

    if stage == "prepare":
        return _prepare(root, page_path)
    elif stage == "apply":
        return _apply(root, page_path, links)
    else:
        return {"ok": False, "code": "invalid_stage", "error": f"stage must be 'prepare' or 'apply', got '{stage}'"}


def _prepare(root: Path, page_path: str) -> dict[str, Any]:
    target = (root / page_path).resolve()
    if not target.exists():
        return {"ok": False, "code": "page_not_found", "error": f"page not found: {page_path}"}
    if not target.is_relative_to(root):
        return {"ok": False, "code": "path_escape", "error": "page path escapes wiki root"}

    content = target.read_text(encoding="utf-8-sig")
    frontmatter, body = split_frontmatter(content)

    index_content = _read_index_content(root)

    existing_links = set(wikilink_targets(body))

    prompt = _build_enrich_prompt(body, index_content, existing_links)

    return {
        "ok": True,
        "stage": "prepare",
        "page_path": page_path,
        "prompt": prompt,
        "existing_link_count": len(existing_links),
        "instruction": "Feed this prompt to an LLM. Pass the JSON response back via stage='apply' with links parameter.",
    }


def _apply(root: Path, page_path: str, links: list[dict[str, str]] | str | None) -> dict[str, Any]:
    target = (root / page_path).resolve()
    if not target.exists():
        return {"ok": False, "code": "page_not_found", "error": f"page not found: {page_path}"}
    if not target.is_relative_to(root):
        return {"ok": False, "code": "path_escape", "error": "page path escapes wiki root"}

    parsed_links = _parse_links(links)
    if parsed_links is None:
        return {"ok": False, "code": "invalid_links", "error": "links must be a list of {term, target} objects or a JSON string"}

    if not parsed_links:
        return {"ok": True, "stage": "apply", "page_path": page_path, "links_applied": 0, "message": "no links to apply"}

    content = target.read_text(encoding="utf-8-sig")
    frontmatter_text, body = _split_raw(content)

    applied = 0
    seen_targets: set[str] = set()
    for link in parsed_links:
        term = link.get("term", "")
        link_target = link.get("target", "")
        if not term or not link_target:
            continue
        if link_target in seen_targets:
            continue
        if term not in body:
            continue
        if _already_linked(body, term):
            continue
        term_index = _find_safe_term_index(body, term)
        if term_index < 0:
            continue
        link_text = format_wikilink(link_target, term, in_table=is_markdown_table_row_at(body, term_index))
        body = body[:term_index] + link_text + body[term_index + len(term) :]
        seen_targets.add(link_target)
        applied += 1

    if applied == 0:
        return {"ok": True, "stage": "apply", "page_path": page_path, "links_applied": 0, "message": "no applicable links found"}

    new_content = frontmatter_text + body
    target.write_text(new_content, encoding="utf-8")

    return {
        "ok": True,
        "stage": "apply",
        "page_path": page_path,
        "links_applied": applied,
    }


def _read_index_content(root: Path) -> str:
    """Read the root index plus generated split index fragments.

    Root index pages are intentionally kept small for linting, so enrichment
    needs to include sibling index fragments such as index-concepts.md and
    index-sources.md when they exist.
    """
    wiki_dir = root / "wiki"
    index_path = wiki_dir / "index.md"
    parts: list[str] = []
    if index_path.exists():
        parts.append(index_path.read_text(encoding="utf-8-sig"))
    for extra in sorted(wiki_dir.glob("index-*.md")):
        if extra.name == "index.md":
            continue
        parts.append(extra.read_text(encoding="utf-8-sig"))
    return "\n\n".join(part for part in parts if part)


def _build_enrich_prompt(body: str, index_content: str, existing_links: set[str]) -> str:
    existing_note = ""
    if existing_links:
        existing_note = f"\n\nAlready linked targets (do NOT duplicate): {', '.join(sorted(existing_links))}"

    return f"""You identify which terms in a wiki page should become [[wikilinks]] pointing to existing wiki pages.

You will receive:
  - A wiki index listing existing pages
  - The content of ONE wiki page

Return a JSON object listing which terms in the page content should be linked to which index entries.

Response format (EXACTLY this JSON shape, nothing else):
{{
  "links": [
    {{ "term": "exact text appearing in the content", "target": "index page name" }}
  ]
}}

Rules:
- Each "term" MUST be a literal substring present in the page content (case-sensitive).
- Each "target" MUST be a page listed in the wiki index.
- Include at most one entry per target (first mention).
- Only include clearly-matching terms.
- If no terms should be linked, return {{"links": []}}.
- Do NOT output preamble, explanations, or markdown fences — ONLY the JSON object.{existing_note}

## Wiki Index
{index_content}

## Page Content
{body}"""


def _parse_links(links: list[dict[str, str]] | str | None) -> list[dict[str, str]] | None:
    if links is None:
        return None
    if isinstance(links, str):
        try:
            parsed = json.loads(links)
        except json.JSONDecodeError:
            parsed = _extract_json_object(links)
            if parsed is None:
                return None
        if isinstance(parsed, dict) and "links" in parsed:
            parsed = parsed["links"]
        if not isinstance(parsed, list):
            return None
        return parsed
    if isinstance(links, list):
        return links
    return None


def _extract_json_object(text: str) -> Any | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _find_safe_term_index(body: str, term: str) -> int:
    """Return the first occurrence that can be safely replaced by a wikilink."""
    protected_spans = _protected_spans(body)
    start = 0
    while True:
        index = body.find(term, start)
        if index < 0:
            return -1
        if not any(span_start <= index < span_end for span_start, span_end in protected_spans):
            return index
        start = index + len(term)


def _protected_spans(body: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    patterns = [
        r"\[\[[\s\S]*?\]\]",  # existing wikilinks
        r"```[\s\S]*?```",  # fenced code blocks
        r"`[^`\n]+`",  # inline code
        r"\[[^\]\n]+\]\([^\)\n]+\)",  # Markdown links
    ]
    for pattern in patterns:
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, body))
    return spans


def _already_linked(body: str, term: str) -> bool:
    pattern = re.compile(r"\[\[[^\]]*" + re.escape(term) + r"[^\]]*\]\]")
    return bool(pattern.search(body))


def _split_raw(content: str) -> tuple[str, str]:
    lines = content.split("\n")
    if not lines or lines[0] != "---":
        return "", content
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
        frontmatter_text = "\n".join(lines[: end + 1]) + "\n"
        body = "\n".join(lines[end + 1 :])
        return frontmatter_text, body
    except StopIteration:
        return "", content
