from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from netsuite_rag_mcp.note_writer import save_obsidian_note


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _save(vault: Path, **kwargs: object) -> dict[str, object]:
    auto_index = bool(kwargs.pop("auto_index", False))
    return save_obsidian_note(
        title="测试 Title: RESTlet/同步",
        content="正文内容",
        vault_root=str(vault),
        auto_index=auto_index,
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
    path = Path(str(result["absolute_path"]))
    assert path.is_file()
    assert path.resolve().is_relative_to(vault.resolve())
    return path


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
        ("decision", {"project": "project-a"}, "wiki/projects/project-a/decisions/decision-note.md"),
        ("troubleshooting", {"project": "project-a"}, "wiki/projects/project-a/troubleshooting/troubleshooting-note.md"),
        ("requirement", {"project": "project-a"}, "wiki/projects/project-a/requirements/requirement-note.md"),
        ("knowledge", {"domain": "suitescript-patterns"}, "wiki/concepts/suitescript-patterns/knowledge-note.md"),
    ],
)
def test_note_type_path_mappings_create_expected_files(vault: Path, note_type: str, kwargs: dict[str, str], expected_path: str):
    result = _save(vault, note_type=note_type, filename=f"{note_type}-note", **kwargs)

    assert result["ok"] is True
    assert result["path"] == expected_path
    assert (vault / expected_path).is_file()


@pytest.mark.parametrize("note_type", ["decision", "troubleshooting", "requirement"])
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


def test_unknown_knowledge_domain_returns_code(vault: Path):
    result = _save(vault, note_type="knowledge", domain="unknown-domain")

    assert result["ok"] is False
    assert result["code"] == "unknown_subdir"


def test_explicit_filename_appends_markdown_extension(vault: Path):
    result = _save(vault, note_type="decision", project="project-a", filename="chosen-name")

    assert result["ok"] is True
    assert result["path"] == "wiki/projects/project-a/decisions/chosen-name.md"
    assert (vault / "wiki/projects/project-a/decisions/chosen-name.md").is_file()


@pytest.mark.parametrize("filename", ["bad<name", "bad>name", "bad:name", "bad\"name", "bad|name", "bad?name", "bad*name", "bad\x00name", "bad\x1fname"])
def test_explicit_filename_rejects_windows_invalid_characters(vault: Path, filename: str):
    result = _save(vault, note_type="decision", project="project-a", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "invalid_filename"


@pytest.mark.parametrize("filename", ["bad/name", "bad\\name"])
def test_explicit_filename_rejects_path_separators_as_path_escape(vault: Path, filename: str):
    result = _save(vault, note_type="decision", project="project-a", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "path_escape"


@pytest.mark.parametrize("filename", ["CON", "con.md", "NUL.tar.gz", "COM1", "COM¹.md", "LPT9"])
def test_explicit_filename_rejects_windows_reserved_device_names(vault: Path, filename: str):
    result = _save(vault, note_type="decision", project="project-a", filename=filename)

    assert result["ok"] is False
    assert result["code"] == "invalid_filename"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"note_type": "decision", "project": "client:ads"},
        {"note_type": "decision", "project": "bad\x1fname"},
        {"note_type": "decision", "project": "project-a."},
        {"note_type": "decision", "project": "project-a "},
        {"note_type": "decision", "project": "CON"},
        {"note_type": "decision", "project": "con.md"},
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
        {"note_type": "decision", "project": "../../escape"},
        {"note_type": "knowledge", "domain": "../escape"},
        {"note_type": "decision", "project": "project-a", "filename": "../escape"},
    ],
)
def test_path_traversal_returns_path_escape(vault: Path, kwargs: dict[str, str]):
    result = _save(vault, **kwargs)

    assert result["ok"] is False
    assert result["code"] == "path_escape"


def test_auto_index_is_ignored_and_returns_null_indexed(vault: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_index(*args: object, **kwargs: object) -> None:
        raise AssertionError("RAG index must not run")

    monkeypatch.setattr("netsuite_rag_mcp.note_writer.run_index_sources", fail_index, raising=False)

    result = _save(vault, note_type="decision", project="project-a", auto_index=True)

    assert result["ok"] is True
    assert result["indexed"] is None


def test_existing_target_overwrite_false_returns_file_exists_and_preserves_bytes(vault: Path):
    target = vault / "wiki" / "projects" / "project-a" / "decisions" / "existing-note.md"
    target.parent.mkdir(parents=True)
    original = b"original bytes\xff\n"
    target.write_bytes(original)

    result = save_obsidian_note(note_type="decision", title="Existing note", content="Replacement body", project="project-a", filename="existing-note", vault_root=str(vault), auto_index=False)

    assert result["ok"] is False
    assert result["code"] == "file_exists"
    assert target.read_bytes() == original


def test_existing_target_overwrite_true_replaces_content(vault: Path):
    target = vault / "wiki" / "projects" / "project-a" / "decisions" / "existing-note.md"
    target.parent.mkdir(parents=True)
    target.write_text("Original body", encoding="utf-8")

    result = save_obsidian_note(note_type="decision", title="Existing note", content="Replacement body", project="project-a", filename="existing-note", overwrite=True, vault_root=str(vault), auto_index=False)

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") != "Original body"
    assert "Replacement body" in target.read_text(encoding="utf-8")


def test_frontmatter_fixed_fields_and_old_fields_absent(vault: Path):
    result = save_obsidian_note(note_type="decision", title="Decision fields", content="Body", project="project-a", tags=["custom"], related_objects=["salesorder"], related_scripts=["customscript_sync"], decision_status="accepted", vault_root=str(vault), auto_index=False)

    path = _written_path(vault, result)
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["type"] == "decision"
    assert frontmatter["generated"] is False
    assert frontmatter["project"] == "project-a"
    assert frontmatter["author"] == "copilot"
    assert date.fromisoformat(str(frontmatter["updated_at"])) <= date.today()
    assert "netsuite" in frontmatter["tags"]
    assert "decision" in frontmatter["tags"]
    assert "custom" in frontmatter["tags"]
    assert frontmatter["related_objects"] == ["salesorder"]
    assert frontmatter["related_scripts"] == ["customscript_sync"]
    assert "related_records" not in text
    assert "related_script_ids" not in text
    assert "Body" in body


def test_slug_keeps_chinese_and_cleans_punctuation(vault: Path):
    result = save_obsidian_note(note_type="decision", title="  修复 RESTlet: 订单/同步!!!  ", content="Body", project="project-a", vault_root=str(vault), auto_index=False)

    assert result["ok"] is True
    assert result["path"] == "wiki/projects/project-a/decisions/修复-RESTlet-订单-同步.md"


def test_slug_truncates_to_80_characters(vault: Path):
    result = save_obsidian_note(note_type="decision", title="a" * 100, content="Body", project="project-a", vault_root=str(vault), auto_index=False)

    assert result["ok"] is True
    assert len(Path(str(result["path"])).stem) == 80


def test_empty_slug_returns_code(vault: Path):
    result = save_obsidian_note(note_type="decision", title="/\\:*?\"<>| !!!", content="Body", project="project-a", vault_root=str(vault), auto_index=False)

    assert result["ok"] is False
    assert result["code"] == "empty_slug"


def test_yaml_injection_values_stay_parseable(vault: Path):
    injected_title = "Title with colon: value\n---\n- list item\n&anchor value"
    injected_values = ["plain: colon", "--- marker", "- list syntax", "&anchor-like", "line one\nline two"]

    result = save_obsidian_note(note_type="knowledge", title=injected_title, content="Body", domain="common-errors", tags=injected_values, related_objects=injected_values, related_script_types=injected_values, filename="yaml-injection", vault_root=str(vault), auto_index=False)

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["topic"] == injected_title
    assert frontmatter["tags"][2:] == injected_values
    assert frontmatter["related_objects"] == injected_values
    assert frontmatter["related_script_types"] == injected_values
    assert "Body" in body


def test_redacts_sensitive_body_without_corrupting_frontmatter(vault: Path):
    content = "\n".join(["phone 13800138000", "email person@example.com", "token=secret-token-value", "password: SuperSecret123"])

    result = save_obsidian_note(note_type="troubleshooting", title="Redaction check", content=content, project="project-a", vault_root=str(vault), auto_index=False)

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


def test_no_sensitive_body_returns_zero_redactions(vault: Path):
    result = save_obsidian_note(note_type="requirement", title="No redaction", content="普通需求说明，不包含敏感信息。", project="project-a", vault_root=str(vault), auto_index=False)

    path = _written_path(vault, result)
    frontmatter, body = _frontmatter_and_body(path)
    assert frontmatter["type"] == "requirement"
    assert result["redacted_count"] == 0
    assert "普通需求说明" in body


def test_save_note_uses_global_config_without_cwd_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from netsuite_rag_mcp.runtime_config import write_global_config

    configured_vault = tmp_path / "configured-vault"
    configured_vault.mkdir()
    cwd_vault = tmp_path / "cwd-vault"
    cwd_vault.mkdir()
    monkeypatch.chdir(cwd_vault)
    monkeypatch.delenv("NETSUITE_RAG_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_RAG_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NETSUITE_RAG_USER_DATA_DIR", str(tmp_path / "user-data"))
    write_global_config(tmp_path / "config" / "config.yaml", vault_name="homework", vault_root=configured_vault, make_default=True)

    result = save_obsidian_note(note_type="knowledge", title="Runtime Config Note", content="Body", domain="common-errors", auto_index=False)

    assert result["ok"] is True
    assert Path(str(result["absolute_path"])).is_relative_to(configured_vault.resolve())
    assert not (cwd_vault / "wiki").exists()


def test_save_note_missing_config_does_not_use_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cwd_vault = tmp_path / "cwd-vault"
    cwd_vault.mkdir()
    monkeypatch.chdir(cwd_vault)
    monkeypatch.delenv("NETSUITE_RAG_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_RAG_CONFIG_DIR", str(tmp_path / "missing-config"))
    monkeypatch.setenv("NETSUITE_RAG_USER_DATA_DIR", str(tmp_path / "user-data"))

    result = save_obsidian_note(note_type="decision", title="No config", content="Body", project="project-a", auto_index=False)

    assert result["ok"] is False
    assert result["code"] == "missing_vault_root"
    assert not (cwd_vault / "wiki").exists()
