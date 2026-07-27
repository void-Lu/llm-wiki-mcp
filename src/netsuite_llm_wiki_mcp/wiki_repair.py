"""Auditable repair planning for stale wiki pages.

The linter remains read-only.  This module separates discovery from mutation,
requires explicit action selection, and never writes beneath ``raw/``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry
from netsuite_llm_wiki_mcp.wikilinks import normalize_wikilinks, wikilink_targets

_STRUCTURAL_PAGE_NAMES = {"index.md", "log.md", "overview.md"}
_STABLE_ID_FIELDS = ("source_id", "stable_source_id", "stable_id")
_AUDIT_PATH = Path(".llm-wiki/repair-audit.jsonl")


@dataclass(frozen=True)
class RepairAction:
    action_id: str
    kind: str
    page_path: str
    expected_hash: str
    evidence: tuple[dict[str, str], ...] = ()
    writes: tuple[str, ...] = ()
    requires_selection: bool = True
    replacements: dict[str, str] = field(default_factory=dict)
    archive_path: str = ""
    reason: str = ""
    diff: str = ""


@dataclass(frozen=True)
class RepairPlan:
    actions: tuple[RepairAction, ...]
    findings: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class ApplyReport:
    ok: bool
    applied: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]
    audit_path: str
    raw_writes: int
    raw_unchanged: bool
    index_refresh: dict[str, Any] | None


def prepare_wiki_repair(
    vault_root: str | Path,
    *,
    migrations: Mapping[str, str] | None = None,
    today: date | None = None,
) -> RepairPlan:
    """Build a read-only repair plan from deterministic evidence."""
    root = Path(vault_root).expanduser().resolve()
    plan_date = today or date.today()
    migration_map = {
        _relative_text(old): _relative_text(new)
        for old, new in (migrations or {}).items()
    }
    manifest_records = _manifest_records(root)
    raw_hashes = _raw_hash_index(root)
    actions: list[RepairAction] = []
    findings: list[dict[str, str]] = []
    pages = _active_wiki_pages(root)
    wiki_targets = _wiki_target_index(root, pages)

    for page in pages:
        text = page.read_text(encoding="utf-8")
        frontmatter, _body = split_frontmatter(text)
        relative = page.relative_to(root).as_posix()
        expected_hash = _bytes_hash(text.encode("utf-8"))
        generated = frontmatter.get("generated") is True
        sources = _source_values(frontmatter.get("sources"))
        missing_sources = [
            source
            for source in sources
            if source.startswith("raw/") and not _source_exists(root, frontmatter, source)
        ]

        if generated and page.name not in _STRUCTURAL_PAGE_NAMES:
            action, page_findings = _generated_page_action(
                root=root,
                relative=relative,
                expected_hash=expected_hash,
                frontmatter=frontmatter,
                sources=sources,
                missing_sources=missing_sources,
                migrations=migration_map,
                manifest_records=manifest_records,
                raw_hashes=raw_hashes,
                plan_date=plan_date,
            )
            findings.extend(page_findings)
            if action is not None:
                actions.append(action)
            continue

        if not generated:
            suggestion = _manual_link_suggestion(
                page=page,
                relative=relative,
                text=text,
                expected_hash=expected_hash,
                root=root,
                targets=wiki_targets,
            )
            if suggestion is not None:
                actions.append(suggestion)

    return RepairPlan(actions=tuple(actions), findings=tuple(findings))


def apply_wiki_repair(
    vault_root: str | Path,
    plan: RepairPlan,
    selected_action_ids: Sequence[str],
    *,
    generated_replacements: Mapping[str, str] | None = None,
) -> ApplyReport:
    """Apply explicitly selected actions after revalidating all preconditions."""
    root = Path(vault_root).expanduser().resolve()
    selected = set(selected_action_ids)
    replacements = generated_replacements or {}
    raw_before = _tree_hash(root / "raw")
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []

    known_action_ids = {action.action_id for action in plan.actions}
    for action_id in sorted(selected - known_action_ids):
        rejection = {
            "action_id": action_id,
            "code": "unknown_action_id",
        }
        rejected.append(rejection)
        audit_records.append(_apply_audit_record("rejected", rejection))

    for action in plan.actions:
        if action.action_id not in selected:
            skip = {"action_id": action.action_id, "code": "not_selected"}
            skipped.append(skip)
            audit_records.append(_audit_record(action, "skipped", skip))
            continue
        rejection = _validate_action(root, action)
        if rejection is not None:
            rejected.append(rejection)
            audit_records.append(_audit_record(action, "rejected", rejection))
            continue

        page = root / Path(action.page_path)
        current_text = page.read_text(encoding="utf-8")
        current_hash = _bytes_hash(current_text.encode("utf-8"))
        if current_hash != action.expected_hash:
            rejection = {
                "action_id": action.action_id,
                "code": "page_changed",
                "path": action.page_path,
                "expected_hash": action.expected_hash,
                "actual_hash": current_hash,
            }
            rejected.append(rejection)
            audit_records.append(_audit_record(action, "rejected", rejection))
            continue

        try:
            if action.kind == "relink_source":
                result = _apply_relink(root, action, replacements.get(action.action_id))
            elif action.kind == "archive_generated":
                result = _apply_archive(root, action, current_text)
            elif action.kind == "suggest_manual_link":
                result = {
                    "action_id": action.action_id,
                    "code": "manual_rewrite_not_explicit",
                    "path": action.page_path,
                }
            elif action.kind == "rewrite_manual_link":
                result = _apply_manual_rewrite(root, action, current_text)
            else:
                result = {
                    "action_id": action.action_id,
                    "code": "unsupported_action_kind",
                    "path": action.page_path,
                }
        except OSError as exc:
            result = {
                "action_id": action.action_id,
                "code": "write_failed",
                "path": action.page_path,
                "error": str(exc),
            }

        if result.get("ok") is True:
            applied.append(result)
            audit_records.append(_audit_record(action, "applied", result))
        else:
            rejected.append(result)
            audit_records.append(_audit_record(action, "rejected", result))

    # Persist action outcomes before follow-up maintenance.  A refresh or wiki
    # log failure must never erase the audit trail for writes already applied.
    pending_audit = list(audit_records)
    audit_failure = _try_append_audit(root, pending_audit)
    if audit_failure is None:
        pending_audit.clear()

    index_refresh: dict[str, Any] | None = None
    if applied:
        try:
            index_refresh = refresh_indexes(root)
        except OSError as exc:
            index_refresh = {
                "ok": False,
                "code": "index_refresh_failed",
                "error": str(exc),
            }
        if index_refresh.get("ok") is not True:
            pending_audit.append(
                _apply_audit_record(
                    "failed",
                    {
                        "code": "index_refresh_failed",
                        "result": index_refresh,
                    },
                )
            )
        try:
            append_log_entry(
                root,
                WikiLogEntry(
                    operation="wiki_repair",
                    title=f"Applied {len(applied)} wiki repair action(s)",
                    paths=[
                        str(item.get("new_path") or item.get("path") or "")
                        for item in applied
                        if item.get("new_path") or item.get("path")
                    ],
                    status="ok" if index_refresh.get("ok") is True else "partial",
                ),
            )
        except OSError as exc:
            log_failure = {
                "action_id": "",
                "code": "wiki_log_failed",
                "error": str(exc),
            }
            rejected.append(log_failure)
            pending_audit.append(_apply_audit_record("failed", log_failure))

    if pending_audit:
        audit_failure = _try_append_audit(root, pending_audit)
    if audit_failure is not None:
        rejected.append(audit_failure)

    raw_after = _tree_hash(root / "raw")
    raw_unchanged = raw_before == raw_after
    ok = (
        not rejected
        and raw_unchanged
        and (index_refresh is None or index_refresh.get("ok") is True)
    )
    return ApplyReport(
        ok=ok,
        applied=tuple(applied),
        rejected=tuple(rejected),
        skipped=tuple(skipped),
        audit_path=_AUDIT_PATH.as_posix(),
        raw_writes=0,
        raw_unchanged=raw_unchanged,
        index_refresh=index_refresh,
    )


def _generated_page_action(
    *,
    root: Path,
    relative: str,
    expected_hash: str,
    frontmatter: dict[str, Any],
    sources: list[str],
    missing_sources: list[str],
    migrations: Mapping[str, str],
    manifest_records: list[dict[str, str]],
    raw_hashes: dict[str, list[str]],
    plan_date: date,
) -> tuple[RepairAction | None, list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    if not sources:
        return _archive_action(relative, expected_hash, plan_date), findings
    if not missing_sources:
        return None, findings

    relinked: dict[str, str] = {}
    evidence: list[dict[str, str]] = []
    ambiguous = False
    for old_path in missing_sources:
        saved_hashes = _saved_source_values(
            frontmatter,
            old_path,
            sources,
            mapping_fields=("source_hashes", "source_sha256s"),
            scalar_fields=("source_hash", "source_sha256"),
        )
        saved_ids = _saved_source_values(
            frontmatter,
            old_path,
            sources,
            mapping_fields=("source_ids", "stable_source_ids"),
            scalar_fields=_STABLE_ID_FIELDS,
        )
        new_path, proof, candidates = _source_relocation(
            root,
            old_path,
            migrations,
            manifest_records,
            raw_hashes,
            saved_hashes=saved_hashes,
            saved_ids=saved_ids,
            allow_directory=_allows_source_directory(frontmatter),
        )
        if new_path and proof:
            relinked[old_path] = new_path
            evidence.append(proof)
        elif candidates:
            ambiguous = True
            findings.append(
                {
                    "code": "ambiguous_source_relocation",
                    "path": relative,
                    "source": old_path,
                    "candidates": ",".join(candidates),
                }
            )
        else:
            findings.append(
                {
                    "code": "source_relocation_unproven",
                    "path": relative,
                    "source": old_path,
                }
            )

    if len(relinked) == len(missing_sources):
        action_id = _action_id("relink_source", relative, relinked, evidence)
        return (
            RepairAction(
                action_id=action_id,
                kind="relink_source",
                page_path=relative,
                expected_hash=expected_hash,
                evidence=tuple(evidence),
                writes=(relative,),
                replacements=relinked,
                reason="all missing sources have unique deterministic evidence",
            ),
            findings,
        )

    available_sources = [
        source for source in sources if _source_exists(root, frontmatter, source)
    ]
    if not relinked and not ambiguous and not available_sources:
        return _archive_action(relative, expected_hash, plan_date), findings
    return None, findings


def _archive_action(relative: str, expected_hash: str, plan_date: date) -> RepairAction:
    archive_path = (
        Path("wiki")
        / "archives"
        / "stale"
        / f"{plan_date:%Y}"
        / f"{plan_date:%m}"
        / f"{plan_date:%d}"
        / Path(relative)
    ).as_posix()
    action_id = _action_id(
        "archive_generated",
        relative,
        {"archive_path": archive_path},
        (),
    )
    return RepairAction(
        action_id=action_id,
        kind="archive_generated",
        page_path=relative,
        expected_hash=expected_hash,
        evidence=(
            {
                "kind": "missing_source",
                "old_path": relative,
                "new_path": archive_path,
            },
        ),
        writes=(relative, archive_path),
        archive_path=archive_path,
        reason="generated page has no available or relocatable source",
    )


def _source_relocation(
    root: Path,
    old_path: str,
    migrations: Mapping[str, str],
    manifest_records: list[dict[str, str]],
    raw_hashes: dict[str, list[str]],
    *,
    saved_hashes: set[str],
    saved_ids: set[str],
    allow_directory: bool,
) -> tuple[str, dict[str, str] | None, list[str]]:
    migrated = migrations.get(old_path)
    if migrated:
        try:
            migrated_path = _safe_source_reference(root, migrated)
        except ValueError:
            return "", None, [migrated]
        if migrated_path.is_file() or (allow_directory and migrated_path.is_dir()):
            return (
                migrated,
                {
                    "kind": "explicit_migration",
                    "old_path": old_path,
                    "new_path": migrated,
                    "value": f"{old_path}->{migrated}",
                },
                [],
            )
        return "", None, [migrated]

    old_records = [item for item in manifest_records if item.get("path") == old_path]
    stable_ids = saved_ids | {
        item[field_name]
        for item in old_records
        for field_name in _STABLE_ID_FIELDS
        if item.get(field_name)
    }
    for stable_id in sorted(stable_ids):
        declared_candidates = sorted(
            {
                item["path"]
                for item in manifest_records
                if item.get("path")
                and item.get("path") != old_path
                and any(item.get(field_name) == stable_id for field_name in _STABLE_ID_FIELDS)
            }
        )
        candidates = [
            item
            for item in declared_candidates
            if _safe_source_reference(root, item).is_file()
        ]
        if len(candidates) == 1:
            return (
                candidates[0],
                {
                    "kind": "stable_source_id",
                    "old_path": old_path,
                    "new_path": candidates[0],
                    "value": stable_id,
                },
                [],
            )
        if len(candidates) > 1:
            return "", None, candidates
        if declared_candidates:
            return "", None, declared_candidates

    stored_hashes = saved_hashes | {
        item["stored_sha256"]
        for item in old_records
        if item.get("stored_sha256")
    }
    for stored_hash in sorted(stored_hashes):
        candidates = sorted(set(raw_hashes.get(stored_hash, [])) - {old_path})
        if len(candidates) == 1:
            return (
                candidates[0],
                {
                    "kind": "content_hash",
                    "old_path": old_path,
                    "new_path": candidates[0],
                    "value": stored_hash,
                },
                [],
            )
        if len(candidates) > 1:
            return "", None, candidates
    return "", None, []


def _manual_link_suggestion(
    *,
    page: Path,
    relative: str,
    text: str,
    expected_hash: str,
    root: Path,
    targets: dict[str, list[str]],
) -> RepairAction | None:
    replacements: dict[str, str] = {}
    for target in wikilink_targets(text):
        if _wikilink_resolves(target, page, root, targets):
            continue
        key = _canonical_stem(Path(target).stem)
        if not key:
            continue
        matches = [item for item in targets.get(key, []) if item != relative]
        if len(matches) == 1:
            replacements[target] = Path(matches[0]).stem
    if not replacements:
        return None

    rewritten = normalize_wikilinks(text, replacements)
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            rewritten.splitlines(keepends=True),
            fromfile=relative,
            tofile=relative,
        )
    )
    evidence = tuple(
        {
            "kind": "unique_canonical_wikilink_target",
            "old_path": old,
            "new_path": new,
        }
        for old, new in sorted(replacements.items())
    )
    action_id = _action_id("suggest_manual_link", relative, replacements, evidence)
    return RepairAction(
        action_id=action_id,
        kind="suggest_manual_link",
        page_path=relative,
        expected_hash=expected_hash,
        evidence=evidence,
        writes=(relative,),
        replacements=replacements,
        reason="manual pages require an explicitly upgraded rewrite action",
        diff=diff,
    )


def _apply_relink(
    root: Path,
    action: RepairAction,
    replacement_text: str | None,
) -> dict[str, Any]:
    if replacement_text is None:
        return {
            "action_id": action.action_id,
            "code": "replacement_generation_required",
            "path": action.page_path,
        }
    frontmatter, body = split_frontmatter(replacement_text)
    if frontmatter.get("generated") is not True or not body.strip():
        return {
            "action_id": action.action_id,
            "code": "replacement_generation_invalid",
            "path": action.page_path,
        }
    replacement_sources = _source_values(frontmatter.get("sources"))
    for old_path, new_path in action.replacements.items():
        if (
            old_path in replacement_sources
            or new_path not in replacement_sources
            or not _source_exists(root, frontmatter, new_path)
        ):
            return {
                "action_id": action.action_id,
                "code": "replacement_source_validation_failed",
                "path": action.page_path,
                "old_path": old_path,
                "new_path": new_path,
            }
    page = root / Path(action.page_path)
    _atomic_write(page, replacement_text)
    return {
        "ok": True,
        "action_id": action.action_id,
        "kind": action.kind,
        "path": action.page_path,
        "before_hash": action.expected_hash,
        "after_hash": _bytes_hash(replacement_text.encode("utf-8")),
        "evidence": list(action.evidence),
    }


def _apply_archive(
    root: Path,
    action: RepairAction,
    current_text: str,
) -> dict[str, Any]:
    page = root / Path(action.page_path)
    archive = _collision_safe_archive(root, action.archive_path, action.expected_hash)
    frontmatter, body = split_frontmatter(current_text)
    frontmatter.update(
        {
            "archived": True,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "archived_from": action.page_path,
            "archive_reason": action.reason,
            "archive_original_sha256": action.expected_hash,
        }
    )
    archive_text = _render_page(frontmatter, body)
    _atomic_write(archive, archive_text)
    try:
        page.unlink()
    except OSError:
        archive.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "action_id": action.action_id,
        "kind": action.kind,
        "path": action.page_path,
        "old_path": action.page_path,
        "new_path": archive.relative_to(root).as_posix(),
        "before_hash": action.expected_hash,
        "after_hash": _bytes_hash(archive_text.encode("utf-8")),
        "evidence": list(action.evidence),
    }


def _apply_manual_rewrite(
    root: Path,
    action: RepairAction,
    current_text: str,
) -> dict[str, Any]:
    targets = _wiki_target_index(root, _active_wiki_pages(root))
    page = root / Path(action.page_path)
    missing_targets = sorted(
        {
            target
            for target in action.replacements.values()
            if not _wikilink_resolves(target, page, root, targets)
        }
    )
    if missing_targets:
        return {
            "action_id": action.action_id,
            "code": "manual_target_missing",
            "path": action.page_path,
            "targets": missing_targets,
        }
    rewritten = normalize_wikilinks(current_text, action.replacements)
    if rewritten == current_text:
        return {
            "action_id": action.action_id,
            "code": "manual_rewrite_no_change",
            "path": action.page_path,
        }
    _atomic_write(page, rewritten)
    return {
        "ok": True,
        "action_id": action.action_id,
        "kind": action.kind,
        "path": action.page_path,
        "before_hash": action.expected_hash,
        "after_hash": _bytes_hash(rewritten.encode("utf-8")),
        "diff": action.diff,
        "evidence": list(action.evidence),
    }


def _validate_action(root: Path, action: RepairAction) -> dict[str, Any] | None:
    if not _is_safe_wiki_path(root, action.page_path, allow_archive=False):
        return {
            "action_id": action.action_id,
            "code": "unsafe_write_path",
            "path": action.page_path,
        }
    if action.kind == "archive_generated" and not _is_safe_wiki_path(
        root, action.archive_path, allow_archive=True
    ):
        return {
            "action_id": action.action_id,
            "code": "unsafe_write_path",
            "path": action.archive_path,
        }
    if any(
        not _is_safe_wiki_path(
            root,
            relative,
            allow_archive=relative.startswith("wiki/archives/"),
        )
        for relative in action.writes
    ):
        return {
            "action_id": action.action_id,
            "code": "unsafe_write_path",
            "path": action.page_path,
        }
    page = root / Path(action.page_path)
    if not page.is_file():
        return {
            "action_id": action.action_id,
            "code": "page_missing",
            "path": action.page_path,
        }
    return None


def _is_safe_wiki_path(root: Path, relative: str, *, allow_archive: bool) -> bool:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "wiki":
        return False
    if not allow_archive and path.parts[:2] == ("wiki", "archives"):
        return False
    resolved = (root / path).resolve()
    return resolved.is_relative_to(root / "wiki")


def _source_exists(root: Path, frontmatter: Mapping[str, Any], source: str) -> bool:
    try:
        path = _safe_raw_reference(root, source)
    except ValueError:
        return False
    if path.is_file():
        return True
    return _allows_source_directory(frontmatter) and path.is_dir()


def _allows_source_directory(frontmatter: Mapping[str, Any]) -> bool:
    return (
        frontmatter.get("type") == "source_index"
        and frontmatter.get("index_kind") == "lightweight_source_index"
    )


def _safe_raw_reference(root: Path, source: str) -> Path:
    relative = Path(source)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:1] != ("raw",)
    ):
        raise ValueError("source path is outside raw")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root / "raw"):
        raise ValueError("source path is outside raw")
    return resolved


def _safe_source_reference(root: Path, source: str) -> Path:
    resolved = _safe_raw_reference(root, source)
    if Path(source).parts[:2] != ("raw", "sources"):
        raise ValueError("source path is outside raw/sources")
    return resolved


def _manifest_records(root: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    cache_root = root / ".llm-wiki" / "ingest-cache"
    if not cache_root.exists():
        return records
    for path in sorted(cache_root.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        manifest = data.get("manifest") if isinstance(data, dict) else None
        if not isinstance(manifest, list):
            continue
        for value in manifest:
            if not isinstance(value, dict) or not value.get("path"):
                continue
            record = {
                key: str(value[key])
                for key in ("path", "stored_sha256", *_STABLE_ID_FIELDS)
                if value.get(key)
            }
            try:
                record["path"] = _safe_source_reference(root, record["path"]).relative_to(root).as_posix()
            except ValueError:
                continue
            records.append(record)
    return records


def _raw_hash_index(root: Path) -> dict[str, list[str]]:
    hashes: dict[str, list[str]] = {}
    raw = root / "raw" / "sources"
    if not raw.exists():
        return hashes
    for path in sorted(item for item in raw.rglob("*") if item.is_file()):
        digest = _bytes_hash(path.read_bytes())
        hashes.setdefault(digest, []).append(path.relative_to(root).as_posix())
    return hashes


def _active_wiki_pages(root: Path) -> list[Path]:
    wiki = root / "wiki"
    if not wiki.exists():
        return []
    return [
        path
        for path in sorted(wiki.rglob("*.md"))
        if path.relative_to(root).parts[:2] != ("wiki", "archives")
    ]


def _wiki_target_index(root: Path, pages: list[Path]) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for page in pages:
        if page.name in _STRUCTURAL_PAGE_NAMES:
            continue
        relative = page.relative_to(root).as_posix()
        targets.setdefault(_canonical_stem(page.stem), []).append(relative)
    return targets


def _wikilink_resolves(
    target: str,
    page: Path,
    root: Path,
    targets: Mapping[str, list[str]],
) -> bool:
    target_path = Path(target)
    if target_path.suffix != ".md":
        target_path = target_path.with_suffix(".md")
    for candidate in (
        (page.parent / target_path).resolve(),
        (root / "wiki" / target_path).resolve(),
        (root / target_path).resolve(),
    ):
        if candidate.is_relative_to(root) and candidate.is_file():
            return True
    exact_stem = Path(target).stem.casefold()
    return any(
        Path(relative).stem.casefold() == exact_stem
        for values in targets.values()
        for relative in values
    )


def _canonical_stem(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _source_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _saved_source_values(
    frontmatter: Mapping[str, Any],
    source: str,
    all_sources: Sequence[str],
    *,
    mapping_fields: Sequence[str],
    scalar_fields: Sequence[str],
) -> set[str]:
    values: set[str] = set()
    for field_name in mapping_fields:
        mapping = frontmatter.get(field_name)
        if isinstance(mapping, dict) and mapping.get(source):
            values.add(str(mapping[source]))
    if len(all_sources) == 1:
        for field_name in scalar_fields:
            value = frontmatter.get(field_name)
            if isinstance(value, str) and value:
                values.add(value)
    return values


def _relative_text(value: str) -> str:
    return Path(value.replace("\\", "/")).as_posix()


def _action_id(
    kind: str,
    page_path: str,
    replacements: Mapping[str, str],
    evidence: Sequence[Mapping[str, str]],
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "page_path": page_path,
            "replacements": dict(sorted(replacements.items())),
            "evidence": [dict(sorted(item.items())) for item in evidence],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _collision_safe_archive(root: Path, archive_path: str, page_hash: str) -> Path:
    target = root / Path(archive_path)
    if not target.exists():
        return target
    candidate = target.with_name(f"{target.stem}-{page_hash[:12]}{target.suffix}")
    counter = 2
    while candidate.exists():
        candidate = target.with_name(
            f"{target.stem}-{page_hash[:12]}-{counter}{target.suffix}"
        )
        counter += 1
    return candidate


def _render_page(frontmatter: Mapping[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(
        dict(frontmatter),
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    return f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_audit(root: Path, records: list[dict[str, Any]]) -> None:
    path = root / _AUDIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _try_append_audit(
    root: Path,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        _append_audit(root, records)
    except OSError as exc:
        return {
            "action_id": "",
            "code": "audit_write_failed",
            "path": _AUDIT_PATH.as_posix(),
            "error": str(exc),
        }
    return None


def _audit_record(
    action: RepairAction,
    status: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "action": asdict(action),
        "result": dict(result),
        "raw_writes": 0,
    }


def _apply_audit_record(
    status: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "result": dict(result),
        "raw_writes": 0,
    }


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(file_path.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
