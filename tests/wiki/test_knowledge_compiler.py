from __future__ import annotations

from pathlib import Path

from wiki.knowledge_compiler import KnowledgeCompiler, file_hash, filesystem_path
from wiki.knowledge_dependencies import KnowledgeDependencies


def test_raw_change_invalidates_dependencies_without_queueing_a_page(tmp_path: Path) -> None:
    raw = tmp_path / "raw/sources/file/default/readme.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("old", encoding="utf-8")
    page = tmp_path / "wiki/concepts/readme.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ngenerated: true\n---\n\n# Readme\n", encoding="utf-8")

    dependencies = KnowledgeDependencies(tmp_path)
    dependencies.update_page(
        "wiki/concepts/readme.md",
        "page-hash",
        {"raw/sources/file/default/readme.md": file_hash(raw)},
        generated=True,
    )
    compiler = KnowledgeCompiler(tmp_path)

    changed = compiler.raw_changed("raw/sources/file/default/readme.md")

    assert changed["ok"] is True
    assert changed["generation"] == {"enabled": False, "reason": "raw_only"}
    assert changed["stale"] == ["wiki/concepts/readme.md"]
    assert compiler.queue.status()["counts"] == {}
    assert not (tmp_path / "wiki/sources").exists()


def test_capsule_compatibility_boundary_is_disabled(tmp_path: Path) -> None:
    compiler = KnowledgeCompiler(tmp_path)

    assert compiler.enqueue_capsule("raw/sources/file/default/readme.md")["code"] == "capsule_generation_disabled"
    assert compiler.claim("worker")["job"] is None
    assert compiler.apply_capsule("job", "lease", {})["code"] == "capsule_generation_disabled"
    assert compiler.apply_capsules([])["code"] == "capsule_generation_disabled"


def test_file_hash_remains_long_path_safe(tmp_path: Path) -> None:
    nested = Path("raw/sources/file/default")
    for index in range(6):
        nested /= f"deep-provenance-segment-{index:02d}"
    raw = filesystem_path(tmp_path / nested / "source-document.md")
    raw.parent.mkdir(parents=True)
    raw.write_text("source", encoding="utf-8")

    assert len(file_hash(raw)) == 64
