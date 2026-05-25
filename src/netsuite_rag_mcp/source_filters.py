from __future__ import annotations

import fnmatch
from pathlib import Path

# SuiteCloud project-level files that should not be indexed or rendered as object wiki pages.
DEFAULT_FILE_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "src/deploy.xml",
    "src/manifest.xml",
)


def normalized_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def matches_path_pattern(relative_path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    normalized = normalized_path(relative_path)
    name = Path(relative_path).name.casefold()
    for pattern in patterns:
        pattern_text = normalized_path(pattern)
        if fnmatch.fnmatch(normalized, pattern_text) or fnmatch.fnmatch(name, pattern_text):
            return True
    return False


def source_relative_path(file_path: Path, source_root: Path) -> str | None:
    try:
        return file_path.resolve().relative_to(source_root.resolve()).as_posix()
    except ValueError:
        return None


def should_exclude_by_component(file_path: Path, base_path: Path, exclude_names: set[str]) -> bool:
    try:
        relative = file_path.relative_to(base_path)
    except ValueError:
        return True
    normalized_exclude_names = {name.casefold() for name in exclude_names}
    return any(part.casefold() in normalized_exclude_names for part in relative.parts)


def should_exclude_by_file_pattern(
    file_path: Path,
    source_root: Path,
    patterns: list[str] | tuple[str, ...],
) -> bool:
    relative_path = source_relative_path(file_path, source_root)
    if relative_path is None:
        return True
    return matches_path_pattern(relative_path, patterns)
