"""Persistent ingest queue with status tracking.

Provides batch ingest capability with crash recovery. Tasks are
persisted to .llm-wiki/ingest-queue.json and survive process restarts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

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
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
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
