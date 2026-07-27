from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import netsuite_llm_wiki_mcp.wiki_repair as wiki_repair_module
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_lint import wiki_lint
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_query import wiki_query
from netsuite_llm_wiki_mcp.wiki_repair import (
    RepairPlan,
    apply_wiki_repair,
    prepare_wiki_repair,
)


def _raw_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    raw = root / "raw"
    for path in sorted(item for item in raw.rglob("*") if item.is_file()):
        digest.update(path.relative_to(raw).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_page(
    root: Path,
    relative: str,
    *,
    generated: bool,
    sources: list[str],
    body: str = "Body",
    **frontmatter: object,
) -> Path:
    path = root / relative
    write_wiki_page(
        root,
        WikiPage(
            Path(relative),
            {
                "title": path.stem,
                "type": "concept",
                "generated": generated,
                "sources": sources,
                **frontmatter,
            },
            path.stem,
            body,
        ),
        overwrite_generated_only=False,
    )
    return path


def _write_manifest(root: Path, name: str, manifest: list[dict[str, str]]) -> None:
    path = root / ".llm-wiki/ingest-cache/file/proj" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"manifest": manifest}), encoding="utf-8")


def test_unique_content_hash_relocation_requires_generated_replacement(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/file/proj/old/doc.md"
    new_source = "raw/sources/file/proj/new/doc.md"
    raw = root / new_source
    raw.parent.mkdir(parents=True)
    raw.write_text("same immutable snapshot", encoding="utf-8")
    source_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    page = _write_page(
        root,
        "wiki/projects/proj/architecture/generated.md",
        generated=True,
        sources=[old_source],
        source_hashes={old_source: source_hash},
    )
    original = page.read_text(encoding="utf-8")

    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))

    action = next(item for item in plan.actions if item.kind == "relink_source")
    assert action.replacements == {old_source: new_source}
    assert action.evidence[0]["kind"] == "content_hash"
    assert action.evidence[0]["old_path"] == old_source
    assert action.evidence[0]["new_path"] == new_source

    rejected = apply_wiki_repair(root, plan, [action.action_id])
    assert rejected.rejected[0]["code"] == "replacement_generation_required"
    assert page.read_text(encoding="utf-8") == original

    replacement = original.replace(old_source, new_source).replace("Body", "Regenerated body")
    raw_before = _raw_tree_hash(root)
    report = apply_wiki_repair(
        root,
        plan,
        [action.action_id],
        generated_replacements={action.action_id: replacement},
    )

    assert report.ok is True
    assert report.raw_writes == 0
    assert report.raw_unchanged is True
    assert _raw_tree_hash(root) == raw_before
    assert new_source in page.read_text(encoding="utf-8")
    assert "Regenerated body" in page.read_text(encoding="utf-8")
    assert (root / report.audit_path).is_file()


def test_stable_source_id_and_explicit_migration_are_deterministic_evidence(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_by_id = "raw/sources/file/proj/old/id.md"
    new_by_id = "raw/sources/file/proj/new/id.md"
    old_by_map = "raw/sources/file/proj/old/map.md"
    new_by_map = "raw/sources/file/proj/new/map.md"
    for relative in (new_by_id, new_by_map):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    _write_manifest(
        root,
        "ids",
        [
            {"path": new_by_id, "source_id": "stable-doc-1"},
        ],
    )
    _write_page(
        root,
        "wiki/projects/proj/architecture/id-page.md",
        generated=True,
        sources=[old_by_id],
        source_ids={old_by_id: "stable-doc-1"},
    )
    _write_page(
        root,
        "wiki/projects/proj/architecture/map-page.md",
        generated=True,
        sources=[old_by_map],
    )

    plan = prepare_wiki_repair(
        root,
        migrations={old_by_map: new_by_map},
        today=date(2026, 7, 27),
    )

    actions = {item.page_path: item for item in plan.actions}
    assert actions["wiki/projects/proj/architecture/id-page.md"].evidence[0]["kind"] == "stable_source_id"
    assert actions["wiki/projects/proj/architecture/map-page.md"].evidence[0]["kind"] == "explicit_migration"


def test_ambiguous_hash_does_not_relink_or_archive(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/file/proj/old/doc.md"
    matching_hash = hashlib.sha256(b"same").hexdigest()
    for relative in (
        "raw/sources/file/proj/a/doc.md",
        "raw/sources/file/proj/b/doc.md",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"same")
    _write_manifest(root, "old", [{"path": old_source, "stored_sha256": matching_hash}])
    _write_page(
        root,
        "wiki/projects/proj/architecture/generated.md",
        generated=True,
        sources=[old_source],
    )

    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))

    assert plan.actions == ()
    assert any(item["code"] == "ambiguous_source_relocation" for item in plan.findings)


def test_missing_explicit_migration_target_keeps_page_active(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/file/proj/old/doc.md"
    missing_target = "raw/sources/file/proj/new/doc.md"
    page = _write_page(
        root,
        "wiki/projects/proj/architecture/generated.md",
        generated=True,
        sources=[old_source],
    )

    plan = prepare_wiki_repair(
        root,
        migrations={old_source: missing_target},
        today=date(2026, 7, 27),
    )

    assert all(action.page_path != page.relative_to(root).as_posix() for action in plan.actions)
    assert any(
        item["source"] == old_source and missing_target in item["candidates"]
        for item in plan.findings
    )
    assert page.is_file()


def test_generated_page_without_source_is_archived_and_leaves_active_views(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/file/proj/gone/doc.md"
    page = _write_page(
        root,
        "wiki/concepts/stale-generated.md",
        generated=True,
        sources=[old_source],
        body="unique stale needle",
    )
    raw_before = _raw_tree_hash(root)

    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    action = next(item for item in plan.actions if item.kind == "archive_generated")
    report = apply_wiki_repair(root, plan, [action.action_id])

    assert report.ok is True
    assert not page.exists()
    archived_path = root / report.applied[0]["new_path"]
    assert archived_path.is_file()
    archived_text = archived_path.read_text(encoding="utf-8")
    assert "archived_from: wiki/concepts/stale-generated.md" in archived_text
    assert "archive_original_sha256:" in archived_text
    assert _raw_tree_hash(root) == raw_before
    assert report.raw_writes == 0
    assert not any(
        item["path"] == "wiki/concepts/stale-generated.md"
        for item in wiki_lint(root)["issues"]
    )
    assert wiki_query(root, "unique stale needle")["results"] == []


def test_archive_collision_uses_hash_suffix_without_overwriting_history(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/file/proj/gone/doc.md"
    _write_page(
        root,
        "wiki/concepts/stale-generated.md",
        generated=True,
        sources=[old_source],
    )
    historical = (
        root
        / "wiki/archives/stale/2026/07/27/wiki/concepts/stale-generated.md"
    )
    historical.parent.mkdir(parents=True, exist_ok=True)
    historical.write_text("historical archive", encoding="utf-8")
    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    action = next(item for item in plan.actions if item.kind == "archive_generated")

    report = apply_wiki_repair(root, plan, [action.action_id])

    assert historical.read_text(encoding="utf-8") == "historical archive"
    assert report.applied[0]["new_path"].startswith(
        "wiki/archives/stale/2026/07/27/wiki/concepts/stale-generated-"
    )
    assert (root / report.applied[0]["new_path"]).is_file()


def test_existing_source_index_directory_does_not_enter_repair_plan(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    source_dir = root / "raw/sources/references/docs"
    source_dir.mkdir(parents=True)
    _write_page(
        root,
        "wiki/sources/references/docs/_entries.md",
        generated=True,
        sources=["raw/sources/references/docs"],
        type="source_index",
        index_kind="lightweight_source_index",
    )

    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))

    assert all(
        action.page_path != "wiki/sources/references/docs/_entries.md"
        for action in plan.actions
    )


def test_source_index_directory_can_relocate_by_explicit_migration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/references/old-docs"
    new_source = "raw/sources/references/new-docs"
    (root / new_source).mkdir(parents=True)
    page = _write_page(
        root,
        "wiki/sources/references/docs/_entries.md",
        generated=True,
        sources=[old_source],
        type="source_index",
        index_kind="lightweight_source_index",
    )
    original = page.read_text(encoding="utf-8")
    plan = prepare_wiki_repair(
        root,
        migrations={old_source: new_source},
        today=date(2026, 7, 27),
    )
    action = next(item for item in plan.actions if item.kind == "relink_source")

    report = apply_wiki_repair(
        root,
        plan,
        [action.action_id],
        generated_replacements={
            action.action_id: original.replace(old_source, new_source),
        },
    )

    assert report.ok is True
    assert action.evidence[0]["kind"] == "explicit_migration"
    assert new_source in page.read_text(encoding="utf-8")


def test_manual_link_is_suggestion_until_action_is_explicitly_upgraded(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write_page(
        root,
        "wiki/concepts/invoice-approval.md",
        generated=False,
        sources=[],
        body="Canonical target.",
    )
    manual = _write_page(
        root,
        "wiki/concepts/manual.md",
        generated=False,
        sources=[],
        body="See [[invoice_approval|approval]].",
    )
    original = manual.read_text(encoding="utf-8")

    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    suggestion = next(item for item in plan.actions if item.kind == "suggest_manual_link")
    assert suggestion.replacements == {"invoice_approval": "invoice-approval"}
    assert "-See [[invoice_approval|approval]]." in suggestion.diff
    assert "+See [[invoice-approval|approval]]." in suggestion.diff

    rejected = apply_wiki_repair(root, plan, [suggestion.action_id])
    assert rejected.rejected[0]["code"] == "manual_rewrite_not_explicit"
    assert manual.read_text(encoding="utf-8") == original

    rewrite = replace(suggestion, kind="rewrite_manual_link")
    approved_plan = RepairPlan(actions=(rewrite,), findings=plan.findings)
    report = apply_wiki_repair(root, approved_plan, [rewrite.action_id])

    assert report.ok is True
    assert "[[invoice-approval|approval]]" in manual.read_text(encoding="utf-8")


def test_manual_rewrite_revalidates_replacement_target(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write_page(
        root,
        "wiki/concepts/invoice-approval.md",
        generated=False,
        sources=[],
    )
    manual = _write_page(
        root,
        "wiki/concepts/manual.md",
        generated=False,
        sources=[],
        body="See [[invoice_approval]].",
    )
    original = manual.read_text(encoding="utf-8")
    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    suggestion = next(item for item in plan.actions if item.kind == "suggest_manual_link")
    tampered = replace(
        suggestion,
        kind="rewrite_manual_link",
        replacements={"invoice_approval": "target-that-does-not-exist"},
    )

    report = apply_wiki_repair(
        root,
        RepairPlan(actions=(tampered,), findings=()),
        [tampered.action_id],
    )

    assert report.rejected[0]["code"] == "manual_target_missing"
    assert manual.read_text(encoding="utf-8") == original


def test_apply_rejects_changed_page_and_tampered_raw_write_path(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    old_source = "raw/sources/file/proj/gone/doc.md"
    page = _write_page(
        root,
        "wiki/concepts/stale-generated.md",
        generated=True,
        sources=[old_source],
    )
    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    archive = next(item for item in plan.actions if item.kind == "archive_generated")
    page.write_text(page.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")

    changed = apply_wiki_repair(root, plan, [archive.action_id])
    assert changed.rejected[0]["code"] == "page_changed"
    assert page.exists()

    raw_file = root / "raw/sources/sentinel.md"
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text("do not touch", encoding="utf-8")
    raw_before = _raw_tree_hash(root)
    tampered = replace(
        archive,
        page_path="raw/sources/sentinel.md",
        expected_hash=hashlib.sha256(raw_file.read_bytes()).hexdigest(),
    )
    tampered_plan = RepairPlan(actions=(tampered,), findings=())

    unsafe = apply_wiki_repair(root, tampered_plan, [tampered.action_id])

    assert unsafe.rejected[0]["code"] == "unsafe_write_path"
    assert raw_file.read_text(encoding="utf-8") == "do not touch"
    assert _raw_tree_hash(root) == raw_before
    assert unsafe.raw_writes == 0


def test_apply_audits_completed_action_before_index_refresh_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    page = _write_page(
        root,
        "wiki/concepts/stale-generated.md",
        generated=True,
        sources=["raw/sources/file/proj/gone/doc.md"],
    )
    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    action = next(item for item in plan.actions if item.kind == "archive_generated")

    def fail_refresh(_root: Path) -> dict[str, object]:
        raise OSError("refresh unavailable")

    monkeypatch.setattr(wiki_repair_module, "refresh_indexes", fail_refresh)

    report = apply_wiki_repair(root, plan, [action.action_id])

    assert report.ok is False
    assert report.index_refresh is not None
    assert report.index_refresh["code"] == "index_refresh_failed"
    assert not page.exists()
    audit_records = [
        json.loads(line)
        for line in (root / report.audit_path).read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["status"] == "applied"
        and record["action"]["action_id"] == action.action_id
        for record in audit_records
    )
    assert any(
        record["status"] == "failed"
        and record["result"]["code"] == "index_refresh_failed"
        for record in audit_records
    )


def test_apply_reports_and_audits_wiki_log_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write_page(
        root,
        "wiki/concepts/stale-generated.md",
        generated=True,
        sources=["raw/sources/file/proj/gone/doc.md"],
    )
    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    action = next(item for item in plan.actions if item.kind == "archive_generated")

    def fail_log(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("log unavailable")

    monkeypatch.setattr(wiki_repair_module, "append_log_entry", fail_log)

    report = apply_wiki_repair(root, plan, [action.action_id])

    assert report.ok is False
    assert any(item["code"] == "wiki_log_failed" for item in report.rejected)
    audit_records = [
        json.loads(line)
        for line in (root / report.audit_path).read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["status"] == "failed"
        and record["result"]["code"] == "wiki_log_failed"
        for record in audit_records
    )


def test_apply_records_skipped_actions_and_rejects_unknown_selection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write_page(
        root,
        "wiki/concepts/stale-generated.md",
        generated=True,
        sources=["raw/sources/file/proj/gone/doc.md"],
    )
    plan = prepare_wiki_repair(root, today=date(2026, 7, 27))
    action = next(item for item in plan.actions if item.kind == "archive_generated")

    report = apply_wiki_repair(root, plan, ["not-in-plan"])

    assert report.ok is False
    assert report.rejected == (
        {"action_id": "not-in-plan", "code": "unknown_action_id"},
    )
    assert report.skipped == (
        {"action_id": action.action_id, "code": "not_selected"},
    )
    audit_records = [
        json.loads(line)
        for line in (root / report.audit_path).read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in audit_records] == [
        "rejected",
        "skipped",
    ]
