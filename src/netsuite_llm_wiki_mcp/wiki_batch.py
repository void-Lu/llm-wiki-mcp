"""Persistent ingest queue with status tracking.

Provides batch ingest capability with crash recovery. Tasks are
persisted to .llm-wiki/ingest-queue.json and survive process restarts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_ingest import staged_wiki_ingest, _read_cache
from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_overview import refresh_overview
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry, WikiLogEntry
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter

_MAX_RETRIES = 3


def wiki_ingest_batch(
    vault_root: str | Path,
    action: str = "status",
    tasks: list[dict[str, str]] | None = None,
    task_id: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manage the persistent ingest queue.

    action="enqueue": add tasks to the queue.
    action="next": get the next pending task for processing.
    action="complete": mark a task as done (pass task_id + result).
    action="fail": mark a task as failed (pass task_id + result).
    action="retry": retry failed tasks.
    action="status": return queue status.
    action="cancel": cancel a pending task (pass task_id).
    action="clear_done": remove completed tasks from queue.
    action="reapply": re-apply from cache for pending/failed tasks (batch).
    action="prepare_all": run prepare stage for all pending tasks (batch).
    action="apply_all": run apply stage for all prepared tasks (batch).
    """
    root = Path(vault_root).expanduser().resolve()
    queue_path = _queue_path(root)

    if action == "enqueue":
        return _enqueue(queue_path, tasks or [])
    elif action == "next":
        return _next(queue_path)
    elif action == "complete":
        return _update_status(queue_path, task_id, "done", result)
    elif action == "fail":
        return _update_status(queue_path, task_id, "failed", result)
    elif action == "retry":
        return _retry_failed(queue_path)
    elif action == "status":
        return _status(queue_path)
    elif action == "cancel":
        return _cancel(queue_path, task_id)
    elif action == "clear_done":
        return _clear_done(queue_path)
    elif action == "reapply":
        return _reapply_from_cache(root, queue_path, task_id)
    elif action == "prepare_all":
        return _prepare_all(root, queue_path)
    elif action == "apply_all":
        return _apply_all(root, queue_path)
    else:
        return {"ok": False, "code": "invalid_action", "error": f"unknown action: {action}"}


def _queue_path(root: Path) -> Path:
    return root / ".llm-wiki" / "ingest-queue.json"


def _load_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_queue(path: Path, queue: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


def _generate_id() -> str:
    return f"ingest-{int(time.time() * 1000)}-{hash(time.time()) % 10000:04d}"


def _enqueue(path: Path, tasks: list[dict[str, str]]) -> dict[str, Any]:
    queue = _load_queue(path)
    added: list[str] = []

    for task in tasks:
        source_path = task.get("source_path", "")
        project = task.get("project", "")
        source_name = task.get("source_name", "")
        source_type = task.get("source_type", "file")

        if not source_path and not source_name:
            continue

        task_id = _generate_id()
        queue.append({
            "id": task_id,
            "source_path": source_path,
            "project": project,
            "source_name": source_name,
            "source_type": source_type,
            "status": "pending",
            "added_at": time.time(),
            "retry_count": 0,
            "error": None,
        })
        added.append(task_id)

    _save_queue(path, queue)
    return {"ok": True, "action": "enqueue", "added": added, "queue_size": len(queue)}


def _next(path: Path) -> dict[str, Any]:
    queue = _load_queue(path)
    for task in queue:
        if task["status"] == "pending":
            task["status"] = "processing"
            task["started_at"] = time.time()
            _save_queue(path, queue)
            return {"ok": True, "action": "next", "task": task}

    return {"ok": True, "action": "next", "task": None, "message": "no pending tasks"}


def _update_status(
    path: Path, task_id: str | None, status: str, result: dict[str, Any] | None
) -> dict[str, Any]:
    if not task_id:
        return {"ok": False, "code": "missing_task_id", "error": "task_id is required"}

    queue = _load_queue(path)
    for task in queue:
        if task["id"] == task_id:
            task["status"] = status
            task["completed_at"] = time.time()
            if result:
                task["result"] = result
            if status == "failed":
                task["retry_count"] = task.get("retry_count", 0) + 1
                task["error"] = (result or {}).get("error", "unknown error")
            _save_queue(path, queue)
            return {"ok": True, "action": status, "task_id": task_id}

    return {"ok": False, "code": "task_not_found", "error": f"task {task_id} not found"}


def _retry_failed(path: Path) -> dict[str, Any]:
    queue = _load_queue(path)
    retried: list[str] = []
    for task in queue:
        if task["status"] == "failed" and task.get("retry_count", 0) < _MAX_RETRIES:
            task["status"] = "pending"
            retried.append(task["id"])

    _save_queue(path, queue)
    return {"ok": True, "action": "retry", "retried": retried}


def _status(path: Path) -> dict[str, Any]:
    queue = _load_queue(path)
    counts = {"pending": 0, "processing": 0, "prepared": 0, "done": 0, "failed": 0}
    for task in queue:
        status = task.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1

    return {
        "ok": True,
        "action": "status",
        "total": len(queue),
        "counts": counts,
        "tasks": queue,
    }


def _cancel(path: Path, task_id: str | None) -> dict[str, Any]:
    if not task_id:
        return {"ok": False, "code": "missing_task_id", "error": "task_id is required"}

    queue = _load_queue(path)
    new_queue = [t for t in queue if t["id"] != task_id]
    if len(new_queue) == len(queue):
        return {"ok": False, "code": "task_not_found", "error": f"task {task_id} not found"}

    _save_queue(path, new_queue)
    return {"ok": True, "action": "cancel", "task_id": task_id}


def _clear_done(path: Path) -> dict[str, Any]:
    queue = _load_queue(path)
    new_queue = [t for t in queue if t["status"] != "done"]
    removed = len(queue) - len(new_queue)
    _save_queue(path, new_queue)
    return {"ok": True, "action": "clear_done", "removed": removed}


def _check_page_integrity(root: Path, written_paths: list[str]) -> dict[str, Any]:
    """Check integrity of previously written wiki pages.

    Returns dict with:
      pages_restored — count of intact generated pages
      regeneration_needed — list of page paths that are missing or corrupted
    """
    pages_restored = 0
    regeneration_needed: list[str] = []

    for page_path_str in written_paths:
        page_file = root / page_path_str
        if not page_file.exists():
            regeneration_needed.append(page_path_str)
            continue
        try:
            text = page_file.read_text(encoding="utf-8")
            fm, _ = split_frontmatter(text)
            if not fm.get("generated"):
                regeneration_needed.append(page_path_str)
            else:
                pages_restored += 1
        except Exception:
            regeneration_needed.append(page_path_str)

    return {"pages_restored": pages_restored, "regeneration_needed": regeneration_needed}


def _reapply_from_cache(root: Path, queue_path: Path, task_id: str | None) -> dict[str, Any]:
    """Re-apply from cache for pending or failed tasks.

    If task_id is provided, processes only that task. Otherwise processes
    all pending and failed tasks in the queue. Re-runs the apply stage
    from existing cache data WITHOUT needing to re-prepare or call an LLM.
    Useful when wiki pages were corrupted but the cache is intact.

    Also checks page integrity from cache written_paths and reports
    pages that need full LLM re-ingest via regeneration_needed.
    """
    queue = _load_queue(queue_path)

    if task_id:
        targets = [t for t in queue if t["id"] == task_id]
        if not targets:
            return {"ok": False, "code": "task_not_found", "error": f"task {task_id} not found"}
    else:
        targets = [t for t in queue if t.get("status") in ("pending", "failed")]

    if not targets:
        return {"ok": True, "action": "reapply", "processed_count": 0, "skipped_count": 0, "failed_count": 0, "results": [], "message": "no eligible tasks"}

    processed = 0
    skipped = 0
    failed = 0
    results: list[dict[str, Any]] = []
    any_index_refreshed = False

    for task in targets:
        project = task.get("project", "")
        source_name = task.get("source_name", "")
        source_type = task.get("source_type", "file")

        if not project or not source_name:
            skipped += 1
            results.append({"task_id": task["id"], "status": "skipped", "reason": "missing project or source_name"})
            continue

        cache = _read_cache(root, project, source_name, source_type)
        if not cache or cache.get("status") not in ("prepared", "applied"):
            skipped += 1
            results.append({"task_id": task["id"], "status": "skipped", "reason": "cache missing or status not prepared/applied"})
            continue

        # Check page integrity from cache written_paths
        written_paths = cache.get("written_paths", [])
        integrity = _check_page_integrity(root, written_paths)

        source_summary = cache.get("source_summary", f"Source index for {project}/{source_name}")
        generation = {"source_summary": source_summary, "pages": []}

        try:
            apply_result = staged_wiki_ingest(
                root, stage="apply", project=project,
                source_name=source_name, generation=generation,
                source_type=source_type,
            )
            if apply_result.get("ok"):
                task["status"] = "done"
                task["completed_at"] = time.time()
                processed += 1
                any_index_refreshed = True
                results.append({
                    "task_id": task["id"],
                    "status": "done",
                    "written": apply_result.get("written", 0),
                    "pages_restored": integrity["pages_restored"],
                    "regeneration_needed": integrity["regeneration_needed"],
                })
            else:
                task["status"] = "failed"
                task["retry_count"] = task.get("retry_count", 0) + 1
                task["error"] = apply_result.get("error", "apply failed")
                failed += 1
                results.append({"task_id": task["id"], "status": "failed", "error": task["error"]})
        except Exception as exc:
            task["status"] = "failed"
            task["retry_count"] = task.get("retry_count", 0) + 1
            task["error"] = str(exc)
            failed += 1
            results.append({"task_id": task["id"], "status": "failed", "error": str(exc)})

    _save_queue(queue_path, queue)

    # Refresh index/overview/log if any task was processed
    if any_index_refreshed:
        try:
            refresh_indexes(root)
            refresh_overview(root)
            append_log_entry(
                root,
                WikiLogEntry(
                    operation="batch_reapply",
                    title="batch reapply from cache",
                    paths=[],
                    sources=[],
                    project="",
                    status="ok",
                ),
            )
        except Exception:
            pass  # Index refresh is best-effort; don't fail the batch

    return {
        "ok": True,
        "action": "reapply",
        "processed_count": processed,
        "skipped_count": skipped,
        "failed_count": failed,
        "results": results,
        "index_refreshed": any_index_refreshed,
    }


def _prepare_all(root: Path, queue_path: Path) -> dict[str, Any]:
    """Run the prepare stage for all pending tasks.

    For each pending task, calls staged_wiki_ingest with stage="prepare".
    If prepare returns needs_model, updates the task status to "prepared"
    and stores the prompt/context in the task's result field.
    If prepare returns source_unchanged, marks the task as done.
    If prepare fails, marks the task as failed.
    """
    queue = _load_queue(queue_path)
    targets = [t for t in queue if t.get("status") == "pending"]

    if not targets:
        return {"ok": True, "action": "prepare_all", "processed_count": 0, "skipped_count": 0, "failed_count": 0, "results": [], "message": "no pending tasks"}

    processed = 0
    skipped = 0
    failed = 0
    results: list[dict[str, Any]] = []

    for task in targets:
        project = task.get("project", "")
        source_name = task.get("source_name", "")
        source_type = task.get("source_type", "file")
        source_path = task.get("source_path", "")

        if not project or not source_name:
            skipped += 1
            results.append({"task_id": task["id"], "status": "skipped", "reason": "missing project or source_name"})
            continue

        if not source_path:
            skipped += 1
            results.append({"task_id": task["id"], "status": "skipped", "reason": "missing source_path"})
            continue

        try:
            prepare_result = staged_wiki_ingest(
                root, stage="prepare", project=project,
                source_name=source_name, source_path=source_path,
                source_type=source_type,
            )
            status = prepare_result.get("status", "")

            if prepare_result.get("ok") and status == "needs_model":
                task["status"] = "prepared"
                task["result"] = {
                    "prompt": prepare_result.get("prompt"),
                    "expected_response_schema": prepare_result.get("expected_response_schema"),
                    "source_hash": prepare_result.get("source_hash"),
                }
                processed += 1
                results.append({"task_id": task["id"], "status": "prepared", "source_hash": prepare_result.get("source_hash")})
            elif prepare_result.get("ok") and status in ("skipped", "source_unchanged"):
                task["status"] = "done"
                task["completed_at"] = time.time()
                processed += 1
                results.append({"task_id": task["id"], "status": "done", "reason": "source unchanged"})
            else:
                task["status"] = "failed"
                task["retry_count"] = task.get("retry_count", 0) + 1
                task["error"] = prepare_result.get("error", "prepare failed")
                failed += 1
                results.append({"task_id": task["id"], "status": "failed", "error": task["error"]})
        except Exception as exc:
            task["status"] = "failed"
            task["retry_count"] = task.get("retry_count", 0) + 1
            task["error"] = str(exc)
            failed += 1
            results.append({"task_id": task["id"], "status": "failed", "error": str(exc)})

    _save_queue(queue_path, queue)
    return {
        "ok": True,
        "action": "prepare_all",
        "processed_count": processed,
        "skipped_count": skipped,
        "failed_count": failed,
        "results": results,
    }


def _apply_all(root: Path, queue_path: Path) -> dict[str, Any]:
    """Run the apply stage for all prepared tasks.

    For each task with status "prepared", calls staged_wiki_ingest with
    stage="apply" using the generation stored in the task's result field.
    Marks the task as done or failed accordingly.
    """
    queue = _load_queue(queue_path)
    targets = [t for t in queue if t.get("status") == "prepared"]

    if not targets:
        return {"ok": True, "action": "apply_all", "processed_count": 0, "skipped_count": 0, "failed_count": 0, "results": [], "message": "no prepared tasks"}

    processed = 0
    skipped = 0
    failed = 0
    results: list[dict[str, Any]] = []

    for task in targets:
        project = task.get("project", "")
        source_name = task.get("source_name", "")
        source_type = task.get("source_type", "file")
        generation = (task.get("result") or {}).get("generation")

        if not project or not source_name:
            skipped += 1
            results.append({"task_id": task["id"], "status": "skipped", "reason": "missing project or source_name"})
            continue

        if not generation:
            skipped += 1
            results.append({"task_id": task["id"], "status": "skipped", "reason": "missing generation in task result"})
            continue

        try:
            apply_result = staged_wiki_ingest(
                root, stage="apply", project=project,
                source_name=source_name, generation=generation,
                source_type=source_type,
            )
            if apply_result.get("ok"):
                task["status"] = "done"
                task["completed_at"] = time.time()
                processed += 1
                results.append({"task_id": task["id"], "status": "done", "written": apply_result.get("written", 0)})
            else:
                task["status"] = "failed"
                task["retry_count"] = task.get("retry_count", 0) + 1
                task["error"] = apply_result.get("error", "apply failed")
                failed += 1
                results.append({"task_id": task["id"], "status": "failed", "error": task["error"]})
        except Exception as exc:
            task["status"] = "failed"
            task["retry_count"] = task.get("retry_count", 0) + 1
            task["error"] = str(exc)
            failed += 1
            results.append({"task_id": task["id"], "status": "failed", "error": str(exc)})

    _save_queue(queue_path, queue)
    return {
        "ok": True,
        "action": "apply_all",
        "processed_count": processed,
        "skipped_count": skipped,
        "failed_count": failed,
        "results": results,
    }
