"""Admin-only replacement of one external Raw Source tree."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from typing import Iterable, Mapping, cast

import yaml

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.atomic_file import atomic_write_bytes, atomic_write_text, sha256_file
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import derive_page_policy
from wiki.repair_plan import RepairPlanOwner
from wiki.wiki_io import split_frontmatter
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import MIGRATIONS_DIR, WikiPathError, admin_wiki_page_file, filesystem_path, safe_segment

PLAN_KIND = "raw_source_tree_replacement"
PLAN_PREFIX = "raw-replace-"
_ADMIN_STATE_DIR = MIGRATIONS_DIR.parts[0]
_MARKDOWN_LINK = re.compile(r"\[[^\]\r\n]*\]\((?P<target>[^)\r\n]*)\)")
_SOURCE_KEYS = ("source", "url", "canonical_url")


class RawReplacementError(ValueError):
    def __init__(self, code: str, message: str = "raw source replacement could not be completed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    size_bytes: int
    sha256: str
    title: str = ""
    source_url: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            **({"title": self.title} if self.title else {}),
            **({"source_url": self.source_url} if self.source_url else {}),
        }


class RawReplacementService:
    """Plan and apply a complete Raw Source tree replacement."""

    def __init__(self, vault_root: str | Path, *, target_path: str | Path | None = None):
        self.root = Path(vault_root).expanduser().resolve()
        if not self.root.is_dir():
            raise RawReplacementError("invalid_vault_root")
        self.target_relative_path = _normalize_target_path(target_path) if target_path is not None else None
        self.owner = RepairPlanOwner(
            self.root,
            kind=PLAN_KIND,
            plan_prefix=PLAN_PREFIX,
            audit_dir=MIGRATIONS_DIR,
            error_type=RawReplacementError,
        )

    def plan(self, source_root: str | Path) -> dict[str, object]:
        target_relative_path = self._require_target_path()
        target = self.root / Path(target_relative_path)
        raw_prefix = target_relative_path + "/"
        source = _validate_source_root(source_root, target, self.root)
        new_files = _inventory(source, "source")
        old_files = _inventory(target, "target") if target.exists() else ()
        new_by_key = {record.relative_path.casefold(): record for record in new_files}
        old_by_key = {record.relative_path.casefold(): record for record in old_files}
        identity_maps = _build_identity_maps(new_files)
        entries, mapping = _plan_page_rewrites(
            self.root,
            raw_prefix=raw_prefix,
            old_by_key=old_by_key,
            new_by_key=new_by_key,
            by_url=identity_maps[0],
            by_basename=identity_maps[1],
        )
        mapping_summary = _mapping_summary(mapping)
        summary = {
            "target_path": target_relative_path,
            "state": "blocked" if mapping_summary["unmatched"] or mapping_summary["ambiguous"] else "ready",
            "source_file_count": len(new_files),
            "source_markdown_count": sum(record.relative_path.casefold().endswith(".md") for record in new_files),
            "source_manifest_file_count": sum(record.relative_path.casefold().startswith("manifest/") for record in new_files),
            "source_deprecated_file_count": sum(record.relative_path.casefold().startswith("_deprecated_archive/") for record in new_files),
            "source_indexable_markdown_count": sum(
                record.relative_path.casefold().endswith(".md")
                and not record.relative_path.casefold().startswith(("manifest/", "_deprecated_archive/"))
                for record in new_files
            ),
            "target_file_count": len(old_files),
            "common_relative_path_count": len(set(new_by_key) & set(old_by_key)),
            "new_only_relative_path_count": len(set(new_by_key) - set(old_by_key)),
            "old_only_relative_path_count": len(set(old_by_key) - set(new_by_key)),
            "source_fingerprint": _tree_fingerprint(new_files),
            "target_fingerprint": _tree_fingerprint(old_files) if old_files else None,
            **mapping_summary,
        }
        plan = self.owner.create_plan(
            {
                "source_root": str(source),
                "target_path": target_relative_path,
                "source_fingerprint": summary["source_fingerprint"],
                "target_fingerprint": summary["target_fingerprint"],
                "source_files": [record.as_dict() for record in new_files],
                "entries": entries,
                "mapping": mapping,
                "summary": summary,
            }
        )
        return {
            "ok": True,
            "kind": PLAN_KIND,
            "plan_id": plan["plan_id"],
            "dry_run": True,
            "state": summary["state"],
            "summary": summary,
            "entries": [
                {
                    "page_path": entry["page_path"],
                    "source_rewrite_count": entry["source_rewrite_count"],
                    "body_rewrite_count": entry["body_rewrite_count"],
                }
                for entry in entries
            ],
        }

    def apply(self, plan_id: str) -> dict[str, object]:
        plan = self.owner.read_plan(plan_id)
        if self.owner.already_applied(plan_id):
            return {"ok": True, "already_applied": True, "plan_id": plan_id, "state": "already_applied"}
        summary_value = plan.get("summary")
        summary: Mapping[str, object] = cast(Mapping[str, object], summary_value) if isinstance(summary_value, Mapping) else {}
        if summary.get("unmatched") or summary.get("ambiguous"):
            return {
                "ok": False,
                "code": "mapping_incomplete",
                "plan_id": plan_id,
                "state": "blocked",
                "summary": _public_summary(summary),
            }

        target_relative_path = _normalize_target_path(plan.get("target_path"))
        if self.target_relative_path is not None and self.target_relative_path != target_relative_path:
            raise RawReplacementError("target_path_mismatch")
        target = self.root / Path(target_relative_path)
        source = _validate_source_root(plan.get("source_root"), target, self.root)
        expected_source_fingerprint = str(plan.get("source_fingerprint") or "")
        current_source_files = _inventory(source, "source")
        if _tree_fingerprint(current_source_files) != expected_source_fingerprint:
            return {"ok": False, "code": "source_changed", "plan_id": plan_id, "state": "blocked"}
        current_target_files = _inventory(target, "target") if target.exists() else ()
        current_target_fingerprint = _tree_fingerprint(current_target_files) if target.exists() else None
        if current_target_fingerprint != plan.get("target_fingerprint"):
            return {"ok": False, "code": "target_changed", "plan_id": plan_id, "state": "blocked"}

        raw_entries = plan.get("entries")
        entries = [entry for entry in raw_entries if isinstance(entry, Mapping)] if isinstance(raw_entries, list) else []
        page_paths = [str(entry.get("page_path", "")) for entry in entries]
        try:
            page_originals = _read_page_cas(self.root, entries)
        except RawReplacementError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id, "state": "blocked"}
        dependency_snapshots = {
            page_path: KnowledgeDependencies.read_page_projection(self.root, page_path)
            for page_path in page_paths
        }
        staging = source.parent / f".{source.name}{_ADMIN_STATE_DIR}-stage-{plan_id}"
        backup = source.parent / f".{source.name}{_ADMIN_STATE_DIR}-backup-{plan_id}"
        state = {
            "staging": staging,
            "backup": backup,
            "backup_created": False,
            "target_removed": False,
            "target_installed": False,
            "written_pages": [],
            "updated_dependencies": [],
        }
        try:
            _remove_if_empty_staging(staging)
            _copy_tree(source, staging)
            if _tree_fingerprint(_inventory(staging, "staging")) != expected_source_fingerprint:
                raise RawReplacementError("staging_changed")

            if target.exists():
                if backup.exists():
                    if _tree_fingerprint(_inventory(backup, "backup")) != plan.get("target_fingerprint"):
                        raise RawReplacementError("backup_exists")
                else:
                    _copy_tree(target, backup)
                state["backup_created"] = True
                if _tree_fingerprint(_inventory(backup, "backup")) != plan.get("target_fingerprint"):
                    raise RawReplacementError("backup_failed")
                state["target_removed"] = True
                shutil.rmtree(filesystem_path(target))
            state["target_removed"] = True
            _copy_tree(staging, target)
            state["target_installed"] = True
            if _tree_fingerprint(_inventory(target, "target")) != expected_source_fingerprint:
                raise RawReplacementError("target_install_failed")
            shutil.rmtree(filesystem_path(staging))

            _read_page_cas(self.root, entries)
            for entry in entries:
                page_path = str(entry["page_path"])
                target = self.owner.page_file(page_path)
                current = target.read_bytes()
                rewritten = _rewrite_page(current, entry)
                if rewritten != current:
                    state["written_pages"].append(page_path)
                    atomic_write_bytes(target, rewritten)

            dependencies = KnowledgeDependencies(self.root)
            for entry in entries:
                page_path = str(entry["page_path"])
                page_file = self.owner.page_file(page_path)
                frontmatter, _body = split_frontmatter(page_file.read_text(encoding="utf-8-sig"))
                source_hashes = frontmatter.get("source_hashes", {})
                policy = derive_page_policy(frontmatter, source_hashes if isinstance(source_hashes, Mapping) else {})
                state["updated_dependencies"].append(page_path)
                dependencies.update_page(
                    page_path,
                    sha256_file(page_file),
                    source_hashes if isinstance(source_hashes, Mapping) else {},
                    policy=policy,
                )
        except Exception as exc:
            rollback_errors = _rollback_facts(self.root, target_relative_path, state, page_originals, dependency_snapshots)
            audit_state = "rolled_back" if not rollback_errors else "repair_pending"
            self._write_audit(
                plan_id,
                state=audit_state,
                summary=summary,
                error_code=_stable_code(exc),
                rollback_errors=rollback_errors,
            )
            return {
                "ok": False,
                "code": "raw_replace_rolled_back" if not rollback_errors else "raw_replace_rollback_failed",
                "plan_id": plan_id,
                "state": audit_state,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
            }

        projection_errors: list[str] = []
        raw_index = _build_retrieval_index(self.root, "raw")
        if not raw_index.get("ok"):
            projection_errors.append(str(raw_index.get("code") or "raw_index_failed"))
        active_index = _build_retrieval_index(self.root, "active")
        if not active_index.get("ok"):
            projection_errors.append(str(active_index.get("code") or "active_index_failed"))

        if not projection_errors:
            try:
                append_log_entry(
                    self.root,
                    WikiLogEntry(
                        operation="raw-replace",
                        title=f"Raw Source Tree Replacement: {Path(target_relative_path).name}",
                        paths=[target_relative_path],
                        sources=[target_relative_path],
                        status="completed",
                        operation_id=plan_id,
                    ),
                )
            except Exception:
                projection_errors.append("audit_log_failed")

        if projection_errors:
            result = {
                "ok": False,
                "code": "repair_pending",
                "plan_id": plan_id,
                "state": "repair_pending",
                "repair_action": "rebuild_retrieval_index",
                "projection_errors": sorted(set(projection_errors)),
            }
            self._write_audit(plan_id, state="repair_pending", summary=summary, projection_errors=projection_errors)
            return result

        self._write_audit(plan_id, state="applied", summary=summary)
        return {
            "ok": True,
            "plan_id": plan_id,
            "state": "completed",
            "applied": True,
            "written_pages": len(state["written_pages"]),
            "raw_index": _safe_index_result(raw_index),
            "active_index": _safe_index_result(active_index),
        }

    def recover(self, plan_id: str) -> dict[str, object]:
        plan = self.owner.read_plan(plan_id)
        audit = self.owner.read_audit(plan_id)
        rollback_errors = audit.get("rollback_errors")
        if audit.get("state") != "repair_pending" or not isinstance(rollback_errors, list):
            return {"ok": False, "code": "recovery_not_available", "plan_id": plan_id, "state": "blocked"}
        if audit.get("projection_errors"):
            return {"ok": False, "code": "recovery_not_available", "plan_id": plan_id, "state": "blocked"}

        target_relative_path = _normalize_target_path(plan.get("target_path"))
        if self.target_relative_path is not None and self.target_relative_path != target_relative_path:
            raise RawReplacementError("target_path_mismatch")
        target = self.root / Path(target_relative_path)
        source = _validate_source_root(plan.get("source_root"), target, self.root)
        backup = source.parent / f".{source.name}{_ADMIN_STATE_DIR}-backup-{plan_id}"
        staging = source.parent / f".{source.name}{_ADMIN_STATE_DIR}-stage-{plan_id}"
        raw_entries = plan.get("entries")
        entries = [entry for entry in raw_entries if isinstance(entry, Mapping)] if isinstance(raw_entries, list) else []
        try:
            _read_page_cas(self.root, entries)
            if plan.get("target_fingerprint") is None:
                if backup.exists():
                    raise RawReplacementError("recovery_backup_unexpected")
            elif not backup.is_dir() or _tree_fingerprint(_inventory(backup, "backup")) != plan.get("target_fingerprint"):
                raise RawReplacementError("recovery_backup_invalid")
            if target.exists():
                shutil.rmtree(filesystem_path(target))
            if backup.exists():
                _copy_tree(backup, target)
            if staging.exists():
                shutil.rmtree(filesystem_path(staging))
            restored = _inventory(target, "target") if target.exists() else ()
            if _tree_fingerprint(restored) != plan.get("target_fingerprint"):
                raise RawReplacementError("recovery_verify_failed")
        except RawReplacementError as exc:
            return {"ok": False, "code": exc.code, "plan_id": plan_id, "state": "repair_pending"}
        except Exception:
            return {"ok": False, "code": "recovery_failed", "plan_id": plan_id, "state": "repair_pending"}

        summary_value = plan.get("summary")
        summary = cast(Mapping[str, object], summary_value) if isinstance(summary_value, Mapping) else {}
        self._write_audit(plan_id, state="recovered", summary=summary)
        return {"ok": True, "plan_id": plan_id, "state": "recovered", "backup_retained": True}

    def _require_target_path(self) -> str:
        if self.target_relative_path is None:
            raise RawReplacementError("target_path_required")
        return self.target_relative_path

    def _write_audit(
        self,
        plan_id: str,
        *,
        state: str,
        summary: Mapping[str, object],
        error_code: str | None = None,
        rollback_errors: Iterable[str] = (),
        projection_errors: Iterable[str] = (),
    ) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": PLAN_KIND,
            "plan_id": plan_id,
            "state": "applied" if state == "applied" else state,
            "rolled_back": state == "rolled_back",
            "summary": _public_summary(summary),
        }
        if error_code:
            payload["error_code"] = error_code
        if rollback_errors:
            payload["rollback_errors"] = sorted(set(rollback_errors))
        if projection_errors:
            payload["projection_errors"] = sorted(set(projection_errors))
        try:
            atomic_write_text(self.owner.audit_path(plan_id), json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            raise RawReplacementError("audit_write_failed") from exc


def _validate_source_root(value: object, target: Path, vault_root: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise RawReplacementError("source_root_required")
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise RawReplacementError("source_symlink")
    try:
        source = candidate.resolve(strict=True)
    except OSError as exc:
        raise RawReplacementError("source_root_invalid") from exc
    if not source.is_dir():
        raise RawReplacementError("source_root_invalid")
    target_resolved = target.resolve()
    vault_resolved = vault_root.resolve()
    if source.parent.is_relative_to(vault_resolved):
        raise RawReplacementError("external_backup_required")
    if source == target_resolved or source.is_relative_to(target_resolved) or target_resolved.is_relative_to(source):
        raise RawReplacementError("source_target_overlap")
    return source


def _normalize_target_path(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise RawReplacementError("target_path_required")
    text = str(value).replace("\\", "/")
    if not text or text.startswith("/") or (len(text) >= 2 and text[1] == ":") or text.endswith("/"):
        raise RawReplacementError("target_path_invalid")
    parts = text.split("/") if text else []
    if len(parts) < 3 or parts[0].casefold() != "raw" or parts[1].casefold() != "sources":
        raise RawReplacementError("target_path_invalid")
    try:
        for part in parts:
            safe_segment(part)
    except WikiPathError as exc:
        raise RawReplacementError("target_path_invalid") from exc
    return "/".join(parts)


def _inventory(root: Path, label: str) -> tuple[FileRecord, ...]:
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise RawReplacementError(f"{label}_tree_invalid")
    records: list[FileRecord] = []

    def visit(directory: Path, relative: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise RawReplacementError(f"{label}_read_failed") from exc
        for child in children:
            path = Path(child.path)
            if child.is_symlink():
                raise RawReplacementError(f"{label}_symlink")
            child_relative = relative / child.name
            if child.is_dir(follow_symlinks=False):
                visit(path, child_relative)
                continue
            if not child.is_file(follow_symlinks=False):
                raise RawReplacementError(f"{label}_file_invalid")
            try:
                stat_value = path.stat()
                digest = sha256_file(path)
            except OSError as exc:
                raise RawReplacementError(f"{label}_read_failed") from exc
            title, source_url = _markdown_identity(path)
            records.append(FileRecord(child_relative.as_posix(), int(stat_value.st_size), digest, title, source_url))

    visit(filesystem_path(root), Path())
    return tuple(sorted(records, key=lambda record: record.relative_path.casefold()))


def _markdown_identity(path: Path) -> tuple[str, str]:
    if path.suffix.casefold() != ".md":
        return "", ""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return "", ""
    frontmatter, body = split_frontmatter(text)
    title = str(frontmatter.get("title") or "")
    if not title:
        title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
    source_url = next((str(frontmatter[key]) for key in _SOURCE_KEYS if frontmatter.get(key)), "")
    return title, source_url


def _build_identity_maps(records: Iterable[FileRecord]) -> tuple[dict[str, list[FileRecord]], dict[str, list[FileRecord]]]:
    by_url: dict[str, list[FileRecord]] = {}
    by_basename: dict[str, list[FileRecord]] = {}
    for record in records:
        if record.source_url:
            by_url.setdefault(record.source_url, []).append(record)
        by_basename.setdefault(Path(record.relative_path).name.casefold(), []).append(record)
    return by_url, by_basename


def _plan_page_rewrites(
    root: Path,
    *,
    raw_prefix: str,
    old_by_key: Mapping[str, FileRecord],
    new_by_key: Mapping[str, FileRecord],
    by_url: Mapping[str, list[FileRecord]],
    by_basename: Mapping[str, list[FileRecord]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    mapping_by_key: dict[str, dict[str, object]] = {}
    wiki_root = root / "wiki"
    if not wiki_root.is_dir():
        return entries, []
    pages = sorted(path for path in wiki_root.rglob("*.md") if path.is_file() and path.name.casefold() != "log.md" and "archives" not in path.relative_to(root).parts)
    for page in pages:
        relative_page = page.relative_to(root).as_posix()
        try:
            admin_wiki_page_file(root, relative_page)
        except Exception:
            continue
        try:
            raw_bytes = page.read_bytes()
            text = raw_bytes.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise RawReplacementError("page_read_failed") from exc
        frontmatter, body = split_frontmatter(text)
        source_locators = list(_nested_raw_locators(frontmatter.get("sources"), raw_prefix))
        body_locators = list(_body_raw_locators(body, raw_prefix))
        locators = source_locators + body_locators
        if not locators:
            continue
        page_rewrites: dict[str, str] = {}
        for locator in locators:
            result = mapping_by_key.get(locator.casefold())
            if result is None:
                result = _resolve_mapping(locator, raw_prefix=raw_prefix, old_by_key=old_by_key, new_by_key=new_by_key, by_url=by_url, by_basename=by_basename)
                mapping_by_key[locator.casefold()] = result
            if result.get("status") == "mapped":
                page_rewrites[locator] = str(result["new_locator"])
        source_rewrite_count = sum(
            1 for locator in source_locators if mapping_by_key[locator.casefold()].get("status") == "mapped"
        )
        body_rewrite_count = sum(
            1 for locator in body_locators if mapping_by_key[locator.casefold()].get("status") == "mapped"
        )
        new_source_hashes = _planned_source_hashes(frontmatter, page_rewrites, new_by_key, raw_prefix=raw_prefix)
        has_frontmatter_change = bool(source_locators) and bool(page_rewrites)
        if has_frontmatter_change:
            desired = "review_required" if frontmatter.get("maintenance") == "manual" or frontmatter.get("generated") is not True else "stale"
        else:
            desired = str(frontmatter.get("freshness") or "")
        if page_rewrites or has_frontmatter_change:
            entries.append(
                {
                    "page_path": relative_page,
                    "expected_page_hash": sha256(raw_bytes).hexdigest(),
                    "rewrites": [{"old": old, "new": new} for old, new in sorted(page_rewrites.items())],
                    "source_rewrite_count": source_rewrite_count,
                    "body_rewrite_count": body_rewrite_count,
                    "new_source_hashes": new_source_hashes,
                    "freshness": desired,
                }
            )
    return entries, [mapping_by_key[key] for key in sorted(mapping_by_key)]


def _resolve_mapping(
    locator: str,
    *,
    raw_prefix: str,
    old_by_key: Mapping[str, FileRecord],
    new_by_key: Mapping[str, FileRecord],
    by_url: Mapping[str, list[FileRecord]],
    by_basename: Mapping[str, list[FileRecord]],
) -> dict[str, object]:
    if not locator.startswith(raw_prefix):
        return {"locator": locator, "status": "ignored"}
    relative = locator[len(raw_prefix) :].replace("\\", "/")
    exact = new_by_key.get(relative.casefold())
    if exact is not None:
        return {"locator": locator, "status": "mapped", "match": "relative_path", "new_locator": raw_prefix + exact.relative_path}
    old = old_by_key.get(relative.casefold())
    if old is not None and old.source_url:
        candidates = by_url.get(old.source_url, [])
        if len(candidates) == 1:
            return {"locator": locator, "status": "mapped", "match": "canonical_url", "new_locator": raw_prefix + candidates[0].relative_path}
        if len(candidates) > 1:
            return {"locator": locator, "status": "ambiguous", "reason": "canonical_url_multiple"}
    candidates = by_basename.get(Path(relative).name.casefold(), [])
    if len(candidates) == 1:
        return {"locator": locator, "status": "mapped", "match": "basename_unique", "new_locator": raw_prefix + candidates[0].relative_path}
    if len(candidates) > 1:
        return {"locator": locator, "status": "ambiguous", "reason": "basename_multiple"}
    return {"locator": locator, "status": "unmatched", "reason": "identity_not_found"}


def _planned_source_hashes(
    frontmatter: Mapping[str, object],
    rewrites: Mapping[str, str],
    new_by_key: Mapping[str, FileRecord],
    *,
    raw_prefix: str,
) -> dict[str, str]:
    current = frontmatter.get("source_hashes")
    existing = dict(current) if isinstance(current, Mapping) else {}
    result: dict[str, str] = {}
    for key, value in existing.items():
        old = str(key)
        new = rewrites.get(old, old)
        record = new_by_key.get(new.removeprefix(raw_prefix).casefold()) if new.startswith(raw_prefix) else None
        result[new] = record.sha256 if record is not None else str(value)
    for source in _nested_raw_locators(frontmatter.get("sources"), raw_prefix):
        new = rewrites.get(source, source)
        record = new_by_key.get(new.removeprefix(raw_prefix).casefold()) if new.startswith(raw_prefix) else None
        if record is not None:
            result[new] = record.sha256
    return result


def _nested_raw_locators(value: object, raw_prefix: str) -> Iterable[str]:
    if isinstance(value, str) and value.startswith(raw_prefix):
        yield value.replace("\\", "/")
    elif isinstance(value, list):
        for item in value:
            yield from _nested_raw_locators(item, raw_prefix)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _nested_raw_locators(item, raw_prefix)


def _body_raw_locators(body: str, raw_prefix: str) -> list[str]:
    pattern = re.compile(re.escape(raw_prefix) + r"[^\r\n)`]*?\.md(?=[)`\s]|$)")
    return pattern.findall(body)


def _rewrite_page(raw_bytes: bytes, entry: Mapping[str, object]) -> bytes:
    text = raw_bytes.decode("utf-8-sig")
    frontmatter, body, frontmatter_prefix = _split_frontmatter_preserving_body(text)
    raw_rewrites = entry.get("rewrites")
    rewrites = {
        str(item["old"]): str(item["new"])
        for item in raw_rewrites
        if isinstance(item, Mapping) and item.get("old") and item.get("new")
    } if isinstance(raw_rewrites, list) else {}
    raw_source_count = entry.get("source_rewrite_count")
    source_rewrite_count = int(raw_source_count) if isinstance(raw_source_count, (int, str)) and str(raw_source_count).isdigit() else 0
    if frontmatter and rewrites and source_rewrite_count:
        replaced_frontmatter = _replace_nested(frontmatter, rewrites)
        frontmatter = cast(dict[str, object], replaced_frontmatter) if isinstance(replaced_frontmatter, dict) else frontmatter
        if isinstance(frontmatter.get("sources"), (list, str, dict)):
            raw_hashes = entry.get("new_source_hashes")
            frontmatter["source_hashes"] = dict(raw_hashes) if isinstance(raw_hashes, Mapping) else {}
            freshness = str(entry.get("freshness") or "")
            if freshness:
                frontmatter["freshness"] = freshness
        yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        text = f"---\n{yaml_text}\n---\n{_rewrite_body(body, rewrites)}"
    elif frontmatter:
        text = frontmatter_prefix + _rewrite_body(body, rewrites)
    else:
        text = _rewrite_body(body, rewrites)
    return text.encode("utf-8")


def _split_frontmatter_preserving_body(text: str) -> tuple[dict[str, object], str, str]:
    cleaned = text.lstrip("﻿")
    lines = cleaned.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, cleaned, ""
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}, cleaned, ""
    try:
        loaded = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        return {}, cleaned, ""
    return (
        dict(loaded) if isinstance(loaded, Mapping) else {},
        "".join(lines[end + 1 :]),
        "".join(lines[: end + 1]),
    )


def _replace_nested(value: object, rewrites: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return rewrites.get(value, value)
    if isinstance(value, list):
        return [_replace_nested(item, rewrites) for item in value]
    if isinstance(value, dict):
        return {key: _replace_nested(item, rewrites) for key, item in value.items()}
    return value


def _rewrite_body(body: str, rewrites: Mapping[str, str]) -> str:
    if not rewrites:
        return body
    ordered = sorted(rewrites.items(), key=lambda item: len(item[0]), reverse=True)

    def replace_segment(segment: str) -> str:
        for old, new in ordered:
            segment = segment.replace(old, new)
        return segment

    output: list[str] = []
    cursor = 0
    for match in _MARKDOWN_LINK.finditer(body):
        output.append(replace_segment(body[cursor : match.start()]))
        target_start, target_end = match.span("target")
        output.append(body[match.start() : target_start])
        output.append(replace_segment(body[target_start:target_end]))
        output.append(body[target_end : match.end()])
        cursor = match.end()
    output.append(replace_segment(body[cursor:]))
    return "".join(output)


def _read_page_cas(root: Path, entries: Iterable[Mapping[str, object]]) -> dict[str, bytes]:
    originals: dict[str, bytes] = {}
    for entry in entries:
        page_path = str(entry.get("page_path", ""))
        target = admin_wiki_page_file(root, page_path)
        current = target.read_bytes()
        actual = sha256(current).hexdigest()
        if actual != str(entry.get("expected_page_hash") or ""):
            raise RawReplacementError("page_cas_mismatch")
        originals[page_path] = current
    return originals


def _rollback_facts(
    root: Path,
    target_relative_path: str,
    state: Mapping[str, object],
    originals: Mapping[str, bytes],
    dependency_snapshots: Mapping[str, Mapping[str, object]],
) -> list[str]:
    errors: list[str] = []
    written_pages = state.get("written_pages")
    for page_path in reversed(written_pages if isinstance(written_pages, list) else []):
        try:
            atomic_write_bytes(admin_wiki_page_file(root, str(page_path)), originals[str(page_path)])
        except Exception:
            errors.append("page_restore_failed")
    try:
        dependencies = KnowledgeDependencies(root)
        updated_dependencies = state.get("updated_dependencies")
        for page_path in reversed(updated_dependencies if isinstance(updated_dependencies, list) else []):
            _restore_dependency(dependencies, str(page_path), dependency_snapshots.get(str(page_path), {}))
    except Exception:
        errors.append("dependency_restore_failed")
    target = root / Path(target_relative_path)
    if (bool(state.get("target_installed")) or bool(state.get("target_removed"))) and target.exists():
        try:
            shutil.rmtree(filesystem_path(target))
        except Exception:
            errors.append("raw_tree_restore_failed")
    backup = state.get("backup")
    if bool(state.get("backup_created")) and isinstance(backup, Path) and backup.exists():
        try:
            _copy_tree(backup, target)
        except Exception:
            errors.append("raw_tree_restore_failed")
    elif bool(state.get("target_removed")) and not target.exists():
        errors.append("raw_tree_restore_failed")
    staging = state.get("staging")
    if isinstance(staging, Path) and staging.exists():
        try:
            shutil.rmtree(filesystem_path(staging))
        except Exception:
            errors.append("staging_cleanup_failed")
    return errors


def _restore_dependency(dependencies: KnowledgeDependencies, page_path: str, snapshot: Mapping[str, object]) -> None:
    if snapshot.get("state") != "ready":
        dependencies.remove_page(page_path)
        return
    policy = derive_page_policy(
        {
            "freshness": snapshot.get("freshness"),
            "lifecycle": snapshot.get("lifecycle"),
            "generated": snapshot.get("generated"),
            "maintenance": snapshot.get("maintenance"),
            "replaced_by": snapshot.get("replaced_by"),
        }
    )
    edges = snapshot.get("edges")
    dependencies.update_page(page_path, str(snapshot.get("page_hash") or ""), cast(Mapping[str, str], edges) if isinstance(edges, Mapping) else {}, policy=policy)


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RawReplacementError("destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(filesystem_path(source), filesystem_path(destination), symlinks=False, copy_function=shutil.copy2)
    except Exception as exc:
        try:
            if destination.exists():
                shutil.rmtree(filesystem_path(destination), ignore_errors=True)
        except Exception:
            pass
        raise RawReplacementError("tree_copy_failed") from exc


def _remove_if_empty_staging(path: Path) -> None:
    if path.exists():
        try:
            shutil.rmtree(filesystem_path(path))
        except Exception as exc:
            raise RawReplacementError("staging_exists") from exc


def _tree_fingerprint(records: Iterable[FileRecord]) -> str:
    payload = "".join(f"{record.relative_path}\0{record.size_bytes}\0{record.sha256}\n" for record in sorted(records, key=lambda item: item.relative_path.casefold()))
    return sha256(payload.encode("utf-8")).hexdigest()


def _mapping_summary(mapping: Iterable[Mapping[str, object]]) -> dict[str, object]:
    counts = {"relative_path": 0, "canonical_url": 0, "basename_unique": 0, "unmatched": 0, "ambiguous": 0}
    for item in mapping:
        status = str(item.get("status"))
        if status in {"unmatched", "ambiguous"}:
            counts[status] += 1
        else:
            counts[str(item.get("match") or "relative_path")] = counts.get(str(item.get("match") or "relative_path"), 0) + 1
    return {
        "mapped_locator_count": sum(counts[key] for key in ("relative_path", "canonical_url", "basename_unique")),
        "relative_path_matches": counts["relative_path"],
        "canonical_url_matches": counts["canonical_url"],
        "basename_unique_matches": counts["basename_unique"],
        "unmatched": counts["unmatched"],
        "ambiguous": counts["ambiguous"],
    }


def _public_summary(summary: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in summary.items() if key not in {"source_root"}}


def _build_retrieval_index(root: Path, scope: str) -> dict[str, object]:
    try:
        store = RetrievalIndexStore(root, scope=scope)  # type: ignore[arg-type]
        return store.build(store.iter_vault_pages())
    except Exception as exc:
        return {"ok": False, "code": _stable_code(exc)}


def _safe_index_result(result: Mapping[str, object]) -> dict[str, object]:
    return {key: result[key] for key in ("ok", "state", "scope", "page_count", "passage_count", "fingerprint", "operation") if key in result}


def _stable_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]*", code) else "raw_replace_failed"


__all__ = [
    "RawReplacementError",
    "RawReplacementService",
]
