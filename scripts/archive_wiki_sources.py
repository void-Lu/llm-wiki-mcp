#!/usr/bin/env python3
"""一次性归档 wiki/sources，并清理活动 Wiki 页面的旧溯源字段。

默认只做预检和生成计划；只有显式传入 ``--apply`` 才会写页面、取消旧
队列任务并提交 archive bundle。脚本不会写入、移动或删除 raw 文件。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Iterable

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMESPACE = "wiki/sources"
REMOVED_FIELDS = ("source_capsules", "source_capsule")
ALGORITHM_VERSION = "raw-source-remap-v1"

if str(REPOSITORY_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from archive.archive_planner import ArchivePlanner  # noqa: E402
from archive.archive_service import ArchiveService  # noqa: E402
from wiki.generation_queue import DISABLED_JOB_TYPES, GenerationQueue  # noqa: E402
from wiki.knowledge_dependencies import KnowledgeDependencies  # noqa: E402
from retrieval.retrieval_index import RetrievalIndexStore  # noqa: E402
from retrieval.vector_index import VectorIndexStore  # noqa: E402
from wiki.wiki_io import split_frontmatter  # noqa: E402
from wiki.wiki_query import vector_index_records  # noqa: E402


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_hash(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _is_under(root: Path, path: Path, prefix: str) -> bool:
    try:
        return _relative(root, path).startswith(prefix.rstrip("/") + "/")
    except ValueError:
        return False


def _normal_hash(value: object) -> str:
    text = str(value or "").strip().casefold()
    return text.removeprefix("sha256:")


def _hash_matches(path: Path, expected: object) -> bool:
    normalized = _normal_hash(expected)
    return bool(normalized) and normalized == _normal_hash(_file_hash(path))


def _raw_hash_tree(root: Path) -> dict[str, str]:
    raw_root = root / "raw"
    if not raw_root.exists():
        return {}
    return {
        _relative(root, path): _file_hash(path)
        for path in sorted(raw_root.rglob("*"))
        if path.is_file()
    }


def _source_files(root: Path) -> list[Path]:
    source_root = root / SOURCE_NAMESPACE
    if not source_root.exists():
        return []
    return [path for path in sorted(source_root.rglob("*")) if path.is_file()]


def _raw_files(root: Path) -> list[Path]:
    raw_root = root / "raw" / "sources"
    if not raw_root.exists():
        return []
    return [path for path in sorted(raw_root.rglob("*")) if path.is_file()]


def _active_wiki_pages(root: Path) -> list[Path]:
    wiki_root = root / "wiki"
    if not wiki_root.exists():
        return []
    return [
        path
        for path in sorted(wiki_root.rglob("*.md"))
        if path.is_file() and not _is_under(root, path, SOURCE_NAMESPACE)
    ]


def _as_strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).replace("\\", "/") for item in value if isinstance(item, str)]
    if isinstance(value, str) and value.strip():
        return [value.replace("\\", "/")]
    return []


def _source_hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(path).replace("\\", "/"): str(digest)
        for path, digest in value.items()
        if isinstance(path, str) and isinstance(digest, str)
    }


def _safe_raw_path(root: Path, value: object) -> tuple[str, Path] | None:
    if not isinstance(value, str):
        return None
    relative = value.replace("\\", "/")
    candidate = (root / relative).resolve()
    if not relative.startswith("raw/sources/") or not candidate.is_file() or not _is_under(root, candidate, "raw/sources"):
        return None
    return relative, candidate


def _normalized_component(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _capsule_directory(relative: str) -> tuple[str, ...]:
    parts = Path(relative).parts
    if len(parts) < 3 or parts[:2] != ("wiki", "sources"):
        return ()
    parent = list(parts[2:-1])
    if parent and parent[-1].casefold() == "capsules":
        parent.pop()
    return tuple(_normalized_component(part) for part in parent if part)


def _raw_directory(relative: str) -> tuple[str, ...]:
    parts = Path(relative).parts
    if len(parts) < 4 or parts[:2] != ("raw", "sources"):
        return ()
    return tuple(_normalized_component(part) for part in parts[2:-1] if part)


def _raw_candidates(raw_files: Iterable[Path], root: Path, capsule_relative: str) -> list[tuple[str, Path]]:
    capsule_stem = _normalized_component(Path(capsule_relative).stem)
    capsule_directory = _capsule_directory(capsule_relative)
    return [
        (_relative(root, path), path)
        for path in raw_files
        if _normalized_component(path.stem) == capsule_stem and _raw_directory(_relative(root, path)) == capsule_directory
    ]


def _capsule_mapping(root: Path, capsule_relative: str, raw_files: list[Path]) -> dict[str, Any]:
    capsule = root / capsule_relative
    result: dict[str, Any] = {
        "capsule": capsule_relative,
        "capsule_hash": _file_hash(capsule) if capsule.is_file() else None,
        "candidates": [],
        "raw_path": None,
        "raw_hash": None,
        "confidence": None,
        "algorithm": ALGORITHM_VERSION,
        "status": "unresolved",
        "reason": None,
    }
    if not capsule.is_file():
        result["reason"] = "capsule_not_found"
        return result

    try:
        frontmatter, _ = split_frontmatter(capsule.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        frontmatter = {}

    exact_path = _safe_raw_path(root, frontmatter.get("source_path"))
    exact_hash = frontmatter.get("source_hash")
    if exact_path is not None and _hash_matches(exact_path[1], exact_hash):
        result.update(
            {
                "raw_path": exact_path[0],
                "raw_hash": _file_hash(exact_path[1]),
                "confidence": "exact_frontmatter",
                "status": "resolved",
            }
        )
        return result

    candidates = _raw_candidates(raw_files, root, capsule_relative)
    result["candidates"] = [relative for relative, _ in candidates]
    if exact_path is not None and exact_path[1].is_file() and not _hash_matches(exact_path[1], exact_hash):
        result["reason"] = "exact_source_hash_mismatch"
    if len(candidates) == 1:
        relative, path = candidates[0]
        result.update(
            {
                "raw_path": relative,
                "raw_hash": _file_hash(path),
                "confidence": "fuzzy_filename_directory",
                "status": "resolved",
                "reason": result["reason"] or "frontmatter_missing_or_invalid",
            }
        )
        return result
    if not candidates:
        result["reason"] = result["reason"] or "no_unique_filename_directory_match"
    else:
        result["reason"] = result["reason"] or "multiple_filename_directory_matches"
    return result


def _serialize_page(frontmatter: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{yaml_text}\n---\n\n{body.strip()}\n"


def _page_update(root: Path, path: Path, raw_files: list[Path]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    relative = _relative(root, path)
    original = path.read_bytes()
    current_hash = _sha256_bytes(original)
    try:
        frontmatter, body = split_frontmatter(original.decode("utf-8"))
    except (UnicodeDecodeError, OSError):
        return {"page": relative, "status": "unresolved", "reason": "page_unreadable"}, None

    capsules = _as_strings(frontmatter.get("source_capsules"))
    capsules.extend(value for value in _as_strings(frontmatter.get("source_capsule")) if value not in capsules)
    if not capsules:
        return {"page": relative, "status": "not_applicable", "old_hash": current_hash}, None

    existing_sources = _as_strings(frontmatter.get("sources"))
    existing_hashes = _source_hashes(frontmatter.get("source_hashes"))
    valid_existing: list[str] = []
    provenance: dict[str, str] = {}
    existing_issues: list[dict[str, Any]] = []
    for source in existing_sources:
        safe = _safe_raw_path(root, source)
        if safe is None:
            existing_issues.append({"source": source, "reason": "existing_raw_source_missing_or_invalid"})
            continue
        expected = existing_hashes.get(source)
        if expected and not _hash_matches(safe[1], expected):
            existing_issues.append({"source": source, "reason": "existing_raw_source_hash_mismatch", "current_hash": _file_hash(safe[1])})
            continue
        valid_existing.append(source)
        provenance[source] = expected or _file_hash(safe[1])

    mappings = [_capsule_mapping(root, capsule, raw_files) for capsule in capsules]
    unresolved = [item for item in mappings if item["status"] != "resolved"]
    conflicts: list[dict[str, Any]] = []
    updated_sources = list(valid_existing)
    for mapping in mappings:
        raw_path = mapping.get("raw_path")
        raw_hash = mapping.get("raw_hash")
        if not raw_path or not raw_hash:
            continue
        if raw_path in provenance and not _hash_matches(root / raw_path, provenance[raw_path]):
            conflicts.append({"capsule": mapping["capsule"], "raw_path": raw_path, "reason": "existing_source_hash_wins"})
            continue
        if raw_path not in updated_sources:
            updated_sources.append(raw_path)
        provenance.setdefault(raw_path, raw_hash)

    new_frontmatter = dict(frontmatter)
    for field in REMOVED_FIELDS:
        new_frontmatter.pop(field, None)
    if updated_sources:
        new_frontmatter["sources"] = updated_sources
        new_frontmatter["source_hashes"] = provenance
    elif "sources" in frontmatter or "source_hashes" in frontmatter:
        new_frontmatter.pop("sources", None)
        new_frontmatter.pop("source_hashes", None)
    if unresolved or conflicts or existing_issues:
        new_frontmatter["freshness"] = "review_required"
        new_frontmatter["source_mapping_status"] = "unresolved"
        status = "unresolved"
    else:
        new_frontmatter["source_mapping_status"] = "resolved"
        status = "resolved"
    updated = _serialize_page(new_frontmatter, body).encode("utf-8")
    new_hash = _sha256_bytes(updated)
    audit = {
        "page": relative,
        "old_hash": current_hash,
        "new_hash": new_hash,
        "status": status,
        "capsules": mappings,
        "existing_sources": existing_sources,
        "existing_issues": existing_issues,
        "conflicts": conflicts,
        "final_sources": updated_sources,
    }
    update = None if updated == original else {"path": path, "old": original, "new": updated, "old_hash": current_hash, "new_hash": new_hash}
    return audit, update


def _page_plan(root: Path, raw_files: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    audits: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for page in _active_wiki_pages(root):
        audit, update = _page_update(root, page, raw_files)
        if audit.get("status") != "not_applicable":
            audits.append(audit)
        if update is not None:
            updates.append(update)
    return audits, updates


def _audit_document(root: Path, source_files: list[Path], page_audits: list[dict[str, Any]], raw_before: dict[str, str]) -> str:
    payload = {
        "schema_version": 1,
        "kind": "wiki_sources_archive_audit",
        "algorithm": ALGORITHM_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "namespace": SOURCE_NAMESPACE,
        "archive_policy": {"reason": "retention", "restorable": False, "visibility": "archive_only"},
        "source_files": [{"path": _relative(root, path), "content_hash": _file_hash(path)} for path in source_files],
        "page_mappings": page_audits,
        "raw_hash_tree_before": raw_before,
        "summary": {
            "source_files": len(source_files),
            "pages_with_removed_fields": len(page_audits),
            "pages_updated": sum(1 for item in page_audits if item.get("old_hash") != item.get("new_hash")),
            "unresolved_pages": sum(1 for item in page_audits if item.get("status") == "unresolved"),
            "unresolved_capsules": sum(len([capsule for capsule in item.get("capsules", []) if capsule.get("status") != "resolved"]) for item in page_audits),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _public_plan(plan: Any) -> dict[str, Any]:
    payload = plan.to_dict()
    payload["attachments"] = [item.to_dict() for item in plan.attachments]
    return payload


def _atomic_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _apply_page_updates(updates: list[dict[str, Any]]) -> None:
    applied: list[dict[str, Any]] = []
    try:
        for item in updates:
            path = item["path"]
            if _sha256_bytes(path.read_bytes()) != item["old_hash"]:
                raise RuntimeError(f"page changed during preflight: {path}")
            _atomic_write(path, item["new"])
            applied.append(item)
    except BaseException:
        _rollback_page_updates(applied)
        raise


def _rollback_page_updates(updates: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    for item in reversed(updates):
        path = item["path"]
        try:
            if path.is_file() and _sha256_bytes(path.read_bytes()) == item["new_hash"]:
                _atomic_write(path, item["old"])
            else:
                errors.append(str(path))
        except OSError:
            errors.append(str(path))
    if errors:
        raise RuntimeError("page rollback encountered conflicts: " + ", ".join(errors))


def _refresh_dependencies(root: Path, updates: list[dict[str, Any]]) -> None:
    dependencies = KnowledgeDependencies(root)
    for item in updates:
        path = item["path"]
        frontmatter, _ = split_frontmatter(item["new"].decode("utf-8"))
        sources = _as_strings(frontmatter.get("sources"))
        hashes = _source_hashes(frontmatter.get("source_hashes"))
        dependencies.update_page(
            _relative(root, path),
            item["new_hash"],
            {source: hashes.get(source, "") for source in sources},
            generated=bool(frontmatter.get("generated")),
            maintenance=str(frontmatter.get("maintenance") or "auto"),
            lifecycle=str(frontmatter.get("lifecycle") or "active"),
            replaced_by=frontmatter.get("replaced_by"),
            freshness=str(frontmatter.get("freshness") or "fresh"),
        )


def _readonly_queue_status(root: Path) -> dict[str, Any]:
    path = root / ".llm-wiki" / "state.sqlite3"
    if not path.exists():
        return {"ok": True, "counts": {}, "state": "not_initialized"}
    try:
        with sqlite3.connect(path) as connection:
            rows = connection.execute("SELECT state, COUNT(*) FROM generation_jobs GROUP BY state").fetchall()
        return {"ok": True, "counts": {str(state): int(count) for state, count in rows}}
    except sqlite3.Error as exc:
        return {"ok": False, "code": "queue_status_unavailable", "error": str(exc)}


def _rebuild_active_index(root: Path) -> dict[str, Any]:
    store = RetrievalIndexStore(root)
    return store.build(store.iter_vault_pages())


def _vector_projection(root: Path) -> dict[str, Any]:
    try:
        records = vector_index_records(root, include_raw_sources=False)
        status = VectorIndexStore(root).status(records, include_raw_sources=False)
        return {
            "ok": True,
            "state": "stale" if status.get("state") != "fresh" else "fresh",
            "code": "vector_rebuild_skipped",
            "rebuild": "disabled_for_one_time_archive",
            "status": status,
        }
    except Exception as exc:
        return {"ok": False, "state": "stale", "code": "vector_projection_unavailable", "error": str(exc)}


def _preflight(root: Path) -> dict[str, Any]:
    source_files = _source_files(root)
    raw_before = _raw_hash_tree(root)
    page_audits, updates = _page_plan(root, _raw_files(root))
    audit_text = _audit_document(root, source_files, page_audits, raw_before)
    planner = ArchivePlanner(root)
    should_commit = bool(source_files or page_audits)
    plan = planner.archive_plan(
        [_relative(root, path) for path in source_files],
        reason="retention",
        force_namespace=SOURCE_NAMESPACE,
        restorable=False,
        attachments={"source-remap.json": audit_text},
    ) if should_commit else None
    pending = _readonly_queue_status(root)
    payload: dict[str, Any] = {
        "ok": plan is None or not plan.blockers,
        "mode": "dry-run",
        "namespace": SOURCE_NAMESPACE,
        "source_files": len(source_files),
        "page_updates": len(updates),
        "page_mappings": page_audits,
        "raw_hash_tree_before": raw_before,
        "pending_queue": pending,
        "disabled_job_types": sorted(DISABLED_JOB_TYPES),
    }
    if plan is not None:
        payload["archive_plan"] = _public_plan(plan)
        if plan.blockers:
            payload["code"] = str(plan.blockers[0].get("code", "archive_plan_blocked"))
            payload["blockers"] = list(plan.blockers)
    else:
        payload["already_archived"] = True
    return {**payload, "_source_files": source_files, "_updates": updates, "_audit": audit_text, "_raw_before": raw_before}


def run(root: Path, *, apply: bool) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "code": "invalid_vault_root", "error": str(root)}
    preflight = _preflight(root)
    private_keys = {"_source_files", "_updates", "_audit", "_raw_before"}
    if not apply:
        return {key: value for key, value in preflight.items() if key not in private_keys}
    if not preflight["ok"]:
        return {key: value for key, value in preflight.items() if key not in private_keys}

    source_files: list[Path] = preflight["_source_files"]
    updates: list[dict[str, Any]] = preflight["_updates"]
    page_updates_applied = False
    archive_committed = False
    canceled_jobs: list[str] = []
    warnings: list[dict[str, Any]] = []
    try:
        _apply_page_updates(updates)
        page_updates_applied = True
        if source_files or preflight["page_mappings"]:
            service = ArchiveService(root, actor="archive-wiki-sources")
            planned = service.plan_archive(
                [_relative(root, path) for path in source_files],
                reason="retention",
                force_namespace=SOURCE_NAMESPACE,
                restorable=False,
                attachments={"source-remap.json": preflight["_audit"]},
            )
            if not planned.get("ok"):
                raise RuntimeError(json.dumps(planned, ensure_ascii=False))
            archived = service.apply(str(planned["plan_id"]))
            if not archived.get("ok"):
                raise RuntimeError(json.dumps(archived, ensure_ascii=False))
            archive_committed = True
        else:
            archived = {"ok": True, "state": "already_archived", "archive_id": None}
        try:
            _refresh_dependencies(root, updates)
        except Exception as exc:  # noqa: BLE001 - projection is rebuildable and must not block archive
            warnings.append({"code": "knowledge_dependency_projection_failed", "error": str(exc)})
        try:
            canceled_jobs = GenerationQueue(root).supersede_job_types(set(DISABLED_JOB_TYPES), reason="wiki_sources_archived")
        except Exception as exc:  # noqa: BLE001 - legacy cleanup is non-blocking
            warnings.append({"code": "legacy_job_cleanup_failed", "error": str(exc)})
        active_index = _rebuild_active_index(root)
        raw_after = _raw_hash_tree(root)
        raw_unchanged = raw_after == preflight["_raw_before"]
        remaining = [_relative(root, path) for path in _source_files(root)]
        vector = _vector_projection(root)
        archive_index = archived.get("archive_index", {}) if isinstance(archived, dict) else {}
        archive_index_ok = bool(archive_index.get("ok", True)) if isinstance(archive_index, dict) else True
        result = {
            "ok": bool(active_index.get("ok")) and archive_index_ok and raw_unchanged and not remaining,
            "mode": "apply",
            "archive": archived,
            "archive_index": archive_index,
            "page_updates": len(updates),
            "canceled_jobs": canceled_jobs,
            "active_index": active_index,
            "vector": vector,
            "raw_unchanged": raw_unchanged,
            "remaining_source_files": remaining,
            "unresolved_pages": sum(1 for item in preflight["page_mappings"] if item.get("status") == "unresolved"),
            "warnings": warnings,
        }
        if not raw_unchanged:
            result["code"] = "raw_changed_during_archive"
        elif remaining:
            result["code"] = "source_namespace_not_empty"
        elif not active_index.get("ok"):
            result["code"] = "active_index_stale"
        elif not archive_index_ok:
            result["code"] = "archive_index_stale"
        return result
    except BaseException as exc:
        rollback_error = None
        if page_updates_applied and not archive_committed:
            try:
                _rollback_page_updates(updates)
            except BaseException as rollback_exc:
                rollback_error = str(rollback_exc)
        result = {"ok": False, "code": "archive_wiki_sources_failed", "error": str(exc), "canceled_jobs": canceled_jobs}
        if rollback_error:
            result["rollback_error"] = rollback_error
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="归档 wiki/sources 并清理旧 capsule 溯源字段")
    parser.add_argument("--vault-root", required=True, help="Obsidian vault root")
    parser.add_argument("--apply", action="store_true", help="提交归档和页面清理；省略时只做预检")
    args = parser.parse_args(argv)
    result = run(Path(args.vault_root), apply=bool(args.apply))
    print(json.dumps({key: value for key, value in result.items() if not key.startswith("_")}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
