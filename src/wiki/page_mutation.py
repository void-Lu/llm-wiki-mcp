"""Page fact commit and projection orchestration."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import threading
from typing import Any, Callable, Mapping

from common.privacy_policy import normalize_vault_relative
from wiki.atomic_file import AtomicFileError, FaultBarrier, atomic_write_text, sha256_file
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_operation_store import PAGE_STAGES, PageOperation, PageOperationError, PageOperationStore
from wiki.update_plan_store import UpdatePlanError, UpdatePlanStore
from wiki.wiki_index import refresh_navigation
from wiki.wiki_io import read_markdown_page, refresh_page_retrieval
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import WikiPathError, validate_wiki_page_path


Projection = Callable[[], Mapping[str, object] | None]
ProjectionBuilder = Callable[["PageMutationCoordinator", PageOperation, Path, FaultBarrier | None], dict[str, Projection]]


@dataclass(frozen=True)
class ProjectionProfile:
    """Named projection policy selected by the durable operation kind."""

    name: str
    build: ProjectionBuilder


@dataclass(frozen=True)
class MutationResult:
    """Typed, bounded result view returned by the deep mutation facade."""

    ok: bool
    state: str | None = None
    code: str | None = None
    operation_id: str | None = None
    page_hash: str | None = None
    repair_action: str | None = None
    failed_stage: str | None = None
    already_applied: bool = False
    stages: Mapping[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        result: Mapping[str, object],
        *,
        stages: Mapping[str, dict[str, object]] | None = None,
    ) -> "MutationResult":
        return cls(
            ok=bool(result.get("ok")),
            state=str(result["state"]) if result.get("state") is not None else None,
            code=str(result["code"]) if result.get("code") is not None else None,
            operation_id=str(result["operation_id"]) if result.get("operation_id") is not None else None,
            page_hash=str(result["page_hash"]) if result.get("page_hash") is not None else None,
            repair_action=str(result["repair_action"]) if result.get("repair_action") is not None else None,
            failed_stage=str(result["failed_stage"]) if result.get("failed_stage") is not None else None,
            already_applied=bool(result.get("already_applied", False)),
            stages=dict(stages or {}),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the legacy-shaped scalar result plus the safe stage view."""

        result: dict[str, object] = {"ok": self.ok}
        for key, value in (
            ("state", self.state),
            ("code", self.code),
            ("operation_id", self.operation_id),
            ("page_hash", self.page_hash),
            ("repair_action", self.repair_action),
            ("failed_stage", self.failed_stage),
        ):
            if value is not None:
                result[key] = value
        if self.already_applied:
            result["already_applied"] = True
        if self.stages:
            result["stages"] = {key: dict(value) for key, value in self.stages.items()}
        return result


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

    def __init__(
        self,
        vault_root: str | Path,
        *,
        store: PageOperationStore | None = None,
        plan_store: UpdatePlanStore | None = None,
        fault: FaultBarrier | None = None,
        profiles: Mapping[str, ProjectionProfile] | None = None,
    ):
        self.root = Path(vault_root).expanduser().resolve()
        self._store = store or PageOperationStore(self.root)
        self._plan_store = plan_store or UpdatePlanStore(self._store)
        self.fault = fault
        self.profiles = {**_DEFAULT_PROFILES, **dict(profiles or {})}

    def prepare(
        self,
        *,
        request_key: str,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
    ) -> PageOperation:
        return self._store.create_operation(
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
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        target = self._target(operation.page_path, allow_raw_source=operation.operation_kind == "chat_source")
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
                committed = self._store.set_operation_state(operation_id, "page_committed")
            except Exception:
                classified = self._classify_disk(operation, target)
                if classified == "intended":
                    try:
                        self._store.set_operation_state(operation_id, "repair_pending", error_code="journal_commit_pending")
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
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        if operation.state == "completed":
            return {"ok": True, "state": "completed", "operation_id": operation_id, "already_applied": True}
        if operation.state not in {"page_committed", "repair_pending"}:
            return {"ok": False, "code": "operation_not_committed", "state": operation.state, "operation_id": operation_id}

        for stage in PAGE_STAGES:
            current = self._store.get_operation(operation_id)
            if current is None:
                return {"ok": False, "code": "operation_not_found"}
            if current.stages.get(stage, {}).get("state") == "succeeded":
                continue
            try:
                self._store.record_stage(operation_id, stage, "running")
                self._invoke(f"projection:{stage}", fault)
                callback = projections.get(stage)
                result = callback() if callback is not None else {"ok": True, "state": "not_configured"}
                result_dict = dict(result or {})
                if result_dict.get("ok") is False:
                    code = str(result_dict.get("code") or f"{stage}_failed")
                    self._store.record_stage(operation_id, stage, "failed", code=code, result=result_dict)
                    self._store.set_operation_state(operation_id, "repair_pending", error_code=code)
                    return {"ok": True, "state": "repair_pending", "operation_id": operation_id, "failed_stage": stage, "code": "projection_repair_required", "repair_action": "repair_page_operation"}
                self._store.record_stage(operation_id, stage, "succeeded", result=result_dict)
            except Exception as exc:
                code = str(getattr(exc, "code", None) or f"{stage}_failed")
                try:
                    self._store.record_stage(operation_id, stage, "failed", code=code)
                    self._store.set_operation_state(operation_id, "repair_pending", error_code=code)
                except Exception:
                    pass
                return {"ok": True, "state": "repair_pending", "operation_id": operation_id, "failed_stage": stage, "code": "projection_repair_required", "repair_action": "repair_page_operation"}
        self._store.set_operation_state(operation_id, "completed")
        return {"ok": True, "state": "completed", "operation_id": operation_id}

    def recover(self, operation_id: str) -> dict[str, object]:
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        if operation.state == "completed":
            return {"ok": True, "state": "completed", "operation_id": operation_id}
        classification = self._classify_disk(
            operation,
            self._target(operation.page_path, allow_raw_source=operation.operation_kind == "chat_source"),
        )
        if classification == "intended":
            try:
                self._store.set_operation_state(operation_id, "page_committed")
            except PageOperationError:
                pass
            return {"ok": True, "state": "repair_pending", "operation_id": operation_id, "hash_classification": "intended"}
        if classification == "base":
            self._mark_precommit_failure(operation_id, "write_failed_precommit")
            return {"ok": False, "state": "failed_precommit", "operation_id": operation_id, "code": "write_failed_precommit", "hash_classification": "base"}
        try:
            self._store.set_operation_state(operation_id, "conflict", error_code="operation_conflict")
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

    def project_existing(
        self,
        operation_id: str,
        *,
        projections: Mapping[str, Projection] | None = None,
        fault: FaultBarrier | None = None,
    ) -> dict[str, object]:
        """Project an already durable fact without rewriting its file.

        Immutable raw revisions can predate the page-operation journal (or a
        process can crash after the file replace).  Recovery classifies the
        existing bytes as the intended fact and then reuses the same stage
        runner, so idempotent retries never create a second revision.
        """

        operation = self._store.get_operation(operation_id)
        if operation is None:
            return {"ok": False, "code": "operation_not_found"}
        if projections is not None:
            selected = projections
        elif fault is None:
            selected = self.projections_for(operation)
        else:
            selected = self.projections_for(operation, fault=fault)
        return self.repair(operation_id, selected, fault=fault)

    def projections_for(
        self,
        operation: PageOperation,
        *,
        fault: FaultBarrier | None = None,
    ) -> dict[str, Projection]:
        """Build the projection set selected by the operation's source profile."""

        target = self._target(operation.page_path, allow_raw_source=operation.operation_kind == "chat_source")
        if not target.is_file():
            return {
                stage: (lambda: {"ok": False, "code": "page_not_found"})
                for stage in PAGE_STAGES
            }
        projection_fault = fault or self.fault
        profile = self.profiles.get(operation.operation_kind, self.profiles["formal_page"])
        return profile.build(self, operation, target, projection_fault)

    def _formal_page_projections(
        self,
        operation: PageOperation,
        target: Path,
        projection_fault: FaultBarrier | None,
    ) -> dict[str, Projection]:
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
            return refresh_navigation(self.root, fault=projection_fault)

        def overview() -> dict[str, object]:
            return refresh_overview(self.root, fault=projection_fault)

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
                fault=projection_fault,
            )

        return {
            "dependencies": dependencies,
            "retrieval": retrieval,
            "navigation": navigation,
            "overview": overview,
            "audit_log": audit_log,
        }

    def _chat_source_projections(
        self,
        operation: PageOperation,
        target: Path,
        projection_fault: FaultBarrier | None,
    ) -> dict[str, Projection]:
        """Build raw-chat projections without treating the source as a page."""

        page = read_markdown_page(target, self.root)
        session_id = str(page.frontmatter.get("session_id") or target.parent.name)
        project = str(page.frontmatter.get("project") or "")
        source_hash = operation.intended_hash

        def dependencies() -> dict[str, object]:
            dependencies = KnowledgeDependencies(self.root)
            affected: set[str] = set()
            # A formal page may pin any immutable revision. A new revision
            # supersedes the whole session lineage, so compare the new bytes
            # against every revision path without changing stored source edges.
            for revision_path in sorted(target.parent.glob("revision-*.md")):
                relative = revision_path.relative_to(self.root).as_posix()
                affected.update(dependencies.source_changed(relative, source_hash))
            return {"ok": True, "state": "ready", "affected_count": len(affected)}

        def retrieval() -> dict[str, object]:
            # Chat revisions live in the active/history projection.  The raw
            # store intentionally excludes chat, so update the active store
            # incrementally. Missing/incompatible stores require an explicit
            # administrator rebuild and must not trigger a hidden full build.
            from retrieval.retrieval_index import RetrievalIndexStore, page_from_file

            store = RetrievalIndexStore(self.root, scope="active")
            indexed = page_from_file(self.root, target, scope="active")
            if indexed is None:
                return {"ok": True, "state": "not_applicable", "code": "not_eligible"}
            status = store.status()
            if not status.get("ok"):
                result = {
                    "ok": True,
                    "state": "rebuild_required",
                    "code": str(status.get("code") or "index_missing"),
                    "operation": "update",
                    "repair_action": "rebuild_retrieval_index",
                }
            else:
                result = store.update_page(indexed)
            if not result.get("ok"):
                return result
            return {
                "ok": True,
                "state": str(result.get("state") or "ready"),
                "code": str(result.get("code") or "ready"),
                "operation": result.get("operation"),
                "retrieval_index": dict(result),
            }

        def not_applicable() -> dict[str, object]:
            return {"ok": True, "state": "not_applicable", "code": "not_applicable"}

        def audit_log() -> dict[str, object]:
            return append_log_entry(
                self.root,
                WikiLogEntry(
                    operation="chat_source",
                    title=session_id,
                    paths=[operation.page_path],
                    sources=[],
                    project=project,
                    status="ok",
                    operation_id=operation.operation_id,
                ),
                fault=projection_fault,
            )

        return {
            "dependencies": dependencies,
            "retrieval": retrieval,
            "navigation": not_applicable,
            "overview": not_applicable,
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

        operation = self._store.get_operation(operation_id)
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
        # Keep the long-standing two-argument override contract usable for
        # callers/tests that customize the projection set.  Only pass the
        # optional fault keyword when the caller actually supplied one;
        # otherwise an override written before fault propagation was added
        # would fail before any projection runs.
        projections = self.projections_for(operation) if fault is None else self.projections_for(operation, fault=fault)
        result = self.run_projections(operation_id, projections, fault=fault)
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
        plan_id: str | None = None,
        intent_hash: str | None = None,
        fault: FaultBarrier | None = None,
    ) -> MutationResult:
        """Prepare, commit, project, and optionally consume one mutation plan."""

        if plan_id is not None:
            plan = self._plan_store.get(plan_id)
            if plan is None:
                return MutationResult(ok=False, code="plan_unknown")
            if plan.state == "consumed":
                if intent_hash != plan.intent_hash:
                    return MutationResult(ok=False, code="plan_intent_drift")
                operation = self._store.get_operation(plan.operation_id) if plan.operation_id else None
                return self._mutation_result(
                    {
                        "ok": True,
                        "state": "already_applied",
                        "operation_id": plan.operation_id,
                        "page_hash": plan.consumed_hash,
                        "already_applied": True,
                        **(
                            {"repair_action": "repair_page_operation"}
                            if operation is not None and operation.state != "completed"
                            else {}
                        ),
                    },
                    operation,
                )
            if _plan_is_expired(plan.expires_at):
                return MutationResult(ok=False, code="plan_expired")
            if plan.state == "expired" or plan.state not in {"issued", "claimed"}:
                return MutationResult(ok=False, code="plan_expired" if plan.state == "expired" else "plan_unknown")
            if plan.state == "claimed" and plan.operation_id:
                claimed = self._store.get_operation(plan.operation_id)
                if claimed is None:
                    return MutationResult(ok=False, code="plan_claimed", operation_id=plan.operation_id)
                if claimed.intended_hash != intended_hash:
                    return MutationResult(ok=False, code="plan_intent_drift", operation_id=claimed.operation_id)
                if claimed.state == "prepared":
                    return MutationResult(ok=False, code="plan_claimed", operation_id=claimed.operation_id)
                operation = claimed
            else:
                operation = None
        else:
            operation = None

        if operation_kind == "chat_source" and operation is None:
            existing = self._store.get_operation_by_request_key(request_key)
            if existing is not None:
                existing_result = self.project_existing(existing.operation_id, fault=fault)
                existing_result = {
                    **existing_result,
                    "already_applied": True,
                }
                return self._mutation_result(
                    existing_result,
                    existing,
                )

        if operation is None:
            try:
                operation = self.prepare(
                    request_key=request_key,
                    operation_kind=operation_kind,
                    page_path=page_path,
                    base_hash=base_hash,
                    intended_hash=intended_hash,
                )
            except PageOperationError as exc:
                return MutationResult(ok=False, code=exc.code)

        if plan_id is not None:
            plan = self._plan_store.get(plan_id)
            if plan is not None and plan.state == "issued":
                try:
                    self._plan_store.claim(
                        plan_id,
                        page_path=page_path,
                        base_hash=base_hash or "",
                        intent_hash=intent_hash or "",
                        operation_id=operation.operation_id,
                    )
                except UpdatePlanError as exc:
                    self._mark_precommit_failure(operation.operation_id, exc.code)
                    return MutationResult(
                        ok=False,
                        state="failed_precommit",
                        code=exc.code,
                        operation_id=operation.operation_id,
                    )

        projection = self.commit_with_projections(
            operation.operation_id,
            text,
            expected_hash=expected_hash,
            fault=fault,
        )
        if not projection.get("ok"):
            return self._mutation_result(projection, operation)

        if plan_id is not None:
            try:
                self._plan_store.consume(
                    plan_id,
                    operation_id=operation.operation_id,
                    committed_hash=str(projection.get("page_hash") or intended_hash),
                )
            except UpdatePlanError:
                try:
                    self._store.set_operation_state(operation.operation_id, "repair_pending", error_code="plan_consume_pending")
                except PageOperationError:
                    pass
                return self._mutation_result(
                    {
                        "ok": True,
                        "state": "repair_pending",
                        "code": "plan_consume_pending",
                        "operation_id": operation.operation_id,
                        "page_hash": projection.get("page_hash") or intended_hash,
                        "repair_action": "repair_page_operation",
                    },
                    operation,
                )
        return self._mutation_result(projection, operation)

    def _mutation_result(
        self,
        result: Mapping[str, object],
        operation: PageOperation | None = None,
    ) -> MutationResult:
        operation_id = str(result["operation_id"]) if result.get("operation_id") is not None else None
        current = operation
        if operation_id is not None:
            current = self._store.get_operation(operation_id) or operation
        stages: dict[str, dict[str, object]] = {}
        if current is not None:
            stages = {
                name: self._store.safe_stage_record(record)
                for name, record in current.stages.items()
            }
        return MutationResult.from_mapping(result, stages=stages)

    def _classify_commit_failure(self, operation: PageOperation, target: Path) -> dict[str, object]:
        classification = self._classify_disk(operation, target)
        if classification == "intended":
            try:
                self._store.set_operation_state(operation.operation_id, "page_committed")
            except Exception:
                pass
            return {"ok": True, "state": "repair_pending", "operation_id": operation.operation_id, "page_hash": operation.intended_hash, "code": "projection_repair_required", "repair_action": "repair_page_operation"}
        if classification == "base":
            self._mark_precommit_failure(operation.operation_id, "write_failed_precommit")
            return {"ok": False, "code": "write_failed_precommit", "state": "failed_precommit", "operation_id": operation.operation_id}
        try:
            self._store.set_operation_state(operation.operation_id, "conflict", error_code="operation_conflict")
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
            self._store.set_operation_state(operation_id, "failed_precommit", error_code=code)
        except Exception:
            pass

    def _target(self, page_path: str, *, allow_raw_source: bool = False) -> Path:
        if allow_raw_source:
            normalized = normalize_vault_relative(page_path)
            relative = Path(*normalized.split("/"))
        else:
            try:
                relative = validate_wiki_page_path(page_path, allow_navigation_index=False)
            except WikiPathError as exc:
                raise PageMutationError(exc.code) from exc
        target = (self.root / relative).resolve()
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


def _formal_page_profile(
    coordinator: PageMutationCoordinator,
    operation: PageOperation,
    target: Path,
    fault: FaultBarrier | None,
) -> dict[str, Projection]:
    return coordinator._formal_page_projections(operation, target, fault)


def _chat_source_profile(
    coordinator: PageMutationCoordinator,
    operation: PageOperation,
    target: Path,
    fault: FaultBarrier | None,
) -> dict[str, Projection]:
    return coordinator._chat_source_projections(operation, target, fault)


_DEFAULT_PROFILES: dict[str, ProjectionProfile] = {
    "formal_page": ProjectionProfile("formal_page", _formal_page_profile),
    "create": ProjectionProfile("formal_page", _formal_page_profile),
    "update": ProjectionProfile("formal_page", _formal_page_profile),
    "chat_source": ProjectionProfile("chat_source", _chat_source_profile),
}


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


def _plan_is_expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.now(UTC)
    except ValueError:
        return True


__all__ = [
    "MutationResult",
    "PageMutationCoordinator",
    "PageMutationError",
    "Projection",
    "ProjectionProfile",
    "fault_barrier",
]
