"""Tests for wikilink target validation and auto-normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from wiki.wikilink_validator import auto_normalize_wikilinks, build_stem_index, validate_wikilinks


@pytest.fixture
def vault_with_pages(tmp_path: Path) -> Path:
    """Create a minimal vault with a few wiki pages."""
    wiki = tmp_path / "wiki" / "concepts" / "test-domain"
    wiki.mkdir(parents=True)
    (wiki / "MapReduce-上下文对象-API-详解.md").write_text("# test", encoding="utf-8")
    (wiki / "MapReduce-治理限制与数据约束.md").write_text("# test", encoding="utf-8")
    (wiki / "Simple-Page.md").write_text("# test", encoding="utf-8")
    # Non-wiki file should be ignored
    (tmp_path / "raw" / "sources").mkdir(parents=True)
    (tmp_path / "raw" / "sources" / "ignored.md").write_text("# raw", encoding="utf-8")
    return tmp_path


class TestBuildStemIndex:
    def test_indexes_wiki_md_files(self, vault_with_pages: Path):
        index = build_stem_index(vault_with_pages)
        assert "mapreduce-上下文对象-api-详解" in index
        assert index["mapreduce-上下文对象-api-详解"] == "MapReduce-上下文对象-API-详解"
        assert "mapreduce-治理限制与数据约束" in index
        assert "simple-page" in index

    def test_excludes_raw_sources(self, vault_with_pages: Path):
        index = build_stem_index(vault_with_pages)
        assert "ignored" not in index

    def test_empty_vault(self, tmp_path: Path):
        index = build_stem_index(tmp_path)
        assert index == {}


class TestValidateWikilinks:
    def test_valid_wikilink_no_broken(self, vault_with_pages: Path):
        content = "See [[MapReduce-上下文对象-API-详解]] for details."
        broken = validate_wikilinks(content, vault_with_pages)
        assert broken == []

    def test_space_wikilink_detected_as_broken(self, vault_with_pages: Path):
        """Wikilink with spaces doesn't match filename with hyphens."""
        content = "See [[MapReduce 上下文对象 API 详解]] for details."
        broken = validate_wikilinks(content, vault_with_pages)
        assert len(broken) == 1
        assert broken[0]["target"] == "MapReduce 上下文对象 API 详解"
        assert broken[0]["suggestion"] == "MapReduce-上下文对象-API-详解"

    def test_nonexistent_target_is_broken(self, vault_with_pages: Path):
        content = "See [[Nonexistent Page]] for details."
        broken = validate_wikilinks(content, vault_with_pages)
        assert len(broken) == 1
        assert broken[0]["target"] == "Nonexistent Page"

    def test_ignores_wikilinks_in_code_blocks(self, vault_with_pages: Path):
        content = "```\n[[Nonexistent In Code]]\n```\n[[Simple-Page]]"
        broken = validate_wikilinks(content, vault_with_pages)
        assert broken == []

    def test_ignores_inline_code(self, vault_with_pages: Path):
        content = "Use `[[Nonexistent Inline]]` and [[Simple-Page]]."
        broken = validate_wikilinks(content, vault_with_pages)
        assert broken == []

    def test_path_prefixed_target(self, vault_with_pages: Path):
        content = "See [[test-domain/Simple-Page]] for details."
        broken = validate_wikilinks(content, vault_with_pages)
        assert broken == []


class TestAutoNormalizeWikilinks:
    def test_normalizes_spaces_to_hyphens(self, vault_with_pages: Path):
        content = "See [[MapReduce 上下文对象 API 详解]] for details."
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        assert count == 1
        assert "[[MapReduce-上下文对象-API-详解]]" in normalized

    def test_already_correct_target_no_change(self, vault_with_pages: Path):
        content = "See [[MapReduce-上下文对象-API-详解]] for details."
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        assert count == 0
        assert normalized == content

    def test_nonexistent_target_left_untouched(self, vault_with_pages: Path):
        content = "See [[Nonexistent Page]] for details."
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        assert count == 0
        assert "[[Nonexistent Page]]" in normalized

    def test_preserves_alias(self, vault_with_pages: Path):
        content = "See [[MapReduce 上下文对象 API 详解|API 文档]] for details."
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        assert count == 1
        assert "[[MapReduce-上下文对象-API-详解|API 文档]]" in normalized

    def test_preserves_path_prefix(self, vault_with_pages: Path):
        content = "See [[test-domain/MapReduce 上下文对象 API 详解]] for details."
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        assert count == 1
        assert "[[test-domain/MapReduce-上下文对象-API-详解]]" in normalized

    def test_multiple_wikilinks(self, vault_with_pages: Path):
        content = "[[MapReduce 上下文对象 API 详解]] and [[MapReduce 治理限制与数据约束]] and [[Simple Page]]"
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        assert count == 3
        assert "[[MapReduce-上下文对象-API-详解]]" in normalized
        assert "[[MapReduce-治理限制与数据约束]]" in normalized
        assert "[[Simple-Page]]" in normalized

    def test_ignores_code_blocks(self, vault_with_pages: Path):
        content = "```\n[[MapReduce 上下文对象 API 详解]]\n```\n[[Simple Page]]"
        normalized, count = auto_normalize_wikilinks(content, vault_with_pages)
        # Only the one outside code block should be normalized
        assert count == 1
        assert "[[Simple-Page]]" in normalized
