from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_batch import wiki_ingest_batch


@pytest.fixture
def batch_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / ".llm-wiki").mkdir(parents=True)
    return root


def test_enqueue_and_status(batch_root: Path):
    tasks = [
        {"source_path": "raw/sources/file/proj/doc.md", "project": "proj", "source_name": "doc"},
        {"source_path": "raw/sources/file/proj/other.md", "project": "proj", "source_name": "other"},
    ]
    result = wiki_ingest_batch(str(batch_root), action="enqueue", tasks=tasks)
    assert result["ok"] is True
    assert len(result["added"]) == 2
    assert result["queue_size"] == 2

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["ok"] is True
    assert status["counts"]["pending"] == 2


def test_next_and_complete(batch_root: Path):
    tasks = [{"source_path": "raw/a.md", "project": "p", "source_name": "a"}]
    wiki_ingest_batch(str(batch_root), action="enqueue", tasks=tasks)

    next_result = wiki_ingest_batch(str(batch_root), action="next")
    assert next_result["ok"] is True
    assert next_result["task"] is not None
    task_id = next_result["task"]["id"]
    assert next_result["task"]["status"] == "processing"

    complete_result = wiki_ingest_batch(str(batch_root), action="complete", task_id=task_id, result={"pages": 3})
    assert complete_result["ok"] is True

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["counts"]["done"] == 1


def test_fail_and_retry(batch_root: Path):
    tasks = [{"source_path": "raw/b.md", "project": "p", "source_name": "b"}]
    wiki_ingest_batch(str(batch_root), action="enqueue", tasks=tasks)

    next_result = wiki_ingest_batch(str(batch_root), action="next")
    task_id = next_result["task"]["id"]

    wiki_ingest_batch(str(batch_root), action="fail", task_id=task_id, result={"error": "timeout"})

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["counts"]["failed"] == 1

    retry_result = wiki_ingest_batch(str(batch_root), action="retry")
    assert retry_result["ok"] is True
    assert len(retry_result["retried"]) == 1

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["counts"]["pending"] == 1


def test_cancel(batch_root: Path):
    tasks = [{"source_path": "raw/c.md", "project": "p", "source_name": "c"}]
    result = wiki_ingest_batch(str(batch_root), action="enqueue", tasks=tasks)
    task_id = result["added"][0]

    cancel_result = wiki_ingest_batch(str(batch_root), action="cancel", task_id=task_id)
    assert cancel_result["ok"] is True

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["total"] == 0


def test_clear_done(batch_root: Path):
    tasks = [{"source_path": "raw/d.md", "project": "p", "source_name": "d"}]
    wiki_ingest_batch(str(batch_root), action="enqueue", tasks=tasks)
    next_result = wiki_ingest_batch(str(batch_root), action="next")
    wiki_ingest_batch(str(batch_root), action="complete", task_id=next_result["task"]["id"])

    clear_result = wiki_ingest_batch(str(batch_root), action="clear_done")
    assert clear_result["ok"] is True
    assert clear_result["removed"] == 1

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["total"] == 0


def test_next_empty_queue(batch_root: Path):
    result = wiki_ingest_batch(str(batch_root), action="next")
    assert result["ok"] is True
    assert result["task"] is None
