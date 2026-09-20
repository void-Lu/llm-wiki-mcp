"""Low-frequency project-spec mirroring and shared-spec page management."""

from __future__ import annotations

from collections.abc import Mapping
import difflib
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from wiki.atomic_file import AtomicFileError, atomic_write_text, sha256_file
from wiki.page_mutation import DELETED_PAGE_HASH, PageMutationCoordinator
from wiki.page_policy import stamp_page_policy
from wiki.wiki_io import render_page, strip_leading_h1
from wiki.wiki_paths import (
    ADMIN_PLANS_DIR,
    WikiPathError,
    create_wiki_root,
    resolve_within_root,
    safe_segment,
    validate_wiki_page_path,
)


_SPEC_PREFIX = Path("wiki/projects")
_SHARED_PREFIX = Path("wiki/entities/shared-specs")
_APPLIES_TO_KEYS = frozenset({"languages", "platforms", "frameworks"})
_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")


class SpecReuseError(ValueError):
    """Stable validation or plan error for spec reuse operations."""

    def __init__(self, code: str, message: str = "spec reuse operation failed") -> None:
        super().__init__(message)
        self.code = code


def _validate_plan_id(value: object) -> str:
    if not isinstance(value, str) or _PLAN_ID.fullmatch(value) is None:
        raise SpecReuseError("invalid_plan_id")
    return value


class SpecPlanStore:
    """Persist a preview snapshot and page-to-operation map."""

    def __init__(self, vault_root: str | Path, *, kind: str, prefix: str) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.kind = kind
        self.prefix = prefix

    def path_for(self, plan_id: str) -> Path:
        return self.root / ADMIN_PLANS_DIR / f"{self.prefix}{_validate_plan_id(plan_id)}.json"

    def create(self, payload: Mapping[str, object]) -> dict[str, object]:
        plan_id = uuid4().hex
        plan = {"schema_version": 1, "kind": self.kind, "plan_id": plan_id, "dry_run": True, "operations": {}, **dict(payload)}
        plan["kind"] = self.kind
        plan["plan_id"] = plan_id
        try:
            atomic_write_text(self.path_for(plan_id), json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            raise SpecReuseError("plan_write_failed") from exc
        return plan

    def read(self, plan_id: str) -> dict[str, object]:
        path = self.path_for(plan_id)
        if not path.is_file():
            raise SpecReuseError("plan_not_found")
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpecReuseError("plan_invalid") from exc
        if not isinstance(plan, dict) or plan.get("kind") != self.kind or plan.get("plan_id") != plan_id:
            raise SpecReuseError("plan_invalid")
        return plan

    def persist(self, plan: Mapping[str, object]) -> None:
        try:
            atomic_write_text(self.path_for(str(plan["plan_id"])), json.dumps(dict(plan), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            raise SpecReuseError("plan_progress_persist_failed") from exc

    def discard(self, plan_id: str) -> bool:
        try:
            self.path_for(plan_id).unlink(missing_ok=True)
            return True
        except OSError:
            return False


def _utf8_text(path: Path, *, code: str) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SpecReuseError(code) from exc
    if data.startswith(b"\xef\xbb\xbf"):
        raise SpecReuseError("source_not_utf8")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecReuseError("source_not_utf8") from exc


def _origin_fingerprints(root: Path, origins: list[Mapping[str, str]]) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for origin in origins:
        path = str(origin["path"])
        _, target = _page_path(root, path)
        if not target.is_file():
            raise SpecReuseError("invalid_derived_from")
        fingerprints[path] = sha256_file(target)
    return fingerprints


def _safe_tree(root: Path, label: str) -> dict[str, str]:
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise SpecReuseError(f"{label}_tree_invalid")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink():
            raise SpecReuseError(f"{label}_symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if Path(relative).suffix.casefold() != ".md":
            raise SpecReuseError(f"{label}_file_invalid")
        try:
            for part in Path(relative).parts:
                safe_segment(part)
            _utf8_text(path, code=f"{label}_not_utf8")
            result[relative] = sha256_file(path)
        except SpecReuseError:
            raise
        except (OSError, WikiPathError) as exc:
            raise SpecReuseError(f"{label}_read_failed") from exc
    return result


def _fingerprint(files: Mapping[str, str]) -> str:
    value = "\n".join(f"{path}\0{digest}" for path, digest in sorted(files.items(), key=lambda item: item[0].casefold()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_project(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecReuseError("missing_project")
    try:
        return safe_segment(value.strip())
    except WikiPathError as exc:
        raise SpecReuseError(exc.code) from exc


def _page_path(root: Path, value: object) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise SpecReuseError("missing_page_path")
    try:
        relative = validate_wiki_page_path(value)
        target = resolve_within_root(root, relative)
    except WikiPathError as exc:
        raise SpecReuseError(exc.code) from exc
    return relative.as_posix(), target


def _shared_page(root: Path, value: object) -> tuple[str, Path]:
    relative, target = _page_path(root, value)
    parts = Path(relative).parts
    if len(parts) != 4 or Path(*parts[:3]) != _SHARED_PREFIX:
        raise SpecReuseError("shared_spec_path_required")
    return relative, target


def _spec_page(root: Path, project: str, relative: str) -> str:
    try:
        page = validate_wiki_page_path(_SPEC_PREFIX / project / "specs" / relative)
    except WikiPathError as exc:
        raise SpecReuseError(exc.code) from exc
    return page.as_posix()


def _plan_response(store: SpecPlanStore, plan: Mapping[str, object], summary: Mapping[str, object], entries: list[Mapping[str, object]]) -> dict[str, object]:
    response: dict[str, object] = {
        "ok": True,
        "kind": plan.get("kind", store.kind),
        "plan_id": plan.get("plan_id"),
        "dry_run": True,
        "summary": dict(summary),
        "entries": [dict(entry) for entry in entries],
    }
    for key in ("source_fingerprint", "target_fingerprint", "current_hash", "desired_hash", "origin_hashes"):
        if key in plan:
            response[key] = plan[key]
    return response


def _completed_pages(plan: Mapping[str, object]) -> list[str]:
    return [str(value) for value in plan.get("completed", []) if isinstance(value, str)]


def _operation_map(plan: Mapping[str, object]) -> dict[str, str]:
    raw = plan.get("operations", {})
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items() if str(key) and str(value)}


def _remember_operation(plan: dict[str, object], page_path: str, operation_id: object) -> None:
    if not isinstance(operation_id, str) or not operation_id:
        return
    operations = _operation_map(plan)
    operations[page_path] = operation_id
    plan["operations"] = operations


def _mutation_payload(mutation: Mapping[str, object], *, plan_id: str, page_path: str, completed: list[str], writes: int) -> dict[str, object]:
    payload = dict(mutation)
    payload.update({"plan_id": plan_id, "page_path": page_path, "failed_page": page_path, "completed": completed, "writes": writes})
    return payload


def _apply_page(
    coordinator: PageMutationCoordinator,
    plan: dict[str, object],
    *,
    page_path: str,
    action: str,
    text: str,
    current_hash: str | None,
    desired_hash: str | None,
) -> dict[str, object]:
    existing_id = _operation_map(plan).get(page_path)
    if existing_id:
        return coordinator.repair(existing_id).to_dict()
    if action == "delete":
        return coordinator.write_and_project(
            operation_kind="delete",
            page_path=page_path,
            base_hash=current_hash,
            text="",
            intended_hash=DELETED_PAGE_HASH,
            expected_hash=current_hash,
        ).to_dict()
    return coordinator.write_and_project(
        operation_kind="create" if current_hash is None else "update",
        page_path=page_path,
        base_hash=current_hash,
        text=text,
        intended_hash=desired_hash,
        expected_hash=current_hash,
    ).to_dict()


class SpecMirrorService:
    """Plan and apply a current `.trellis/spec` tree mirror."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.store = SpecPlanStore(self.root, kind="spec_mirror", prefix="spec-mirror-")

    def plan(self, source_root: str | Path, project: str) -> dict[str, object]:
        project_name = _safe_project(project)
        source = Path(source_root).expanduser().resolve()
        if not source.is_dir() or source.is_symlink():
            return {"ok": False, "code": "source_tree_invalid"}
        source_files = _safe_tree(source, "source")
        target_relative = Path("wiki/projects") / project_name / "specs"
        target = resolve_within_root(self.root, target_relative)
        target_files = _safe_tree(target, "target")
        entries: list[dict[str, object]] = []
        for relative in sorted(set(source_files) | set(target_files), key=str.casefold):
            source_hash = source_files.get(relative)
            target_hash = target_files.get(relative)
            action = "unchanged" if source_hash == target_hash else "create" if target_hash is None else "delete" if source_hash is None else "update"
            if action != "unchanged":
                entries.append(
                    {
                        "action": action,
                        "page_path": _spec_page(self.root, project_name, relative),
                        "relative": relative,
                        "source_relative": relative if source_hash is not None else None,
                        "source_hash": source_hash,
                        "target_hash": target_hash,
                    }
                )
        summary = {action: sum(1 for entry in entries if entry["action"] == action) for action in ("create", "update", "delete")}
        summary["unchanged"] = len(set(source_files) & set(target_files)) - summary["update"]
        plan = self.store.create(
            {
                "source_root": str(source),
                "project": project_name,
                "target_relative": target_relative.as_posix(),
                "source_files": source_files,
                "target_files": target_files,
                "source_fingerprint": _fingerprint(source_files),
                "target_fingerprint": _fingerprint(target_files),
                "completed": [],
                "operations": {},
                "entries": entries,
                "summary": summary,
            }
        )
        return _plan_response(self.store, plan, summary, entries)

    def apply(self, plan_id: str) -> dict[str, object]:
        try:
            plan = self.store.read(plan_id)
        except SpecReuseError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id, "writes": 0}
        source = Path(str(plan["source_root"])).expanduser().resolve()
        target = resolve_within_root(self.root, Path(str(plan["target_relative"])))
        try:
            source_files = _safe_tree(source, "source")
            if _fingerprint(source_files) != plan.get("source_fingerprint"):
                return {"ok": False, "code": "source_changed", "plan_id": plan_id, "writes": 0}
            target_files = _safe_tree(target, "target")
        except SpecReuseError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id, "writes": 0}
        planned_source = {str(key): str(value) for key, value in dict(plan.get("source_files", {})).items()}
        planned_target = {str(key): str(value) for key, value in dict(plan.get("target_files", {})).items()}
        completed = _completed_pages(plan)
        completed_set = set(completed)
        operations = _operation_map(plan)
        for relative in set(target_files) | set(planned_target) | set(planned_source):
            current = target_files.get(relative)
            allowed = {planned_target.get(relative)}
            page_path = _spec_page(self.root, str(plan["project"]), relative)
            if relative in completed_set or page_path in operations:
                allowed.add(planned_source.get(relative))
                if relative not in planned_source:
                    allowed.add(None)
            if current not in allowed:
                return {"ok": False, "code": "target_changed", "plan_id": plan_id, "writes": 0, "completed": completed}
        create_wiki_root(self.root)
        coordinator = PageMutationCoordinator(self.root)
        writes = 0
        current_page: str | None = None
        try:
            for entry in [item for item in plan.get("entries", []) if isinstance(item, Mapping)]:
                page_path = str(entry["page_path"])
                current_page = page_path
                relative = str(entry["relative"])
                action = str(entry["action"])
                if page_path in completed_set and page_path not in operations:
                    continue
                text = "" if action == "delete" else _utf8_text(source / relative, code="source_not_utf8")
                mutation = _apply_page(
                    coordinator,
                    plan,
                    page_path=page_path,
                    action=action,
                    text=text,
                    current_hash=planned_target.get(relative),
                    desired_hash=planned_source.get(relative),
                )
                _remember_operation(plan, page_path, mutation.get("operation_id"))
                if mutation.get("state") == "repair_pending" or mutation.get("code") == "projection_repair_required":
                    if page_path not in completed_set:
                        completed.append(page_path)
                    plan["completed"] = completed
                    try:
                        self.store.persist(plan)
                    except SpecReuseError as persist_exc:
                        return {"ok": False, "code": persist_exc.code, "state": "repair_pending", "plan_id": plan_id, "failed_page": page_path, "completed": completed, "writes": writes, "failed_stage": mutation.get("failed_stage")}
                    return _mutation_payload(mutation, plan_id=plan_id, page_path=page_path, completed=completed, writes=writes)
                if mutation.get("ok") is not True:
                    plan["completed"] = completed
                    try:
                        self.store.persist(plan)
                    except SpecReuseError:
                        pass
                    return _mutation_payload(mutation, plan_id=plan_id, page_path=page_path, completed=completed, writes=writes)
                if page_path not in completed_set:
                    completed.append(page_path)
                    completed_set.add(page_path)
                    writes += 1
            plan["completed"] = completed
        except (OSError, AtomicFileError, SpecReuseError) as exc:
            plan["completed"] = completed
            try:
                self.store.persist(plan)
            except SpecReuseError:
                return {"ok": False, "code": "plan_progress_persist_failed", "state": "repair_pending", "plan_id": plan_id, "failed_page": current_page, "completed": completed, "writes": writes}
            return {"ok": False, "code": getattr(exc, "code", "spec_write_failed"), "plan_id": plan_id, "failed_page": current_page, "completed": completed, "writes": writes}
        if not self.store.discard(plan_id):
            return {"ok": False, "code": "plan_discard_failed", "state": "repair_pending", "plan_id": plan_id, "completed": completed, "writes": writes}
        return {"ok": True, "plan_id": plan_id, "applied": True, "writes": writes, "completed": completed}

    def discard(self, plan_id: str) -> dict[str, object]:
        try:
            self.store.read(plan_id)
        except SpecReuseError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id}
        if not self.store.discard(plan_id):
            return {"ok": False, "code": "plan_discard_failed", "state": "repair_pending", "plan_id": plan_id}
        return {"ok": True, "plan_id": plan_id, "discarded": True}


def _normalize_applies_to(value: object) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - _APPLIES_TO_KEYS:
        raise SpecReuseError("invalid_applies_to")
    result: dict[str, list[str]] = {}
    for key in sorted(value):
        items = value[key]
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise SpecReuseError("invalid_applies_to")
        result[key] = sorted({item.strip() for item in items}, key=lambda item: (item.casefold(), item))
    return result


def _normalize_origins(root: Path, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SpecReuseError("invalid_derived_from")
    origins: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise SpecReuseError("invalid_derived_from")
        project = _safe_project(item.get("project"))
        page, _ = _page_path(root, item.get("path"))
        parts = Path(page).parts
        if len(parts) < 5 or parts[0:2] != ("wiki", "projects") or parts[2] != project or parts[3] != "specs":
            raise SpecReuseError("invalid_derived_from")
        rule = item.get("rule")
        if not isinstance(rule, str) or not rule.strip():
            raise SpecReuseError("invalid_derived_from")
        origins.add((project, page, rule.strip()))
    return [
        {"project": project, "path": page, "rule": rule}
        for project, page, rule in sorted(origins, key=lambda item: tuple(value.casefold() for value in item))
    ]


class SharedSpecService:
    """Plan and apply one validated shared-spec page mutation."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.store = SpecPlanStore(self.root, kind="shared_spec", prefix="shared-spec-")

    def plan(
        self,
        operation: str,
        page_path: str,
        *,
        body: str = "",
        title: str = "",
        applies_to: Mapping[str, object] | None = None,
        conditions: str = "",
        derived_from: object = None,
    ) -> dict[str, object]:
        if operation not in {"upsert", "delete"}:
            return {"ok": False, "code": "invalid_operation"}
        relative, target = _shared_page(self.root, page_path)
        current_hash = sha256_file(target) if target.is_file() else None
        current_text = target.read_text(encoding="utf-8") if target.is_file() else ""
        origin_hashes: dict[str, str] = {}
        if operation == "delete":
            if current_hash is None:
                return {"ok": False, "code": "page_not_found"}
            desired_text = ""
            summary = {"delete": 1}
            desired_hash = None
        else:
            if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
                return {"ok": False, "code": "invalid_shared_spec"}
            origins = _normalize_origins(self.root, derived_from)
            origin_hashes = _origin_fingerprints(self.root, origins)
            frontmatter: dict[str, object] = {
                "type": "entity",
                "generated": False,
                "domain": "shared-specs",
                "title": title.strip(),
                "applies_to": _normalize_applies_to(applies_to),
                "conditions": conditions.strip() if isinstance(conditions, str) else "",
                "derived_from": origins,
                "tags": ["shared-spec"],
                "lifecycle": "active",
            }
            frontmatter.update(stamp_page_policy(frontmatter, source_hashes={}))
            desired_text = render_page(frontmatter, strip_leading_h1(body), title_heading=f"# {title.strip()}")
            desired_hash = hashlib.sha256(desired_text.encode("utf-8")).hexdigest()
            summary = {"create": 1, "update": 0} if current_hash is None else {"create": 0, "update": int(current_hash != desired_hash)}
        entry = {"operation": operation, "page_path": relative, "current_hash": current_hash, "desired_hash": desired_hash}
        if operation == "upsert":
            entry["diff"] = "".join(difflib.unified_diff(current_text.splitlines(True), desired_text.splitlines(True), fromfile="current", tofile="incoming"))
        plan = self.store.create(
            {
                "operation": operation,
                "page_path": relative,
                "current_hash": current_hash,
                "desired_hash": desired_hash,
                "desired_text": desired_text,
                "origin_hashes": origin_hashes,
                "completed": [],
                "operations": {},
                "summary": summary,
                "entries": [entry],
            }
        )
        return _plan_response(self.store, plan, summary, [entry])

    def apply(self, plan_id: str) -> dict[str, object]:
        try:
            plan = self.store.read(plan_id)
        except SpecReuseError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id, "writes": 0}
        try:
            relative, target = _shared_page(self.root, plan.get("page_path"))
        except SpecReuseError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id, "writes": 0}
        current_hash = sha256_file(target) if target.is_file() else None
        expected = plan.get("current_hash")
        desired_hash = plan.get("desired_hash")
        operation = str(plan.get("operation"))
        completed = _completed_pages(plan)
        origin_hashes = plan.get("origin_hashes", {})
        if operation == "upsert" and isinstance(origin_hashes, Mapping):
            current_origins = _origin_fingerprints(self.root, [{"path": str(path)} for path in origin_hashes])
            if current_origins != {str(path): str(value) for path, value in origin_hashes.items()}:
                return {"ok": False, "code": "source_changed", "plan_id": plan_id, "writes": 0, "completed": completed}
        if operation == "upsert" and current_hash != expected and not (relative in completed and current_hash == desired_hash):
            return {"ok": False, "code": "target_changed", "plan_id": plan_id, "writes": 0, "completed": completed}
        if operation == "delete" and current_hash != expected and not (relative in completed and current_hash is None):
            return {"ok": False, "code": "target_changed", "plan_id": plan_id, "writes": 0, "completed": completed}
        create_wiki_root(self.root)
        coordinator = PageMutationCoordinator(self.root)
        writes = 0
        operation_id: str | None = None
        try:
            mutation = _apply_page(
                coordinator,
                plan,
                page_path=relative,
                action="delete" if operation == "delete" else ("create" if expected is None else "update"),
                text=str(plan.get("desired_text") or ""),
                current_hash=expected if isinstance(expected, str) else None,
                desired_hash=str(desired_hash) if desired_hash is not None else None,
            )
            operation_id = str(mutation["operation_id"]) if mutation.get("operation_id") else None
            _remember_operation(plan, relative, operation_id)
            if mutation.get("state") == "repair_pending" or mutation.get("code") == "projection_repair_required":
                if relative not in completed:
                    completed.append(relative)
                plan["completed"] = completed
                try:
                    self.store.persist(plan)
                except SpecReuseError as persist_exc:
                    return {"ok": False, "code": persist_exc.code, "state": "repair_pending", "plan_id": plan_id, "failed_page": relative, "completed": completed, "writes": writes, "failed_stage": mutation.get("failed_stage")}
                return _mutation_payload(mutation, plan_id=plan_id, page_path=relative, completed=completed, writes=writes)
            if mutation.get("ok") is not True:
                return _mutation_payload(mutation, plan_id=plan_id, page_path=relative, completed=completed, writes=writes)
            if relative not in completed:
                completed.append(relative)
                writes = 1
        except (OSError, AtomicFileError, SpecReuseError) as exc:
            try:
                plan["completed"] = completed
                self.store.persist(plan)
            except SpecReuseError:
                return {"ok": False, "code": "plan_progress_persist_failed", "state": "repair_pending", "plan_id": plan_id, "failed_page": relative, "writes": writes, "completed": completed}
            return {"ok": False, "code": getattr(exc, "code", "shared_spec_write_failed"), "plan_id": plan_id, "failed_page": relative, "writes": writes, "completed": completed}
        if not self.store.discard(plan_id):
            return {"ok": False, "code": "plan_discard_failed", "state": "repair_pending", "plan_id": plan_id, "page_path": relative, "writes": writes, "completed": completed}
        result: dict[str, object] = {"ok": True, "applied": True, "plan_id": plan_id, "page_path": relative, "writes": writes, "completed": completed}
        if operation_id is not None:
            result["operation_id"] = operation_id
        return result

    def discard(self, plan_id: str) -> dict[str, object]:
        try:
            self.store.read(plan_id)
        except SpecReuseError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id}
        if not self.store.discard(plan_id):
            return {"ok": False, "code": "plan_discard_failed", "state": "repair_pending", "plan_id": plan_id}
        return {"ok": True, "plan_id": plan_id, "discarded": True}


__all__ = ["SharedSpecService", "SpecMirrorService", "SpecPlanStore", "SpecReuseError"]

