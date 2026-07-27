from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module, metadata
from pathlib import Path
from typing import Callable, Literal

from netsuite_llm_wiki_mcp.git_utils import get_git_dirty, get_git_revision

BUILD_INFO_SCHEMA_VERSION = 1
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)
_AUTO_SOURCE_ROOT = object()

RevisionSource = Literal["build", "editable", "unknown"]


@dataclass(frozen=True)
class BuildMetadata:
    revision: str
    dirty: bool
    schema_version: int = BUILD_INFO_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BUILD_INFO_SCHEMA_VERSION:
            raise ValueError("unsupported build metadata schema")
        if not _REVISION_PATTERN.fullmatch(self.revision):
            raise ValueError("invalid build revision")
        if type(self.dirty) is not bool:
            raise ValueError("invalid build dirty flag")


@dataclass(frozen=True)
class RuntimeProvenance:
    package_version: str
    revision: str | None
    dirty: bool | None
    revision_source: RevisionSource
    started_at: str
    warnings: tuple[str, ...] = ()

    @property
    def server_version(self) -> str:
        if not self.revision:
            return self.package_version
        suffix = f"+{self.revision[:12]}"
        if self.dirty:
            suffix += ".dirty"
        return f"{self.package_version}{suffix}"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "package_version": self.package_version,
            "revision": self.revision or "unknown",
            "dirty": self.dirty,
            "revision_source": self.revision_source,
            "started_at": self.started_at,
            "provenance_incomplete": "provenance_incomplete" in self.warnings,
            "warnings": list(self.warnings),
        }


BuildMetadataLoader = Callable[[], BuildMetadata | None]
RevisionGetter = Callable[[Path], str]
DirtyGetter = Callable[[Path], bool | None]
Clock = Callable[[], datetime]


def create_runtime_provenance(
    *,
    package_version: str | None = None,
    build_metadata_loader: BuildMetadataLoader = lambda: _load_build_metadata(),
    source_root: Path | None | object = _AUTO_SOURCE_ROOT,
    revision_getter: RevisionGetter = get_git_revision,
    dirty_getter: DirtyGetter = get_git_dirty,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> RuntimeProvenance:
    version = package_version if package_version is not None else _package_version()
    started_at = _format_started_at(clock())

    try:
        build_metadata = build_metadata_loader()
    except (ImportError, TypeError, ValueError):
        return _unknown_provenance(
            version,
            started_at,
            warnings=("invalid_build_info", "provenance_incomplete"),
        )

    if build_metadata is not None:
        return RuntimeProvenance(
            package_version=version,
            revision=build_metadata.revision,
            dirty=build_metadata.dirty,
            revision_source="build",
            started_at=started_at,
        )

    root = (
        _find_source_root(Path(__file__).resolve())
        if source_root is _AUTO_SOURCE_ROOT
        else source_root
    )
    if isinstance(root, Path):
        revision = revision_getter(root)
        if _REVISION_PATTERN.fullmatch(revision):
            dirty = dirty_getter(root)
            warnings: tuple[str, ...] = ()
            if dirty is None:
                warnings = ("dirty_state_unknown", "provenance_incomplete")
            return RuntimeProvenance(
                package_version=version,
                revision=revision,
                dirty=dirty,
                revision_source="editable",
                started_at=started_at,
                warnings=warnings,
            )

    return _unknown_provenance(
        version,
        started_at,
        warnings=("provenance_incomplete",),
    )


def _load_build_metadata() -> BuildMetadata | None:
    module_name = "netsuite_llm_wiki_mcp._build_info"
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise ValueError("invalid build metadata") from exc
    except Exception as exc:
        raise ValueError("invalid build metadata") from exc

    try:
        return BuildMetadata(
            revision=module.REVISION,
            dirty=module.DIRTY,
            schema_version=module.SCHEMA_VERSION,
        )
    except AttributeError as exc:
        raise ValueError("invalid build metadata") from exc


def _package_version() -> str:
    try:
        return metadata.version("netsuite-llm-wiki-mcp")
    except (metadata.PackageNotFoundError, ValueError):
        return "unknown"


def _find_source_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _format_started_at(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    normalized = value.astimezone(timezone.utc).isoformat()
    return normalized.replace("+00:00", "Z")


def _unknown_provenance(
    package_version: str,
    started_at: str,
    *,
    warnings: tuple[str, ...],
) -> RuntimeProvenance:
    return RuntimeProvenance(
        package_version=package_version,
        revision=None,
        dirty=None,
        revision_source="unknown",
        started_at=started_at,
        warnings=warnings,
    )


RUNTIME_PROVENANCE = create_runtime_provenance()
