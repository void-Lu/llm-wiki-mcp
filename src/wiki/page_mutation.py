"""Page fact commit and projection orchestration."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

import yaml

from wiki.atomic_file import AtomicFileError, atomic_write_text, current_fault, sha256_file
from wiki.page_mutation_adapters import (
    ChatSourceAdapter,
    FormalPageAdapter,
    Projection,
    ProjectionContext,
    WriteAdapter,
    WriteAdapterError,
    WriteAdapterRegistry,
    default_write_adapters,
)
from wiki.page_operation_store import PageOperation, PageOperationError, PageOperationStore, UpdatePlan, UpdatePlanError, plan_is_expired
from wiki.wiki_log import WikiLogStore
from wiki.wiki_paths import WikiPathError, translate_path_error


@dataclass(frozen=True)
class PlanIntent:
    """The domain inputs whose normalized shape is protected by an update plan."""

    body: str
    frontmatter: Mapping[str, Any]


def plan_intent_hash(page_path: str, base_hash: str, intent: PlanIntent) -> str:
    """Derive the stable plan intent without exposing a raw hash parameter to writers."""

    dumped = yaml.safe_dump(dict(intent.frontmatter), sort_keys=True, allow_unicode=True)
    payload = "\0".join((page_path, base_hash, intent.body, dumped))
    return sha256(payload.encode()).hexdigest()


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
            already_applied=result.get("already_applied") is True,
            stages=dict(stages or {}),
        )

    def stage_result(self, stage: str) -> dict[str, object] | None:
        """Return one safe, persisted stage result without exposing store records."""

        record = self.stages.get(stage)
        if not isinstance(record, Mapping):
            return None
        value = record.get("result")
        return dict(value) if isinstance(value, Mapping) else None

    def dependency_projection(self) -> dict[str, object]:
        """Explain the formal dependencies projection from the safe stage view."""

        stage = self.stages.get("dependencies")
        if not isinstance(stage, Mapping):
            return {"ok": True, "state": "ready"}
        result = stage.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        if stage.get("state") == "succeeded":
            return {"ok": True, "state": "ready"}
        state = stage.get("state")
        if isinstance(state, str) and state:
            code = stage.get("code")
            return {
                "ok": False,
                "state": state,
                "code": str(code) if isinstance(code, str) and code else f"dependencies_{state}",
            }
        return {"ok": True, "state": "ready"}

    def retrieval_index(self) -> dict[str, object] | None:
        """Return the safe retrieval projection, preserving the missing shape."""

        return self.stage_result("retrieval")

    def index_response(self) -> dict[str, Any]:
        """Project chat retrieval state into the stable chat response shape."""

        stage = self.stages.get("retrieval", {})
        if not isinstance(stage, Mapping):
            stage = {}
        stage_result = self.stage_result("retrieval") or {}
        if stage.get("state") == "succeeded":
            retrieval_index = dict(stage_result)
            indexed = retrieval_index.get("state") not in {"rebuild_required", "not_indexed"} and retrieval_index.get("ok") is True
            return {
                "ok": indexed,
                "generation": {"enabled": False, "reason": "raw_only"},
                "retrieval_index": retrieval_index,
            }
        return {
            "ok": False,
            "generation": {"enabled": False, "reason": "raw_only"},
            "retrieval_index": {
                "ok": False,
                "state": stage.get("state", "pending"),
                "code": stage.get("code") or "retrieval_pending",
            },
        }

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


def safe_stages_of(operation: PageOperation) -> dict[str, dict[str, object]]:
    """返回 page-operation store 所拥有的有界阶段视图。"""

    return {name: PageOperationStore.safe_stage_record(record) for name, record in operation.stages.items()}


class PageMutationError(ValueError):
    """Stable failure from the page mutation coordinator."""

    def __init__(self, code: str, message: str = "page mutation could not be completed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlanResolution:
    """One durable plan lookup result for the current orchestration call."""

    operation: PageOperation | None = None
    result: MutationResult | None = None
    intent_hash: str | None = None
    base_hash: str | None = None
    consumed_plan: bool = False
    consumed_base_applied: bool = False


class PlanLifecycle:
    """Own the durable update-plan transitions without retaining session state."""

    def __init__(self, store: PageOperationStore):
        self._store = store

    def issue(self, page_path: str, base_hash: str, intent: PlanIntent, *, ttl_seconds: int = 300) -> UpdatePlan:
        """Issue a plan while keeping its intent serialization inside the lifecycle owner."""

        return self._store.issue_plan(page_path, base_hash, plan_intent_hash(page_path, base_hash, intent), ttl_seconds=ttl_seconds)

    def resolve(
        self,
        plan_id: str | None,
        *,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
        intent: PlanIntent | None,
    ) -> PlanResolution:
        candidate_intent = plan_intent_hash(page_path, base_hash, intent) if base_hash and intent is not None else None
        if plan_id is None:
            return PlanResolution(intent_hash=candidate_intent, base_hash=base_hash)

        plan = self._store.get_plan(plan_id)
        if plan is None:
            return PlanResolution(result=MutationResult(ok=False, code="plan_unknown"), intent_hash=candidate_intent, base_hash=base_hash)
        if plan.state == "consumed":
            consumed_intent = plan_intent_hash(plan.page_path, plan.base_hash, intent) if intent is not None else None
            if consumed_intent != plan.intent_hash:
                return PlanResolution(
                    result=MutationResult(ok=False, code="plan_intent_drift"),
                    intent_hash=consumed_intent,
                    base_hash=plan.base_hash,
                    consumed_plan=True,
                )
            operation = self._store.get_operation(plan.operation_id) if plan.operation_id else None
            return PlanResolution(
                operation=operation,
                result=MutationResult(
                    ok=True,
                    state="already_applied",
                    operation_id=plan.operation_id,
                    page_hash=plan.consumed_hash,
                    repair_action=(
                        "repair_page_operation"
                        if operation is not None and operation.state != "completed"
                        else None
                    ),
                    already_applied=True,
                    stages=safe_stages_of(operation) if operation is not None else {},
                ),
                intent_hash=consumed_intent,
                base_hash=plan.base_hash,
                consumed_plan=True,
                consumed_base_applied=True,
            )
        if plan_is_expired(plan.expires_at):
            return PlanResolution(result=MutationResult(ok=False, code="plan_expired"), intent_hash=candidate_intent, base_hash=base_hash)
        if plan.state == "expired" or plan.state not in {"issued", "claimed"}:
            return PlanResolution(
                result=MutationResult(ok=False, code="plan_expired" if plan.state == "expired" else "plan_unknown"),
                intent_hash=candidate_intent,
                base_hash=base_hash,
            )
        if plan.state == "claimed":
            if not plan.operation_id:
                return PlanResolution(result=MutationResult(ok=False, code="plan_claimed"), intent_hash=candidate_intent, base_hash=base_hash)
            operation = self._store.get_operation(plan.operation_id)
            if operation is None:
                return PlanResolution(
                    result=MutationResult(ok=False, code="plan_claimed", operation_id=plan.operation_id),
                    intent_hash=candidate_intent,
                    base_hash=base_hash,
                )
            if operation.intended_hash != intended_hash:
                return PlanResolution(
                    result=MutationResult(ok=False, code="plan_intent_drift", operation_id=operation.operation_id),
                    intent_hash=candidate_intent,
                    base_hash=base_hash,
                )
            if operation.state == "prepared":
                return PlanResolution(
                    result=MutationResult(ok=False, code="plan_claimed", operation_id=operation.operation_id),
                    intent_hash=candidate_intent,
                    base_hash=base_hash,
                )
            return PlanResolution(operation=operation, intent_hash=candidate_intent, base_hash=base_hash)
        return PlanResolution(intent_hash=candidate_intent, base_hash=base_hash)

    def claim(
        self,
        plan_id: str | None,
        operation: PageOperation,
        page_path: str,
        base_hash: str,
        intent_hash: str,
    ) -> MutationResult | None:
        if plan_id is None:
            return None
        try:
            self._store.claim_plan(
                plan_id,
                page_path=page_path,
                base_hash=base_hash,
                intent_hash=intent_hash,
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
        return None

    def consume(
        self,
        plan_id: str | None,
        operation_id: str,
        committed_hash: str,
        intended_hash: str,
    ) -> MutationResult | None:
        if plan_id is None:
            return None
        page_hash = committed_hash or intended_hash
        try:
            self._store.consume_plan(
                plan_id,
                operation_id=operation_id,
                committed_hash=page_hash,
            )
        except UpdatePlanError:
            try:
                self._store.set_operation_state(operation_id, "repair_pending", error_code="plan_consume_pending")
            except PageOperationError:
                pass
            operation = self._store.get_operation(operation_id)
            return MutationResult(
                ok=True,
                state="repair_pending",
                code="plan_consume_pending",
                operation_id=operation_id,
                page_hash=page_hash,
                repair_action="repair_page_operation",
                stages=safe_stages_of(operation) if operation is not None else {},
            )
        return None

    def _mark_precommit_failure(self, operation_id: str, code: str) -> None:
        try:
            self._store.set_operation_state(operation_id, "failed_precommit", error_code=code)
        except PageOperationError:
            pass


class PageMutationCoordinator:
    """Serialize in-process page writers and persist recoverable stages."""

    _locks: dict[tuple[str, str], threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(
        self,
        vault_root: str | Path,
        *,
        store: PageOperationStore | None = None,
        adapters: Iterable[WriteAdapter] | None = None,
    ):
        self.root = Path(vault_root).expanduser().resolve()
        self._store = store or PageOperationStore(self.root)
        self._log_store = WikiLogStore(self.root, operation_store=self._store)
        self._plan_lifecycle = PlanLifecycle(self._store)
        self._adapters = WriteAdapterRegistry(adapters if adapters is not None else default_write_adapters())

    def issue_plan(
        self,
        *,
        page_path: str,
        base_hash: str,
        intent: PlanIntent,
        ttl_seconds: int = 300,
    ) -> UpdatePlan:
        """Issue one update plan through the lifecycle owner."""

        return self._plan_lifecycle.issue(page_path, base_hash, intent, ttl_seconds=ttl_seconds)

    def build_plan_intent(
        self,
        *,
        operation_kind: str,
        body: str,
        frontmatter: Mapping[str, Any],
    ) -> PlanIntent:
        """Build a plan intent through the registered kind adapter."""

        try:
            adapter = self._adapter_for_operation(operation_kind)
        except WriteAdapterError as exc:
            raise PageMutationError(exc.code) from exc
        return adapter.build_plan_intent(body, frontmatter)

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
    ) -> MutationResult:
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return MutationResult(ok=False, code="operation_not_found")
        adapter = self._adapter_for_operation(operation.operation_kind)
        target = self._target(operation.page_path, adapter=adapter)
        current_hash = _hash_if_exists(target)
        expected_base = operation.base_hash if expected_hash is None else expected_hash
        if current_hash != expected_base:
            self._mark_precommit_failure(operation_id, "expected_hash_mismatch")
            return self._mutation_result(
                {"ok": False, "code": "expected_hash_mismatch", "operation_id": operation_id, "state": "failed_precommit"},
                operation,
            )
        intended_hash = sha256(text.encode("utf-8")).hexdigest()
        if intended_hash != operation.intended_hash:
            self._mark_precommit_failure(operation_id, "operation_intent_drift")
            return self._mutation_result(
                {"ok": False, "code": "operation_intent_drift", "operation_id": operation_id, "state": "failed_precommit"},
                operation,
            )

        with self._page_lock(operation.page_path):
            current_hash = _hash_if_exists(target)
            if current_hash != expected_base:
                self._mark_precommit_failure(operation_id, "expected_hash_mismatch")
                return self._mutation_result(
                    {"ok": False, "code": "expected_hash_mismatch", "operation_id": operation_id, "state": "failed_precommit"},
                    operation,
                )
            try:
                atomic_write_text(target, text)
            except AtomicFileError:
                return self._classify_commit_failure(operation, target)
            try:
                self._invoke("journal_commit")
                committed = self._store.set_operation_state(operation_id, "page_committed")
            except Exception:
                classified = self._classify_disk(operation, target)
                if classified == "intended":
                    try:
                        self._store.set_operation_state(operation_id, "repair_pending", error_code="journal_commit_pending")
                    except Exception:
                        pass
                    return self._mutation_result(
                        {
                            "ok": True,
                            "state": "repair_pending",
                            "operation_id": operation_id,
                            "page_hash": operation.intended_hash,
                            "code": "journal_commit_pending",
                        },
                        operation,
                    )
                return self._classify_commit_failure(operation, target)
        return self._mutation_result(
            {
                "ok": True,
                "state": "page_committed",
                "operation_id": committed.operation_id,
                "page_hash": committed.intended_hash,
            },
            committed,
        )

    def run_projections(
        self,
        operation_id: str,
        projections: Mapping[str, Projection],
    ) -> MutationResult:
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return MutationResult(ok=False, code="operation_not_found")
        if operation.state == "completed":
            return self._mutation_result(
                {"ok": True, "state": "completed", "operation_id": operation_id, "already_applied": True},
                operation,
            )
        if operation.state not in {"page_committed", "repair_pending"}:
            return self._mutation_result(
                {"ok": False, "code": "operation_not_committed", "state": operation.state, "operation_id": operation_id},
                operation,
            )

        adapter = self._adapter_for_operation(operation.operation_kind)
        for stage in adapter.projection_stages():
            current = self._store.get_operation(operation_id)
            if current is None:
                return MutationResult(ok=False, code="operation_not_found")
            if current.stages.get(stage, {}).get("state") == "succeeded":
                continue
            try:
                self._store.record_stage(operation_id, stage, "running")
                self._invoke(f"projection:{stage}")
                callback = projections.get(stage)
                result = callback() if callback is not None else {"ok": True, "state": "not_configured"}
                result_dict = dict(result or {})
                if result_dict.get("ok") is False:
                    code = str(result_dict.get("code") or f"{stage}_failed")
                    self._store.record_stage(operation_id, stage, "failed", code=code, result=result_dict)
                    self._store.set_operation_state(operation_id, "repair_pending", error_code=code)
                    updated = self._store.get_operation(operation_id) or current
                    return self._mutation_result(
                        {
                            "ok": True,
                            "state": "repair_pending",
                            "operation_id": operation_id,
                            "failed_stage": stage,
                            "code": "projection_repair_required",
                            "repair_action": "repair_page_operation",
                        },
                        updated,
                    )
                self._store.record_stage(operation_id, stage, "succeeded", result=result_dict)
            except Exception as exc:
                code = str(getattr(exc, "code", None) or f"{stage}_failed")
                try:
                    self._store.record_stage(operation_id, stage, "failed", code=code)
                    self._store.set_operation_state(operation_id, "repair_pending", error_code=code)
                except Exception:
                    pass
                updated = self._store.get_operation(operation_id) or current
                return self._mutation_result(
                    {
                        "ok": True,
                        "state": "repair_pending",
                        "operation_id": operation_id,
                        "failed_stage": stage,
                        "code": "projection_repair_required",
                        "repair_action": "repair_page_operation",
                    },
                    updated,
                )
        self._store.set_operation_state(operation_id, "completed")
        completed = self._store.get_operation(operation_id) or operation
        return self._mutation_result({"ok": True, "state": "completed", "operation_id": operation_id}, completed)

    def recover(self, operation_id: str) -> MutationResult:
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return MutationResult(ok=False, code="operation_not_found")
        if operation.state == "completed":
            return self._mutation_result({"ok": True, "state": "completed", "operation_id": operation_id}, operation)
        classification = self._classify_disk(
            operation,
            self._target(operation.page_path, adapter=self._adapter_for_operation(operation.operation_kind)),
        )
        if classification == "intended":
            try:
                self._store.set_operation_state(operation_id, "page_committed")
            except PageOperationError:
                pass
            return self._mutation_result({"ok": True, "state": "repair_pending", "operation_id": operation_id}, operation)
        if classification == "base":
            self._mark_precommit_failure(operation_id, "write_failed_precommit")
            return self._mutation_result(
                {"ok": False, "state": "failed_precommit", "operation_id": operation_id, "code": "write_failed_precommit"},
                operation,
            )
        try:
            self._store.set_operation_state(operation_id, "conflict", error_code="operation_conflict")
        except PageOperationError:
            pass
        return self._mutation_result(
            {"ok": False, "state": "conflict", "operation_id": operation_id, "code": "operation_conflict"},
            operation,
        )

    def repair(self, operation_id: str, projections: Mapping[str, Projection]) -> MutationResult:
        recovered = self.recover(operation_id)
        if recovered.state == "completed":
            return recovered
        if recovered.state != "repair_pending":
            return recovered
        return self.run_projections(operation_id, projections)

    def project_existing(
        self,
        page_path_or_operation_id: str,
        content_hash: str | None = None,
        *,
        projections: Mapping[str, Projection] | None = None,
    ) -> MutationResult:
        """Project durable bytes without rewriting their file.

        The public ``(page_path, content_hash)`` form is the first-class
        existing-fact entry point.  The operation-id form remains as a narrow
        compatibility seam for repair callers and older integrations.
        """

        if content_hash is None:
            return self._project_existing_operation(page_path_or_operation_id, projections=projections)
        return self._project_existing_path(page_path_or_operation_id, content_hash)

    def _project_existing_operation(
        self,
        operation_id: str,
        *,
        projections: Mapping[str, Projection] | None = None,
    ) -> MutationResult:
        operation = self._store.get_operation(operation_id)
        if operation is None:
            return MutationResult(ok=False, code="operation_not_found")
        selected = projections if projections is not None else self.projections_for(operation)
        return self.repair(operation_id, selected)

    def _project_existing_path(self, page_path: str, content_hash: str) -> MutationResult:
        try:
            adapter = self._adapter_for_path(page_path)
            target = self._target(page_path, adapter=adapter)
        except (PageMutationError, WriteAdapterError) as exc:
            return MutationResult(ok=False, code=str(getattr(exc, "code", "path_not_allowed")))
        actual_hash = _hash_if_exists(target)
        if actual_hash is None:
            return MutationResult(ok=False, code="page_not_found")
        if actual_hash != content_hash:
            return MutationResult(ok=False, code="expected_hash_mismatch", page_hash=actual_hash)
        request_key = adapter.existing_request_key(self.root, page_path, content_hash, target)
        existing = self._store.get_operation_by_request_key(request_key)
        if existing is not None:
            return replace(self._project_existing_operation(existing.operation_id), already_applied=True)
        try:
            operation = self.prepare(
                request_key=request_key,
                operation_kind=adapter.existing_operation_kind(),
                page_path=page_path,
                base_hash=content_hash,
                intended_hash=content_hash,
            )
        except PageOperationError as exc:
            return MutationResult(ok=False, code=exc.code)
        result = self._project_existing_operation(operation.operation_id)
        return result

    def projections_for(
        self,
        operation: PageOperation,
    ) -> dict[str, Projection]:
        """Build the projection set selected by the registered adapter."""

        adapter = self._adapter_for_operation(operation.operation_kind)
        target = self._target(operation.page_path, adapter=adapter)
        if not target.is_file():
            return {
                stage: (lambda: {"ok": False, "code": "page_not_found"})
                for stage in adapter.projection_stages()
            }
        return adapter.build_projections(ProjectionContext(self.root, self._log_store), operation, target)

    def _commit_with_projections(
        self,
        operation_id: str,
        text: str,
        *,
        expected_hash: str | None = None,
    ) -> MutationResult:
        """Commit a prepared operation and run its standard projections."""

        operation = self._store.get_operation(operation_id)
        if operation is None:
            return MutationResult(ok=False, code="operation_not_found")
        if operation.state == "completed":
            return self._mutation_result(
                {
                    "ok": True,
                    "state": "completed",
                    "operation_id": operation.operation_id,
                    "page_hash": operation.intended_hash,
                    "already_applied": True,
                },
                operation,
            )
        if operation.state == "prepared":
            commit_result = self.commit(operation_id, text, expected_hash=expected_hash)
            if not commit_result.ok:
                return commit_result
        elif operation.state not in {"page_committed", "repair_pending"}:
            return self._mutation_result(
                {
                    "ok": False,
                    "code": "operation_not_committed",
                    "state": operation.state,
                    "operation_id": operation.operation_id,
                },
                operation,
            )
        projections = self.projections_for(operation)
        result = self.run_projections(operation_id, projections)
        if result.page_hash is None:
            return replace(result, page_hash=operation.intended_hash)
        return result

    def write_and_project(
        self,
        *,
        request_key: str | None = None,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        text: str,
        expected_hash: str | None = None,
        plan_id: str | None = None,
        plan_intent: PlanIntent | None = None,
    ) -> MutationResult:
        """Prepare, commit, project, and optionally consume one mutation plan.

        Request-key derivation belongs to the registered kind adapter.  An
        explicit key remains accepted for compatibility with lower-level
        callers and durable plan replay.
        """

        try:
            adapter = self._adapter_for_operation(operation_kind)
        except WriteAdapterError as exc:
            return MutationResult(ok=False, code=exc.code)
        intended_hash = sha256(text.encode("utf-8")).hexdigest()
        effective_request_key = adapter.request_key(
            operation_kind=operation_kind,
            page_path=page_path,
            base_hash=base_hash,
            intended_hash=intended_hash,
            text=text,
            plan_id=plan_id,
            explicit_request_key=request_key,
        )
        resolution = self._plan_lifecycle.resolve(
            plan_id,
            page_path=page_path,
            base_hash=base_hash,
            intended_hash=intended_hash,
            intent=plan_intent,
        )
        if not resolution.consumed_plan:
            if base_hash is not None and expected_hash is None:
                return MutationResult(ok=False, code="expected_hash_required")
            if base_hash is not None and expected_hash != base_hash:
                return MutationResult(ok=False, code="expected_hash_mismatch")
        if resolution.result is not None:
            return resolution.result
        operation = resolution.operation

        if adapter.replay_existing_requests and operation is None:
            existing = self._store.get_operation_by_request_key(effective_request_key)
            if existing is not None:
                existing_result = self._project_existing_operation(existing.operation_id)
                return replace(existing_result, already_applied=True)

        if operation is None:
            try:
                operation = self.prepare(
                    request_key=effective_request_key,
                    operation_kind=operation_kind,
                    page_path=page_path,
                    base_hash=base_hash,
                    intended_hash=intended_hash,
                )
            except PageOperationError as exc:
                return MutationResult(ok=False, code=exc.code)

        claim_failure = None
        if resolution.operation is None:
            claim_failure = self._plan_lifecycle.claim(
                plan_id,
                operation,
                page_path,
                base_hash or "",
                resolution.intent_hash or "",
            )
        if claim_failure is not None:
            return claim_failure

        projection = self._commit_with_projections(
            operation.operation_id,
            text,
            expected_hash=expected_hash,
        )
        if not projection.ok:
            return projection

        consume_failure = self._plan_lifecycle.consume(
            plan_id,
            operation.operation_id,
            str(projection.page_hash or intended_hash),
            intended_hash,
        )
        if consume_failure is not None:
            return consume_failure
        return projection

    def _mutation_result(
        self,
        result: Mapping[str, object],
        operation: PageOperation | None = None,
    ) -> MutationResult:
        operation_id = str(result["operation_id"]) if result.get("operation_id") is not None else None
        current = operation
        if operation_id is not None:
            current = self._store.get_operation(operation_id) or operation
        stages: dict[str, dict[str, object]] = safe_stages_of(current) if current is not None else {}
        return MutationResult.from_mapping(result, stages=stages)

    def _classify_commit_failure(self, operation: PageOperation, target: Path) -> MutationResult:
        classification = self._classify_disk(operation, target)
        if classification == "intended":
            try:
                self._store.set_operation_state(operation.operation_id, "page_committed")
            except Exception:
                pass
            return self._mutation_result(
                {
                    "ok": True,
                    "state": "repair_pending",
                    "operation_id": operation.operation_id,
                    "page_hash": operation.intended_hash,
                    "code": "projection_repair_required",
                    "repair_action": "repair_page_operation",
                },
                operation,
            )
        if classification == "base":
            self._mark_precommit_failure(operation.operation_id, "write_failed_precommit")
            return self._mutation_result(
                {"ok": False, "code": "write_failed_precommit", "state": "failed_precommit", "operation_id": operation.operation_id},
                operation,
            )
        try:
            self._store.set_operation_state(operation.operation_id, "conflict", error_code="operation_conflict")
        except Exception:
            pass
        return self._mutation_result(
            {"ok": False, "code": "operation_conflict", "state": "conflict", "operation_id": operation.operation_id},
            operation,
        )

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

    def _adapter_for_operation(self, operation_kind: str) -> WriteAdapter:
        try:
            return self._adapters.for_operation_kind(operation_kind)
        except WriteAdapterError:
            raise

    def _adapter_for_path(self, page_path: str) -> WriteAdapter:
        return self._adapters.for_path(page_path)

    def _target(self, page_path: str, *, adapter: WriteAdapter) -> Path:
        try:
            return adapter.target(self.root, page_path)
        except WikiPathError as exc:
            raise PageMutationError(translate_path_error(exc.code, "mutation")) from exc

    def _invoke(self, stage: str) -> None:
        current_fault()(stage)

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


__all__ = [
    "MutationResult",
    "ChatSourceAdapter",
    "FormalPageAdapter",
    "PlanIntent",
    "PlanLifecycle",
    "PlanResolution",
    "PageMutationCoordinator",
    "PageMutationError",
    "Projection",
    "plan_intent_hash",
    "safe_stages_of",
]
