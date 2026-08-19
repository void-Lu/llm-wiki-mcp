from __future__ import annotations

from datetime import date
from hashlib import sha256
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

import wiki.note_writer as note_writer_module
from wiki.note_writer import save_obsidian_note
from wiki.chat_memory import ChatMemoryService
from wiki.page_mutation import PageMutationCoordinator


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _save(vault: Path, **kwargs: Any) -> dict[str, object]:
    return save_obsidian_note(
        title="测试 Title: RESTlet/同步",
        content="正文内容",
        vault_root=str(vault),
        **kwargs,
    )


def _frontmatter_and_body(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "---"
    end = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
    frontmatter = yaml.safe_load("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1 :])
    assert isinstance(frontmatter, dict)
    return frontmatter, body


def _written_path(vault: Path, result: dict[str, object]) -> Path:
    assert result["ok"] is True
    assert "absolute_path" not in result
    path = vault / str(result["path"])
    assert path.is_file()
    assert path.resolve().is_relative_to(vault.resolve())
    return path


def test_save_note_signature_drops_dead_internal_parameters() -> None:
    parameters = inspect.signature(save_obsidian_note).parameters

    assert len(parameters) == 19
    assert {"script_type", "object_type", "decision_status", "overwrite"}.isdisjoint(parameters)
    assert "decision_status" not in inspect.signature(note_writer_module._frontmatter).parameters


def test_invalid_note_type_returns_code(vault: Path):
    result = _save(vault, note_type="meeting", project="project-a")

    assert result["ok"] is False
    assert result["code"] == "invalid_note_type"


@pytest.mark.parametrize("note_type", ["script", "object"])
def test_script_and_object_note_types_are_removed(vault: Path, note_type: str):
    result = _save(vault, note_type=note_type, project="project-a")

    assert result["ok"] is False
    assert result["code"] == "invalid_note_type"


@pytest.mark.parametrize(
    ("note_type", "kwargs", "expected_path"),
    [
        ("spec", {"project": "project-a"}, "wiki/projects/project-a/specs/spec-note.md"),
        ("plan", {"project": "project-a"}, "wiki/projects/project-a/plans/plan-note.md"),
        ("troubleshooting", {"project": "project-a"}, "wiki/projects/project-a/troubleshooting/troubleshooting-note.md"),
        ("researches", {"project": "project-a"}, "wiki/projects/project-a/researches/researches-note.md"),
        ("knowledge", {"domain": "integration-patterns"}, "wiki/concepts/integration-patterns/knowledge-note.md"),
        ("entity", {"domain": "integration-patterns"}, "wiki/entities/integration-patterns/entity-note.md"),
    ],
)
def test_note_type_path_mappings_create_expected_files(vault: Path, note_type: str, kwargs: dict[str, str], expected_path: str):
    result = _save(vault, note_type=note_type, filename=f"{note_type}-note", **kwargs)

    assert result["ok"] is True
    assert result["path"] == expected_path
    assert (vault / expected_path).is_file()


@pytest.mark.parametrize("note_type", ["spec", "plan", "troubleshooting", "researches"])
def test_project_note_types_require_project(vault: Path, note_type: str):
    result = _save(vault, note_type=note_type)

    assert result["ok"] is False
    assert result["code"] == "missing_project"


def test_knowledge_requires_domain(vault: Path):
    result = _save(vault, note_type="knowledge")

    assert result["ok"] is False
    assert result["code"] == "missing_domain"


def test_knowledge_rejects_project(vault: Path):
    result = _save(vault, note_type="knowledge", domain="common-errors", project="project-a")

    assert result["ok"] is False
    assert result["code"] == "knowledge_project_not_allowed"


def test_chat_derived_entity_requires_and_locks_chat_source(vault: Path):
    source = ChatMemoryService(vault).save(
        "## User\n\n记录结论。\n\n## Assistant\n\n已记录。",
        {"session_id": "writer-session", "summary": "结论", "decisions": ["记录"], "open_questions": [], "tags": []},
    )
    missing = _save(vault, note_type="entity", domain="integration-patterns", filename="missing", chat_derived=True)
    assert missing["code"] == "chat_sources_required"
    result = _save(
        vault,
        note_type="entity",
        domain="integration-patterns",
        filename="derived",
        chat_derived=True,
        chat_sources=[{"source_id": source["source_id"], "revision": source["revision"], "redacted_hash": source["redacted_hash"]}],
    )
    path = _written_path(vault, result)
    frontmatter, _ = _frontmatter_and_body(path)
    assert frontmatter["chat_derived"] is True
    assert frontmatter["chat_sources"] == [{"source_id": "writer-session", "revision": 1, "redacted_hash": source["redacted_hash"]}]


def test_chat_note_does_not_consume_wiki_or_raw_reference_arguments(vault: Path):
    result = save_obsidian_note(
        note_type="chat",
        title="Chat note",
        content="## User\n\n问题\n\n## Assistant\n\n回答",
        vault_root=str(vault),
        chat_metadata={
            "session_id": "chat-writer",
            "summary": "记录结论",
            "decisions": ["记录"],
            "open_questions": [],
            "tags": [],
        },
        related_pages=[{"path": "wiki/concepts/related.md", "title": "Related"}],
        sources=["raw/sources/reference.txt"],
    )

    assert result["ok"] is True
    path = vault / str(result["path"])
    frontmatter, body = _frontmatter_and_body(path)
    assert "sources" not in frontmatter
    assert "## 参考来源" not in body


def test_unknown_knowledge_domain_returns_code(vault: Path):
    result = _save(vault, note_type="knowledge", domain="unknown-domain")

    assert result["ok"] is False
    assert result["code"] == "unknown_subdir"


def test_explicit_filename_appends_markdown_extension(vault: Path):
    result = _save(vault, note_type="spec", project="project-a", filename="chosen-name")

    assert result["ok"] is True
    assert result["path"] == "wiki/projects/project-a/specs/chosen-name.md"
    assert (vault / "wiki/projects/project-a/specs/chosen-name.md").is_file()


@pytest.mark.parametrize("filename", ["bad<name", "bad>name", "bad:name", "bad\"name", "bad|name", "bad?name", "bad*name", "bad\x00name", "bad\x1fname"])
def test_explicit_filename_rejects_windows_invalid_characters(vault: Path, filename: str):
    result = _save(vault, note_type="spec", project="project-a", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "invalid_filename"


@pytest.mark.parametrize("filename", ["bad/name", "bad\\name"])
def test_explicit_filename_rejects_path_separators_as_path_escape(vault: Path, filename: str):
    result = _save(vault, note_type="spec", project="project-a", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "path_escape"


@pytest.mark.parametrize("filename", ["index", "index.md"])
def test_note_writer_rejects_navigation_index_filename(vault: Path, filename: str) -> None:
    result = _save(vault, note_type="knowledge", domain="common-errors", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "invalid_wiki_path"


@pytest.mark.parametrize("filename", ["CON", "con.md", "NUL.tar.gz", "COM1", "COM¹.md", "LPT9"])
def test_explicit_filename_rejects_windows_reserved_device_names(vault: Path, filename: str):
    result = _save(vault, note_type="spec", project="project-a", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "invalid_filename"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"note_type": "spec", "project": "client:ads"},
        {"note_type": "spec", "project": "bad\x1fname"},
        {"note_type": "spec", "project": "project-a."},
        {"note_type": "spec", "project": "project-a "},
        {"note_type": "spec", "project": "CON"},
        {"note_type": "spec", "project": "con.md"},
        {"note_type": "knowledge", "domain": "common-errors:ads"},
    ],
)
def test_path_segments_reject_windows_invalid_components(vault: Path, kwargs: dict[str, str]):
    result = _save(vault, **kwargs)

    assert result["ok"] is False
    assert result["code"] == "invalid_path_component"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"note_type": "spec", "project": "../../escape"},
        {"note_type": "knowledge", "domain": "../escape"},
        {"note_type": "spec", "project": "project-a", "filename": "../escape"},
    ],
)
def test_path_traversal_returns_path_escape(vault: Path, kwargs: dict[str, str]):
    result = _save(vault, **kwargs)

    assert result["ok"] is False
    assert result["code"] == "path_escape"


def test_note_write_returns_null_indexed(vault: Path):
    result = _save(vault, note_type="spec", project="project-a")

    assert result["ok"] is True
    assert result["indexed"] is None


def test_existing_target_returns_file_exists_and_preserves_bytes(vault: Path):
    target = vault / "wiki" / "projects" / "project-a" / "specs" / "existing-note.md"
    target.parent.mkdir(parents=True)
    original = b"original bytes\xff\n"
    target.write_bytes(original)

    result = save_obsidian_note(note_type="spec", title="Existing note", content="Replacement body", project="project-a", filename="existing-note", vault_root=str(vault))

    assert result["ok"] is False
    assert result["code"] == "file_exists"
    assert target.read_bytes() == original


def test_frontmatter_fixed_fields_and_old_fields_absent(vault: Path):
    result = save_obsidian_note(note_type="spec", title="Spec fields", content="Body", project="project-a", tags=["custom"], related_objects=["salesorder"], related_scripts=["customscript_sync"], status="accepted", vault_root=str(vault))

    path = _written_path(vault, result)
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["type"] == "spec"
    assert frontmatter["generated"] is False
    assert frontmatter["project"] == "project-a"
    assert frontmatter["author"] == "copilot"
    assert date.fromisoformat(str(frontmatter["updated_at"])) <= date.today()
    tags = frontmatter["tags"]
    assert isinstance(tags, list)
    assert "netsuite" not in tags
    assert "spec" in tags
    assert "custom" in tags
    assert frontmatter["related_objects"] == ["salesorder"]
    assert frontmatter["related_scripts"] == ["customscript_sync"]
    assert frontmatter["provenance_unverified"] is True
    assert frontmatter["freshness"] == "review_required"
    assert "related_records" not in text
    assert "related_script_ids" not in text
    assert "Body" in body


def test_slug_keeps_chinese_and_cleans_punctuation(vault: Path):
    result = save_obsidian_note(note_type="spec", title="  修复 RESTlet: 订单/同步!!!  ", content="Body", project="project-a", vault_root=str(vault))

    assert result["ok"] is True
    assert result["path"] == "wiki/projects/project-a/specs/修复-RESTlet-订单-同步.md"


def test_slug_truncates_to_80_characters(vault: Path):
    result = save_obsidian_note(note_type="spec", title="a" * 100, content="Body", project="project-a", vault_root=str(vault))

    assert result["ok"] is True
    assert len(Path(str(result["path"])).stem) == 80


def test_empty_slug_returns_code(vault: Path):
    result = save_obsidian_note(note_type="spec", title="/\\:*?\"<>| !!!", content="Body", project="project-a", vault_root=str(vault))

    assert result["ok"] is False
    assert result["code"] == "empty_slug"


def test_new_note_does_not_migrate_existing_legacy_filename(vault: Path):
    legacy = vault / "wiki" / "projects" / "project-a" / "specs" / "Legacy-Title.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy body", encoding="utf-8")

    result = save_obsidian_note(
        note_type="spec",
        title="Legacy Title",
        content="new body",
        project="project-a",
        filename="New-Title",
        vault_root=str(vault),
    )

    assert result["ok"] is True
    assert result["path"] == "wiki/projects/project-a/specs/New-Title.md"
    assert legacy.read_text(encoding="utf-8") == "legacy body"


def test_yaml_injection_values_stay_parseable(vault: Path):
    injected_title = "Title with colon: value\n---\n- list item\n&anchor value"
    injected_values = ["plain: colon", "--- marker", "- list syntax", "&anchor-like", "line one\nline two"]

    result = save_obsidian_note(note_type="knowledge", title=injected_title, content="Body", domain="common-errors", tags=injected_values, related_objects=injected_values, related_script_types=injected_values, filename="yaml-injection", vault_root=str(vault))

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["topic"] == injected_title
    tags = frontmatter["tags"]
    assert isinstance(tags, list)
    assert tags[1:] == injected_values
    assert frontmatter["related_objects"] == injected_values
    assert frontmatter["related_script_types"] == injected_values
    assert "Body" in body


def test_redacts_sensitive_body_without_corrupting_frontmatter(vault: Path):
    content = "\n".join(["phone 13800138000", "email person@example.com", "token=secret-token-value", "password: SuperSecret123"])

    result = save_obsidian_note(note_type="troubleshooting", title="Redaction check", content=content, project="project-a", vault_root=str(vault))

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["type"] == "troubleshooting"
    assert result["redacted_count"] > 0
    assert "13800138000" not in body
    assert "person@example.com" not in body
    assert "secret-token-value" not in body
    assert "SuperSecret123" not in body
    assert "[REDACTED_PHONE]" in body
    assert "[REDACTED_EMAIL]" in body
    assert "[REDACTED_SECRET]" in body


def test_redacts_sensitive_title_before_filename_and_page_render(vault: Path):
    result = save_obsidian_note(
        note_type="spec",
        title="Owner person@example.com",
        content="Body",
        project="project-a",
        vault_root=str(vault),
    )

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert "person@example.com" not in str(result["path"])
    assert frontmatter["title"] == "Owner [REDACTED_EMAIL]"
    assert "person@example.com" not in body
    assert result["redacted_count"] == 1


def test_no_sensitive_body_returns_zero_redactions(vault: Path):
    result = save_obsidian_note(note_type="researches", title="No redaction", content="普通需求说明，不包含敏感信息。", project="project-a", vault_root=str(vault))

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["type"] == "researches"
    assert result["redacted_count"] == 0
    assert "普通需求说明" in body


def test_note_write_uses_prepare_as_single_redaction_count_owner(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wiki.wiki_io as wiki_io

    original_counter = wiki_io.count_redactions
    calls: list[tuple[str, str]] = []

    def counting_counter(original: str, redacted: str) -> int:
        calls.append((original, redacted))
        return original_counter(original, redacted)

    monkeypatch.setattr(wiki_io, "count_redactions", counting_counter)
    result = save_obsidian_note(
        note_type="troubleshooting",
        title="Single count",
        content="Contact person@example.com and use token=secret-token-value.",
        project="project-a",
        filename="single-count",
        vault_root=str(vault),
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert result["redacted_count"] == original_counter(*calls[0])


def test_related_pages_and_raw_sources_are_written_with_verified_provenance(vault: Path):
    related = vault / "wiki" / "concepts" / "related.md"
    related.parent.mkdir(parents=True)
    related.write_text("Related", encoding="utf-8")
    source = vault / "raw" / "sources" / "reference.txt"
    source.parent.mkdir(parents=True)
    source.write_text("raw", encoding="utf-8")

    result = save_obsidian_note(
        note_type="knowledge",
        title="Reference note",
        content="Body",
        domain="common-errors",
        related_pages=[
            {"path": "wiki/concepts/related.md", "title": "Related Page"},
            {"path": "raw/sources/reference.txt", "title": "Raw"},
        ],
        sources=["raw/sources/reference.txt"],
        vault_root=str(vault),
    )

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["sources"] == ["raw/sources/reference.txt"]
    assert frontmatter["source_hashes"] == {"raw/sources/reference.txt": sha256(b"raw").hexdigest()}
    assert frontmatter["provenance_unverified"] is False
    assert frontmatter["freshness"] == "fresh"
    assert "## 参考来源" in body
    assert "[[wiki/concepts/related|Related Page]]" in body
    assert result["related_pages_skipped"][0]["reason"] == "raw_source_use_sources"
    assert result["provenance_status"] == "verified"


def test_invalid_explicit_source_fails_before_page_or_dependency_writes(vault: Path):
    result = _save(
        vault,
        note_type="knowledge",
        domain="common-errors",
        filename="invalid-source",
        sources=["wiki/concepts/related.md"],
    )

    assert result == {
        "ok": False,
        "code": "source_path_not_allowed",
        "error": "source provenance could not be verified",
    }
    assert not (vault / "wiki").exists()
    assert not (vault / ".llm-wiki").exists()


def test_save_note_requires_explicit_vault_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cwd_vault = tmp_path / "cwd-vault"
    cwd_vault.mkdir()
    monkeypatch.chdir(cwd_vault)
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)

    result = save_obsidian_note(note_type="knowledge", title="Runtime Config Note", content="Body", domain="common-errors")

    assert result["ok"] is False
    assert result["code"] == "missing_vault_root"
    assert not (cwd_vault / "wiki").exists()


def test_save_note_strips_duplicate_leading_h1(vault: Path):
    result = save_obsidian_note(
        note_type="knowledge",
        title="去重标题",
        content="# 去重标题\n\n正文内容",
        domain="common-errors",
        vault_root=str(vault),
    )

    path = _written_path(vault, result)
    text = path.read_text(encoding="utf-8")
    assert text.count("# 去重标题") == 1
    assert "正文内容" in text


def test_write_note_returns_wikilink_target_and_normalizes_wikilinks(vault: Path) -> None:
    """write_note should return wikilink_target, normalize body wikilinks, and report broken ones."""
    # Create a target page so the stem index can match it.
    target_dir = vault / "wiki" / "concepts" / "common-errors"
    target_dir.mkdir(parents=True)
    (target_dir / "Target-Page.md").write_text("# Target", encoding="utf-8")

    result = save_obsidian_note(
        note_type="knowledge",
        title="Source Page",
        content="See [[Target Page]] and [[Nonexistent]] for details.",
        domain="common-errors",
        vault_root=str(vault),
    )

    assert result["ok"] is True
    assert result["wikilink_target"] == "Source-Page"
    assert result["normalized_wikilinks"] == 1  # "Target Page" -> "Target-Page"
    broken = result["broken_wikilinks"]
    assert len(broken) == 1
    assert broken[0]["target"] == "Nonexistent"

    # Verify the written content has the normalized wikilink.
    path = _written_path(vault, result)
    text = path.read_text(encoding="utf-8")
    assert "[[Target-Page]]" in text
    assert "[[Target Page]]" not in text


def test_write_note_returns_page_operation_contract(vault: Path) -> None:
    result = _save(vault, note_type="spec", project="project-a", filename="operation-contract")

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert isinstance(result["operation_id"], str) and result["operation_id"]
    assert isinstance(result["page_hash"], str) and len(result["page_hash"]) == 64
    assert "repair_action" not in result
    assert "failed_stage" not in result


def test_write_note_exposes_repair_contract_when_projection_fails(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_projections(self: PageMutationCoordinator, operation: object) -> dict[str, object]:
        del self, operation
        return {"dependencies": lambda: {"ok": False, "code": "dependency_unavailable"}}

    monkeypatch.setattr(PageMutationCoordinator, "projections_for", failing_projections)
    result = _save(vault, note_type="spec", project="project-a", filename="repair-contract")

    assert result["ok"] is True
    assert result["state"] == "repair_pending"
    assert isinstance(result["operation_id"], str) and result["operation_id"]
    assert isinstance(result["page_hash"], str) and len(result["page_hash"]) == 64
    assert result["repair_action"] == "repair_page_operation"
    assert result["failed_stage"] == "dependencies"
