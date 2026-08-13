"""Plan and apply removal of legacy CodeGraph Wiki artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Literal, Mapping, cast

from retrieval.retrieval_index import RetrievalIndexError, RetrievalIndexStore
from wiki.wiki_io import split_frontmatter


_CODEGRAPH_SOURCE_NAME = "codegraph"
_CODEGRAPH_RETRIEVAL_SCOPE = "project_code"
_StoreScope = Literal["active", "archive", "raw"]


@dataclass(frozen=True)
class _RemovalTargets:
    pages: tuple[Path, ...]
    directories: tuple[Path, ...]
    raw_files: tuple[Path, ...]


def _is_codegraph_page(frontmatter: Mapping[str, Any]) -> bool:
    """Return whether frontmatter has the complete legacy ownership marker."""

    return (
        frontmatter.get("generated") is True
        and frontmatter.get("managed_by") == _CODEGRAPH_SOURCE_NAME
        and frontmatter.get("source_name") == _CODEGRAPH_SOURCE_NAME
        and frontmatter.get("retrieval_scope") == _CODEGRAPH_RETRIEVAL_SCOPE
    )


def plan_codegraph_removal(vault_root: str | Path) -> dict[str, object]:
    """Return a read-only preview of legacy CodeGraph artifacts to remove."""

    root = _vault_root(vault_root)
    targets = _scan_targets(root)
    return _result(
        root,
        targets,
        dry_run=True,
        pages=targets.pages,
        directories=targets.directories,
        projection={},
    )


def apply_codegraph_removal(vault_root: str | Path) -> dict[str, object]:
    """Delete legacy CodeGraph artifacts and repair existing retrieval rows."""

    root = _vault_root(vault_root)
    targets = _scan_targets(root)
    page_paths = tuple(_relative(root, path) for path in targets.pages)
    raw_paths = tuple(_relative(root, path) for path in targets.raw_files)
    deleted_pages: list[Path] = []
    deleted_directories: list[Path] = []

    for path in targets.pages:
        if _unlinkable_file(root, path):
            path.unlink()
            deleted_pages.append(path)
    for path in targets.directories:
        if _removable_directory(root, path):
            shutil.rmtree(path)
            deleted_directories.append(path)

    projection = _delete_retrieval_rows(root, page_paths, raw_paths)
    rebuild_required = _vector_rebuild_requirements(root, page_paths, raw_paths)
    return _result(
        root,
        targets,
        dry_run=False,
        pages=tuple(deleted_pages),
        directories=tuple(deleted_directories),
        projection=projection,
        rebuild_required=rebuild_required,
    )


def _vault_root(vault_root: str | Path) -> Path:
    return Path(vault_root).expanduser().resolve()


def _scan_targets(root: Path) -> _RemovalTargets:
    pages = {
        path
        for project_root in _direct_children(root / "wiki" / "projects")
        for path in _architecture_pages(project_root / "architecture")
    }
    pages.update(
        path
        for path in _markdown_files(root / "archives" / "bundles")
        if _is_codegraph_file(path)
    )
    directories: set[Path] = set()
    raw_files: set[Path] = set()
    for project_root in _direct_children(root / "raw" / "sources" / "projects"):
        candidate = project_root / "codegraph"
        if not _removable_directory(root, candidate):
            continue
        directories.add(candidate)
        raw_files.update(path for path in candidate.rglob("*") if _unlinkable_file(root, path))
    return _RemovalTargets(
        pages=tuple(sorted(pages, key=lambda path: _relative(root, path))),
        directories=tuple(sorted(directories, key=lambda path: _relative(root, path))),
        raw_files=tuple(sorted(raw_files, key=lambda path: _relative(root, path))),
    )


def _direct_children(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir() or directory.is_symlink():
        return ()
    return tuple(sorted((path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()), key=lambda path: path.name))


def _architecture_pages(architecture: Path) -> tuple[Path, ...]:
    if not architecture.is_dir() or architecture.is_symlink():
        return ()
    candidates: set[Path] = set()
    for directory_name in ("code-facts", "pipelines"):
        directory = architecture / directory_name
        if directory.is_dir() and not directory.is_symlink():
            candidates.update(path for path in directory.rglob("*.md") if _unlinkable_file(architecture, path))
    overview = architecture / "code-overview.md"
    if _unlinkable_file(architecture, overview):
        candidates.add(overview)
    return tuple(sorted((path for path in candidates if _is_codegraph_file(path)), key=lambda path: path.as_posix()))


def _markdown_files(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir() or directory.is_symlink():
        return ()
    return tuple(path for path in directory.rglob("*.md") if _unlinkable_file(directory, path))


def _is_codegraph_file(path: Path) -> bool:
    try:
        frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return False
    return _is_codegraph_page(frontmatter)


def _unlinkable_file(root: Path, path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and _inside(root, path)


def _removable_directory(root: Path, path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and _inside(root, path)


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _delete_retrieval_rows(root: Path, page_paths: tuple[str, ...], raw_paths: tuple[str, ...]) -> dict[str, object]:
    operations: dict[str, object] = {}
    archive_paths = tuple(path for path in page_paths if path.startswith("archives/bundles/"))
    active_paths = tuple(path for path in page_paths if not path.startswith("archives/bundles/"))
    for scope, paths in (("active", active_paths), ("archive", archive_paths), ("raw", raw_paths)):
        if not paths:
            continue
        store = RetrievalIndexStore(root, scope=cast(_StoreScope, scope))
        if not store.path.is_file():
            continue
        try:
            results = [store.delete_page(path) for path in paths]
        except RetrievalIndexError as exc:
            operations[scope] = {
                "ok": False,
                "operation": "delete",
                "affected_count": len(paths),
                "code": exc.code,
                "rebuild_required": True,
            }
            continue
        failed = [result for result in results if not result.get("ok")]
        operations[scope] = {
            "ok": not failed,
            "operation": "delete",
            "affected_count": len(paths),
            "results": results,
            **({"rebuild_required": True} if failed else {}),
        }
    return operations


def _vector_rebuild_requirements(root: Path, page_paths: tuple[str, ...], raw_paths: tuple[str, ...]) -> list[str]:
    required: list[str] = []
    active_paths = tuple(path for path in page_paths if not path.startswith("archives/bundles/"))
    archive_paths = tuple(path for path in page_paths if path.startswith("archives/bundles/"))
    if (active_paths or raw_paths) and (root / ".llm-wiki" / "vector-index" / "manifest.json").is_file():
        required.append("vector-index")
    if archive_paths and (root / ".llm-wiki" / "archive-vector-index" / "manifest.json").is_file():
        required.append("archive-vector-index")
    return required


def _result(
    root: Path,
    targets: _RemovalTargets,
    *,
    dry_run: bool,
    pages: tuple[Path, ...],
    directories: tuple[Path, ...],
    projection: dict[str, object],
    rebuild_required: list[str] | None = None,
) -> dict[str, object]:
    page_values = [_relative(root, path) for path in pages]
    directory_values = [_relative(root, path) for path in directories]
    result: dict[str, object] = {
        "ok": True,
        "kind": "codegraph_removal",
        "dry_run": dry_run,
        "pages": page_values,
        "directories": directory_values,
        "count": len(page_values) + len(directory_values),
        "raw_file_count": len(targets.raw_files),
        "projection": projection,
    }
    if rebuild_required:
        result["rebuild_required"] = rebuild_required
    return result


__all__ = ["apply_codegraph_removal", "plan_codegraph_removal"]
