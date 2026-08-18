"""Query V2 质量门禁的纯特征与策略 owner。

本模块只消费已经冻结的候选 mapping 或 ``CandidateFeature``，不读取 store、
snapshot、文件系统或执行上下文。v0 只做 keep-all 决策，拒绝和 fail-open
reason code 先作为稳定接口保留给后续校准/rollout 子任务。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
import math
import re
from types import MappingProxyType
from typing import Any, Literal, TypeGuard


ScoreFamily = Literal[
    "main_rrf",
    "wiki_relaxed",
    "raw_recovery",
    "coverage_fusion",
    "graph_extension",
]

SCORE_FAMILIES: tuple[ScoreFamily, ...] = (
    "main_rrf",
    "wiki_relaxed",
    "raw_recovery",
    "coverage_fusion",
    "graph_extension",
)
SCORE_FAMILY_SET = frozenset(SCORE_FAMILIES)

GATE_KEEP_EXACT_IDENTIFIER = "gate_keep_exact_identifier"
GATE_KEEP_PHRASE = "gate_keep_phrase"
GATE_KEEP_TERM_COVERAGE = "gate_keep_term_coverage"
GATE_KEEP_MULTI_SIGNAL = "gate_keep_multi_signal"
GATE_KEEP_RAW_COVERAGE = "gate_keep_raw_coverage"
GATE_KEEP_GRAPH_SUPPORTED = "gate_keep_graph_supported"
GATE_KEEP_TOP1_RESCUE = "gate_keep_top1_rescue"
GATE_KEEP_DEFAULT = "gate_keep_default"

GATE_REJECT_SCORE_FLOOR = "gate_reject_score_floor"
GATE_REJECT_SCORE_CLIFF = "gate_reject_score_cliff"
GATE_REJECT_NO_INDEPENDENT_SIGNAL = "gate_reject_no_independent_signal"
GATE_REJECT_RAW_NO_COVERAGE = "gate_reject_raw_no_coverage"
GATE_REJECT_GRAPH_ONLY_WEAK = "gate_reject_graph_only_weak"
GATE_REJECT_LOW_CONFIDENCE = "gate_reject_low_confidence"

GATE_FAIL_OPEN_LOW_SAMPLE = "gate_fail_open_low_sample"
GATE_FAIL_OPEN_POLICY_MISSING = "gate_fail_open_policy_missing"
GATE_FAIL_OPEN_ERROR = "gate_fail_open_error"

KEEP_REASON_CODES = frozenset(
    {
        GATE_KEEP_EXACT_IDENTIFIER,
        GATE_KEEP_PHRASE,
        GATE_KEEP_TERM_COVERAGE,
        GATE_KEEP_MULTI_SIGNAL,
        GATE_KEEP_RAW_COVERAGE,
        GATE_KEEP_GRAPH_SUPPORTED,
        GATE_KEEP_TOP1_RESCUE,
        GATE_KEEP_DEFAULT,
    }
)
REJECT_REASON_CODES = frozenset(
    {
        GATE_REJECT_SCORE_FLOOR,
        GATE_REJECT_SCORE_CLIFF,
        GATE_REJECT_NO_INDEPENDENT_SIGNAL,
        GATE_REJECT_RAW_NO_COVERAGE,
        GATE_REJECT_GRAPH_ONLY_WEAK,
        GATE_REJECT_LOW_CONFIDENCE,
    }
)
FAIL_OPEN_REASON_CODES = frozenset(
    {
        GATE_FAIL_OPEN_LOW_SAMPLE,
        GATE_FAIL_OPEN_POLICY_MISSING,
        GATE_FAIL_OPEN_ERROR,
    }
)
GATE_REASON_CODES = frozenset(
    {*KEEP_REASON_CODES, *REJECT_REASON_CODES, *FAIL_OPEN_REASON_CODES}
)

QUALITY_POLICY_VERSION = "query-quality-policy-v0"
_SNAKE_CASE_REASON = re.compile(r"^gate_(?:keep|reject|fail_open)_[a-z0-9_]+$")
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uff00-\uffef]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_VALID_SCOPES = frozenset({"knowledge", "history", "all", "archive", "raw"})


def is_score_family(value: object) -> TypeGuard[ScoreFamily]:
    """Return whether ``value`` is one of the five frozen family names."""

    return isinstance(value, str) and value in SCORE_FAMILY_SET


def is_gate_reason_code(value: object) -> bool:
    """Validate the public-safe, stable gate reason vocabulary."""

    return isinstance(value, str) and bool(_SNAKE_CASE_REASON.fullmatch(value)) and value in GATE_REASON_CODES


def _read(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _hit(candidate: object) -> object:
    return _read(candidate, "hit")


def _candidate_or_hit(candidate: object, key: str, default: Any = None) -> Any:
    value = _read(candidate, key, None)
    if value is not None:
        return value
    return _read(_hit(candidate), key, default)


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.casefold() in {"1", "true", "yes", "on", "matched", "present"}
    return bool(value)


def _normalized_path(value: object) -> str:
    path = str(value or "").replace("\\", "/")
    if not path:
        raise ValueError("candidate page_path is required")
    return path.casefold()


def _page_path(candidate: object) -> str:
    path = _candidate_or_hit(candidate, "page_path", None)
    if not path:
        path = _read(candidate, "path", None)
    if not path:
        path = _read(_hit(candidate), "path", None)
    if not path:
        raise ValueError("candidate page_path is required")
    return str(path).replace("\\", "/")


def _candidate_keys(candidate: object) -> set[str]:
    if isinstance(candidate, Mapping):
        return {str(key) for key in candidate}
    return set()


def _branch_name(candidate: object, branch: str | None) -> str:
    if branch:
        return branch.casefold()
    for key in ("branch", "fallback_branch", "recovery_branch", "score_branch"):
        value = _read(candidate, key, None)
        if isinstance(value, str) and value.strip():
            return value.casefold()
    fallback_reason = _read(candidate, "fallback_reason", "")
    if isinstance(fallback_reason, str):
        lowered = fallback_reason.casefold()
        if "relaxed" in lowered:
            return "wiki_relaxed"
        if "coverage" in lowered:
            return "coverage"
        if "raw" in lowered:
            return "raw"
    return ""


def derive_score_family(candidate: Mapping[str, Any] | object, *, branch: str | None = None) -> ScoreFamily:
    """从既有候选字段派生一个 ``score_family``。

    显式 branch/family 标记优先；否则只使用候选自身的 rank、fusion、raw
    source 和 graph 字段。这里不比较不同分支的裸 score。
    """

    explicit = _read(candidate, "score_family", None)
    if explicit is not None:
        if not is_score_family(explicit):
            raise ValueError(f"unsupported score_family: {explicit!r}")
        return explicit

    branch_name = _branch_name(candidate, branch)
    if branch_name in {"coverage", "all_coverage", "coverage_fusion"}:
        return "coverage_fusion"
    if branch_name in {"wiki_relaxed", "relaxed", "active_relaxed"}:
        return "wiki_relaxed"
    if branch_name in {"raw", "raw_zero", "raw_recovery", "raw_only"}:
        return "raw_recovery"

    keys = _candidate_keys(candidate)
    coverage_keys = {
        "coverage_terms",
        "coverage_ratio",
        "source_local_rank",
        "source_local_rrf",
        "fusion_score",
        "fusion_source",
        "fusion_local_position",
    }
    if keys & coverage_keys:
        return "coverage_fusion"

    graph_score = _number(_read(candidate, "graph_score", 0.0))
    has_fts = _positive_int(_read(candidate, "fts_rank", None)) is not None
    has_vector = (
        _positive_int(_read(candidate, "vector_rank", None)) is not None
        or _number(_read(candidate, "vector_score", 0.0)) != 0.0
    )
    has_title = _positive_int(_read(candidate, "title_rank", None)) is not None
    if graph_score > 0 and not has_fts and not has_vector and not has_title:
        return "graph_extension"

    source_kind = str(_candidate_or_hit(candidate, "source_kind", "")).casefold()
    fallback_level = str(_read(candidate, "fallback_level", "")).casefold()
    if source_kind == "raw" or fallback_level == "raw":
        return "raw_recovery"

    return "main_rrf"


def resolve_effective_scope(
    candidate: Mapping[str, Any] | object,
    *,
    scope: str = "auto",
    effective_scope: str | None = None,
) -> str:
    """Resolve the internal scope without treating public ``auto`` as a bucket."""

    internal = effective_scope or _read(candidate, "effective_scope", None) or _read(candidate, "internal_scope", None)
    if isinstance(internal, str) and internal.casefold() in _VALID_SCOPES:
        return internal.casefold()
    requested = scope.casefold()
    if requested == "auto":
        # Query V2's auto default is knowledge unless its internal intent has
        # already supplied a history effective scope above.
        return "knowledge"
    if requested not in _VALID_SCOPES:
        raise ValueError(f"unsupported effective scope: {scope!r}")
    return requested


def _fallback_fields(
    candidate: object,
    *,
    fallback_level: str | None,
    fallback_reason: str | None,
) -> tuple[str, str]:
    fallback = _read(candidate, "fallback", None)
    level = fallback_level or _read(candidate, "fallback_level", None)
    reason = fallback_reason or _read(candidate, "fallback_reason", None)
    reasons = _read(candidate, "fallback_reasons", None)
    if isinstance(fallback, Mapping):
        level = level or fallback.get("level")
        reason = reason or fallback.get("reason")
        reasons = reasons or fallback.get("reasons")
    if not level:
        source_kind = str(_candidate_or_hit(candidate, "source_kind", "")).casefold()
        level = "raw" if source_kind == "raw" else "none"
    if not reason and isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)):
        reason = next((str(item) for item in reasons if str(item).strip()), "")
    return str(level), str(reason or "")


def _coverage(candidate: object, query_terms: Sequence[str]) -> tuple[float, tuple[str, ...]]:
    raw_terms = _read(candidate, "covered_terms", None)
    if raw_terms is None:
        raw_terms = _read(candidate, "coverage_terms", None)
    covered = tuple(sorted({str(term).casefold() for term in raw_terms or () if str(term).strip()}))
    ratio = _read(candidate, "term_coverage", None)
    if ratio is None:
        ratio = _read(candidate, "coverage_ratio", None)
    if ratio is None:
        ratio = _read(candidate, "coverage", None)
    if isinstance(ratio, Mapping):
        ratio = ratio.get("ratio", ratio.get("coverage_ratio"))
    if ratio is None and query_terms:
        normalized_terms = {str(term).casefold() for term in query_terms if str(term).strip()}
        if covered:
            ratio = len(normalized_terms & set(covered)) / max(len(normalized_terms), 1)
        else:
            raw_uncovered = _read(candidate, "uncovered_terms", ())
            uncovered = {str(term).casefold() for term in raw_uncovered or ()}
            ratio = 1.0 - len(uncovered & normalized_terms) / max(len(normalized_terms), 1)
    value = min(max(_number(ratio), 0.0), 1.0)
    return value, covered


def _language_bucket(candidate: object, explicit: str | None) -> str:
    value = explicit or _read(candidate, "language_bucket", None) or _read(candidate, "language", None)
    if isinstance(value, str) and value.strip():
        return value.casefold()
    text = " ".join(
        str(_candidate_or_hit(candidate, key, ""))
        for key in ("title", "text")
    )
    has_cjk = bool(_CJK_RE.search(text))
    has_latin = bool(_LATIN_RE.search(text))
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "cjk"
    if has_latin:
        return "latin"
    return "unknown"


def _signal_values(candidate: object) -> tuple[bool, bool, bool, bool, tuple[str, ...]]:
    exact = _truthy(_read(candidate, "exact_signal", _read(candidate, "exact_match", _read(candidate, "exact", False))))
    identifier = _truthy(
        _read(
            candidate,
            "identifier_signal",
            _read(candidate, "qualified_identifier", _read(candidate, "identifier", False)),
        )
    )
    phrase = _truthy(
        _read(candidate, "phrase_signal", _read(candidate, "phrase_match", _read(candidate, "identifier_phrase", False)))
    )
    title = _truthy(_read(candidate, "title_signal", _read(candidate, "title_match", False)))
    if not title:
        title = _positive_int(_read(candidate, "title_rank", None)) is not None or _number(_read(candidate, "title_overlap", 0.0)) > 0

    signals: list[str] = []
    for name, present in (
        ("fts", _positive_int(_read(candidate, "fts_rank", None)) is not None),
        ("title", title),
        (
            "vector",
            _positive_int(_read(candidate, "vector_rank", None)) is not None
            or _number(_read(candidate, "vector_score", 0.0)) != 0.0,
        ),
        ("exact", exact),
        ("identifier", identifier),
        ("phrase", phrase),
        ("coverage", _number(_read(candidate, "coverage_ratio", 0.0)) > 0),
        ("graph", _number(_read(candidate, "graph_score", 0.0)) > 0),
    ):
        if present:
            signals.append(name)
    explicit = _read(candidate, "independent_signals", None)
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        signals = sorted({str(item).casefold() for item in explicit if str(item).strip()})
    explicit_count = _positive_int(_read(candidate, "independent_signal_count", None))
    if explicit_count is not None and explicit_count > len(signals):
        signals.extend(f"signal_{index}" for index in range(len(signals), explicit_count))
    return exact, identifier, phrase, title, tuple(sorted(set(signals)))


def _candidate_rank(candidate: object) -> int | None:
    for key in ("branch_rank", "branch_local_rank", "local_rank", "source_local_rank"):
        rank = _positive_int(_read(candidate, key, None))
        if rank is not None:
            return rank
    ranks = [
        rank
        for key in ("fts_rank", "title_rank", "vector_rank")
        if (rank := _positive_int(_read(candidate, key, None))) is not None
    ]
    return min(ranks) if ranks else None


@dataclass(frozen=True)
class CandidateFeature:
    """冻结的 page-level 候选特征；不携带正文或 query。"""

    page_path: str
    score_family: ScoreFamily
    score: float = 0.0
    branch_rank: int | None = None
    branch_margin: float | None = None
    term_coverage: float = 0.0
    covered_terms: tuple[str, ...] = ()
    exact_signal: bool = False
    identifier_signal: bool = False
    phrase_signal: bool = False
    title_signal: bool = False
    independent_signal_count: int = 0
    independent_signals: tuple[str, ...] = ()
    fallback_level: str = "none"
    fallback_reason: str = ""
    authority: str = ""
    source_kind: str = ""
    effective_scope: str = "knowledge"
    language_bucket: str = "unknown"
    retrieval_mode: str = "lexical"
    top1_rescue: bool = False
    normalized_page_path: str = ""

    @property
    def branch_score(self) -> float:
        """Use the candidate score as the branch-local score in v0."""

        return self.score

    @property
    def rank(self) -> int | None:
        return self.branch_rank

    @property
    def margin(self) -> float | None:
        return self.branch_margin

    @property
    def exact(self) -> bool:
        return self.exact_signal


def extract_candidate_feature(
    candidate: Mapping[str, Any] | object,
    *,
    scope: str = "auto",
    effective_scope: str | None = None,
    retrieval_mode: str | None = None,
    language_bucket: str | None = None,
    branch: str | None = None,
    fallback_level: str | None = None,
    fallback_reason: str | None = None,
    query_terms: Sequence[str] = (),
    branch_rank: int | None = None,
    branch_margin: float | None = None,
) -> CandidateFeature:
    """从一个既有候选 mapping 提取无副作用、不可变的 feature record。"""

    path = _page_path(candidate)
    family = derive_score_family(candidate, branch=branch)
    level, reason = _fallback_fields(
        candidate,
        fallback_level=fallback_level,
        fallback_reason=fallback_reason,
    )
    coverage, covered_terms = _coverage(candidate, query_terms)
    exact, identifier, phrase, title, signals = _signal_values(candidate)
    rank = branch_rank if branch_rank is not None else _candidate_rank(candidate)
    score = _number(_read(candidate, "branch_score", _read(candidate, "score", 0.0)))
    source_kind = str(_candidate_or_hit(candidate, "source_kind", "")).casefold()
    authority = str(_candidate_or_hit(candidate, "authority", "")).casefold()
    mode = retrieval_mode or _read(candidate, "retrieval_mode", None) or "lexical"
    rescue = _truthy(_read(candidate, "top1_rescue", _read(candidate, "rescue", False)))
    raw_margin = _read(candidate, "branch_margin", None)
    if raw_margin is None:
        raw_margin = _read(candidate, "margin", None)
    resolved_margin = None if raw_margin is None else _number(raw_margin)
    return CandidateFeature(
        page_path=path,
        score_family=family,
        score=score,
        branch_rank=rank,
        branch_margin=branch_margin if branch_margin is not None else resolved_margin,
        term_coverage=coverage,
        covered_terms=covered_terms,
        exact_signal=exact,
        identifier_signal=identifier,
        phrase_signal=phrase,
        title_signal=title,
        independent_signal_count=len(signals),
        independent_signals=signals,
        fallback_level=level,
        fallback_reason=reason,
        authority=authority,
        source_kind=source_kind,
        effective_scope=resolve_effective_scope(candidate, scope=scope, effective_scope=effective_scope),
        language_bucket=_language_bucket(candidate, language_bucket),
        retrieval_mode=str(mode).casefold(),
        top1_rescue=rescue,
        normalized_page_path=_normalized_path(path),
    )


def build_candidate_features(
    candidates: Sequence[Mapping[str, Any] | object],
    *,
    scope: str = "auto",
    effective_scope: str | None = None,
    retrieval_mode: str | None = None,
    language_bucket: str | None = None,
    branch: str | None = None,
    fallback_level: str | None = None,
    fallback_reason: str | None = None,
    query_terms: Sequence[str] = (),
) -> tuple[CandidateFeature, ...]:
    """批量提取 branch-local rank/margin，并按 normalized path 稳定 tie-break。"""

    preliminary = tuple(
        extract_candidate_feature(
            candidate,
            scope=scope,
            effective_scope=effective_scope,
            retrieval_mode=retrieval_mode,
            language_bucket=language_bucket,
            branch=branch,
            fallback_level=fallback_level,
            fallback_reason=fallback_reason,
            query_terms=query_terms,
        )
        for candidate in candidates
    )
    by_family: dict[ScoreFamily, list[tuple[int, CandidateFeature]]] = {}
    for index, feature in enumerate(preliminary):
        by_family.setdefault(feature.score_family, []).append((index, feature))

    derived: list[CandidateFeature | None] = [None] * len(preliminary)
    for family_items in by_family.values():
        ordered = sorted(
            family_items,
            key=lambda item: (-item[1].score, item[1].normalized_page_path, item[1].page_path),
        )
        for position, (index, feature) in enumerate(ordered, 1):
            next_score = ordered[position][1].score if position < len(ordered) else None
            margin = feature.branch_margin
            if margin is None or margin == 0.0:
                margin = None if next_score is None else round(feature.score - next_score, 12)
            rank = feature.branch_rank or position
            derived[index] = replace(feature, branch_rank=rank, branch_margin=margin)
    return tuple(feature for feature in derived if feature is not None)


extract_candidate_features = build_candidate_features


@dataclass(frozen=True)
class GateCandidateDecision:
    """一个候选的确定性 page-level 决策和内部 feature 记录。"""

    feature: CandidateFeature
    accepted: bool
    reason_code: str

    @property
    def page_path(self) -> str:
        return self.feature.page_path

    @property
    def score_family(self) -> ScoreFamily:
        return self.feature.score_family

    @property
    def rejected(self) -> bool:
        return not self.accepted


@dataclass(frozen=True)
class GateSummary:
    """只含有界计数的策略摘要，不保存 path/query/正文。"""

    candidate_count: int
    accepted_count: int
    rejected_count: int
    score_family_counts: Mapping[str, int]
    reason_counts: Mapping[str, int]
    low_sample_buckets: tuple[str, ...] = ()
    fail_open: bool = False
    policy_version: str = QUALITY_POLICY_VERSION
    calibration_revision: str = ""
    threshold_selection_counts: Mapping[str, int] = MappingProxyType({})


@dataclass(frozen=True)
class QualityThresholdView:
    """A parsed, branch-relative threshold view supplied by calibration.

    The policy module deliberately owns only this small input contract.  The
    calibration artifact, its file format, and its loader live in a separate
    module so the v0 policy remains usable without any artifact or IO
    dependency.  ``score_ratio`` is relative to the selected score family;
    this type intentionally has no absolute score floor.
    """

    bucket_key: str = ""
    score_ratio: float | None = None
    margin: float | None = None
    term_coverage: float | None = None
    sample_count: int | None = None
    backoff_depth: int = 0
    selection: Literal["exact", "backoff", "fail_open"] = "exact"
    fail_open: bool = False
    fail_open_reason: str | None = None
    low_sample_buckets: tuple[str, ...] = ()
    policy_version: str = QUALITY_POLICY_VERSION
    calibration_revision: str = ""

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("score_ratio", self.score_ratio, 0.0, 1.0),
            ("term_coverage", self.term_coverage, 0.0, 1.0),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.margin is not None and (
            isinstance(self.margin, bool)
            or not isinstance(self.margin, (int, float))
            or not math.isfinite(float(self.margin))
            or float(self.margin) < 0
        ):
            raise ValueError("margin must be a finite non-negative number")
        if self.sample_count is not None and (type(self.sample_count) is not int or self.sample_count < 0):
            raise ValueError("sample_count must be a non-negative integer")
        if type(self.backoff_depth) is not int or self.backoff_depth < 0:
            raise ValueError("backoff_depth must be a non-negative integer")
        if self.selection not in {"exact", "backoff", "fail_open"}:
            raise ValueError("selection must be exact, backoff, or fail_open")
        if self.fail_open_reason is not None and self.fail_open_reason not in FAIL_OPEN_REASON_CODES:
            raise ValueError("fail_open_reason is not a stable fail-open reason code")

    @property
    def available(self) -> bool:
        """Whether this view contains an enforceable threshold set."""

        return not self.fail_open and any(
            value is not None for value in (self.score_ratio, self.margin, self.term_coverage)
        )


@dataclass(frozen=True)
class QualityGateResult:
    """质量门禁纯函数的完整结果。"""

    decisions: tuple[GateCandidateDecision, ...]
    accepted: tuple[GateCandidateDecision, ...]
    rejected: tuple[GateCandidateDecision, ...]
    summary: GateSummary

    @property
    def accepted_features(self) -> tuple[CandidateFeature, ...]:
        return tuple(decision.feature for decision in self.accepted)

    @property
    def rejected_features(self) -> tuple[CandidateFeature, ...]:
        return tuple(decision.feature for decision in self.rejected)


def _keep_reason(feature: CandidateFeature) -> str:
    if feature.exact_signal or feature.identifier_signal:
        return GATE_KEEP_EXACT_IDENTIFIER
    if feature.phrase_signal:
        return GATE_KEEP_PHRASE
    if feature.term_coverage > 0:
        return GATE_KEEP_RAW_COVERAGE if feature.score_family == "coverage_fusion" and feature.source_kind == "raw" else GATE_KEEP_TERM_COVERAGE
    if feature.score_family == "graph_extension":
        return GATE_KEEP_GRAPH_SUPPORTED
    if feature.independent_signal_count >= 2:
        return GATE_KEEP_MULTI_SIGNAL
    if feature.top1_rescue:
        return GATE_KEEP_TOP1_RESCUE
    return GATE_KEEP_DEFAULT


def _count_values(values: Iterable[str]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return MappingProxyType(dict(sorted(counts.items())))


def _threshold_view_for(
    threshold_view: object,
    feature: CandidateFeature,
    *,
    index: int | None = None,
) -> QualityThresholdView | None:
    """Resolve one candidate's view without importing the calibration owner."""

    if isinstance(threshold_view, QualityThresholdView):
        return threshold_view
    if (
        index is not None
        and isinstance(threshold_view, Sequence)
        and not isinstance(threshold_view, (str, bytes, bytearray, Mapping))
        and index < len(threshold_view)
    ):
        resolved = threshold_view[index]
        return resolved if isinstance(resolved, QualityThresholdView) else None
    resolver = getattr(threshold_view, "for_feature", None)
    if callable(resolver):
        try:
            resolved = resolver(feature)
        except Exception:
            return None
        return resolved if isinstance(resolved, QualityThresholdView) else None
    resolver = getattr(threshold_view, "resolve", None)
    if callable(resolver):
        try:
            resolved = resolver(feature)
        except Exception:
            return None
        return resolved if isinstance(resolved, QualityThresholdView) else None
    if isinstance(threshold_view, Mapping):
        keys = (
            feature.normalized_page_path,
            feature.page_path,
            feature.score_family,
        )
        for key in keys:
            resolved = threshold_view.get(key)
            if isinstance(resolved, QualityThresholdView):
                return resolved
    return None


def _threshold_group(feature: CandidateFeature, view: QualityThresholdView | None) -> tuple[str, ...]:
    if view is not None and view.bucket_key:
        return ("bucket", view.bucket_key)
    return (
        "feature",
        feature.score_family,
        feature.effective_scope,
        feature.source_kind,
        feature.language_bucket,
        feature.retrieval_mode,
    )


def _threshold_decision(
    feature: CandidateFeature,
    view: QualityThresholdView | None,
    peak_score: float,
) -> tuple[bool, str]:
    """Apply only branch-relative threshold signals supplied by a view."""

    if view is None:
        return True, _keep_reason(feature)
    if view.fail_open:
        return True, view.fail_open_reason or GATE_FAIL_OPEN_POLICY_MISSING

    # Strong evidence and the first result remain protected.  The later
    # enforce rollout can tighten this rule only after holdout evidence exists;
    # the v0 policy must not turn a calibration defect into an empty result.
    if feature.exact_signal or feature.identifier_signal or feature.phrase_signal or feature.top1_rescue:
        return True, _keep_reason(feature)

    if view.score_ratio is not None and peak_score > 0:
        ratio = feature.score / peak_score
        if not math.isfinite(ratio) or ratio < view.score_ratio:
            return False, GATE_REJECT_SCORE_FLOOR
    if view.margin is not None and feature.branch_margin is not None and feature.branch_margin < view.margin:
        return False, GATE_REJECT_SCORE_CLIFF
    if view.term_coverage is not None and feature.term_coverage < view.term_coverage:
        if feature.independent_signal_count < 2:
            return False, GATE_REJECT_LOW_CONFIDENCE
    if feature.score_family == "raw_recovery" and view.term_coverage is not None:
        if feature.term_coverage <= 0 and feature.independent_signal_count == 0:
            return False, GATE_REJECT_RAW_NO_COVERAGE
    if feature.score_family == "graph_extension" and feature.independent_signal_count == 0:
        # Graph-only candidates are intentionally retained until an explicit
        # rollout policy decides otherwise; a threshold view alone is not that
        # evidence.
        return True, GATE_KEEP_GRAPH_SUPPORTED
    return True, _keep_reason(feature)


def evaluate_quality_policy(
    features: Sequence[CandidateFeature],
    *,
    policy_version: str = QUALITY_POLICY_VERSION,
    threshold_view: object | None = None,
) -> QualityGateResult:
    """执行纯策略；没有 threshold view 时保持 v0 keep-all 兼容。"""

    views = tuple(
        (
            None
            if threshold_view is None
            else _threshold_view_for(threshold_view, feature, index=index)
            or QualityThresholdView(
                selection="fail_open",
                fail_open=True,
                fail_open_reason=GATE_FAIL_OPEN_POLICY_MISSING,
                policy_version=policy_version,
            )
        )
        for index, feature in enumerate(features)
    )
    peaks: dict[tuple[str, ...], float] = {}
    for feature, view in zip(features, views):
        group = _threshold_group(feature, view)
        if math.isfinite(feature.score):
            peaks[group] = max(peaks.get(group, float("-inf")), feature.score)
    decisions = tuple(
        GateCandidateDecision(feature, *_threshold_decision(feature, view, peaks.get(_threshold_group(feature, view), 0.0)))
        for feature, view in zip(features, views)
    )
    accepted = tuple(decision for decision in decisions if decision.accepted)
    rejected = tuple(decision for decision in decisions if not decision.accepted)
    fail_open_views = tuple(view for view in views if view is not None and view.fail_open)
    low_sample_buckets = tuple(
        sorted({bucket for view in fail_open_views for bucket in view.low_sample_buckets})
    )
    fail_open = bool(fail_open_views)
    if fail_open:
        policy_version = next(
            (view.policy_version for view in fail_open_views if view.policy_version),
            policy_version,
        )
    calibration_revision = next(
        (view.calibration_revision for view in views if view is not None and view.calibration_revision),
        "",
    )
    threshold_selection_counts = _count_values(
        view.selection for view in views if view is not None
    )
    summary = GateSummary(
        candidate_count=len(decisions),
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        score_family_counts=_count_values(feature.score_family for feature in features),
        reason_counts=_count_values(decision.reason_code for decision in decisions),
        low_sample_buckets=low_sample_buckets,
        fail_open=fail_open,
        policy_version=policy_version,
        calibration_revision=calibration_revision,
        threshold_selection_counts=threshold_selection_counts,
    )
    return QualityGateResult(decisions, accepted, rejected, summary)


def evaluate_quality_gate(
    features: Sequence[CandidateFeature],
    *,
    policy_version: str = QUALITY_POLICY_VERSION,
    threshold_view: object | None = None,
) -> QualityGateResult:
    """对外语义别名；可选 view 不改变无 view 的 keep-all 行为。"""

    return evaluate_quality_policy(features, policy_version=policy_version, threshold_view=threshold_view)


def evaluate_candidates(
    candidates: Sequence[Mapping[str, Any] | object],
    *,
    policy_version: str = QUALITY_POLICY_VERSION,
    scope: str = "auto",
    effective_scope: str | None = None,
    retrieval_mode: str | None = None,
    language_bucket: str | None = None,
    branch: str | None = None,
    fallback_level: str | None = None,
    fallback_reason: str | None = None,
    query_terms: Sequence[str] = (),
    threshold_view: object | None = None,
) -> QualityGateResult:
    """便捷 seam：一次提取冻结特征并执行纯策略。"""

    features = build_candidate_features(
        candidates,
        scope=scope,
        effective_scope=effective_scope,
        retrieval_mode=retrieval_mode,
        language_bucket=language_bucket,
        branch=branch,
        fallback_level=fallback_level,
        fallback_reason=fallback_reason,
        query_terms=query_terms,
    )
    return evaluate_quality_policy(features, policy_version=policy_version, threshold_view=threshold_view)


__all__ = [
    "CandidateFeature",
    "FAIL_OPEN_REASON_CODES",
    "GATE_FAIL_OPEN_ERROR",
    "GATE_FAIL_OPEN_LOW_SAMPLE",
    "GATE_FAIL_OPEN_POLICY_MISSING",
    "GATE_KEEP_DEFAULT",
    "GATE_KEEP_EXACT_IDENTIFIER",
    "GATE_KEEP_GRAPH_SUPPORTED",
    "GATE_KEEP_MULTI_SIGNAL",
    "GATE_KEEP_PHRASE",
    "GATE_KEEP_RAW_COVERAGE",
    "GATE_KEEP_TERM_COVERAGE",
    "GATE_KEEP_TOP1_RESCUE",
    "GATE_REASON_CODES",
    "GATE_REJECT_GRAPH_ONLY_WEAK",
    "GATE_REJECT_LOW_CONFIDENCE",
    "GATE_REJECT_NO_INDEPENDENT_SIGNAL",
    "GATE_REJECT_RAW_NO_COVERAGE",
    "GATE_REJECT_SCORE_CLIFF",
    "GATE_REJECT_SCORE_FLOOR",
    "GateCandidateDecision",
    "GateSummary",
    "KEEP_REASON_CODES",
    "QUALITY_POLICY_VERSION",
    "QualityGateResult",
    "QualityThresholdView",
    "REJECT_REASON_CODES",
    "SCORE_FAMILIES",
    "ScoreFamily",
    "build_candidate_features",
    "derive_score_family",
    "evaluate_candidates",
    "evaluate_quality_gate",
    "evaluate_quality_policy",
    "extract_candidate_feature",
    "extract_candidate_features",
    "is_gate_reason_code",
    "is_score_family",
    "resolve_effective_scope",
]
