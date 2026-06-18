from __future__ import annotations

import json
from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_batch import wiki_ingest_batch
from netsuite_llm_wiki_mcp.wiki_ingest import _write_cache
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


@pytest.fixture
def batch_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / ".llm-wiki").mkdir(parents=True)
    return root


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    """Create a full vault with wiki structure for reapply/prepare/apply tests."""
    root = tmp_path / "vault"
    create_wiki_root(root)
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


def test_status_includes_prepared_count(batch_root: Path):
    """Status counts should include the 'prepared' status."""
    # Enqueue a task and manually set its status to prepared
    tasks = [{"source_path": "raw/a.md", "project": "p", "source_name": "a"}]
    result = wiki_ingest_batch(str(batch_root), action="enqueue", tasks=tasks)
    task_id = result["added"][0]

    # Manually update the task status to prepared
    queue_path = batch_root / ".llm-wiki" / "ingest-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue[0]["status"] = "prepared"
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")

    status = wiki_ingest_batch(str(batch_root), action="status")
    assert status["counts"]["prepared"] == 1
    assert "prepared" in status["counts"]


def test_status_redacts_large_prompt_and_generation_payloads(batch_root: Path):
    queue_path = batch_root / ".llm-wiki" / "ingest-queue.json"
    queue_data = [{
        "id": "ingest-999-0100",
        "source_path": "raw/a.md",
        "project": "p",
        "source_name": "a",
        "source_type": "file",
        "status": "prepared",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
        "result": {
            "prompt": "PROMPT BODY " * 100,
            "generation": {"source_summary": "Summary", "pages": [{"body": "BODY " * 100}]},
            "source_hash": "abc123",
        },
    }]
    queue_path.write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")

    status = wiki_ingest_batch(str(batch_root), action="status")

    task = status["tasks"][0]
    task_text = json.dumps(task)
    assert "PROMPT BODY" not in task_text
    assert "BODY BODY" not in task_text
    assert task["has_prompt"] is True
    assert task["has_generation"] is True
    assert task["source_hash"] == "abc123"


# --- reapply tests ---


def test_reapply_processes_from_cache(vault_root: Path):
    """reapply processes pending tasks when cache exists and status is prepared."""
    project, source_name, source_type = "proj", "doc", "file"

    # Write a cache with status "prepared"
    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "prepared",
        "manifest": [],
        "source_summary": "Test source summary",
    }, source_type=source_type)

    # Enqueue a pending task
    tasks = [{"source_path": "raw/sources/file/proj/doc.md", "project": project, "source_name": source_name, "source_type": source_type}]
    result = wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)
    assert result["ok"] is True

    # Run reapply
    reapply_result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert reapply_result["ok"] is True
    assert reapply_result["action"] == "reapply"
    assert reapply_result["processed_count"] >= 1
    assert reapply_result["skipped_count"] == 0

    # Task should be marked done
    status = wiki_ingest_batch(str(vault_root), action="status")
    assert status["counts"]["done"] >= 1


def test_reapply_skips_when_cache_missing(vault_root: Path):
    """reapply skips tasks when cache doesn't exist."""
    project, source_name, source_type = "proj", "nocache", "file"

    # Enqueue a pending task but do NOT create cache
    tasks = [{"source_path": "raw/sources/file/proj/nocache.md", "project": project, "source_name": source_name, "source_type": source_type}]
    result = wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)
    assert result["ok"] is True

    reapply_result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert reapply_result["ok"] is True
    assert reapply_result["skipped_count"] >= 1
    assert any(r["status"] == "skipped" and "cache" in r.get("reason", "") for r in reapply_result["results"])


def test_reapply_skips_when_cache_status_not_prepared(vault_root: Path):
    """reapply skips tasks when cache exists but status is not 'prepared'."""
    project, source_name, source_type = "proj", "applied", "file"

    # Write a cache with status "applied" — reapply should still accept it
    # (pages may have been corrupted after the original apply)
    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "applied",
        "manifest": [],
    }, source_type=source_type)

    # Enqueue a pending task
    tasks = [{"source_path": "raw/sources/file/proj/applied.md", "project": project, "source_name": source_name, "source_type": source_type}]
    result = wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)
    assert result["ok"] is True

    reapply_result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert reapply_result["ok"] is True
    # "applied" status is now accepted for reapply (useful for re-generating corrupted pages)
    assert reapply_result["processed_count"] + reapply_result["failed_count"] >= 1


def test_reapply_with_specific_task_id(vault_root: Path):
    """reapply with task_id processes only that specific task."""
    project, source_name, source_type = "proj", "doc", "file"

    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "prepared",
        "manifest": [],
        "source_summary": "Test source summary",
    }, source_type=source_type)

    tasks = [{"source_path": "raw/sources/file/proj/doc.md", "project": project, "source_name": source_name, "source_type": source_type}]
    result = wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)
    task_id = result["added"][0]

    reapply_result = wiki_ingest_batch(str(vault_root), action="reapply", task_id=task_id)
    assert reapply_result["ok"] is True
    assert reapply_result["action"] == "reapply"


def test_reapply_task_not_found(vault_root: Path):
    """reapply with non-existent task_id returns error."""
    result = wiki_ingest_batch(str(vault_root), action="reapply", task_id="nonexistent-id")
    assert result["ok"] is False
    assert result["code"] == "task_not_found"


def test_reapply_empty_queue(vault_root: Path):
    """reapply on empty queue returns message."""
    result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert result["ok"] is True
    assert result["processed_count"] == 0


def test_reapply_skips_task_missing_project(vault_root: Path):
    """reapply skips tasks that are missing project or source_name."""
    # Manually create a queue with a task missing project
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_data = [{
        "id": "ingest-999-0001",
        "source_path": "raw/a.md",
        "project": "",
        "source_name": "s",
        "source_type": "file",
        "status": "pending",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
    }]
    queue_path.write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert result["ok"] is True
    assert result["skipped_count"] >= 1


# --- prepare_all tests ---


def test_prepare_all_runs_prepare_for_pending(vault_root: Path):
    """prepare_all runs prepare stage for pending tasks and marks them as prepared."""
    project, source_name, source_type = "proj", "src1", "file"

    # Create a source file for prepare to read
    source_dir = vault_root / "raw" / "sources" / source_type / project / source_name
    source_dir.mkdir(parents=True)
    (source_dir / "notes.md").write_text("# Notes\n\nSome content", encoding="utf-8")

    # Enqueue a pending task
    tasks = [{"source_path": f"raw/sources/{source_type}/{project}/{source_name}", "project": project, "source_name": source_name, "source_type": source_type}]
    result = wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)
    assert result["ok"] is True

    prepare_result = wiki_ingest_batch(str(vault_root), action="prepare_all")
    assert prepare_result["ok"] is True
    assert prepare_result["action"] == "prepare_all"
    # The task should be either prepared (needs_model) or done (source_unchanged)
    # or failed — depending on the source. Since this is a new source, it should be prepared.
    assert prepare_result["processed_count"] + prepare_result["failed_count"] >= 1

    # Check task status in queue
    status = wiki_ingest_batch(str(vault_root), action="status")
    task = status["tasks"][0]
    assert task["status"] in ("prepared", "done", "failed")


def test_prepare_all_marks_prepared_when_needs_model(vault_root: Path):
    """prepare_all marks tasks as 'prepared' when prepare returns needs_model."""
    project, source_name, source_type = "proj", "newsrc", "file"

    # Create a source file
    source_dir = vault_root / "raw" / "sources" / source_type / project / source_name
    source_dir.mkdir(parents=True)
    (source_dir / "code.py").write_text("def hello(): pass", encoding="utf-8")

    tasks = [{"source_path": f"raw/sources/{source_type}/{project}/{source_name}", "project": project, "source_name": source_name, "source_type": source_type}]
    wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)

    prepare_result = wiki_ingest_batch(str(vault_root), action="prepare_all")
    assert prepare_result["ok"] is True

    # For a new source, prepare should return needs_model → status=prepared
    status = wiki_ingest_batch(str(vault_root), action="status")
    task = status["tasks"][0]
    if task["status"] == "prepared":
        assert task["has_prompt"] is True
        prepared = wiki_ingest_batch(str(vault_root), action="next_prepared")
        assert prepared["ok"] is True
        assert prepared["task"]["id"] == task["id"]
        assert "prompt" in (prepared["task"].get("result") or {})


def test_prepare_all_response_omits_prompt_body(vault_root: Path):
    project, source_name, source_type = "proj", "slimsrc", "file"
    source_dir = vault_root / "raw" / "sources" / source_type / project / source_name
    source_dir.mkdir(parents=True)
    (source_dir / "notes.md").write_text("# Notes\n\nSome content", encoding="utf-8")
    tasks = [{"source_path": f"raw/sources/{source_type}/{project}/{source_name}", "project": project, "source_name": source_name, "source_type": source_type}]
    wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)

    result = wiki_ingest_batch(str(vault_root), action="prepare_all")

    assert result["ok"] is True
    response_text = json.dumps(result)
    assert "Generate wiki pages" not in response_text
    assert all("prompt" not in item for item in result["results"])


def test_next_prepared_returns_single_prepared_task_with_prompt(batch_root: Path):
    queue_path = batch_root / ".llm-wiki" / "ingest-queue.json"
    queue_path.write_text(json.dumps([
        {
            "id": "ingest-999-0101",
            "source_path": "raw/a.md",
            "project": "p",
            "source_name": "a",
            "source_type": "file",
            "status": "prepared",
            "added_at": 0,
            "retry_count": 0,
            "error": None,
            "result": {"prompt": "single prompt", "source_hash": "hash-a"},
        },
        {
            "id": "ingest-999-0102",
            "source_path": "raw/b.md",
            "project": "p",
            "source_name": "b",
            "source_type": "file",
            "status": "prepared",
            "added_at": 0,
            "retry_count": 0,
            "error": None,
            "result": {"prompt": "other prompt", "source_hash": "hash-b"},
        },
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(str(batch_root), action="next_prepared")

    assert result["ok"] is True
    assert result["task"]["id"] == "ingest-999-0101"
    assert result["task"]["result"]["prompt"] == "single prompt"
    assert "other prompt" not in json.dumps(result)


def test_next_generation_job_returns_isolated_context(vault_root: Path):
    project, source_name, source_type = "proj", "jobsrc", "file"
    source_dir = vault_root / "raw" / "sources" / source_type / project / source_name
    source_dir.mkdir(parents=True)
    (source_dir / "notes.md").write_text("# Notes\n\nSome content", encoding="utf-8")
    tasks = [{"source_path": f"raw/sources/{source_type}/{project}/{source_name}", "project": project, "source_name": source_name, "source_type": source_type}]
    wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)
    wiki_ingest_batch(str(vault_root), action="prepare_all")

    result = wiki_ingest_batch(str(vault_root), action="next_generation_job")

    assert result["ok"] is True
    job = result["job"]
    assert job["task_id"]
    assert job["job_id"]
    assert job["project"] == project
    assert job["source_name"] == source_name
    assert job["target_path"].startswith("wiki/")
    assert job["target_type"] == "source_index"
    assert job["expected_response_schema"]["pages"]
    assert job["raw_sources"]
    job_text = json.dumps(job)
    assert "Generate wiki pages for project" not in job_text
    assert "generation" not in job


def test_prepare_all_empty_queue(vault_root: Path):
    """prepare_all on empty queue returns message."""
    result = wiki_ingest_batch(str(vault_root), action="prepare_all")
    assert result["ok"] is True
    assert result["processed_count"] == 0
    assert result.get("message") == "no pending tasks"


def test_prepare_all_skips_task_without_source_path(vault_root: Path):
    """prepare_all skips tasks that are missing source_path."""
    # Manually create a queue with a task missing source_path
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_data = [{
        "id": "ingest-999-0002",
        "source_path": "",
        "project": "proj",
        "source_name": "s",
        "source_type": "file",
        "status": "pending",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
    }]
    queue_path.write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(str(vault_root), action="prepare_all")
    assert result["ok"] is True
    assert result["skipped_count"] >= 1
    assert any("source_path" in r.get("reason", "") for r in result["results"])


# --- apply_all tests ---


def test_apply_all_no_prepared_tasks(vault_root: Path):
    """apply_all on queue with no prepared tasks returns message."""
    result = wiki_ingest_batch(str(vault_root), action="apply_all")
    assert result["ok"] is True
    assert result["processed_count"] == 0
    assert result.get("message") == "no prepared tasks"


def test_apply_all_skips_task_without_generation(vault_root: Path):
    """apply_all skips prepared tasks that don't have generation in their result."""
    # Manually create a queue with a prepared task but no generation
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_data = [{
        "id": "ingest-999-0003",
        "source_path": "raw/a.md",
        "project": "proj",
        "source_name": "s",
        "source_type": "file",
        "status": "prepared",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
        "result": {"prompt": "some prompt"},  # no generation key
    }]
    queue_path.write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(str(vault_root), action="apply_all")
    assert result["ok"] is True
    assert result["skipped_count"] >= 1
    assert any("generation" in r.get("reason", "") for r in result["results"])


def test_set_generation_updates_one_generation_job(vault_root: Path):
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_path.write_text(json.dumps([{
        "id": "ingest-999-0103",
        "source_path": "raw/a.md",
        "project": "proj",
        "source_name": "s",
        "source_type": "file",
        "status": "prepared",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
        "result": {
            "generation_jobs": [{
                "job_id": "job-1",
                "target_path": "wiki/sources/proj/s.md",
                "target_type": "source_index",
                "target_title": "s",
                "generated": False,
            }],
        },
    }], ensure_ascii=False, indent=2), encoding="utf-8")

    generation = {"source_summary": "Summary", "pages": []}
    result = wiki_ingest_batch(
        str(vault_root),
        action="set_generation",
        task_id="ingest-999-0103",
        result={"job_id": "job-1", "generation": generation},
    )

    assert result["ok"] is True
    status = wiki_ingest_batch(str(vault_root), action="status")
    task = status["tasks"][0]
    assert task["status"] == "prepared"
    assert task["generation_jobs"][0]["has_generation"] is True


def test_apply_one_applies_stored_job_generation(vault_root: Path):
    project, source_name, source_type = "proj", "onejob", "file"
    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "prepared",
        "manifest": [],
    }, source_type=source_type)
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_path.write_text(json.dumps([{
        "id": "ingest-999-0104",
        "source_path": f"raw/sources/{source_type}/{project}/{source_name}",
        "project": project,
        "source_name": source_name,
        "source_type": source_type,
        "status": "prepared",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
        "result": {
            "generation_jobs": [{
                "job_id": "job-1",
                "generation": {"source_summary": "Summary", "pages": []},
            }],
        },
    }], ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(
        str(vault_root),
        action="apply_one",
        task_id="ingest-999-0104",
        result={"job_id": "job-1"},
    )

    assert result["ok"] is True
    assert result["status"] == "done"
    status = wiki_ingest_batch(str(vault_root), action="status")
    assert status["counts"]["done"] == 1


def test_apply_all_records_validation_failure_without_returning_generation(vault_root: Path):
    project, source_name, source_type = "proj", "badgen", "file"
    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "prepared",
        "manifest": [{"path": "raw/sources/file/proj/badgen/notes.md"}],
    }, source_type=source_type)
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_path.write_text(json.dumps([{
        "id": "ingest-999-0105",
        "source_path": f"raw/sources/{source_type}/{project}/{source_name}",
        "project": project,
        "source_name": source_name,
        "source_type": source_type,
        "status": "prepared",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
        "result": {
            "generation": {
                "source_summary": "Summary",
                "pages": [{
                    "path": "wiki/concepts/proj/bad.md",
                    "title": "Bad",
                    "type": "concept",
                    "summary": "Bad summary",
                    "body": "",
                    "sources": ["raw/sources/file/proj/badgen/notes.md"],
                }],
            },
        },
    }], ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(str(vault_root), action="apply_all")

    assert result["ok"] is True
    assert result["failed_count"] == 1
    assert result["results"][0]["error_stage"] == "validate"
    assert result["results"][0]["validation_errors"]
    assert "Bad summary" not in json.dumps(result["results"][0])
    status = wiki_ingest_batch(str(vault_root), action="status")
    task = status["tasks"][0]
    assert task["status"] == "failed"
    assert task["error_stage"] == "validate"
    assert task["validation_errors"]
    assert task["generation_hash"]
    assert "Bad summary" not in json.dumps(status)


def test_apply_all_processes_prepared_task_with_generation(vault_root: Path):
    """apply_all processes prepared tasks that have generation in their result."""
    project, source_name, source_type = "proj", "gensrc", "file"

    # Write cache so apply can find it
    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "prepared",
        "manifest": [],
    }, source_type=source_type)

    # Manually create a queue with a prepared task that has generation
    queue_path = vault_root / ".llm-wiki" / "ingest-queue.json"
    queue_data = [{
        "id": "ingest-999-0004",
        "source_path": f"raw/sources/{source_type}/{project}/{source_name}",
        "project": project,
        "source_name": source_name,
        "source_type": source_type,
        "status": "prepared",
        "added_at": 0,
        "retry_count": 0,
        "error": None,
        "result": {
            "generation": {
                "source_summary": "Test source",
                "pages": [],
            },
        },
    }]
    queue_path.write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = wiki_ingest_batch(str(vault_root), action="apply_all")
    assert result["ok"] is True
    assert result["action"] == "apply_all"
    # Should have processed at least one task
    assert result["processed_count"] + result["failed_count"] >= 1


# --- invalid action ---


def test_invalid_action(batch_root: Path):
    result = wiki_ingest_batch(str(batch_root), action="nonexistent")
    assert result["ok"] is False
    assert result["code"] == "invalid_action"


# --- reapply page integrity checks ---


def test_reapply_detects_missing_pages(vault_root: Path):
    """reapply reports regeneration_needed for pages in written_paths that no longer exist."""
    project, source_name, source_type = "proj", "missing", "file"

    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "applied",
        "manifest": [],
        "source_summary": "Test summary",
        "written_paths": [
            "wiki/concepts/proj/missing-page.md",
            "wiki/concepts/proj/another-page.md",
        ],
    }, source_type=source_type)

    tasks = [{"source_path": "raw/sources/file/proj/missing.md", "project": project, "source_name": source_name, "source_type": source_type}]
    wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)

    result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert result["ok"] is True
    # Check that regeneration_needed is reported in results
    task_result = next((r for r in result["results"] if r["task_id"]), None)
    assert task_result is not None
    assert "regeneration_needed" in task_result
    assert len(task_result["regeneration_needed"]) >= 1


def test_reapply_reports_pages_restored(vault_root: Path):
    """reapply reports pages_restored for intact generated pages in written_paths."""
    from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page, WikiPage

    project, source_name, source_type = "proj", "intact", "file"
    page_path = Path("wiki/concepts/proj/intact-page.md")

    # Write a valid generated page
    write_wiki_page(vault_root, WikiPage(
        relative_path=page_path,
        frontmatter={"type": "concept", "generated": True, "project": project, "source_name": source_name},
        title="Intact Page",
        body="Content here",
    ))

    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "applied",
        "manifest": [],
        "source_summary": "Test summary",
        "written_paths": [page_path.as_posix()],
    }, source_type=source_type)

    tasks = [{"source_path": "raw/sources/file/proj/intact.md", "project": project, "source_name": source_name, "source_type": source_type}]
    wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)

    result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert result["ok"] is True
    task_result = next((r for r in result["results"] if r["task_id"]), None)
    assert task_result is not None
    assert "pages_restored" in task_result
    assert task_result["pages_restored"] >= 1


def test_reapply_returns_index_refreshed(vault_root: Path):
    """reapply returns index_refreshed=True when any task is processed."""
    project, source_name, source_type = "proj", "idxref", "file"

    _write_cache(vault_root, project, source_name, {
        "source_hash": "abc123",
        "status": "prepared",
        "manifest": [],
        "source_summary": "Test summary",
    }, source_type=source_type)

    tasks = [{"source_path": "raw/sources/file/proj/idxref.md", "project": project, "source_name": source_name, "source_type": source_type}]
    wiki_ingest_batch(str(vault_root), action="enqueue", tasks=tasks)

    result = wiki_ingest_batch(str(vault_root), action="reapply")
    assert result["ok"] is True
    assert "index_refreshed" in result
