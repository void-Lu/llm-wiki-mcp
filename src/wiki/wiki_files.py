from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.runtime_provenance import RUNTIME_PROVENANCE
from retrieval.retrieval_index import RetrievalIndexStore
from retrieval.vector_index import VectorIndexStore
from wiki.wiki_paths import DEFAULT_FILES, TOP_LEVEL_DIRS


def wiki_status(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    missing = _missing_required_paths(root)
    return {
        "ok": True,
        "vault_root": str(root),
        "initialized": root.exists() and not missing,
        "missing_required_paths": missing,
        "queue": _queue_status(root),
        "vector": _vector_status(root),
        "retrieval": {
            "active": RetrievalIndexStore(root).status(),
            "archive": RetrievalIndexStore(root, scope="archive").status(),
            "raw": RetrievalIndexStore(root, scope="raw").status(),
        },
        "version": RUNTIME_PROVENANCE.package_version,
        "runtime": RUNTIME_PROVENANCE.to_public_dict(),
    }


def _missing_required_paths(root: Path) -> list[str]:
    required = list(TOP_LEVEL_DIRS) + list(DEFAULT_FILES)
    return [relative.as_posix() for relative in required if not (root / relative).exists()]


def _queue_status(root: Path) -> dict[str, Any]:
    queue_path = root / ".llm-wiki" / "ingest-queue.json"
    if not queue_path.exists():
        return {"path": queue_path.relative_to(root).as_posix(), "total": 0, "counts": {}}
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": queue_path.relative_to(root).as_posix(), "total": 0, "counts": {}, "error": str(exc)}
    queue = data if isinstance(data, list) else []
    counts: dict[str, int] = {}
    for item in queue:
        status = str(item.get("status", "unknown")) if isinstance(item, dict) else "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {"path": queue_path.relative_to(root).as_posix(), "total": len(queue), "counts": counts}


def _vector_status(root: Path) -> dict[str, object]:
    """Expose index state without loading models, dependencies, or page bodies."""

    return VectorIndexStore(root).status()
