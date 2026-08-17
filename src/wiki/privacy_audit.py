"""Administrative privacy audit plans for historical Wiki pages.

The service reports display redaction and locator impact without putting page
body text in a plan.  Applying a plan is an explicit CAS operation.  Locator
changes (renames and wikilinks) are refused unless the administrator opts in;
there is no implicit history or journal rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
from uuid import uuid4

from common.privacy_policy import LocatorError, PrivacyPolicy, field_class, redact_storage_value
from common.redaction import count_redaction_categories, redact_sensitive_text
from retrieval.retrieval_index import RetrievalIndexStore
from wiki.atomic_file import AtomicFileError, atomic_write_bytes, atomic_write_text, sha256_file
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import derive_page_policy
from wiki.projection_profile import projection_stages
from wiki.wiki_io import render_page, split_frontmatter
from wiki.wiki_paths import ADMIN_PLANS_DIR, KNOWLEDGE_DEPENDENCIES_DB, PRIVACY_AUDIT_DIR


class PrivacyAuditError(ValueError):
    """Stable administrative privacy audit failure."""

    def __init__(self, code: str, message: str = "privacy audit failed") -> None:
        super().__init__(message)
        self.code = code


_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")


class PrivacyAuditService:
    """Plan/apply display redaction while preserving identity fields."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.policy = PrivacyPolicy()

    def plan(self) -> dict[str, object]:
        raw_pages: dict[str, dict[str, object]] = {}
        for path in self._iter_page_files():
            page_path = path.relative_to(self.root).as_posix()
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                frontmatter, body = split_frontmatter(text)
            except (OSError, UnicodeDecodeError):
                raw_pages[page_path] = {
                    "page_path": page_path,
                    "expected_page_hash": _safe_hash(path),
                    "issues": ["page_read_failed"],
                    "hit_fields": [],
                    "redaction_categories": {},
                    "body_redaction_count": 0,
                    "filename_change": None,
                    "wikilink_changes": [],
                    "locator_review_required": True,
                    "has_frontmatter": False,
                }
                continue
            try:
                projected = redact_storage_value(frontmatter, policy=self.policy)
                projected_frontmatter = projected if isinstance(projected, dict) else {}
                issues: list[str] = []
            except LocatorError as exc:
                projected_frontmatter = frontmatter
                issues = [exc.code]
            redacted_body = redact_sensitive_text(body)
            hit_fields = _changed_fields(frontmatter, projected_frontmatter)
            if redacted_body != body:
                hit_fields.append("body")
            filename_change = _filename_change(page_path, hashlib.sha256(raw).hexdigest())
            raw_pages[page_path] = {
                "page_path": page_path,
                "expected_page_hash": hashlib.sha256(raw).hexdigest(),
                "hit_fields": sorted(set(hit_fields)),
                "redaction_categories": count_redaction_categories(body, redacted_body),
                "body_redaction_count": sum(count_redaction_categories(body, redacted_body).values()),
                "filename_change": filename_change,
                "wikilink_changes": [],
                "locator_review_required": bool(issues or filename_change),
                "issues": sorted(set(issues)),
                "has_frontmatter": bool(frontmatter),
            }

        filename_map: dict[str, str] = {}
        for old, entry in raw_pages.items():
            filename_change = entry.get("filename_change")
            if not isinstance(filename_change, Mapping):
                continue
            new_page_path = filename_change.get("new_page_path")
            if new_page_path:
                filename_map[old] = str(new_page_path)
        for page_path, entry in raw_pages.items():
            try:
                text = (self.root / Path(*page_path.split("/"))).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            changes = _wikilink_changes(text, filename_map)
            if changes:
                entry["wikilink_changes"] = changes
                entry["locator_review_required"] = True

        entries = [
            entry
            for entry in raw_pages.values()
            if entry.get("hit_fields") or entry.get("filename_change") or entry.get("wikilink_changes") or entry.get("issues")
        ]
        plan_id = uuid4().hex
        plan = {
            "schema_version": 1,
            "kind": "privacy_audit",
            "plan_id": plan_id,
            "created_at": datetime.now(UTC).isoformat(),
            "dry_run": True,
            "filename_changes": [{"old_page_path": old, "new_page_path": new} for old, new in sorted(filename_map.items())],
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

    def apply(self, plan_id: str, *, allow_locator_changes: bool = False) -> dict[str, object]:
        plan = self._read_plan(plan_id)
        audit_path = self.root / PRIVACY_AUDIT_DIR / f"{plan_id}.audit.json"
        if audit_path.is_file():
            try:
                previous = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
            if isinstance(previous, dict) and previous.get("state") == "applied":
                return {"ok": True, "already_applied": True, "plan_id": plan_id, "audit_state": "applied"}

        raw_entries = plan.get("entries", [])
        entries = [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []
        locator_entries = [entry for entry in entries if _has_locator_change(entry)]
        if locator_entries and not allow_locator_changes:
            return {
                "ok": False,
                "code": "privacy_locator_review_required",
                "plan_id": plan_id,
                "writes": 0,
                "locator_changes": len(locator_entries),
            }

        raw_filename_changes = plan.get("filename_changes", [])
        filename_changes = raw_filename_changes if isinstance(raw_filename_changes, list) else []
        filename_map = {
            str(item["old_page_path"]): str(item["new_page_path"])
            for item in filename_changes
            if isinstance(item, Mapping) and item.get("old_page_path") and item.get("new_page_path")
        }
        old_paths = {str(entry.get("page_path", "")) for entry in entries}
        for old, new in filename_map.items():
            if new in old_paths and new != old:
                return {"ok": False, "code": "privacy_locator_rename_collision", "plan_id": plan_id, "writes": 0}

        originals: dict[str, bytes] = {}
        targets: dict[str, Path] = {}
        projections: dict[str, dict[str, object]] = {}
        written_targets: list[Path] = []
        dependency: KnowledgeDependencies | None = None
        db_path = self.root / KNOWLEDGE_DEPENDENCIES_DB
        if db_path.is_file():
            dependency = KnowledgeDependencies(self.root)
        results: list[dict[str, object]] = []
        warnings: list[dict[str, str]] = []
        try:
            for entry in entries:
                page_path = str(entry.get("page_path", ""))
                source = self._page_file(page_path)
                original = source.read_bytes()
                originals[page_path] = original
                expected = str(entry.get("expected_page_hash", ""))
                actual = hashlib.sha256(original).hexdigest()
                if actual != expected:
                    return {
                        "ok": False,
                        "code": "privacy_audit_cas_mismatch",
                        "plan_id": plan_id,
                        "page_path": page_path,
                        "expected_page_hash": expected,
                        "actual_page_hash": actual,
                        "writes": 0,
                    }
                target_path = self._page_file(filename_map.get(page_path, page_path), allow_missing=True)
                if target_path != source and target_path.exists():
                    return {"ok": False, "code": "privacy_locator_rename_collision", "plan_id": plan_id, "writes": 0}
                targets[page_path] = target_path
                if dependency is not None:
                    projections[page_path] = KnowledgeDependencies.read_page_projection(self.root, page_path)

            for entry in entries:
                page_path = str(entry["page_path"])
                original = originals[page_path]
                transformed = _transform_text(original.decode("utf-8"), filename_map)
                target = targets[page_path]
                source = self._page_file(page_path)
                renamed = target != source
                if transformed.encode("utf-8") != original or renamed:
                    atomic_write_bytes(target, transformed.encode("utf-8"))
                    written_targets.append(target)
                after_hash = sha256_file(target)
                if dependency is not None:
                    frontmatter, _ = split_frontmatter(transformed)
                    desired_hashes = _string_map(frontmatter.get("source_hashes"))
                    policy = derive_page_policy(frontmatter)
                    dependency.update_page(
                        target.relative_to(self.root).as_posix(),
                        after_hash,
                        desired_hashes,
                        generated=policy.generated,
                        maintenance=policy.maintenance,
                        lifecycle=policy.lifecycle,
                        replaced_by=policy.replaced_by,
                        freshness=policy.freshness,
                    )
                    if target != self._page_file(page_path):
                        dependency.remove_page(page_path)
                if renamed:
                    source.unlink()
                warning = self._refresh_retrieval(
                    target,
                    page_path,
                    old_page_path=page_path if renamed else None,
                )
                if warning is not None:
                    warnings.append(warning)
                results.append(
                    {
                        "page_path": page_path,
                        "after_page_path": target.relative_to(self.root).as_posix(),
                        "before_page_hash": hashlib.sha256(original).hexdigest(),
                        "after_page_hash": after_hash,
                        "hit_fields": entry.get("hit_fields", []),
                        "filename_change": entry.get("filename_change"),
                        "wikilink_changes": entry.get("wikilink_changes", []),
                    }
                )

            audit = {
                "schema_version": 1,
                "kind": "privacy_audit",
                "plan_id": plan_id,
                "state": "applied",
                "rolled_back": False,
                "allow_locator_changes": allow_locator_changes,
                "entries": results,
                "writes": len(written_targets),
                "history_written": False,
                "warnings": warnings,
            }
            atomic_write_text(audit_path, json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            response = {"ok": True, "plan_id": plan_id, "applied": True, "writes": len(written_targets), "history_written": False, "entries": results}
            if warnings:
                response["warnings"] = warnings
            return response
        except Exception as exc:
            rollback_errors = _rollback(self.root, originals, targets, written_targets, projections, dependency)
            rollback_audit = {
                "schema_version": 1,
                "kind": "privacy_audit",
                "plan_id": plan_id,
                "state": "rolled_back",
                "rolled_back": not rollback_errors,
                "error_code": _stable_error_code(exc),
                "rollback_errors": rollback_errors,
                "entries": results,
                "history_written": False,
                "warnings": warnings,
            }
            try:
                atomic_write_text(audit_path, json.dumps(rollback_audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            except Exception:
                pass
            return {
                "ok": False,
                "code": "privacy_audit_rolled_back" if not rollback_errors else "privacy_audit_rollback_failed",
                "plan_id": plan_id,
                "rolled_back": not rollback_errors,
                "writes": 0,
                "error_code": _stable_error_code(exc),
            }

    def _refresh_retrieval(
        self,
        target: Path,
        page_path: str,
        *,
        old_page_path: str | None = None,
    ) -> dict[str, str] | None:
        """按 admin profile 刷新 active retrieval，失败只留下审计 warning。"""

        for stage in projection_stages("privacy"):
            if stage != "retrieval":
                continue
            store = RetrievalIndexStore(self.root, scope="active")
            try:
                result = store.update_page_from_file(target)
            except Exception as exc:
                return _projection_warning(page_path, exc)
            if result.get("ok") is not True or result.get("state") == "rebuild_required":
                return _projection_warning(page_path, result)
            if old_page_path is not None:
                try:
                    deleted = store.delete_page(old_page_path)
                except Exception as exc:
                    return _projection_warning(page_path, exc)
                if deleted.get("ok") is False and deleted.get("code") != "index_missing":
                    return _projection_warning(page_path, deleted)
        return None

    def _iter_page_files(self) -> Iterable[Path]:
        wiki_root = self.root / "wiki"
        if not wiki_root.is_dir():
            return ()
        return (
            path
            for path in sorted(wiki_root.rglob("*.md"))
            if path.is_file() and "archives" not in path.relative_to(self.root).parts
        )

    def _page_file(self, value: str, *, allow_missing: bool = False) -> Path:
        if not isinstance(value, str) or not value.startswith("wiki/"):
            raise PrivacyAuditError("invalid_page_path")
        parts = value.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise PrivacyAuditError("invalid_page_path")
        candidate = (self.root / Path(*parts)).resolve()
        relative_parts = candidate.relative_to(self.root).parts if candidate.is_relative_to(self.root) else ()
        if not candidate.is_relative_to(self.root) or not relative_parts or relative_parts[0].casefold() != "wiki" or candidate.suffix.casefold() != ".md" or (not allow_missing and not candidate.is_file()):
            raise PrivacyAuditError("page_not_found")
        return candidate

    def _write_plan(self, plan_id: str, plan: Mapping[str, object]) -> None:
        target = self.root / ADMIN_PLANS_DIR / f"privacy-audit-{plan_id}.json"
        atomic_write_text(target, json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def _read_plan(self, plan_id: str) -> dict[str, object]:
        if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
            raise PrivacyAuditError("invalid_plan_id")
        path = self.root / ADMIN_PLANS_DIR / f"privacy-audit-{plan_id}.json"
        if not path.is_file():
            raise PrivacyAuditError("plan_not_found")
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrivacyAuditError("plan_invalid") from exc
        if not isinstance(plan, dict) or plan.get("kind") != "privacy_audit" or plan.get("plan_id") != plan_id:
            raise PrivacyAuditError("plan_invalid")
        return plan


def _changed_fields(before: object, after: object, prefix: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[str] = []
        for key in set(before) | set(after):
            name = f"{prefix}.{key}" if prefix else str(key)
            if field_class(str(key)) in {"locator", "integrity", "internal"}:
                continue
            changes.extend(_changed_fields(before.get(key), after.get(key), name))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes: list[str] = []
        for index, (left, right) in enumerate(zip(before, after, strict=False)):
            changes.extend(_changed_fields(left, right, f"{prefix}[{index}]"))
        return changes
    return [prefix] if before != after and prefix else []


def _filename_change(page_path: str, page_hash: str) -> dict[str, str] | None:
    if redact_sensitive_text(page_path) == page_path:
        return None
    parent, _, name = page_path.rpartition("/")
    suffix = Path(name).suffix
    replacement = f"privacy-redacted-{page_hash[:12]}{suffix}"
    return {"old_page_path": page_path, "new_page_path": f"{parent}/{replacement}" if parent else replacement}


def _wikilink_changes(text: str, filename_map: Mapping[str, str]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for match in _WIKILINK.finditer(text):
        target = match.group(1).replace("\\", "/")
        for old, new in filename_map.items():
            old_no_ext = old[:-3] if old.casefold().endswith(".md") else old
            if target not in {old, old_no_ext}:
                continue
            new_target = new[:-3] if target == old_no_ext and new.casefold().endswith(".md") else new
            changes.append({"old": target, "new": new_target})
            break
    return changes


def _transform_text(text: str, filename_map: Mapping[str, str]) -> str:
    # Replace locator tokens before display redaction so an email-like legacy
    # filename inside ``[[...]]`` is not turned into a broken wikilink.
    locator_safe_text = _replace_wikilinks(text, filename_map)
    frontmatter, body = split_frontmatter(locator_safe_text)
    try:
        projected = redact_storage_value(frontmatter)
        safe_frontmatter = projected if isinstance(projected, dict) else {}
    except LocatorError as exc:
        raise PrivacyAuditError(exc.code) from exc
    if frontmatter or text.lstrip().startswith("---"):
        redacted_body = redact_sensitive_text(body)
        transformed = (
            render_page(safe_frontmatter, redacted_body)
            if safe_frontmatter != frontmatter or redacted_body != body
            else locator_safe_text
        )
    else:
        transformed = redact_sensitive_text(locator_safe_text)
    return transformed


def _replace_wikilinks(text: str, filename_map: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        target = match.group(1).replace("\\", "/")
        for old, new in filename_map.items():
            old_no_ext = old[:-3] if old.casefold().endswith(".md") else old
            if target == old:
                target = new
            elif target == old_no_ext:
                target = new[:-3] if new.casefold().endswith(".md") else new
            else:
                continue
            return f"[[{target}{match.group(2) or ''}{match.group(3) or ''}]]"
        return match.group(0)

    return _WIKILINK.sub(replace, text)


def _has_locator_change(entry: Mapping[str, object]) -> bool:
    return bool(entry.get("filename_change") or entry.get("wikilink_changes"))


def _rollback(
    root: Path,
    originals: Mapping[str, bytes],
    targets: Mapping[str, Path],
    written_targets: Iterable[Path],
    projections: Mapping[str, dict[str, object]],
    dependency: KnowledgeDependencies | None,
) -> list[str]:
    errors: list[str] = []
    old_paths = {(root / Path(*path.split("/"))).resolve() for path in originals}
    for target in reversed(list(written_targets)):
        if target not in old_paths:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                errors.append("target_remove_failed")
    for page_path, content in originals.items():
        old = (root / Path(*page_path.split("/"))).resolve()
        try:
            atomic_write_bytes(old, content)
        except Exception:
            errors.append("page_restore_failed")
    if dependency is not None:
        for page_path, projection in projections.items():
            try:
                target = targets[page_path]
                old = (root / Path(*page_path.split("/"))).resolve()
                if target != old:
                    dependency.remove_page(target.relative_to(root).as_posix())
                if projection.get("state") == "ready":
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
                else:
                    dependency.remove_page(page_path)
            except Exception:
                errors.append("dependency_restore_failed")
    return errors


def _summary(entries: Iterable[Mapping[str, object]]) -> dict[str, int]:
    result = {"pages": 0, "display_hits": 0, "locator_changes": 0}
    for entry in entries:
        result["pages"] += 1
        if entry.get("hit_fields"):
            result["display_hits"] += 1
        if _has_locator_change(entry):
            result["locator_changes"] += 1
    return result


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _stable_error_code(exc: Exception) -> str:
    if isinstance(exc, (PrivacyAuditError, AtomicFileError)):
        return getattr(exc, "code", "privacy_audit_failed")
    return "privacy_audit_failed"


def _projection_warning(page_path: str, result_or_error: object) -> dict[str, str]:
    """把索引失败压缩为稳定 code 与安全短消息，不携带原始异常。"""

    raw_code = getattr(result_or_error, "code", None)
    if isinstance(result_or_error, Mapping):
        raw_code = result_or_error.get("code")
    code = str(raw_code or "retrieval_projection_failed")
    if re.fullmatch(r"[a-z][a-z0-9_]*", code) is None:
        code = "retrieval_projection_failed"
    return {
        "page_path": page_path,
        "stage": "retrieval",
        "code": code,
        "message": "retrieval projection was not refreshed",
    }


def _safe_hash(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


__all__ = ["PrivacyAuditError", "PrivacyAuditService"]
