from __future__ import annotations

from pathlib import Path

import pytest

from wiki.atomic_file import AtomicFileError, atomic_write_text, fault_context
from wiki.chat_memory import ChatMemoryError, ChatMemoryService, _chat_index_response
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import PagePolicy
from wiki.page_operation_store import PageOperationStore
from retrieval.retrieval_index import RetrievalIndexStore
from retrieval.query_pipeline import run_query_v2
from wiki.wiki_index import rebuild_retrieval_index


def _metadata() -> dict[str, object]:
    return {
        "session_id": "session-a",
        "summary": "讨论 API 密钥的保存方式。",
        "decisions": ["使用 vault。"],
        "open_questions": [],
        "tags": ["security"],
    }


def _transcript(extra: str = "") -> str:
    return "## User\n\n请保存 token=sk-1234567890abcdefgh。\n\n## Assistant\n\n会脱敏保存。" + extra


def test_chat_source_is_redacted_immutable_idempotent_and_append_aware(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    first = service.save(_transcript(), _metadata())

    assert first["ok"] and first["revision"] == 1
    assert first["redaction_policy_version"] == "redaction-v1"
    assert first["redacted_categories"] == {"secret": 1}
    first_text = (tmp_path / first["path"]).read_text(encoding="utf-8")
    assert "sk-1234567890abcdefgh" not in first_text
    assert "[REDACTED_SECRET]" in first_text
    assert service.save(_transcript(), _metadata())["idempotent"] is True

    appended = service.save(_transcript("\n\n## User\n\n新增可见消息。"), _metadata())
    assert appended["revision"] == 2
    assert appended["processing_mode"] == "incremental"
    replacement = service.save("## User\n\n替换历史。\n\n## Assistant\n\n已替换。", _metadata())
    assert replacement["revision"] == 3
    assert replacement["processing_mode"] == "full"
    assert (tmp_path / first["path"]).is_file()


def test_chat_transcript_requires_a_visible_role_at_the_start(tmp_path: Path) -> None:
    with pytest.raises(ChatMemoryError) as captured:
        ChatMemoryService(tmp_path).save("未标记的前言\n\n## User\n\n消息", _metadata())
    assert captured.value.code == "invalid_chat_content"


def test_idempotent_save_rechecks_index(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    service.save(_transcript(), _metadata())
    retried = service.save(_transcript(), _metadata())
    assert retried["idempotent"] is True
    assert retried["index"]["generation"] == {"enabled": False, "reason": "raw_only"}


def test_chat_index_response_uses_flattened_success_and_synthesizes_failure() -> None:
    safe_result = PageOperationStore.safe_stage_result(
        {"ok": True, "state": "ready", "operation": "update", "retrieval_index": {"nested": "discarded"}}
    )
    success = _chat_index_response({"state": "succeeded"}, safe_result)
    failure = _chat_index_response({"state": "failed", "code": "retrieval_unavailable"}, None)

    assert success == {
        "ok": True,
        "generation": {"enabled": False, "reason": "raw_only"},
        "retrieval_index": {"ok": True, "state": "ready", "operation": "update"},
    }
    assert failure == {
        "ok": False,
        "generation": {"enabled": False, "reason": "raw_only"},
        "retrieval_index": {"ok": False, "state": "failed", "code": "retrieval_unavailable"},
    }


def test_chat_save_reports_rebuild_required_without_creating_a_formal_page(tmp_path: Path) -> None:
    source = ChatMemoryService(tmp_path).save(_transcript(), _metadata())

    paths = [item["path"] for item in RetrievalIndexStore(tmp_path).page_candidates()]
    assert source["index"]["ok"] is False
    assert source["index"]["retrieval_index"]["state"] == "rebuild_required"
    assert source["path"] not in paths
    assert not (tmp_path / "wiki/sources").exists()
    assert rebuild_retrieval_index(tmp_path)["ok"] is True
    assert source["path"] in [item["path"] for item in RetrievalIndexStore(tmp_path).page_candidates()]


def test_chat_save_uses_one_operation_and_deduplicates_audit_log(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    first = service.save(_transcript(), _metadata())
    retried = service.save(_transcript(), _metadata())

    assert first["operation_id"] == retried["operation_id"]
    assert retried["state"] == "completed"
    assert len(list((tmp_path / "raw/sources/chat").rglob("revision-*.md"))) == 1
    log = (tmp_path / "wiki/log.md").read_text(encoding="utf-8")
    assert log.count(f"- operation_id: {first['operation_id']}") == 1


def test_chat_projection_failure_is_repairable_without_a_second_revision(tmp_path: Path) -> None:
    def fault(stage: str) -> None:
        if stage == "projection:retrieval":
            raise RuntimeError("injected")

    with fault_context(fault):
        failed = ChatMemoryService(tmp_path).save(_transcript(), _metadata())
    assert failed["ok"] is True
    assert failed["state"] == "repair_pending"
    assert failed["failed_stage"] == "retrieval"

    repaired = ChatMemoryService(tmp_path).save(_transcript(), _metadata())
    assert repaired["state"] == "completed"
    assert repaired["operation_id"] == failed["operation_id"]
    assert len(list((tmp_path / "raw/sources/chat").rglob("revision-*.md"))) == 1
    assert (tmp_path / "wiki/log.md").read_text(encoding="utf-8").count(f"- operation_id: {failed['operation_id']}") == 1


def test_new_chat_revision_marks_exact_session_dependents_by_maintenance_mode(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    first = service.save(_transcript(), _metadata())
    first_path = tmp_path / first["path"]
    first_hash = __import__("hashlib").sha256(first_path.read_bytes()).hexdigest()
    deps = KnowledgeDependencies(tmp_path)
    deps.update_page(
        "wiki/concepts/generated.md",
        "page-generated",
        {first["path"]: first_hash},
        policy=PagePolicy(freshness="fresh", maintenance="auto", lifecycle="active", generated=True, replaced_by=None),
    )
    deps.update_page(
        "wiki/concepts/manual.md",
        "page-manual",
        {first["path"]: first_hash},
        policy=PagePolicy(freshness="fresh", maintenance="manual", lifecycle="active", generated=False, replaced_by=None),
    )

    second = service.save(_transcript("\n\n## User\n\n新的 session marker。"), _metadata())

    assert second["revision"] == 2
    generated = KnowledgeDependencies.read_page_projection(tmp_path, "wiki/concepts/generated.md")
    manual = KnowledgeDependencies.read_page_projection(tmp_path, "wiki/concepts/manual.md")
    assert generated["freshness"] == "stale"
    assert generated["lifecycle"] == "stale"
    assert manual["freshness"] == "review_required"
    assert manual["lifecycle"] == "review_required"


def test_chat_history_is_queryable_but_knowledge_scope_excludes_it(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    service.save(
        "## User\n\n历史 scope marker。\n\n## Assistant\n\n已记录。",
        {**_metadata(), "session_id": "history-session"},
    )
    assert rebuild_retrieval_index(tmp_path)["ok"] is True

    history = run_query_v2(tmp_path, "历史 scope marker", scope="history", retrieval_mode="lexical")
    knowledge = run_query_v2(tmp_path, "历史 scope marker", scope="knowledge", retrieval_mode="lexical")

    assert history["results"][0]["path"].startswith("raw/sources/chat/")
    assert all(not item["path"].startswith("raw/sources/chat/") for item in knowledge["results"])


def test_incremental_prompt_uses_full_source_line_anchors(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    service.save(_transcript(), _metadata())
    service.save(_transcript("\n\n## User\n\n新增可见消息。"), _metadata())
    assert not (tmp_path / "wiki/sources").exists()


def test_chat_provenance_locks_session_revision_and_redacted_hash(tmp_path: Path) -> None:
    source = ChatMemoryService(tmp_path).save(_transcript(), _metadata())
    service = ChatMemoryService(tmp_path)
    valid = service.provenance([{"source_id": source["source_id"], "revision": source["revision"], "redacted_hash": source["redacted_hash"]}])
    assert valid["ok"]
    assert valid["sources"][0]["path"] == source["path"]
    assert service.provenance([{"source_id": source["source_id"], "revision": source["revision"], "redacted_hash": "0" * 64}])["code"] == "chat_source_hash_mismatch"


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_chat_atomic_write_fault_before_replace_keeps_old_bytes(tmp_path: Path, stage: str) -> None:
    target = tmp_path / "raw/sources/chat/revision.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with fault_context(fault):
        with pytest.raises(AtomicFileError) as error:
            atomic_write_text(target, "new")

    assert error.value.code == "atomic_write_failed"
    assert target.read_text(encoding="utf-8") == "old"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_chat_atomic_write_post_replace_fault_keeps_complete_new_bytes(tmp_path: Path) -> None:
    target = tmp_path / "raw/sources/chat/revision.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    def fault(stage: str) -> None:
        if stage == "post_replace":
            raise RuntimeError("injected")

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
