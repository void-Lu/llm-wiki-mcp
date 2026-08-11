"""Admin-only recovery of page projections after a durable fact commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_mutation import PageMutationCoordinator
from wiki.page_operation_store import PageOperation, PageOperationStore
from wiki.wiki_index import refresh_indexes
from wiki.wiki_io import read_markdown_page, refresh_page_retrieval
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_overview import refresh_overview


class PageRepairService:
    """Expose safe repair planning and application to the administrative CLI."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.store = PageOperationStore(self.root)
        self.coordinator = PageMutationCoordinator(self.root, store=self.store)

    def plan(self, operation_id: str | None = None) -> dict[str, object]:
        if operation_id:
            operation = self.store.get_operation(operation_id)
            if operation is None:
                return {"ok": False, "code": "operation_not_found"}
            return {"ok": True, "operations": [_operation_summary(operation)]}
        return {"ok": True, "operations": [_operation_summary(item) for item in self.store.pending_operations()]}

    def apply(self, operation_id: str) -> dict[str, object]:
        operation = self.store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        return self.coordinator.repair(operation_id, self.projections_for(operation))

    def projections_for(self, operation: PageOperation) -> dict[str, Any]:
        target = (self.root / Path(*operation.page_path.split("/"))).resolve()
        if not target.is_file() or not target.is_relative_to(self.root):
            return {
                "dependencies": lambda: {"ok": False, "code": "page_not_found"},
                "retrieval": lambda: {"ok": False, "code": "page_not_found"},
                "navigation": lambda: {"ok": False, "code": "page_not_found"},
                "overview": lambda: {"ok": False, "code": "page_not_found"},
                "audit_log": lambda: {"ok": False, "code": "page_not_found"},
            }
        page = read_markdown_page(target, self.root)
        source_hashes = _source_hashes(page.frontmatter)
        sources = _sources(page.frontmatter)
        freshness = str(page.frontmatter.get("freshness") or ("fresh" if source_hashes else "review_required"))
        generated = bool(page.frontmatter.get("generated"))
        maintenance = str(page.frontmatter.get("maintenance") or ("auto" if generated else "manual"))
        lifecycle = str(page.frontmatter.get("lifecycle") or "active")
        replaced_by = page.frontmatter.get("replaced_by")

        def dependencies() -> dict[str, object]:
            KnowledgeDependencies(self.root).update_page(
                operation.page_path,
                operation.intended_hash,
                source_hashes,
                generated=generated,
                maintenance=maintenance,
                lifecycle=lifecycle,
                replaced_by=str(replaced_by) if replaced_by else None,
                freshness=freshness if freshness in {"fresh", "stale", "review_required"} else "review_required",
            )
            return {"ok": True, "state": "ready"}

        def retrieval() -> dict[str, object]:
            return refresh_page_retrieval(self.root, target)

        def navigation() -> dict[str, object]:
            return refresh_indexes(self.root)

        def overview() -> dict[str, object]:
            return refresh_overview(self.root)

        def audit_log() -> dict[str, object]:
            return append_log_entry(
                self.root,
                WikiLogEntry(
                    operation="update" if operation.operation_kind == "update" else "note",
                    title=page.title,
                    paths=[operation.page_path],
                    sources=sources,
                    project=str(page.frontmatter.get("project") or ""),
                    status="ok",
                    operation_id=operation.operation_id,
                ),
            )

        return {
            "dependencies": dependencies,
            "retrieval": retrieval,
            "navigation": navigation,
            "overview": overview,
            "audit_log": audit_log,
        }


def _operation_summary(operation: PageOperation) -> dict[str, object]:
    return {
        "operation_id": operation.operation_id,
        "operation_kind": operation.operation_kind,
        "page_path": operation.page_path,
        "base_hash": operation.base_hash,
        "intended_hash": operation.intended_hash,
        "state": operation.state,
        "error_code": operation.error_code,
        "stages": {key: dict(value) for key, value in operation.stages.items()},
    }


def _source_hashes(frontmatter: dict[str, Any]) -> dict[str, str]:
    value = frontmatter.get("source_hashes")
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key) and str(item)}


def _sources(frontmatter: dict[str, Any]) -> list[str]:
    value = frontmatter.get("sources", [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)] if value else []


__all__ = ["PageRepairService"]
