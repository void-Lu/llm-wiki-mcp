from __future__ import annotations

from pathlib import Path

import pytest

from wiki.chat_memory import ChatMemoryError, ChatMemoryService
from wiki.generation_queue import GenerationQueue
from retrieval.retrieval_index import RetrievalIndexStore


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


def test_idempotent_save_rechecks_index_and_queue(tmp_path: Path) -> None:
    service = ChatMemoryService(tmp_path)
    service.save(_transcript(), _metadata())
    retried = service.save(_transcript(), _metadata())
    assert retried["idempotent"] is True
    assert retried["index"]["generation"] == {"enabled": False, "reason": "raw_only"}
    assert GenerationQueue(tmp_path).status()["counts"] == {}


def test_chat_save_indexes_raw_source_without_creating_a_formal_page(tmp_path: Path) -> None:
    source = ChatMemoryService(tmp_path).save(_transcript(), _metadata())

    paths = [item["path"] for item in RetrievalIndexStore(tmp_path).page_candidates()]
    assert source["path"] in paths
    assert not (tmp_path / "wiki/sources").exists()


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
