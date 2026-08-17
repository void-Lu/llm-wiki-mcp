"""页面 frontmatter 派生策略的唯一 owner。

读侧通过 :func:`derive` 取得完整的页面策略，写侧通过 :func:`stamp`
取得需要持久化的纯派生字段。两条入口共享同一套归一化规则，但写侧不
接管由调用方表达的维护意图或替代关系。
"""

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


@dataclass(frozen=True)
class PagePolicySourceFacts:
    """写侧计算页面策略所需的 frontmatter 与已验证来源事实。

    ``source_hashes`` 为 ``None`` 表示本次写入没有重新声明来源；此时
    ``stamp`` 会复用 frontmatter 中已有的 server-owned hash。传入空映射
    则表示来源事实为空，页面必须保持未验证状态。
    """

    frontmatter: Mapping[str, object]
    source_hashes: Mapping[str, object] | None = None


def _normalize_freshness(value: object) -> str | None:
    if not value:
        return None
    normalized = str(value)
    return normalized if normalized in VALID_FRESHNESS else "review_required"


def _stored_source_hashes(frontmatter: Mapping[str, object]) -> dict[str, str]:
    value = frontmatter.get("source_hashes")
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key) and str(item)}


def _derive_freshness(
    frontmatter: Mapping[str, object],
    source_hashes: Mapping[str, object] | None,
) -> str:
    explicit = _normalize_freshness(frontmatter.get("freshness"))
    if explicit is not None:
        return explicit
    return "fresh" if source_hashes is None or source_hashes else "review_required"


def _derive_policy(
    frontmatter: Mapping[str, object],
    source_hashes: Mapping[str, object] | None = None,
) -> PagePolicy:
    generated = bool(frontmatter.get("generated"))
    maintenance = str(frontmatter.get("maintenance") or ("auto" if generated else "manual"))

    lifecycle_value = str(frontmatter.get("lifecycle") or "active")
    lifecycle = lifecycle_value if lifecycle_value in VALID_LIFECYCLE else "review_required"

    replaced_value = frontmatter.get("replaced_by")
    replaced_by = None if replaced_value in (None, "") else str(replaced_value)

    return PagePolicy(
        freshness=_derive_freshness(frontmatter, source_hashes),
        maintenance=maintenance,
        lifecycle=lifecycle,
        generated=generated,
        replaced_by=replaced_by,
    )


def derive(
    frontmatter: Mapping[str, object],
    source_hashes: Mapping[str, object] | None = None,
) -> PagePolicy:
    """从 frontmatter 纯派生页面策略，不执行任何 I/O。"""

    return _derive_policy(frontmatter, source_hashes)


def derive_page_policy(
    frontmatter: Mapping[str, object],
    source_hashes: Mapping[str, object] | None = None,
) -> PagePolicy:
    """兼容入口：保持既有读侧页面策略行为。"""

    return derive(frontmatter, source_hashes)


def _validate_writer_owned_fields(frontmatter: Mapping[str, object]) -> None:
    """校验调用方拥有的字段形状，但不发明维护策略词汇表。"""

    maintenance = frontmatter.get("maintenance")
    if maintenance not in (None, "") and not isinstance(maintenance, str):
        raise ValueError("maintenance must be a string")
    replaced_by = frontmatter.get("replaced_by")
    if replaced_by not in (None, "") and not isinstance(replaced_by, str):
        raise ValueError("replaced_by must be a string")


def _stamp_fields(
    frontmatter: Mapping[str, object],
    source_hashes: Mapping[str, object],
    *,
    source_facts_supplied: bool,
) -> dict[str, object]:
    has_sources = bool(source_hashes)
    existing_unverified = frontmatter.get("provenance_unverified") is True
    if not has_sources or (existing_unverified and not source_facts_supplied):
        return {"freshness": "review_required", "provenance_unverified": True}

    if source_facts_supplied:
        freshness = "fresh"
    else:
        # Reuse the shared read-side normalization for a body-only update of a
        # verified page. An explicit stale/review_required value is preserved;
        # an absent value is fresh because the stored source hashes are present.
        freshness = _derive_freshness(frontmatter, source_hashes)
    return {"freshness": freshness, "provenance_unverified": False}


def stamp_page_policy(
    source_facts: PagePolicySourceFacts | Mapping[str, object],
    source_hashes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """计算写侧需要持久化的 freshness/provenance 字段。

    ``source_hashes`` 非 ``None`` 时表示调用方刚完成了来源解析与验证；
    空映射会明确产生未验证状态。省略或传入 ``None`` 时，stamp 只使用
    frontmatter 中既有的来源身份，因此纯正文更新不会把页面提升为 fresh。
    ``maintenance`` 与 ``replaced_by`` 仍由调用方设置，本函数只校验它们
    的可持久化形状，并不会把它们包含在返回值中。
    """

    if isinstance(source_facts, PagePolicySourceFacts):
        frontmatter = source_facts.frontmatter
        current_hashes = source_facts.source_hashes
        facts_supplied = current_hashes is not None
    else:
        frontmatter = source_facts
        current_hashes = source_hashes
        facts_supplied = source_hashes is not None
    if not isinstance(frontmatter, Mapping):
        raise TypeError("page policy source facts must contain a frontmatter mapping")
    _validate_writer_owned_fields(frontmatter)

    if current_hashes is None:
        effective_hashes: Mapping[str, object] = _stored_source_hashes(frontmatter)
    elif isinstance(current_hashes, Mapping):
        effective_hashes = current_hashes
    else:
        raise TypeError("source_hashes must be a mapping or None")
    return _stamp_fields(frontmatter, effective_hashes, source_facts_supplied=facts_supplied)


def stamp(
    source_facts: PagePolicySourceFacts | Mapping[str, object],
    source_hashes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """兼容短入口：调用正式的 :func:`stamp_page_policy`。"""

    return stamp_page_policy(source_facts, source_hashes)


def provenance_status(stamped_fields: Mapping[str, object]) -> str:
    """Project the stamped provenance flag into the existing result vocabulary."""

    return "provenance_unverified" if stamped_fields.get("provenance_unverified") is True else "verified"


__all__ = [
    "PagePolicy",
    "PagePolicySourceFacts",
    "VALID_FRESHNESS",
    "VALID_LIFECYCLE",
    "derive",
    "derive_page_policy",
    "provenance_status",
    "stamp",
    "stamp_page_policy",
]
