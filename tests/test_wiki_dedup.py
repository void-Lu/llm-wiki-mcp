from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_dedup import extract_page_summaries, wiki_dedup


@pytest.fixture
def dedup_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    concepts = root / "wiki" / "concepts" / "chemistry"
    concepts.mkdir(parents=True)
    (concepts / "vfa.md").write_text(
        "---\ntype: concept\ntitle: VFA\ntags: [chemistry]\n---\n\n# VFA\n\nVolatile fatty acids.\n",
        encoding="utf-8",
    )
    (concepts / "volatile-fatty-acids.md").write_text(
        "---\ntype: concept\ntitle: Volatile Fatty Acids\ntags: [chemistry]\n---\n\n# Volatile Fatty Acids\n\nShort-chain fatty acids.\n",
        encoding="utf-8",
    )
    (concepts / "paos.md").write_text(
        "---\ntype: concept\ntitle: PAOs\ntags: [biology]\n---\n\n# PAOs\n\nPhosphorus accumulating organisms.\n",
        encoding="utf-8",
    )
    # A page that links to vfa
    code_dir = root / "wiki" / "projects" / "proj" / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "analysis.md").write_text(
        "---\ntype: code\ntitle: Analysis\ngenerated: true\n---\n\n# Analysis\n\nUses [[vfa]] and [[paos]].\n",
        encoding="utf-8",
    )
    return root


def test_extract_page_summaries(dedup_root: Path):
    summaries = extract_page_summaries(dedup_root)
    slugs = {s["slug"] for s in summaries}
    assert "vfa" in slugs
    assert "volatile-fatty-acids" in slugs
    assert "paos" in slugs


def test_detect_returns_prompt(dedup_root: Path):
    result = wiki_dedup(str(dedup_root), stage="detect")
    assert result["ok"] is True
    assert result["stage"] == "detect"
    assert "prompt" in result
    assert "vfa" in result["prompt"]


def test_confirm_filters_invalid_slugs(dedup_root: Path):
    groups = [{"slugs": ["vfa", "volatile-fatty-acids", "nonexistent"], "reason": "same", "confidence": "high"}]
    result = wiki_dedup(str(dedup_root), stage="confirm", groups=groups)
    assert result["ok"] is True
    confirmed = result["groups"]
    assert len(confirmed) == 1
    assert "nonexistent" not in confirmed[0]["slugs"]
    assert "vfa" in confirmed[0]["slugs"]


def test_confirm_json_string(dedup_root: Path):
    import json
    groups_json = json.dumps({"groups": [{"slugs": ["vfa", "volatile-fatty-acids"], "reason": "same", "confidence": "high"}]})
    result = wiki_dedup(str(dedup_root), stage="confirm", groups=groups_json)
    assert result["ok"] is True
    assert len(result["groups"]) == 1


def test_merge_rewrites_references(dedup_root: Path):
    groups = [{"slugs": ["vfa", "volatile-fatty-acids"], "canonical": "vfa"}]
    result = wiki_dedup(str(dedup_root), stage="merge", groups=groups)
    assert result["ok"] is True
    assert result["results"][0]["canonical"] == "vfa"
    assert "volatile-fatty-acids" in result["results"][0]["merged_slugs"]

    # volatile-fatty-acids.md should be deleted
    assert not (dedup_root / "wiki" / "concepts" / "chemistry" / "volatile-fatty-acids.md").exists()
    # vfa.md should still exist
    assert (dedup_root / "wiki" / "concepts" / "chemistry" / "vfa.md").exists()


def test_merge_missing_canonical(dedup_root: Path):
    groups = [{"slugs": ["vfa", "volatile-fatty-acids"], "canonical": "nonexistent"}]
    result = wiki_dedup(str(dedup_root), stage="merge", groups=groups)
    assert result["ok"] is True
    assert "error" in result["results"][0]
