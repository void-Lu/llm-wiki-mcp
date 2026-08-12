"""Admin-only recovery of page projections after a durable fact commit."""

from __future__ import annotations

from pathlib import Path

from wiki.page_mutation import PageMutationCoordinator
from wiki.page_operation_store import PageOperation, PageOperationStore


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
        return self.coordinator.repair(operation_id, self.coordinator.projections_for(operation))


def _operation_summary(operation: PageOperation) -> dict[str, object]:
    summary = operation.to_dict()
    summary["stages"] = {
        key: PageOperationStore.safe_stage_record(value)
        for key, value in operation.stages.items()
    }
    return summary


__all__ = ["PageRepairService"]
