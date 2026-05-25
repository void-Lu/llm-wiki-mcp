from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WINDOWS_RESERVED_CHARS = set('<>:"|?*')
WINDOWS_RESERVED_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"}
WINDOWS_RESERVED_DEVICE_PREFIXES = ("COM", "LPT")
WINDOWS_RESERVED_DEVICE_SUFFIXES = set("123456789¹²³")

TOP_LEVEL_DIRS = (
    Path("raw/sources"),
    Path("raw/assets"),
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/sources"),
    Path("wiki/queries"),
    Path("wiki/synthesis"),
    Path("wiki/comparisons"),
    Path(".obsidian"),
    Path(".llm-wiki"),
)

DEFAULT_FILES = {
    Path("purpose.md"): "# Purpose\n\n描述这个知识库的目标、关键问题和研究范围。\n",
    Path("schema.md"): "# Schema\n\n描述 Wiki 结构规则、页面类型、frontmatter 和维护流程。\n",
    Path("wiki/index.md"): "---\ntype: index\ngenerated: true\n---\n\n# Index\n\n",
    Path("wiki/log.md"): "# Log\n\n",
    Path("wiki/overview.md"): "---\ntype: overview\ngenerated: true\n---\n\n# Overview\n\n",
}


class WikiPathError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WikiPaths:
    root: Path

    def project_root(self, project: str) -> Path:
        return self.root / "wiki" / "projects" / safe_segment(project)

    def project_code_dir(self, project: str) -> Path:
        return self.project_root(project) / "code"

    def project_decisions_dir(self, project: str) -> Path:
        return self.project_root(project) / "decisions"

    def project_troubleshooting_dir(self, project: str) -> Path:
        return self.project_root(project) / "troubleshooting"

    def project_requirements_dir(self, project: str) -> Path:
        return self.project_root(project) / "requirements"

    def concepts_dir(self) -> Path:
        return self.root / "wiki" / "concepts"

    def sources_dir(self) -> Path:
        return self.root / "wiki" / "sources"

    def queries_dir(self) -> Path:
        return self.root / "wiki" / "queries"

    def synthesis_dir(self) -> Path:
        return self.root / "wiki" / "synthesis"

    def comparisons_dir(self) -> Path:
        return self.root / "wiki" / "comparisons"


def safe_segment(value: str) -> str:
    if not value:
        raise WikiPathError("empty_segment", "path segment is required")
    path = Path(value)
    if (
        "/" in value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) != 1
    ):
        raise WikiPathError("path_escape", "value must be a single safe path segment")
    if _has_windows_reserved_character(value) or value.endswith((".", " ")) or _is_windows_reserved_device_name(value):
        raise WikiPathError("invalid_path_component", "value contains a Windows-invalid path component")
    return value


def create_wiki_root(vault_root: str | Path) -> WikiPaths:
    root = Path(vault_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative_dir in TOP_LEVEL_DIRS:
        (root / relative_dir).mkdir(parents=True, exist_ok=True)
    for relative_file, default_text in DEFAULT_FILES.items():
        target = root / relative_file
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(default_text, encoding="utf-8")
    return WikiPaths(root=root)


def slug(value: str) -> str:
    text = value.strip()
    text = re.sub(r"[\s\W]+", "-", text, flags=re.UNICODE).strip("-").lower()
    return text[:80].rstrip("-") or "page"


def _has_windows_reserved_character(value: str) -> bool:
    return any(char in WINDOWS_RESERVED_CHARS or ord(char) < 32 for char in value)


def _is_windows_reserved_device_name(value: str) -> bool:
    base = value.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED_DEVICE_NAMES:
        return True
    return len(base) == 4 and base[:3] in WINDOWS_RESERVED_DEVICE_PREFIXES and base[3] in WINDOWS_RESERVED_DEVICE_SUFFIXES
