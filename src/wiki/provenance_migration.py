"""Explicit provenance migration plans with CAS and compensating rollback.

The migration boundary is deliberately administrative.  MCP handlers never
call this module: a plan records only vault-relative identifiers, hashes and
stable issue codes, and an apply must revalidate both the page and its raw
source snapshots immediately before writing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Mapping

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.atomic_file import AtomicFileError, atomic_write_bytes, atomic_write_text, sha256_file
from common.privacy_policy import normalize_vault_relative
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import derive_page_policy
from wiki.projection_profile import projection_stages
from wiki.repair_plan import (
    RepairPageContext,
    RepairPlanHooks,
    RepairPlanOwner,
    iter_admin_page_files,
    projection_warning,
    safe_file_hash,
    string_map,
)
from wiki.source_provenance import SourceProvenanceError, SourceProvenanceResolver, source_path_key
from wiki.wiki_io import render_page, split_frontmatter
from wiki.wiki_paths import MIGRATIONS_DIR


class ProvenanceMigrationError(ValueError):
    """Stable administrative provenance migration failure."""

    def __init__(self, code: str, message: str = "provenance migration failed") -> None:
        super().__init__(message)
        self.code = code


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
        self.plan_owner = RepairPlanOwner(
            self.root,
            kind="provenance_migration",
            plan_prefix="provenance-migration-",
            audit_dir=MIGRATIONS_DIR,
            error_type=ProvenanceMigrationError,
        )

    def plan(self, page_path: str | None = None) -> dict[str, object]:
        pages = [self._page_file(page_path)] if page_path else list(self._iter_page_files())
        entries: list[dict[str, object]] = []
        for path in pages:
            entry = self._classify_page(path)
            if entry is not None:
                entries.append(entry)
        plan = self.plan_owner.create_plan({"entries": entries, "summary": _summary(entries)})
        return self.plan_owner.plan_response(plan)

    def apply(self, plan_id: str) -> dict[str, object]:
        plan = self.plan_owner.read_plan(plan_id)
        raw_entries = plan.get("entries", [])
        entries = [item for item in raw_entries if isinstance(item, Mapping)] if isinstance(raw_entries, list) else []
        applyable = [item for item in entries if item.get("applyable") is True]
        preflight = self._preflight(applyable)
        if not preflight["ok"]:
            return {**preflight, "plan_id": plan_id, "writes": 0}

        dependency = KnowledgeDependencies(self.root)
        hooks = RepairPlanHooks(
            prepare=lambda context: self._prepare_page(context, dependency),
            apply=lambda context: self._apply_page(context, dependency),
            rollback=lambda context: self._rollback_page(context, dependency),
        )
        return self.plan_owner.apply(
            plan_id,
            plan=plan,
            hooks=hooks,
            entries=applyable,
            cas_code="provenance_migration_cas_mismatch",
            rollback_code="provenance_migration_rolled_back",
            rollback_failed_code="provenance_migration_rollback_failed",
            response_fields={"skipped": len(entries) - len(applyable)},
        )

    def _prepare_page(self, context: RepairPageContext, dependency: KnowledgeDependencies) -> None:
        context.metadata["projection"] = KnowledgeDependencies.read_page_projection(self.root, context.page_path)

    def _apply_page(self, context: RepairPageContext, dependency: KnowledgeDependencies) -> dict[str, object]:
        item = context.entry
        original = context.original
        frontmatter, body = split_frontmatter(original.decode("utf-8"))
        desired_hashes = string_map(item.get("current_source_hashes"))
        current_hashes = _canonical_source_hashes(frontmatter.get("source_hashes"))
        if current_hashes != desired_hashes:
            updated_frontmatter = dict(frontmatter)
            updated_frontmatter["source_hashes"] = desired_hashes
            rendered = render_page(updated_frontmatter, body)
            atomic_write_text(context.source, rendered)
            context.wrote = True
        else:
            rendered = original.decode("utf-8")
        policy = derive_page_policy(frontmatter)
        dependency.update_page(
            context.page_path,
            sha256_file(context.source),
            desired_hashes,
            generated=policy.generated,
            maintenance=policy.maintenance,
            lifecycle=policy.lifecycle,
            replaced_by=policy.replaced_by,
            freshness=policy.freshness,
        )
        warning = self._refresh_retrieval(context.source, context.page_path)
        if warning is not None:
            context.add_warning(warning)
        return {
            "page_path": context.page_path,
            "before_page_hash": hashlib.sha256(original).hexdigest(),
            "after_page_hash": sha256_file(context.source),
            "page_written": rendered != original.decode("utf-8"),
            "dependency_diff": item.get("dependency_diff", {}),
        }

    def _rollback_page(self, context: RepairPageContext, dependency: KnowledgeDependencies) -> None:
        if context.wrote:
            atomic_write_bytes(context.source, context.original)
        projection = context.metadata.get("projection")
        if isinstance(projection, Mapping):
            _restore_projection(dependency, context.page_path, projection)

    def _refresh_retrieval(self, target: Path, page_path: str) -> dict[str, str] | None:
        """按 admin profile 增量刷新页面；索引故障不回滚页面事实。"""

        for stage in projection_stages("provenance"):
            if stage != "retrieval":
                continue
            try:
                result = RetrievalIndexStore(self.root, scope="active").update_page_from_file(target)
            except Exception as exc:
                return projection_warning(page_path, exc)
            if result.get("ok") is not True or result.get("state") == "rebuild_required":
                return projection_warning(page_path, result)
        return None

    def _iter_page_files(self) -> Iterable[Path]:
        return iter_admin_page_files(self.root)

    def _page_file(self, value: str) -> Path:
        return self.plan_owner.page_file(value)

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
                "expected_page_hash": safe_file_hash(path),
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

    def _preflight(self, entries: list[Mapping[str, object]]) -> dict[str, object]:
        for item in entries:
            page_path = str(item.get("page_path", ""))
            expected_sources = string_map(item.get("current_source_hashes"))
            try:
                current = self.resolver.resolve_many(list(expected_sources))
            except SourceProvenanceError as exc:
                return {"ok": False, "code": "provenance_source_drift", "page_path": page_path, "source_code": exc.code}
            if {item.relative_path: item.sha256 for item in current} != expected_sources:
                return {"ok": False, "code": "provenance_source_drift", "page_path": page_path}
        return {"ok": True}


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


def _restore_projection(dependency: KnowledgeDependencies, page_path: str, projection: Mapping[str, object]) -> None:
    if projection.get("state") != "ready":
        dependency.remove_page(page_path)
        return
    policy = derive_page_policy(projection)
    dependency.update_page(
        page_path,
        str(projection.get("page_hash", "")),
        string_map(projection.get("edges")),
        generated=policy.generated,
        maintenance=policy.maintenance,
        lifecycle=policy.lifecycle,
        replaced_by=policy.replaced_by,
        freshness=policy.freshness,
    )

__all__ = ["AtomicFileError", "ProvenanceMigrationError", "ProvenanceMigrationService"]
