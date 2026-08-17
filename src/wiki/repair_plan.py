"""Shared owner for CLI/admin maintenance plans.

This module owns the administrative ``Repair Plan`` skeleton: plan JSON
persistence, plan-id validation, idempotent audit lookup, page CAS checks,
per-page execution, compensating rollback, and stable audit/warning output.
Domain services provide only classification and page action hooks.

The three plan families deliberately remain separate:

* ``Update Plan`` is the short-lived, single-page structural edit permit owned
  by :mod:`wiki.page_mutation` and backed by the page-state journal.
* ``Repair Plan`` is a CLI/admin batch permit owned here and stored as
  vault-local JSON plus audit JSON.
* ``Archive Plan`` is an immutable-bundle lifecycle permit owned by
  :mod:`archive.archive_service` and backed by the archive SQLite journal.

The latter two must not share storage, reducers, operation journals, or
rollback semantics.  In particular, this owner never constructs a
``PageMutationCoordinator`` and never reads the archive plan tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from wiki.atomic_file import atomic_write_text, sha256_file
from wiki.wiki_paths import ADMIN_PLANS_DIR, WikiPathError, admin_wiki_page_file


_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


class RepairPlanError(ValueError):
    """Stable failure at the shared maintenance-plan boundary."""

    def __init__(self, code: str, message: str = "repair plan could not be completed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class RepairPageContext:
    """One page's bounded execution state passed to a domain hook.

    The context contains the original bytes only in memory during apply and
    rollback.  The owner never serializes ``original`` or arbitrary hook
    metadata into a plan or audit record.
    """

    entry: Mapping[str, object]
    page_path: str
    source: Path
    original: bytes
    expected_page_hash: str
    state: object | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    result: dict[str, object] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    apply_started: bool = False
    wrote: bool = False

    @property
    def actual_page_hash(self) -> str:
        return hashlib.sha256(self.original).hexdigest()

    def add_warning(self, warning: Mapping[str, object]) -> None:
        """Append only a stable, redacted warning shape supplied by a hook."""

        page_path = warning.get("page_path", self.page_path)
        stage = warning.get("stage", "repair")
        code = warning.get("code", "repair_warning")
        message = warning.get("message", "repair projection warning")
        self.warnings.append(
            {
                "page_path": str(page_path),
                "stage": _safe_code(stage, "repair"),
                "code": _safe_code(code, "repair_warning"),
                "message": _safe_warning_message(message),
            }
        )


PagePrepare = Callable[[RepairPageContext], object | None]
PageApply = Callable[[RepairPageContext], Mapping[str, object] | None]
PageRollback = Callable[[RepairPageContext], None]
PlanPreflight = Callable[[list[Mapping[str, object]]], Mapping[str, object] | None]


@dataclass(frozen=True)
class RepairPlanHooks:
    """Domain-specific page operations injected into :class:`RepairPlanOwner`."""

    apply: PageApply
    rollback: PageRollback
    prepare: PagePrepare | None = None


def validate_plan_id(value: object) -> str:
    """Validate and return a lowercase hexadecimal maintenance-plan ID."""

    if not isinstance(value, str) or _PLAN_ID.fullmatch(value) is None:
        raise RepairPlanError("invalid_plan_id")
    return value


def safe_file_hash(path: Path) -> str | None:
    """Return a page hash for a plan entry without exposing read exceptions."""

    try:
        return sha256_file(path)
    except OSError:
        return None


def string_map(value: object) -> dict[str, str]:
    """Normalize a JSON mapping to string keys and values."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def stable_error_code(exc: BaseException, fallback: str = "repair_plan_failed") -> str:
    """Map an internal exception to a safe, stable code."""

    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else fallback


def projection_warning(page_path: str, result_or_error: object) -> dict[str, str]:
    """Build the shared short warning for a failed derived projection."""

    raw_code = getattr(result_or_error, "code", None)
    if isinstance(result_or_error, Mapping):
        raw_code = result_or_error.get("code")
    return {
        "page_path": page_path,
        "stage": "retrieval",
        "code": _safe_code(raw_code, "retrieval_projection_failed"),
        "message": "retrieval projection was not refreshed",
    }


def iter_admin_page_files(vault_root: str | Path) -> tuple[Path, ...]:
    """Enumerate non-archive Wiki Markdown files through the admin path owner."""

    root = Path(vault_root).expanduser().resolve()
    wiki_root = root / "wiki"
    if not wiki_root.is_dir():
        return ()
    pages: list[Path] = []
    for path in sorted(wiki_root.rglob("*.md")):
        if not path.is_file() or "archives" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            pages.append(admin_wiki_page_file(root, relative))
        except WikiPathError:
            continue
    return tuple(pages)


class RepairPlanOwner:
    """Own one kind of JSON-backed administrative maintenance plan."""

    def __init__(
        self,
        vault_root: str | Path,
        *,
        kind: str,
        plan_prefix: str,
        audit_dir: str | Path,
        error_type: type[ValueError] = RepairPlanError,
    ) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.kind = kind
        self.plan_prefix = plan_prefix
        self.audit_dir = Path(audit_dir)
        self.error_type = error_type

    def page_file(self, value: str, *, allow_missing: bool = False) -> Path:
        """Resolve an admin page without applying ordinary Wiki writer policy."""

        try:
            return admin_wiki_page_file(self.root, value, allow_missing=allow_missing)
        except WikiPathError as exc:
            raise self._error(exc.code) from exc

    def create_plan(self, payload: Mapping[str, object], *, plan_id: str | None = None) -> dict[str, object]:
        """Persist and return a plan with the shared envelope fields."""

        candidate = plan_id or uuid4().hex
        try:
            candidate = validate_plan_id(candidate)
        except RepairPlanError as exc:
            raise self._error(exc.code) from exc
        if not isinstance(payload, Mapping):
            raise self._error("plan_invalid")
        if payload.get("kind") not in (None, self.kind):
            raise self._error("plan_invalid")
        plan: dict[str, object] = {
            "schema_version": 1,
            "kind": self.kind,
            "plan_id": candidate,
            "created_at": datetime.now(UTC).isoformat(),
            "dry_run": True,
            **dict(payload),
        }
        plan["kind"] = self.kind
        plan["plan_id"] = candidate
        try:
            atomic_write_text(self.plan_path(candidate), json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            raise self._error("plan_write_failed") from exc
        return plan

    def plan_response(self, plan: Mapping[str, object]) -> dict[str, object]:
        """Project the historical plan response without exposing extra envelope fields."""

        return {
            "ok": True,
            "kind": plan.get("kind", self.kind),
            "plan_id": plan.get("plan_id"),
            "dry_run": True,
            "summary": plan.get("summary", {}),
            "entries": plan.get("entries", []),
        }

    def read_plan(self, plan_id: str) -> dict[str, object]:
        """Load and validate a plan belonging to this owner."""

        try:
            validated = validate_plan_id(plan_id)
        except RepairPlanError as exc:
            raise self._error(exc.code) from exc
        path = self.plan_path(validated)
        if not path.is_file():
            raise self._error("plan_not_found")
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise self._error("plan_invalid") from exc
        if not isinstance(plan, dict) or plan.get("kind") != self.kind or plan.get("plan_id") != validated:
            raise self._error("plan_invalid")
        return plan

    def audit_path(self, plan_id: str) -> Path:
        validated = validate_plan_id(plan_id)
        return self.root / self.audit_dir / f"{validated}.audit.json"

    def read_audit(self, plan_id: str) -> dict[str, object]:
        """Read a prior audit, treating a missing/corrupt audit as not applied."""

        path = self.audit_path(plan_id)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def already_applied(self, plan_id: str) -> bool:
        """Return whether the plan has a durable successful audit."""

        return self.read_audit(plan_id).get("state") == "applied"

    def apply(
        self,
        plan_id: str,
        *,
        hooks: RepairPlanHooks,
        entries: Iterable[Mapping[str, object]] | None = None,
        plan: Mapping[str, object] | None = None,
        preflight: PlanPreflight | None = None,
        cas_code: str = "repair_plan_cas_mismatch",
        rollback_code: str = "repair_plan_rolled_back",
        rollback_failed_code: str = "repair_plan_rollback_failed",
        response_fields: Mapping[str, object] | None = None,
        audit_fields: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Run the shared idempotent CAS/execute/rollback/audit skeleton.

        ``hooks.apply`` owns the domain rewrite and projection refresh.  The
        owner owns the page snapshot/CAS gate, calls hooks for every page,
        invokes compensating rollback in reverse order, and never serializes
        page bytes or raw exception text.
        """

        loaded = dict(plan) if isinstance(plan, Mapping) else self.read_plan(plan_id)
        if loaded.get("kind") != self.kind or loaded.get("plan_id") != plan_id:
            raise self._error("plan_invalid")
        if self.already_applied(plan_id):
            return {"ok": True, "already_applied": True, "plan_id": plan_id, "audit_state": "applied"}

        raw_entries = list(entries if entries is not None else loaded.get("entries", []))
        normalized_entries = [entry for entry in raw_entries if isinstance(entry, Mapping)]
        if preflight is not None:
            try:
                failure = preflight(normalized_entries)
            except Exception as exc:
                return {
                    "ok": False,
                    "code": stable_error_code(exc),
                    "plan_id": plan_id,
                    "writes": 0,
                }
            if failure is not None and failure.get("ok") is False:
                return {**dict(failure), "plan_id": plan_id, "writes": 0}

        contexts: list[RepairPageContext] = []
        try:
            # Snapshot every page before invoking any writer.  A later CAS
            # mismatch therefore cannot leave an earlier page partially changed.
            for entry in normalized_entries:
                page_path = str(entry.get("page_path", ""))
                source = self.page_file(page_path)
                original = source.read_bytes()
                expected = str(entry.get("expected_page_hash", ""))
                actual = hashlib.sha256(original).hexdigest()
                if actual != expected:
                    return {
                        "ok": False,
                        "code": cas_code,
                        "plan_id": plan_id,
                        "page_path": page_path,
                        "expected_page_hash": expected,
                        "actual_page_hash": actual,
                        "writes": 0,
                    }
                context = RepairPageContext(entry, page_path, source, original, expected)
                if hooks.prepare is not None:
                    context.state = hooks.prepare(context)
                contexts.append(context)

            for context in contexts:
                context.apply_started = True
                result = hooks.apply(context)
                if isinstance(result, Mapping):
                    context.result = dict(result)

            warnings = [warning for context in contexts for warning in context.warnings]
            audit = self._audit_payload(
                plan_id,
                state="applied",
                rolled_back=False,
                entries=contexts,
                warnings=warnings,
                audit_fields=audit_fields,
            )
            self._write_audit(plan_id, audit)
            response: dict[str, object] = {
                "ok": True,
                "plan_id": plan_id,
                "applied": True,
                "writes": sum(1 for context in contexts if _context_wrote(context)),
                "entries": [dict(context.result) for context in contexts],
            }
            if response_fields:
                response.update(response_fields)
            if warnings:
                response["warnings"] = warnings
            return response
        except Exception as exc:
            failure_code = stable_error_code(exc)
            rollback_errors = self._rollback(contexts, hooks.rollback)
            warnings = [warning for context in contexts for warning in context.warnings]
            rollback_audit = self._audit_payload(
                plan_id,
                state="rolled_back",
                rolled_back=not rollback_errors,
                entries=contexts,
                warnings=warnings,
                rollback_errors=rollback_errors,
                error_code=failure_code,
                audit_fields=audit_fields,
                include_writes=False,
            )
            try:
                self._write_audit(plan_id, rollback_audit)
            except Exception:
                rollback_errors.append("audit_write_failed")
                warnings.append(_audit_warning())
            response = {
                "ok": False,
                "code": rollback_code if not rollback_errors else rollback_failed_code,
                "plan_id": plan_id,
                "rolled_back": not rollback_errors,
                "state": "rolled_back" if not rollback_errors else "repair_pending",
                "writes": 0,
                "error_code": failure_code,
                "rollback_errors": rollback_errors,
            }
            if response_fields:
                response.update(response_fields)
            if warnings:
                response["warnings"] = warnings
            return response

    def plan_path(self, plan_id: str) -> Path:
        return self.root / ADMIN_PLANS_DIR / f"{self.plan_prefix}{validate_plan_id(plan_id)}.json"

    def _write_audit(self, plan_id: str, audit: Mapping[str, object]) -> None:
        try:
            atomic_write_text(self.audit_path(plan_id), json.dumps(dict(audit), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            # The audit failure is itself part of the returned rollback state;
            # never silently discard it or leak the underlying exception text.
            raise RepairPlanError("audit_write_failed") from exc

    def _audit_payload(
        self,
        plan_id: str,
        *,
        state: str,
        rolled_back: bool,
        entries: Iterable[RepairPageContext],
        warnings: Iterable[Mapping[str, str]],
        rollback_errors: Iterable[str] = (),
        error_code: str | None = None,
        audit_fields: Mapping[str, object] | None = None,
        include_writes: bool = True,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": self.kind,
            "plan_id": plan_id,
            "state": state,
            "rolled_back": rolled_back,
            "entries": [dict(context.result) for context in entries],
            "warnings": [dict(warning) for warning in warnings],
        }
        if include_writes:
            payload["writes"] = sum(1 for context in entries if _context_wrote(context))
        if error_code is not None:
            payload["error_code"] = error_code
        errors = list(rollback_errors)
        if errors:
            payload["rollback_errors"] = errors
        if audit_fields:
            payload.update(audit_fields)
        return payload

    @staticmethod
    def _rollback(contexts: Iterable[RepairPageContext], rollback: PageRollback) -> list[str]:
        errors: list[str] = []
        for context in reversed(list(contexts)):
            if not context.apply_started and context.state is None:
                continue
            try:
                rollback(context)
            except Exception as exc:
                errors.append(stable_error_code(exc, "repair_plan_rollback_failed"))
        return errors

    def _error(self, code: str) -> ValueError:
        try:
            return self.error_type(code)
        except TypeError:
            return RepairPlanError(code)


# ``RepairPlanService`` is kept as a descriptive compatibility alias for
# callers that name the owner after the admin service boundary.
RepairPlanService = RepairPlanOwner


def _context_wrote(context: RepairPageContext) -> bool:
    if context.wrote:
        return True
    return bool(context.result.get("page_written") or context.result.get("renamed") or context.result.get("written"))


def _safe_code(value: object, fallback: str) -> str:
    candidate = str(value) if value is not None else ""
    return candidate if _SAFE_CODE.fullmatch(candidate) else fallback


def _safe_warning_message(value: object) -> str:
    message = str(value)
    if "\\" in message or "/" in message or "\n" in message or "\r" in message:
        return "repair projection warning"
    return message[:120] or "repair projection warning"


def _audit_warning() -> dict[str, str]:
    return {
        "page_path": "",
        "stage": "audit",
        "code": "audit_write_failed",
        "message": "repair audit was not written",
    }


__all__ = [
    "RepairPageContext",
    "RepairPlanError",
    "RepairPlanHooks",
    "RepairPlanOwner",
    "RepairPlanService",
    "iter_admin_page_files",
    "projection_warning",
    "safe_file_hash",
    "stable_error_code",
    "string_map",
    "validate_plan_id",
]
