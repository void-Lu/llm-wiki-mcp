from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WikiPage:
    relative_path: Path
    frontmatter: dict[str, Any]
    title: str
    body: str


@dataclass(frozen=True)
class WikiLogEntry:
    operation: str
    title: str
    paths: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    project: str = ""
    status: str = "ok"
    timestamp: str = ""
