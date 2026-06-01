"""URL-based wiki ingest: fetch HTML → Markdown → raw snapshot → LLM prompt."""

from __future__ import annotations

import hashlib
import html.parser
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.redaction import redact_sensitive_text
from netsuite_llm_wiki_mcp.wiki_ingest import (
    _cache_path,
    _combined_prompt,
    _read_optional,
    _write_cache,
)
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root, safe_segment, slug

_MAX_URLS = 20
_FETCH_TIMEOUT = 30


def wiki_ingest_url(
    vault_root: str | Path,
    urls: list[str],
    project: str,
    source_name: str,
    language: str = "zh-CN",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)

    try:
        project_value = safe_segment(project)
        source_value = safe_segment(source_name)
    except ValueError as exc:
        return {"ok": False, "code": getattr(exc, "code", "invalid_path_component"), "error": str(exc)}

    if not urls:
        return {"ok": False, "code": "no_urls", "error": "urls list is empty"}
    if len(urls) > _MAX_URLS:
        return {"ok": False, "code": "too_many_urls", "error": f"urls list exceeds {_MAX_URLS} items"}

    unique_urls = list(dict.fromkeys(urls))

    url_contents: list[tuple[str, str]] = []
    fetch_errors: list[dict[str, str]] = []
    for url in unique_urls:
        try:
            html_text = _fetch_url(url)
            markdown = _html_to_markdown(html_text)
            url_contents.append((url, markdown))
        except Exception as exc:
            fetch_errors.append({"url": url, "error": str(exc)})

    if not url_contents:
        return {"ok": False, "code": "all_fetches_failed", "error": "all URLs failed to fetch", "fetch_errors": fetch_errors}

    source_hash = _url_content_hash(url_contents)
    cache_path = _cache_path(root, project_value, source_value, "url")
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("source_hash") == source_hash:
            return {
                "ok": True,
                "stage": "prepare",
                "status": "skipped",
                "code": "source_unchanged",
                "project": project_value,
                "source_name": source_value,
                "source_hash": source_hash,
                "fetch_errors": fetch_errors,
                "message": "URL content hash unchanged; reuse previous generated wiki pages",
            }

    raw_dir = root / "raw" / "sources" / "url" / project_value / source_value
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = _write_url_snapshots(raw_dir, root, url_contents)

    _write_cache(root, project_value, source_value, {
        "source_hash": source_hash,
        "source_type": "url",
        "manifest": manifest,
        "status": "prepared",
        "urls": [u for u, _ in url_contents],
    }, source_type="url")

    wiki_context = {
        "purpose": _read_optional(root / "purpose.md"),
        "schema": _read_optional(root / "schema.md"),
        "index": _read_optional(root / "wiki" / "index.md"),
    }
    prompt = _combined_prompt(project_value, source_value, language, manifest, wiki_context)

    return {
        "ok": True,
        "stage": "prepare",
        "status": "needs_model",
        "project": project_value,
        "source_name": source_value,
        "source_hash": source_hash,
        "fetched": len(url_contents),
        "fetch_errors": fetch_errors,
        "prompt": prompt,
        "expected_response_schema": {
            "source_summary": {"title": "string", "summary": "string", "body": "markdown"},
            "pages": [{"path": "wiki/...", "title": "string", "type": "string", "summary": "string", "body": "markdown", "sources": ["raw/..."]}],
        },
        "next_call": {"tool": "wiki_ingest_llm", "stage": "apply", "source_type": "url", "required": ["generation"]},
    }


def _fetch_url(url: str, timeout: int = _FETCH_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "netsuite-llm-wiki-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _html_to_markdown(html_text: str) -> str:
    try:
        import markdownify
        return markdownify.markdownify(html_text, heading_style="ATX", strip=["script", "style", "nav", "footer"])
    except ImportError:
        return _extract_text_fallback(html_text)


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "nav", "footer"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "nav", "footer") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._pieces.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._pieces)


def _extract_text_fallback(html_text: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_text)
    return parser.get_text()


def _url_content_hash(url_contents: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for url, content in sorted(url_contents, key=lambda x: x[0]):
        digest.update(url.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _write_url_snapshots(raw_dir: Path, root: Path, url_contents: list[tuple[str, str]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    used_filenames: set[str] = set()
    for url, markdown in url_contents:
        base = slug(url)
        filename = base + ".md"
        counter = 1
        while filename in used_filenames:
            filename = f"{base}-{counter}.md"
            counter += 1
        used_filenames.add(filename)

        redacted = redact_sensitive_text(markdown)
        target = raw_dir / filename
        target.write_text(redacted, encoding="utf-8")
        manifest.append({
            "url": url,
            "relative_path": filename,
            "path": target.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "stored_sha256": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
            "bytes": len(markdown.encode("utf-8")),
        })
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest