from __future__ import annotations

from pathlib import Path

import scripts.archive_wiki_sources as archive_script
from archive.archive_manifest import verify_bundle
from archive.archive_service import ArchiveService
from retrieval.retrieval_index import RetrievalIndexStore
from wiki.wiki_io import split_frontmatter
from wiki.wiki_paths import create_wiki_root


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _vault_with_source_page(root: Path, *, missing_capsule: bool = False) -> tuple[Path, Path, Path]:
    create_wiki_root(root)
    raw = _write(root, "raw/sources/file/default/report.md", "# Raw report\n\nLegacy report evidence.\n")
    capsule_path = "wiki/sources/file/default/capsules/report.md"
    if missing_capsule:
        capsule_path = "wiki/sources/file/default/capsules/missing.md"
        _write(root, "wiki/sources/file/default/legacy.md", "Legacy source without a page mapping.\n")
    else:
        # Deliberately omit source_path/source_hash to exercise fuzzy mapping.
        _write(root, capsule_path, "---\ntitle: Legacy report capsule\n---\n\nLegacy capsule evidence.\n")
    page = _write(
        root,
        "wiki/concepts/report.md",
        "---\n"
        "type: concept\n"
        "generated: true\n"
        "source_capsules:\n"
        f"  - {capsule_path}\n"
        "---\n\n"
        "# Report\n\nFormal report guidance.\n",
    )
    return raw, page, root / capsule_path


def test_archive_wiki_sources_dry_run_then_commit_is_raw_provenance_safe(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    raw, page, capsule = _vault_with_source_page(root)
    raw_before = raw.read_bytes()
    page_before = page.read_bytes()

    preview = archive_script.run(root, apply=False)

    assert preview["ok"] is True
    assert preview["mode"] == "dry-run"
    assert preview["source_files"] == 1
    assert preview["page_updates"] == 1
    assert preview["archive_plan"]["restorable"] is False
    assert preview["page_mappings"][0]["capsules"][0]["confidence"] == "fuzzy_filename_directory"
    assert page.read_bytes() == page_before
    assert capsule.exists()

    result = archive_script.run(root, apply=True)

    assert result["ok"] is True
    assert result["remaining_source_files"] == []
    assert raw.read_bytes() == raw_before
    assert not capsule.exists()
    frontmatter, _ = split_frontmatter(page.read_text(encoding="utf-8"))
    assert "source_capsules" not in frontmatter
    assert frontmatter["sources"] == ["raw/sources/file/default/report.md"]
    assert frontmatter["source_mapping_status"] == "resolved"
    assert frontmatter.get("freshness") != "review_required"

    bundle = next((root / "archives" / "bundles").glob("*/*/*"))
    manifest = verify_bundle(root, bundle)
    assert manifest.restorable is False
    assert (bundle / "source-remap.json").is_file()
    assert ArchiveService(root).plan_restore(manifest.archive_id)["code"] == "archive_not_restorable"

    active_paths = {str(item["path"]) for item in RetrievalIndexStore(root).page_candidates()}
    assert all(not path.startswith("wiki/sources/") for path in active_paths)


def test_archive_wiki_sources_keeps_unresolved_mapping_audited_but_non_blocking(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _, page, _ = _vault_with_source_page(root, missing_capsule=True)

    result = archive_script.run(root, apply=True)

    assert result["ok"] is True
    assert result["unresolved_pages"] == 1
    frontmatter, _ = split_frontmatter(page.read_text(encoding="utf-8"))
    assert "source_capsules" not in frontmatter
    assert frontmatter["freshness"] == "review_required"
    assert frontmatter["source_mapping_status"] == "unresolved"
    bundle = next((root / "archives" / "bundles").glob("*/*/*"))
    audit = (bundle / "source-remap.json").read_text(encoding="utf-8")
    assert "capsule_not_found" in audit


def test_archive_wiki_sources_drops_invalid_existing_raw_links(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _, page, _ = _vault_with_source_page(root, missing_capsule=True)
    frontmatter, body = split_frontmatter(page.read_text(encoding="utf-8"))
    frontmatter["sources"] = ["raw/sources/file/default/missing.md"]
    frontmatter["source_hashes"] = {"raw/sources/file/default/missing.md": "sha256:missing"}
    page.write_text(archive_script._serialize_page(frontmatter, body), encoding="utf-8")

    result = archive_script.run(root, apply=True)

    assert result["ok"] is True
    cleaned, _ = split_frontmatter(page.read_text(encoding="utf-8"))
    assert "sources" not in cleaned
    assert "source_hashes" not in cleaned
    assert cleaned["source_mapping_status"] == "unresolved"


def test_archive_wiki_sources_rolls_back_page_cleanup_when_archive_commit_fails(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    _, page, capsule = _vault_with_source_page(root)
    original_page = page.read_bytes()

    def fail_apply(self, plan_id: str) -> dict[str, object]:
        del self, plan_id
        return {"ok": False, "code": "injected_archive_failure"}

    monkeypatch.setattr(archive_script.ArchiveService, "apply", fail_apply)

    result = archive_script.run(root, apply=True)

    assert result["ok"] is False
    assert result["code"] == "archive_wiki_sources_failed"
    assert page.read_bytes() == original_page
    assert capsule.exists()
