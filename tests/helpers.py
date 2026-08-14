from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from wiki.atomic_file import atomic_write_text
from wiki.wiki_io import prepare_wiki_page, refresh_page_retrieval
from wiki.wiki_models import WikiPage


def write_test_page(
    root: Path,
    path: str,
    frontmatter: Mapping[str, Any],
    body: str,
    *,
    overwrite_generated_only: bool = False,
    allow_navigation_index: bool = False,
) -> dict[str, Any]:
    page_path = Path(path)
    page_frontmatter = dict(frontmatter)
    title = str(page_frontmatter.get("title") or page_path.stem)
    prepared = prepare_wiki_page(
        root,
        WikiPage(page_path, page_frontmatter, title, body),
        overwrite_generated_only=overwrite_generated_only,
        allow_navigation_index=allow_navigation_index,
    )
    written = atomic_write_text(prepared.target, prepared.text)
    result: dict[str, Any] = {
        "ok": True,
        "path": prepared.relative_path.as_posix(),
        "page_hash": written.content_hash,
        "redacted_count": prepared.redacted_count,
    }
    try:
        result["retrieval_index"] = refresh_page_retrieval(root, prepared.target)
    except Exception as exc:
        result["retrieval_index"] = {
            "ok": False,
            "state": "stale",
            "code": "index_update_failed",
            "error": str(exc),
        }
    return result
