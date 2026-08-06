from __future__ import annotations

from pathlib import Path

from wiki.reference_section import build_reference_section, validate_raw_sources


def _touch(root: Path, relative: str, content: str = "page") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_build_reference_section_appends_links_and_falls_back_to_stem(tmp_path: Path) -> None:
    _touch(tmp_path, "wiki/concepts/related-page.md")
    _touch(tmp_path, "wiki/projects/demo/specs/other-page.md")

    body, skipped = build_reference_section(
        tmp_path,
        "正文",
        [
            {"path": "wiki/concepts/related-page.md", "title": "Related Page"},
            {"path": "wiki/projects/demo/specs/other-page.md"},
        ],
    )

    assert skipped == []
    assert body.endswith(
        "## 参考来源\n\n"
        "- [[wiki/concepts/related-page|Related Page]]\n"
        "- [[wiki/projects/demo/specs/other-page|other-page]]"
    )


def test_build_reference_section_deduplicates_existing_and_repeated_targets(tmp_path: Path) -> None:
    _touch(tmp_path, "wiki/concepts/related.md")
    _touch(tmp_path, "wiki/concepts/new.md")

    body, skipped = build_reference_section(
        tmp_path,
        "已有 [[WIKI/concepts/RELATED]]",
        [
            {"path": "wiki/concepts/related.md", "title": "Duplicate"},
            {"path": "wiki/concepts/new.md", "title": "New"},
            {"path": "wiki/concepts/NEW.md", "title": "Duplicate case"},
        ],
    )

    assert skipped == []
    assert body.count("wiki/concepts/new|New") == 1
    assert "Duplicate" not in body

    unchanged, skipped = build_reference_section(
        tmp_path,
        "已有 [[wiki/concepts/related]]",
        [{"path": "wiki/concepts/related.md", "title": "Duplicate"}],
    )
    assert unchanged == "已有 [[wiki/concepts/related]]"
    assert skipped == []


def test_build_reference_section_skips_unsafe_and_raw_paths(tmp_path: Path) -> None:
    _touch(tmp_path, "wiki/concepts/valid.md")
    _touch(tmp_path, "raw/sources/reference.txt")

    body, skipped = build_reference_section(
        tmp_path,
        "正文",
        [
            {"path": "raw/sources/reference.txt", "title": "Raw"},
            {"path": "wiki/concepts/missing.md"},
            {"path": "wiki/concepts/valid.txt"},
            {"path": "wiki/../outside.md"},
            {"path": "wiki/concepts/valid.md", "title": "Valid"},
        ],
    )

    assert body.endswith("## 参考来源\n\n- [[wiki/concepts/valid|Valid]]")
    assert [item["reason"] for item in skipped] == [
        "raw_source_use_sources",
        "not_found",
        "not_markdown",
        "path_escape",
    ]


def test_validate_raw_sources_only_returns_existing_raw_files(tmp_path: Path) -> None:
    _touch(tmp_path, "raw/sources/reference.txt")

    valid, skipped = validate_raw_sources(
        tmp_path,
        [
            "raw/sources/reference.txt",
            "raw/sources/missing.txt",
            "wiki/concepts/not-raw.md",
        ],
    )

    assert valid == ["raw/sources/reference.txt"]
    assert skipped == [
        {"path": "raw/sources/missing.txt", "reason": "not_found"},
        {"path": "wiki/concepts/not-raw.md", "reason": "path_not_allowed"},
    ]
