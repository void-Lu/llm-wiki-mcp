"""Tests for wiki_ingest_url module."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from netsuite_llm_wiki_mcp.wiki_ingest import staged_wiki_ingest
from netsuite_llm_wiki_mcp.wiki_ingest_url import wiki_ingest_url
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


def _make_fake_urlopen(html_by_url: dict[str, str]):
    def fake_urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url not in html_by_url:
            raise urllib.error.URLError(f"not found: {url}")
        html = html_by_url[url]
        mock_resp = MagicMock()
        mock_resp.read.return_value = html.encode("utf-8")
        mock_resp.headers.get_content_charset.return_value = "utf-8"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp
    return fake_urlopen


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    create_wiki_root(root)
    return root


SAMPLE_HTML = "<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p></body></html>"
SAMPLE_HTML_2 = "<html><body><h2>Second</h2><p>Page content</p></body></html>"


def test_wiki_ingest_url_fetches_and_writes_snapshots(vault: Path, monkeypatch):
    urls_map = {
        "https://example.com/a": SAMPLE_HTML,
        "https://example.com/b": SAMPLE_HTML_2,
    }
    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", _make_fake_urlopen(urls_map))

    result = wiki_ingest_url(vault, ["https://example.com/a", "https://example.com/b"], "alpha", "docs")

    assert result["ok"] is True
    assert result["stage"] == "prepare"
    assert result["status"] == "needs_model"
    assert result["fetched"] == 2
    assert "prompt" in result
    assert result["next_call"]["stage"] == "apply"

    raw_dir = vault / "raw" / "sources" / "url" / "alpha" / "docs"
    assert raw_dir.is_dir()
    assert (raw_dir / "manifest.json").is_file()
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 2
    assert all("url" in item for item in manifest)


def test_wiki_ingest_url_skips_unchanged_content(vault: Path, monkeypatch):
    urls_map = {"https://example.com/a": SAMPLE_HTML}
    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", _make_fake_urlopen(urls_map))

    r1 = wiki_ingest_url(vault, ["https://example.com/a"], "alpha", "docs")
    assert r1["status"] == "needs_model"

    r2 = wiki_ingest_url(vault, ["https://example.com/a"], "alpha", "docs")
    assert r2["ok"] is True
    assert r2["status"] == "skipped"
    assert r2["code"] == "source_unchanged"


def test_wiki_ingest_url_partial_fetch_failure(vault: Path, monkeypatch):
    urls_map = {"https://example.com/good": SAMPLE_HTML}
    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", _make_fake_urlopen(urls_map))

    result = wiki_ingest_url(vault, ["https://example.com/good", "https://example.com/bad"], "alpha", "docs")

    assert result["ok"] is True
    assert result["fetched"] == 1
    assert len(result["fetch_errors"]) == 1
    assert result["fetch_errors"][0]["url"] == "https://example.com/bad"


def test_wiki_ingest_url_all_fetches_fail(vault: Path, monkeypatch):
    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", _make_fake_urlopen({}))

    result = wiki_ingest_url(vault, ["https://example.com/x"], "alpha", "docs")

    assert result["ok"] is False
    assert result["code"] == "all_fetches_failed"
    assert len(result["fetch_errors"]) == 1


def test_wiki_ingest_url_rejects_empty_urls(vault: Path):
    result = wiki_ingest_url(vault, [], "alpha", "docs")
    assert result["ok"] is False
    assert result["code"] == "no_urls"


def test_wiki_ingest_url_rejects_too_many_urls(vault: Path):
    result = wiki_ingest_url(vault, [f"https://example.com/{i}" for i in range(21)], "alpha", "docs")
    assert result["ok"] is False
    assert result["code"] == "too_many_urls"


def test_wiki_ingest_url_redacts_sensitive_content(vault: Path, monkeypatch):
    html_with_phone = "<html><body><p>Call 13812345678 for info</p></body></html>"
    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", _make_fake_urlopen({"https://example.com/a": html_with_phone}))

    wiki_ingest_url(vault, ["https://example.com/a"], "alpha", "docs")

    raw_dir = vault / "raw" / "sources" / "url" / "alpha" / "docs"
    md_files = list(raw_dir.glob("*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text(encoding="utf-8")
    assert "13812345678" not in content


def test_wiki_ingest_url_end_to_end_with_apply(vault: Path, monkeypatch):
    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", _make_fake_urlopen({"https://example.com/a": SAMPLE_HTML}))

    prepared = wiki_ingest_url(vault, ["https://example.com/a"], "alpha", "api-docs")
    assert prepared["ok"] is True

    generation = {
        "source_summary": {"title": "API Docs", "summary": "API documentation", "body": "Full body"},
        "pages": [],
    }
    result = staged_wiki_ingest(vault, "apply", project="alpha", source_name="api-docs", source_type="url", generation=generation)
    assert result["ok"] is True
    assert "wiki/sources/concepts/alpha/api-docs.md" in result["paths"]
    assert (vault / "wiki" / "sources" / "concepts" / "alpha" / "api-docs.md").is_file()


def test_wiki_ingest_url_rejects_invalid_project(vault: Path):
    result = wiki_ingest_url(vault, ["https://example.com/a"], "../escape", "docs")
    assert result["ok"] is False


def test_wiki_ingest_url_deduplicates_urls(vault: Path, monkeypatch):
    call_count = {"n": 0}
    original_fake = _make_fake_urlopen({"https://example.com/a": SAMPLE_HTML})

    def counting_urlopen(req, timeout=30):
        call_count["n"] += 1
        return original_fake(req, timeout)

    monkeypatch.setattr("netsuite_llm_wiki_mcp.wiki_ingest_url.urllib.request.urlopen", counting_urlopen)

    result = wiki_ingest_url(vault, ["https://example.com/a", "https://example.com/a"], "alpha", "docs")
    assert result["ok"] is True
    assert result["fetched"] == 1
    assert call_count["n"] == 1

