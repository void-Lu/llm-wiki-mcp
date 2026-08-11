"""Page fact commit and projection orchestration."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import threading
from typing import Any, Callable, Mapping

from common.privacy_policy import normalize_vault_relative
from wiki.atomic_file import AtomicFileError, FaultBarrier, atomic_write_text, sha256_file
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_operation_store import PAGE_STAGES, PageOperation, PageOperationError, PageOperationStore
from wiki.wiki_index import refresh_indexes
from wiki.wiki_io import read_markdown_page, refresh_page_retrieval
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_overview import refresh_overview


Projection = Callable[[], Mapping[str, object] | None]


class PageMutationError(ValueError):
    """Stable failure from the page mutation coordinator."""

    def __init__(self, code: str, message: str = "page mutation could not be completed") -> None:
        super().__init__(message)
        self.code = code


def fault_barrier(stage: str) -> None:
    """Default no-op barrier for deterministic commit/projection tests."""

    del stage


class PageMutationCoordinator:
    """Serialize in-process page writers and persist recoverable stages."""

    _locks: dict[tuple[str, str], threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, vault_root: str | Path, *, store: PageOperationStore | None = None, fault: FaultBarrier | None = None):
        self.root = Path(vault_root).expanduser().resolve()
        self.store = store or PageOperationStore(self.root)
        self.fault = fault

    def prepare(
        self,
        *,
        request_key: str,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
    ) -> PageOperation:
        return self.store.create_operation(
            request_key=request_key,
            operation_kind=operation_kind,
            page_path=page_path,
            base_hash=base_hash,
            intended_hash=intended_hash,
        )

    def commit(
        self,
        operation_id: str,
        text: str,
        *,
        expected_hash: str | None = None,
        fault: FaultBarrier | None = None,
    ) -> dict[str, object]:
        operation = self.store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        target = self._target(operation.page_path)
        current_hash = _hash_if_exists(target)
        expected_base = operation.base_hash if expected_hash is None else expected_hash
        if current_hash != expected_base:
            self._mark_precommit_failure(operation_id, "expected_hash_mismatch")
            return {"ok": False, "code": "expected_hash_mismatch", "operation_id": operation_id, "state": "failed_precommit"}
        intended_hash = sha256(text.encode("utf-8")).hexdigest()
        if intended_hash != operation.intended_hash:
            self._mark_precommit_failure(operation_id, "operation_intent_drift")
            return {"ok": False, "code": "operation_intent_drift", "operation_id": operation_id, "state": "failed_precommit"}

        with self._page_lock(operation.page_path):
            current_hash = _hash_if_exists(target)
            if current_hash != expected_base:
                self._mark_precommit_failure(operation_id, "expected_hash_mismatch")
                return {"ok": False, "code": "expected_hash_mismatch", "operation_id": operation_id, "state": "failed_precommit"}
            try:
                atomic_write_text(target, text, fault=fault or self.fault)
            except AtomicFileError:
                return self._classify_commit_failure(operation, target)
            try:
                self._invoke("journal_commit", fault)
                committed = self.store.set_operation_state(operation_id, "page_committed")
            except Exception:
                classified = self._classify_disk(operation, target)
                if classified == "intended":
                    try:
                        self.store.set_operation_state(operation_id, "repair_pending", error_code="journal_commit_pending")
                    except Exception:
                        pass
                    return {
                        "ok": True,
                        "state": "repair_pending",
                        "operation_id": operation_id,
                        "page_hash": operation.intended_hash,
                        "code": "journal_commit_pending",
                    }
                return self._classify_commit_failure(operation, target)
        return {
            "ok": True,
            "state": "page_committed",
            "operation_id": committed.operation_id,
            "page_hash": committed.intended_hash,
        }

    def run_projections(
        self,
        operation_id: str,
        projections: Mapping[str, Projection],
        *,
        fault: FaultBarrier | None = None,
    ) -> dict[str, object]:
        operation = self.store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        if operation.state == "completed":
            return {"ok": True, "state": "completed", "operation_id": operation_id, "already_applied": True}
        if operation.state not in {"page_committed", "repair_pending"}:
            return {"ok": False, "code": "operation_not_committed", "state": operation.state, "operation_id": operation_id}

        for stage in PAGE_STAGES:
            current = self.store.get_operation(operation_id)
            if current is None:
                return {"ok": False, "code": "operation_not_found"}
            if current.stages.get(stage, {}).get("state") == "succeeded":
                continue
            try:
                self.store.record_stage(operation_id, stage, "running")
                self._invoke(f"projection:{stage}", fault)
                callback = projections.get(stage)
                result = callback() if callback is not None else {"ok": True, "state": "not_configured"}
                result_dict = dict(result or {})
                if result_dict.get("ok") is False:
                    code = str(result_dict.get("code") or f"{stage}_failed")
                    self.store.record_stage(operation_id, stage, "failed", code=code, result=result_dict)
                    self.store.set_operation_state(operation_id, "repair_pending", error_code=code)
                    return {"ok": True, "state": "repair_pending", "operation_id": operation_id, "failed_stage": stage, "code": "projection_repair_required", "repair_action": "repair_page_operation"}
                self.store.record_stage(operation_id, stage, "succeeded", result=result_dict)
            except Exception as exc:
                code = str(getattr(exc, "code", None) or f"{stage}_failed")
                try:
                    self.store.record_stage(operation_id, stage, "failed", code=code)
                    self.store.set_operation_state(operation_id, "repair_pending", error_code=code)
                except Exception:
                    pass
                return {"ok": True, "state": "repair_pending", "operation_id": operation_id, "failed_stage": stage, "code": "projection_repair_required", "repair_action": "repair_page_operation"}
        self.store.set_operation_state(operation_id, "completed")
        return {"ok": True, "state": "completed", "operation_id": operation_id}

    def recover(self, operation_id: str) -> dict[str, object]:
        operation = self.store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        if operation.state == "completed":
            return {"ok": True, "state": "completed", "operation_id": operation_id}
        classification = self._classify_disk(operation, self._target(operation.page_path))
        if classification == "intended":
            try:
                self.store.set_operation_state(operation_id, "page_committed")
            except PageOperationError:
                pass
            return {"ok": True, "state": "repair_pending", "operation_id": operation_id, "hash_classification": "intended"}
        if classification == "base":
            self._mark_precommit_failure(operation_id, "write_failed_precommit")
            return {"ok": False, "state": "failed_precommit", "operation_id": operation_id, "code": "write_failed_precommit", "hash_classification": "base"}
        try:
            self.store.set_operation_state(operation_id, "conflict", error_code="operation_conflict")
        except PageOperationError:
            pass
        return {"ok": False, "state": "conflict", "operation_id": operation_id, "code": "operation_conflict", "hash_classification": "third"}

    def repair(self, operation_id: str, projections: Mapping[str, Projection], *, fault: FaultBarrier | None = None) -> dict[str, object]:
        recovered = self.recover(operation_id)
        if recovered.get("state") == "completed":
            return recovered
        if recovered.get("state") != "repair_pending":
            return recovered
        return self.run_projections(operation_id, projections, fault=fault)

    def projections_for(self, operation: PageOperation) -> dict[str, Projection]:
        """Build the standard projection set for a committed page operation."""

        target = self._target(operation.page_path)
        if not target.is_file():
            return {
                stage: (lambda: {"ok": False, "code": "page_not_found"})
                for stage in PAGE_STAGES
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

    def commit_with_projections(
        self,
        operation_id: str,
        text: str,
        *,
        expected_hash: str | None = None,
        fault: FaultBarrier | None = None,
    ) -> dict[str, object]:
        """Commit a prepared operation and run its standard projections."""

        operation = self.store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        if operation.state == "completed":
            return {
                "ok": True,
                "state": "completed",
                "operation_id": operation.operation_id,
                "page_hash": operation.intended_hash,
                "already_applied": True,
            }
        if operation.state == "prepared":
            commit_result = self.commit(operation_id, text, expected_hash=expected_hash, fault=fault)
            if not commit_result.get("ok"):
                return commit_result
        elif operation.state not in {"page_committed", "repair_pending"}:
            return {
                "ok": False,
                "code": "operation_not_committed",
                "state": operation.state,
                "operation_id": operation.operation_id,
            }
        result = self.run_projections(operation_id, self.projections_for(operation), fault=fault)
        result.setdefault("page_hash", operation.intended_hash)
        return result

    def write_and_project(
        self,
        *,
        request_key: str,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
        text: str,
        expected_hash: str | None = None,
        fault: FaultBarrier | None = None,
    ) -> dict[str, object]:
        """Prepare, commit, and project one page mutation as one deep operation."""

        try:
            operation = self.prepare(
                request_key=request_key,
                operation_kind=operation_kind,
                page_path=page_path,
                base_hash=base_hash,
                intended_hash=intended_hash,
            )
        except PageOperationError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        return self.commit_with_projections(
            operation.operation_id,
            text,
            expected_hash=expected_hash,
            fault=fault,
        )

    def _classify_commit_failure(self, operation: PageOperation, target: Path) -> dict[str, object]:
        classification = self._classify_disk(operation, target)
        if classification == "intended":
            try:
                self.store.set_operation_state(operation.operation_id, "page_committed")
            except Exception:
                pass
            return {"ok": True, "state": "repair_pending", "operation_id": operation.operation_id, "page_hash": operation.intended_hash, "code": "projection_repair_required", "repair_action": "repair_page_operation"}
        if classification == "base":
            self._mark_precommit_failure(operation.operation_id, "write_failed_precommit")
            return {"ok": False, "code": "write_failed_precommit", "state": "failed_precommit", "operation_id": operation.operation_id}
        try:
            self.store.set_operation_state(operation.operation_id, "conflict", error_code="operation_conflict")
        except Exception:
            pass
        return {"ok": False, "code": "operation_conflict", "state": "conflict", "operation_id": operation.operation_id}

    def _classify_disk(self, operation: PageOperation, target: Path) -> str:
        current = _hash_if_exists(target)
        if current == operation.intended_hash:
            return "intended"
        if current == operation.base_hash:
            return "base"
        return "third"

    def _mark_precommit_failure(self, operation_id: str, code: str) -> None:
        try:
            self.store.set_operation_state(operation_id, "failed_precommit", error_code=code)
        except Exception:
            pass

    def _target(self, page_path: str) -> Path:
        relative = normalize_vault_relative(page_path)
        target = (self.root / Path(*relative.split("/"))).resolve()
        if not target.is_relative_to(self.root):
            raise PageMutationError("path_escape")
        return target

    def _invoke(self, stage: str, fault: FaultBarrier | None) -> None:
        (fault or self.fault or fault_barrier)(stage)

    @contextmanager
    def _page_lock(self, page_path: str):
        key = (str(self.root).casefold(), page_path.casefold())
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.RLock())
        with lock:
            yield


def _hash_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return sha256_file(path)
    except OSError:
        return None


def _source_hashes(frontmatter: Mapping[str, Any]) -> dict[str, str]:
    value = frontmatter.get("source_hashes")
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key) and str(item)}


def _sources(frontmatter: Mapping[str, Any]) -> list[str]:
    value = frontmatter.get("sources", [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)] if value else []


__all__ = ["PageMutationCoordinator", "PageMutationError", "Projection", "fault_barrier"]
