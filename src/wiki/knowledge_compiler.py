"""Raw-only provenance invalidation.

The former capsule compiler has been retired.  Raw snapshots remain immutable
evidence; formal Wiki pages are created or updated explicitly through the
normal note/update workflows.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from wiki.generation_queue import DISABLED_JOB_TYPES, GenerationQueue
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.wiki_paths import filesystem_path


PROMPT_VERSION = "raw-only-v1"
SCHEMA_VERSION = 2


def file_hash(path: Path) -> str:
    return hashlib.sha256(filesystem_path(path).read_bytes()).hexdigest()


class KnowledgeCompiler:
    """Compatibility boundary for raw changes without page generation."""

    def __init__(self, vault_root: str | Path):
        self.root = filesystem_path(vault_root)
        self.queue = GenerationQueue(self.root)
        self.dependencies = KnowledgeDependencies(self.root)

    def raw_changed(self, raw_path: str | Path) -> dict[str, Any]:
        rel = Path(raw_path).as_posix()
        stale = self.dependencies.source_changed(rel)
        superseded = self.queue.supersede_sources({rel})
        return {
            "ok": True,
            "stale": stale,
            "superseded": superseded,
            "generation": {"enabled": False, "reason": "raw_only"},
        }

    def enqueue_capsule(self, raw_path: str | Path) -> dict[str, Any]:
        del raw_path
        return {"ok": False, "code": "capsule_generation_disabled"}

    def claim(self, owner: str, *, lease_seconds: int = 300) -> dict[str, Any]:
        del owner, lease_seconds
        superseded = self.queue.supersede_job_types(set(DISABLED_JOB_TYPES))
        return {
            "ok": True,
            "job": None,
            "generation": {"enabled": False, "reason": "raw_only"},
            "superseded": superseded,
        }

    def apply_capsule(self, job_id: str, lease_token: str, result: Mapping[str, Any]) -> dict[str, Any]:
        del job_id, lease_token, result
        return {"ok": False, "code": "capsule_generation_disabled"}

    def apply_capsules(self, capsules: list[tuple[str, str, Mapping[str, Any]]]) -> dict[str, Any]:
        del capsules
        return {"ok": False, "code": "capsule_generation_disabled"}
