"""变更 kind 到有序派生投影阶段的纯 registry。

本模块只描述“字节变更后要追什么”，不持有执行器、operation journal、
repair 状态或 vault I/O。各调用方按这里返回的阶段名绑定自己的执行函数。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Iterable, Literal, Mapping


ProjectionStage = Literal[
    "dependencies",
    "retrieval",
    "navigation",
    "overview",
    "audit_log",
    "raw_provenance",
]
ProjectionKind = Literal[
    "formal",
    "chat",
    "ingest",
    "ingest_chat",
    "archive",
    "provenance",
    "privacy",
]


FORMAL_PROJECTION_STAGES: Final[tuple[ProjectionStage, ...]] = (
    "dependencies",
    "retrieval",
    "navigation",
    "overview",
    "audit_log",
)
INGEST_PROJECTION_STAGES: Final[tuple[ProjectionStage, ...]] = (
    "raw_provenance",
    "retrieval",
)
INGEST_CHAT_PROJECTION_STAGES: Final[tuple[ProjectionStage, ...]] = ("retrieval",)
RETRIEVAL_ONLY_PROJECTION_STAGES: Final[tuple[ProjectionStage, ...]] = ("retrieval",)

_PROFILES: Final[dict[str, tuple[ProjectionStage, ...]]] = {
    "formal": FORMAL_PROJECTION_STAGES,
    "chat": FORMAL_PROJECTION_STAGES,
    "ingest": INGEST_PROJECTION_STAGES,
    "ingest_chat": INGEST_CHAT_PROJECTION_STAGES,
    "archive": RETRIEVAL_ONLY_PROJECTION_STAGES,
    "provenance": RETRIEVAL_ONLY_PROJECTION_STAGES,
    "privacy": RETRIEVAL_ONLY_PROJECTION_STAGES,
}
PROJECTION_PROFILES: Final[Mapping[str, tuple[ProjectionStage, ...]]] = MappingProxyType(_PROFILES)

# 生产调用方的历史 kind 仍由同一 registry 解析，避免在各调用方复制别名表。
_KIND_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "create": "formal",
        "note": "formal",
        "update": "formal",
        "chat_source": "chat",
        "ingest_text": "ingest",
        "ingest_chat_source": "ingest_chat",
        "provenance_migration": "provenance",
        "privacy_audit": "privacy",
    }
)


class ProjectionProfileError(ValueError):
    """投影 profile 输入不属于已登记 kind。"""

    def __init__(self, code: str = "unknown_projection_kind") -> None:
        super().__init__(code)
        self.code = code


def projection_stages(kind: str) -> tuple[ProjectionStage, ...]:
    """返回 kind 的不可变、有序投影阶段列表。"""

    canonical = _KIND_ALIASES.get(kind, kind)
    stages = PROJECTION_PROFILES.get(canonical)
    if stages is None:
        raise ProjectionProfileError()
    return stages


def projection_profiles() -> Mapping[str, tuple[ProjectionStage, ...]]:
    """返回只读 profile registry 视图，供契约测试和诊断使用。"""

    return PROJECTION_PROFILES


def assert_operation_stage_parity(operation_kinds: Iterable[str]) -> None:
    """断言 durable write kind 全部使用 formal journal 阶段。

    调用方传入真实 write adapter registry 的 operation kinds；这样 profile
    owner 不依赖 adapter 模块，同时仍通过同一 registry 校验 alias 和 canonical kind。
    """

    expected = projection_stages("formal")
    for operation_kind in sorted(set(operation_kinds)):
        canonical = _KIND_ALIASES.get(operation_kind, operation_kind)
        try:
            actual = projection_stages(operation_kind)
        except ProjectionProfileError as exc:
            raise AssertionError(
                f"write kind {operation_kind!r} (canonical {canonical!r}) is not registered; "
                f"expected formal PAGE_STAGES {expected!r}"
            ) from exc
        if actual != expected:
            raise AssertionError(
                f"write kind {operation_kind!r} (canonical {canonical!r}) has stages {actual!r}; "
                f"expected formal PAGE_STAGES {expected!r}"
            )


__all__ = [
    "FORMAL_PROJECTION_STAGES",
    "INGEST_CHAT_PROJECTION_STAGES",
    "INGEST_PROJECTION_STAGES",
    "PROJECTION_PROFILES",
    "ProjectionKind",
    "ProjectionProfileError",
    "ProjectionStage",
    "RETRIEVAL_ONLY_PROJECTION_STAGES",
    "assert_operation_stage_parity",
    "projection_profiles",
    "projection_stages",
]
