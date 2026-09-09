from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil

from retrieval.retrieval_index import RetrievalIndexStore, eligible_path
from wiki.raw_replacement import RawReplacementService

TARGET_ROOT = "raw/sources/file/example-docs"
TARGET_PREFIX = TARGET_ROOT + "/"


def _write(root: Path, relative: str, text: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_raw_auxiliary_files_are_not_raw_retrieval_eligible() -> None:
    assert eligible_path("raw/sources/file/example-docs/ordinary.md", scope="raw")
    assert not eligible_path("raw/sources/file/example-docs/manifest/_manifest.json", scope="raw")
    assert not eligible_path("raw/sources/file/example-docs/_deprecated_archive/old.md", scope="raw")
    assert eligible_path("raw/sources/file/other/ordinary.md", scope="raw")


def test_plan_and_apply_replace_tree_rebind_sources_and_body_links(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    external = tmp_path / "crawler" / "NetSuite Help Docs"
    old_target = vault / TARGET_ROOT
    old_locator = f"{TARGET_PREFIX}old/old-name.md"
    new_locator = f"{TARGET_PREFIX}new/new-name.md"
    source_url = "https://docs.oracle.com/example/article_123.html"

    _write(vault, "wiki/log.md", "# Log\n")
    _write(
        vault,
        "wiki/concepts/example.md",
        f"---\ntype: concept\ngenerated: false\nsources: [{old_locator}]\nsource_hashes: {{{old_locator}: old-hash}}\n---\n\n# Example\n\n[Keep this label]({old_locator})\n",
    )
    _write(
        vault,
        "wiki/concepts/body-only.md",
        f"---\ntype: concept\ntitle: Body only\n---\n\n# Body only\n\n- `{old_locator}`\n",
    )
    _write(
        old_target,
        "old/old-name.md",
        f'---\ntitle: "Old title"\nsource: "{source_url}"\n---\n\nOld\n',
    )
    _write(external, "new/new-name.md", f'---\ntitle: "New title"\nsource: "{source_url}"\n---\n\nNew\n')
    _write(external, "manifest/_manifest.json", "{}\n")
    _write(external, "_deprecated_archive/old.md", "deprecated\n")

    service = RawReplacementService(vault, target_path=TARGET_ROOT)
    plan = service.plan(external)

    assert plan["ok"] is True
    assert plan["state"] == "ready"
    assert plan["summary"]["canonical_url_matches"] == 1
    assert plan["summary"]["unmatched"] == 0
    assert plan["summary"]["ambiguous"] == 0
    page_before = (vault / "wiki/concepts/example.md").read_bytes()
    assert old_target.is_dir()
    assert (vault / ".llm-wiki/raw-retrieval.sqlite3").exists() is False
    assert (vault / "wiki/concepts/example.md").read_bytes() == page_before

    result = service.apply(str(plan["plan_id"]))

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert not (old_target / "old/old-name.md").exists()
    assert (vault / TARGET_ROOT / "new/new-name.md").is_file()
    rewritten = (vault / "wiki/concepts/example.md").read_text(encoding="utf-8")
    assert f"sources:\n- {new_locator}" in rewritten
    assert f"source_hashes:\n  {new_locator}: {sha256((external / 'new/new-name.md').read_bytes()).hexdigest()}" in rewritten
    assert "freshness: review_required" in rewritten
    assert "[Keep this label](" + new_locator + ")" in rewritten
    body_only = (vault / "wiki/concepts/body-only.md").read_text(encoding="utf-8")
    assert "title: Body only" in body_only
    assert f"`{new_locator}`" in body_only
    assert (external.parent / f".{external.name}.llm-wiki-backup-{plan['plan_id']}").is_dir()

    raw_status = RetrievalIndexStore(vault, scope="raw").status()
    assert raw_status["ok"] is True
    assert raw_status["page_count"] == 1
    assert RetrievalIndexStore(vault, scope="raw").search_fts("deprecated")[0:] == []

    repeated = service.apply(str(plan["plan_id"]))
    assert repeated == {"ok": True, "already_applied": True, "plan_id": plan["plan_id"], "state": "already_applied"}


def test_apply_blocks_when_page_cas_drifted(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    external = tmp_path / "crawler"
    locator = f"{TARGET_PREFIX}one.md"
    _write(vault, "wiki/log.md", "# Log\n")
    _write(vault, "wiki/concepts/example.md", f"---\nsources: [{locator}]\n---\n\n# Example\n")
    _write(vault, f"{TARGET_ROOT}/one.md", "old\n")
    _write(external, "one.md", "new\n")

    plan = RawReplacementService(vault, target_path=TARGET_ROOT).plan(external)
    page = vault / "wiki/concepts/example.md"
    page.write_text(page.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")

    result = RawReplacementService(vault).apply(str(plan["plan_id"]))

    assert result["ok"] is False
    assert result["code"] == "page_cas_mismatch"
    assert result["state"] == "blocked"
    assert (vault / TARGET_ROOT / "one.md").read_text(encoding="utf-8") == "old\n"


def test_recover_restores_partial_tree_and_allows_apply_retry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    external = tmp_path / "crawler"
    locator = f"{TARGET_PREFIX}one.md"
    _write(vault, "wiki/log.md", "# Log\n")
    _write(vault, "wiki/concepts/example.md", f"---\nsources: [{locator}]\n---\n\n# Example\n")
    old_target = vault / TARGET_ROOT
    _write(old_target, "one.md", "old\n")
    _write(old_target, "old-only.md", "old-only\n")
    _write(external, "one.md", "new\n")

    service = RawReplacementService(vault, target_path=TARGET_ROOT)
    plan = service.plan(external)
    plan_id = str(plan["plan_id"])
    backup = external.parent / f".{external.name}.llm-wiki-backup-{plan_id}"
    staging = external.parent / f".{external.name}.llm-wiki-stage-{plan_id}"
    shutil.copytree(old_target, backup)
    shutil.copytree(external, staging)
    (old_target / "old-only.md").unlink()
    (staging / "one.md").unlink()
    service._write_audit(plan_id, state="repair_pending", summary=plan["summary"], rollback_errors=["raw_tree_restore_failed"])

    recovered = RawReplacementService(vault).recover(plan_id)

    assert recovered == {"ok": True, "plan_id": plan_id, "state": "recovered", "backup_retained": True}
    assert (old_target / "old-only.md").read_text(encoding="utf-8") == "old-only\n"
    assert not staging.exists()
    assert service.apply(plan_id)["state"] == "completed"
