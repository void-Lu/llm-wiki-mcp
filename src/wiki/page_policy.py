"""页面 frontmatter 派生策略的唯一 owner。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


VALID_LIFECYCLE = {"active", "stale", "review_required", "superseded", "deprecated", "archived"}
VALID_FRESHNESS = {"fresh", "stale", "review_required"}


@dataclass(frozen=True)
class PagePolicy:
    """页面投影所需的标准化派生字段。"""

    freshness: str
    maintenance: str
    lifecycle: str
    generated: bool
    replaced_by: str | None


def derive_page_policy(
    frontmatter: Mapping[str, object],
    source_hashes: Mapping[str, object] | None = None,
) -> PagePolicy:
    """从 frontmatter 纯派生页面策略，不执行任何 I/O。"""

    generated = bool(frontmatter.get("generated"))
    maintenance = str(frontmatter.get("maintenance") or ("auto" if generated else "manual"))

    lifecycle_value = str(frontmatter.get("lifecycle") or "active")
    lifecycle = lifecycle_value if lifecycle_value in VALID_LIFECYCLE else "review_required"

    freshness_value = frontmatter.get("freshness")
    if freshness_value:
        normalized_freshness = str(freshness_value)
        freshness = normalized_freshness if normalized_freshness in VALID_FRESHNESS else "review_required"
    else:
        freshness = "fresh" if source_hashes is None or source_hashes else "review_required"

    replaced_value = frontmatter.get("replaced_by")
    replaced_by = None if replaced_value in (None, "") else str(replaced_value)

    return PagePolicy(
        freshness=freshness,
        maintenance=maintenance,
        lifecycle=lifecycle,
        generated=generated,
        replaced_by=replaced_by,
    )


__all__ = ["PagePolicy", "VALID_FRESHNESS", "VALID_LIFECYCLE", "derive_page_policy"]
