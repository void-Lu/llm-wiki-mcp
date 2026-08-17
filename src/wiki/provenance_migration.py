"""Explicit provenance migration plans with CAS and compensating rollback.

The migration boundary is deliberately administrative.  MCP handlers never
call this module: a plan records only vault-relative identifiers, hashes and
stable issue codes, and an apply must revalidate both the page and its raw
source snapshots immediately before writing.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
from uuid import uuid4

from wiki.atomic_file import AtomicFileError, atomic_write_bytes, atomic_write_text, sha256_file
from common.privacy_policy import normalize_vault_relative
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import derive_page_policy
from wiki.source_provenance import SourceProvenanceError, SourceProvenanceResolver, source_path_key
from wiki.wiki_io import render_page, split_frontmatter
from wiki.wiki_paths import ADMIN_PLANS_DIR, MIGRATIONS_DIR


class ProvenanceMigrationError(ValueError):
    """Stable administrative provenance migration failure."""

    def __init__(self, code: str, message: str = "provenance migration failed") -> None:
        super().__init__(message)
        self.code = code


_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_STRUCTURAL = {"index.md", "log.md", "overview.md"}
_LEGACY_SOURCE_PREFIXES = ("wiki/sources/", "wiki/chatlog/", "wiki/archives/")
_MISSING_SOURCE_CODES = {"source_not_found", "source_not_file", "source_read_failed"}
_INVALID_SOURCE_CODES = {
    "invalid_sources",
    "source_required",
    "source_path_escape",
    "path_escape",
    "source_path_not_allowed",
    "absolute_path_forbidden",
    "sensitive_locator",
}


class ProvenanceMigrationService:
    """Plan and apply safe ``sources``/``source_hashes`` migrations."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.resolver = SourceProvenanceResolver(self.root)

    def plan(self, page_path: str | None = None) -> dict[str, object]:
        pages = [self._page_file(page_path)] if page_path else list(self._iter_page_files())
        entries: list[dict[str, object]] = []
        for path in pages:
            entry = self._classify_page(path)
            if entry is not None:
                entries.append(entry)
        plan_id = uuid4().hex
        plan = {
            "schema_version": 1,
            "kind": "provenance_migration",
            "plan_id": plan_id,
            "created_at": datetime.now(UTC).isoformat(),
            "dry_run": True,
            "entries": entries,
            "summary": _summary(entries),
        }
        self._write_plan(plan_id, plan)
        return {
            "ok": True,
            "kind": plan["kind"],
            "plan_id": plan_id,
            "dry_run": True,
            "summary": plan["summary"],
            "entries": entries,
        }

    def apply(self, plan_id: str) -> dict[str, object]:
        plan = self._read_plan(plan_id)
        audit_path = self.root / MIGRATIONS_DIR / f"{plan_id}.audit.json"
        if audit_path.is_file():
            try:
                previous = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
            if isinstance(previous, dict) and previous.get("state") == "applied":
                return {"ok": True, "already_applied": True, "plan_id": plan_id, "audit_state": "applied"}

        raw_entries = plan.get("entries", [])
        entries = [item for item in raw_entries if isinstance(item, dict)] if isinstance(raw_entries, list) else []
        applyable = [item for item in entries if item.get("applyable") is True]
        preflight = self._preflight(applyable)
        if not preflight["ok"]:
            return {**preflight, "plan_id": plan_id, "writes": 0}

        originals: dict[str, bytes] = {}
        projections: dict[str, dict[str, object]] = {}
        written_pages: list[str] = []
        dependency = KnowledgeDependencies(self.root)
        results: list[dict[str, object]] = []
        try:
            for item in applyable:
                page_path = str(item["page_path"])
                target = self._page_file(page_path)
                original = target.read_bytes()
                originals[page_path] = original
                projections[page_path] = KnowledgeDependencies.read_page_projection(self.root, page_path)
                frontmatter, body = split_frontmatter(original.decode("utf-8"))
                desired_hashes = _string_map(item.get("current_source_hashes"))
                current_hashes = _canonical_source_hashes(frontmatter.get("source_hashes"))
                if current_hashes != desired_hashes:
                    updated_frontmatter = dict(frontmatter)
                    updated_frontmatter["source_hashes"] = desired_hashes
                    rendered = render_page(updated_frontmatter, body)
                    atomic_write_text(target, rendered)
                    written_pages.append(page_path)
                else:
                    rendered = original.decode("utf-8")
                policy = derive_page_policy(frontmatter)
                dependency.update_page(
                    page_path,
                    sha256_file(target),
                    desired_hashes,
                    generated=policy.generated,
                    maintenance=policy.maintenance,
                    lifecycle=policy.lifecycle,
                    replaced_by=policy.replaced_by,
                    freshness=policy.freshness,
                )
                results.append(
                    {
                        "page_path": page_path,
                        "before_page_hash": hashlib.sha256(original).hexdigest(),
                        "after_page_hash": sha256_file(target),
                        "page_written": rendered != original.decode("utf-8"),
                        "dependency_diff": item.get("dependency_diff", {}),
                    }
                )

            audit = {
                "schema_version": 1,
                "kind": "provenance_migration",
                "plan_id": plan_id,
                "state": "applied",
                "rolled_back": False,
                "entries": results,
                "writes": len(written_pages),
            }
            atomic_write_text(audit_path, json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return {
                "ok": True,
                "plan_id": plan_id,
                "applied": True,
                "writes": len(written_pages),
                "skipped": len(entries) - len(applyable),
                "entries": results,
            }
        except Exception as exc:
            rollback_errors = self._rollback(written_pages, originals, projections, dependency)
            rollback_audit = {
                "schema_version": 1,
                "kind": "provenance_migration",
                "plan_id": plan_id,
                "state": "rolled_back",
                "rolled_back": not rollback_errors,
                "error_code": _stable_error_code(exc),
                "rollback_errors": rollback_errors,
                "entries": results,
            }
            try:
                atomic_write_text(audit_path, json.dumps(rollback_audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            except Exception:
                pass
            return {
                "ok": False,
                "code": "provenance_migration_rolled_back" if not rollback_errors else "provenance_migration_rollback_failed",
                "plan_id": plan_id,
                "rolled_back": not rollback_errors,
                "writes": 0,
                "error_code": _stable_error_code(exc),
            }

    def _iter_page_files(self) -> Iterable[Path]:
        wiki_root = self.root / "wiki"
        if not wiki_root.is_dir():
            return ()
        return (
            path
            for path in sorted(wiki_root.rglob("*.md"))
            if path.is_file() and "archives" not in path.relative_to(self.root).parts
        )

    def _page_file(self, value: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ProvenanceMigrationError("invalid_page_path")
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if not normalized.startswith("wiki/") or normalized.endswith("/") or any(not part or part in {".", ".."} for part in parts):
            raise ProvenanceMigrationError("invalid_page_path")
        candidate = (self.root / Path(*parts)).resolve()
        relative_parts = candidate.relative_to(self.root).parts if candidate.is_relative_to(self.root) else ()
        if not candidate.is_relative_to(self.root) or not relative_parts or relative_parts[0].casefold() != "wiki" or not candidate.is_file() or candidate.suffix.casefold() != ".md":
            raise ProvenanceMigrationError("page_not_found")
        return candidate

    def _classify_page(self, path: Path) -> dict[str, object] | None:
        page_path = path.relative_to(self.root).as_posix()
        if path.name in _STRUCTURAL and not _has_provenance_fields(path):
            return None
        try:
            raw = path.read_bytes()
            frontmatter, _ = split_frontmatter(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError):
            return {
                "page_path": page_path,
                "category": "invalid",
                "issues": ["page_read_failed"],
                "expected_page_hash": _safe_hash(path),
                "applyable": False,
                "dependency_diff": {"mode": "full", "added": [], "removed": [], "changed": []},
            }

        legacy_fields = [field for field in ("source_capsules", "source_capsule") if field in frontmatter]
        source_values, source_issue = _source_values(frontmatter.get("sources"))
        stored_values, stored_issue = _stored_hashes(frontmatter.get("source_hashes"))
        legacy_paths = [item for item in source_values if item.casefold().startswith(_LEGACY_SOURCE_PREFIXES)]
        legacy = bool(legacy_fields or legacy_paths)
        issues: list[str] = [*legacy_fields]
        if source_issue:
            issues.append(source_issue)
        if stored_issue:
            issues.append(stored_issue)

        current_hashes: dict[str, str] = {}
        invalid = bool(source_issue or stored_issue)
        missing = False
        missing_source_file = False
        mismatch = False
        for source in source_values:
            try:
                resolved = self.resolver.resolve(source)
            except SourceProvenanceError as exc:
                issues.append(exc.code)
                if exc.code in _MISSING_SOURCE_CODES:
                    missing = True
                    missing_source_file = True
                else:
                    invalid = True
                continue
            current_hashes[resolved.relative_path] = resolved.sha256
            try:
                stored = stored_values.get(normalize_vault_relative(resolved.relative_path))
            except (TypeError, ValueError):
                stored = None
                invalid = True
            if stored is None:
                missing = True
                issues.append("source_hash_missing")
            elif stored != resolved.sha256:
                mismatch = True
                issues.append("source_hash_mismatch")

        if stored_values and current_hashes:
            known_keys = {normalize_vault_relative(path) for path in current_hashes}
            if any(key not in known_keys for key in stored_values):
                invalid = True
                issues.append("source_hash_extra")
        if not source_values and not legacy:
            return None

        if legacy:
            category = "legacy"
        elif invalid:
            category = "invalid"
        elif mismatch:
            category = "hash_mismatch"
        elif missing:
            category = "missing"
        else:
            category = "exact"
        dependency_diff = _dependency_diff(self.root, page_path, current_hashes)
        return {
            "page_path": page_path,
            "category": category,
            "issues": sorted(set(issues)),
            "expected_page_hash": hashlib.sha256(raw).hexdigest(),
            "sources": sorted(current_hashes),
            "current_source_hashes": current_hashes,
            "dependency_diff": dependency_diff,
            "applyable": bool(
                not legacy
                and not invalid
                and current_hashes
                and not missing_source_file
                and category in {"exact", "missing"}
            ),
        }

    def _write_plan(self, plan_id: str, plan: Mapping[str, object]) -> None:
        target = self.root / ADMIN_PLANS_DIR / f"provenance-migration-{plan_id}.json"
        atomic_write_text(target, json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def _read_plan(self, plan_id: str) -> dict[str, object]:
        _validate_plan_id(plan_id)
        path = self.root / ADMIN_PLANS_DIR / f"provenance-migration-{plan_id}.json"
        if not path.is_file():
            raise ProvenanceMigrationError("plan_not_found")
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvenanceMigrationError("plan_invalid") from exc
        if not isinstance(plan, dict) or plan.get("kind") != "provenance_migration" or plan.get("plan_id") != plan_id:
            raise ProvenanceMigrationError("plan_invalid")
        return plan

    def _preflight(self, entries: list[dict[str, object]]) -> dict[str, object]:
        for item in entries:
            page_path = str(item.get("page_path", ""))
            try:
                target = self._page_file(page_path)
            except ProvenanceMigrationError as exc:
                return {"ok": False, "code": exc.code, "page_path": page_path}
            expected = str(item.get("expected_page_hash", ""))
            actual = sha256_file(target)
            if actual != expected:
                return {
                    "ok": False,
                    "code": "provenance_migration_cas_mismatch",
                    "page_path": page_path,
                    "expected_page_hash": expected,
                    "actual_page_hash": actual,
                }
            expected_sources = _string_map(item.get("current_source_hashes"))
            try:
                current = self.resolver.resolve_many(list(expected_sources))
            except SourceProvenanceError as exc:
                return {"ok": False, "code": "provenance_source_drift", "page_path": page_path, "source_code": exc.code}
            if {item.relative_path: item.sha256 for item in current} != expected_sources:
                return {"ok": False, "code": "provenance_source_drift", "page_path": page_path}
        return {"ok": True}

    def _rollback(
        self,
        written_pages: list[str],
        originals: Mapping[str, bytes],
        projections: Mapping[str, dict[str, object]],
        dependency: KnowledgeDependencies,
    ) -> list[str]:
        errors: list[str] = []
        for page_path in reversed(written_pages):
            try:
                atomic_write_bytes(self._page_file(page_path), originals[page_path])
            except Exception:
                errors.append("page_restore_failed")
        for page_path, projection in projections.items():
            try:
                _restore_projection(dependency, page_path, projection)
            except Exception:
                errors.append("dependency_restore_failed")
        return errors


def _has_provenance_fields(path: Path) -> bool:
    try:
        frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return True
    return any(key in frontmatter for key in ("sources", "source_hashes", "source_capsule", "source_capsules"))


def _source_values(value: object) -> tuple[list[str], str | None]:
    if value is None:
        return [], None
    if isinstance(value, str):
        return [value], None
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) and item for item in value):
            return [], "invalid_sources"
        return list(value), None
    return [], "invalid_sources"


def _stored_hashes(value: object) -> tuple[dict[str, str], str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, Mapping):
        return {}, "invalid_source_hashes"
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not key or not item:
            return {}, "invalid_source_hashes"
        try:
            result[normalize_vault_relative(key)] = item
        except (TypeError, ValueError):
            return {}, "invalid_source_hashes"
    return result, None


def _canonical_source_hashes(value: object) -> dict[str, str]:
    values, issue = _stored_hashes(value)
    return {} if issue else values


def _dependency_diff(root: Path, page_path: str, current: Mapping[str, str]) -> dict[str, object]:
    projection = KnowledgeDependencies.read_page_projection(root, page_path)
    previous = projection.get("edges") if projection.get("state") == "ready" else {}
    previous_edges = previous if isinstance(previous, Mapping) else {}
    old = {str(key): str(value) for key, value in previous_edges.items()}
    new = {source_path_key(key): str(value) for key, value in current.items()}
    added = sorted(key for key in new if key not in old)
    removed = sorted(key for key in old if key not in new)
    changed = [
        {"source_path": key, "previous_hash": old[key], "current_hash": new[key]}
        for key in sorted(set(old) & set(new))
        if old[key] != new[key]
    ]
    return {"mode": "incremental" if projection.get("state") == "ready" else "full", "added": added, "removed": removed, "changed": changed}


def _summary(entries: Iterable[Mapping[str, object]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for entry in entries:
        category = str(entry.get("category", "unknown"))
        result[category] = result.get(category, 0) + 1
    return dict(sorted(result.items()))


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _restore_projection(dependency: KnowledgeDependencies, page_path: str, projection: Mapping[str, object]) -> None:
    if projection.get("state") != "ready":
        dependency.remove_page(page_path)
        return
    policy = derive_page_policy(projection)
    dependency.update_page(
        page_path,
        str(projection.get("page_hash", "")),
        _string_map(projection.get("edges")),
        generated=policy.generated,
        maintenance=policy.maintenance,
        lifecycle=policy.lifecycle,
        replaced_by=policy.replaced_by,
        freshness=policy.freshness,
    )


def _validate_plan_id(value: str) -> None:
    if not isinstance(value, str) or _PLAN_ID.fullmatch(value) is None:
        raise ProvenanceMigrationError("invalid_plan_id")


def _safe_hash(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _stable_error_code(exc: Exception) -> str:
    if isinstance(exc, (ProvenanceMigrationError, SourceProvenanceError, AtomicFileError)):
        return getattr(exc, "code", "migration_failed")
    return "migration_failed"


__all__ = ["ProvenanceMigrationError", "ProvenanceMigrationService"]
