"""Stable archive manifests and path/hash validation."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.archive_models import ArchiveError, ArchiveItem, ArchiveManifest


def content_hash(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def vault_relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ArchiveError("path_escape", "archive path must remain inside the vault")
    return resolved.relative_to(resolved_root).as_posix()


def stable_manifest_yaml(manifest: ArchiveManifest) -> str:
    # safe_dump preserves insertion order; ArchiveManifest.to_dict owns that order.
    return yaml.safe_dump(manifest.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False)


def write_manifest(bundle: Path, manifest: ArchiveManifest) -> Path:
    bundle.mkdir(parents=True, exist_ok=True)
    target = bundle / "manifest.yaml"
    if target.exists():
        raise ArchiveError("immutable_bundle", "a committed archive manifest cannot be modified")
    target.write_text(stable_manifest_yaml(manifest), encoding="utf-8", newline="\n")
    return target


def load_manifest(bundle: Path) -> ArchiveManifest:
    path = bundle / "manifest.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ArchiveError("archive_manifest_invalid", "archive manifest cannot be read") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ArchiveError("archive_manifest_invalid", "archive manifest schema is invalid")
    try:
        items = tuple(
            ArchiveItem(
                original_path=str(item["original_path"]), archive_path=str(item["archive_path"]),
                content_hash=str(item["content_hash"]), kind=item["kind"],
                dependencies=tuple(map(str, item.get("dependencies", []))), passage_ids=tuple(map(str, item.get("passage_ids", []))),
            )
            for item in payload["items"]
        )
        reason = str(payload["reason"])
        if reason not in ("superseded", "deprecated", "retention", "migration", "manual"):
            raise ArchiveError("archive_manifest_invalid", "archive manifest has an invalid reason")
        manifest = ArchiveManifest(
            archive_id=str(payload["archive_id"]), operation_id=str(payload["operation_id"]),
            reason=reason, archived_at=str(payload["archived_at"]), items=items,
            actor=str(payload.get("actor", "unknown")), replaced_by=payload.get("replaced_by"),
            restorable=bool(payload.get("restorable", True)), schema_version=int(payload["schema_version"]),
            dependencies=tuple(map(str, payload.get("dependencies", []))), passage_ids=tuple(map(str, payload.get("passage_ids", []))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveError("archive_manifest_invalid", "archive manifest has invalid fields") from exc
    if any(
        not _safe_relative(item.original_path)
        or item.original_path != item.archive_path
        or not item.original_path.startswith(("wiki/", "raw/"))
        for item in manifest.items
    ):
        raise ArchiveError("archive_manifest_invalid", "archive manifest contains an unsafe path")
    return manifest


def verify_bundle(root: Path, bundle: Path) -> ArchiveManifest:
    manifest = load_manifest(bundle)
    if bundle.name != manifest.archive_id:
        raise ArchiveError("archive_manifest_invalid", "bundle name does not match archive id")
    for item in manifest.items:
        payload = bundle / item.archive_path
        if not payload.is_file() or not payload.resolve().is_relative_to(bundle.resolve()):
            raise ArchiveError("archive_payload_missing", f"missing archive payload: {item.original_path}")
        if content_hash(payload) != item.content_hash:
            raise ArchiveError("archive_hash_mismatch", f"archive payload hash differs: {item.original_path}")
    return manifest


def manifest_digest(manifest: ArchiveManifest) -> str:
    return "sha256:" + sha256(stable_manifest_yaml(manifest).encode("utf-8")).hexdigest()


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and "\\" not in value and all(part not in {"", ".", ".."} for part in path.parts)
