"""检索评测数据集的 schema、解析和过滤器投影 owner。

本模块只负责数据集文件与内存 schema 的解释。它不解析 vault、不创建索引，
也不依赖查询运行时，因此可以在没有 vault 的情况下直接测试所有输入校验。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.privacy_policy import LocatorError, normalize_vault_relative
from retrieval.metadata_filters import QUERY_METADATA_FILTERS, normalize_metadata_filters


RETRIEVAL_EVAL_SCHEMA_VERSION = 1
_SUPPORTED_DATASET_SCHEMA_VERSIONS = frozenset({1, 2})
_ALLOWED_FILTERS = frozenset(
    {"project", "filter_type", "filter_tags", "type", "tags", "path_prefix", "pathPrefix"}
)

__all__ = [
    "RETRIEVAL_EVAL_SCHEMA_VERSION",
    "EvaluationFilterContract",
    "Relevance",
    "RetrievalEvalCase",
    "RetrievalEvalDataset",
    "RetrievalEvalError",
    "RetrievalEvalManifest",
    "load_retrieval_dataset",
    "normalize_evaluation_filter_contract",
    "parse_evaluation_case",
    "parse_evaluation_filters",
    "parse_evaluation_manifest",
]


class RetrievalEvalError(ValueError):
    """可由用户修正的数据集或评测配置错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EvaluationFilterContract:
    """同一组过滤器在数据集、MCP、查询和页面匹配层的纯投影。"""

    internal: Mapping[str, Any]
    public: Mapping[str, Any]
    query: Mapping[str, Any]
    matcher: Mapping[str, Any]


@dataclass(frozen=True)
class Relevance:
    path: str
    grade: int


@dataclass(frozen=True)
class RetrievalEvalCase:
    id: str
    query: str
    relevant: tuple[Relevance, ...]
    filters: dict[str, Any]
    answerable: bool
    language: str
    tags: tuple[str, ...]
    notes: str
    scope: str = "knowledge"


@dataclass(frozen=True)
class RetrievalEvalManifest:
    dataset_id: str
    revision: str
    abstention_threshold: float
    status: str = "reviewed"
    schema_version: int = RETRIEVAL_EVAL_SCHEMA_VERSION


@dataclass(frozen=True)
class RetrievalEvalDataset:
    manifest: RetrievalEvalManifest
    cases: tuple[RetrievalEvalCase, ...]


def load_retrieval_dataset(
    dataset_path: str | Path,
    manifest_path: str | Path | None = None,
) -> RetrievalEvalDataset:
    """读取版本化 JSONL 数据集和同名 manifest。

    文件读取只发生在这个薄入口；schema 解释由下方纯解析函数完成，因而
    测试解析错误时不需要准备 vault。
    """

    cases_file = Path(dataset_path).expanduser().resolve()
    manifest_file = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else cases_file.with_name(f"{cases_file.stem}.manifest.json")
    )
    if not cases_file.is_file():
        raise RetrievalEvalError("dataset_missing", f"dataset does not exist: {cases_file}")
    if not manifest_file.is_file():
        raise RetrievalEvalError("manifest_missing", f"manifest does not exist: {manifest_file}")

    try:
        manifest_raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalEvalError("manifest_invalid", f"invalid manifest: {exc}") from exc
    manifest = parse_evaluation_manifest(manifest_raw)

    try:
        lines = cases_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RetrievalEvalError("dataset_unreadable", f"cannot read dataset: {exc}") from exc

    cases: list[RetrievalEvalCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetrievalEvalError("case_invalid_json", f"line {line_number}: {exc.msg}") from exc
        case = parse_evaluation_case(raw, line_number)
        if case.id in seen_ids:
            raise RetrievalEvalError("duplicate_case_id", f"duplicate case id: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise RetrievalEvalError("dataset_empty", "dataset must contain at least one case")
    return RetrievalEvalDataset(manifest=manifest, cases=tuple(cases))


def parse_evaluation_manifest(raw: object) -> RetrievalEvalManifest:
    """纯函数：把 manifest JSON 值解码为不可变 schema。"""

    data = _mapping(raw, "manifest_invalid", "manifest must be a JSON object")
    _require_schema_version(data, "manifest")
    dataset_id = _nonempty_string(data.get("dataset_id"), "manifest_invalid", "manifest.dataset_id must be a string")
    revision = _nonempty_string(data.get("revision"), "manifest_invalid", "manifest.revision must be a string")
    threshold = data.get("abstention_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        raise RetrievalEvalError("manifest_invalid", "manifest.abstention_threshold must be a non-negative number")
    schema_version = int(data.get("schema_version", RETRIEVAL_EVAL_SCHEMA_VERSION))
    status = data.get("status", "reviewed")
    if not isinstance(status, str) or not status.strip():
        raise RetrievalEvalError("manifest_invalid", "manifest.status must be a string")
    return RetrievalEvalManifest(
        dataset_id=dataset_id,
        revision=revision,
        abstention_threshold=float(threshold),
        status=status.strip(),
        schema_version=schema_version,
    )


def parse_evaluation_case(raw: object, line_number: int) -> RetrievalEvalCase:
    """纯函数：把一条 JSONL 记录解码为评测 case。"""

    data = _mapping(raw, "case_invalid", f"line {line_number} must be a JSON object")
    _require_schema_version(data, f"case at line {line_number}")
    case_id = _nonempty_string(data.get("id"), "case_invalid", f"line {line_number}: id must be a string")
    query = _nonempty_string(data.get("query"), "case_invalid", f"case {case_id}: query must be a string")
    answerable = data.get("answerable")
    if type(answerable) is not bool:
        raise RetrievalEvalError("case_invalid", f"case {case_id}: answerable must be boolean")

    filters = parse_evaluation_filters(data.get("filters", {}), case_id)
    relevant_raw = data.get("relevant")
    if not isinstance(relevant_raw, list):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: relevant must be a list")
    relevance: list[Relevance] = []
    seen_paths: set[str] = set()
    for item in relevant_raw:
        item_data = _mapping(item, "case_invalid", f"case {case_id}: relevant item must be an object")
        path = _normalise_relative_path(item_data.get("path"), case_id)
        grade = item_data.get("grade")
        if type(grade) is not int or not 1 <= grade <= 3:
            raise RetrievalEvalError("invalid_grade", f"case {case_id}: grade for {path} must be an integer from 1 to 3")
        if path in seen_paths:
            raise RetrievalEvalError("duplicate_relevant_path", f"case {case_id}: duplicate relevant path {path}")
        seen_paths.add(path)
        relevance.append(Relevance(path=path, grade=grade))
    if not answerable and relevance:
        raise RetrievalEvalError("invalid_no_answer_case", f"case {case_id}: answerable=false requires no relevant paths")
    if answerable and not relevance:
        raise RetrievalEvalError("missing_relevant_path", f"case {case_id}: answerable=true requires at least one relevant path")

    language = _nonempty_string(data.get("language"), "case_invalid", f"case {case_id}: language must be a string")
    scope = data.get("scope", "knowledge")
    if not isinstance(scope, str) or scope not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        raise RetrievalEvalError("invalid_scope", f"case {case_id}: scope must be a supported query scope")
    tags_raw = data.get("tags", [])
    if not isinstance(tags_raw, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags_raw):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: tags must be strings")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: notes must be a string")
    return RetrievalEvalCase(case_id, query, tuple(relevance), filters, answerable, language, tuple(tags_raw), notes, scope)


def normalize_evaluation_filter_contract(
    raw: object,
    case_id: str = "<unknown>",
) -> EvaluationFilterContract:
    """纯函数：集中完成 public/legacy alias 配对和四种投影。"""

    data = _mapping(raw, "invalid_filters", f"case {case_id}: filters must be an object")
    unknown = sorted(set(data) - _ALLOWED_FILTERS)
    if unknown:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: unsupported filters: {', '.join(unknown)}")

    project: str | None = None
    if "project" in data:
        project = _nonempty_string(data["project"], "invalid_filters", f"case {case_id}: project must be a string")

    public_input: dict[str, Any] = {}
    for public_name, legacy_name in (("type", "filter_type"), ("tags", "filter_tags")):
        if public_name in data and legacy_name in data and data[public_name] != data[legacy_name]:
            raise RetrievalEvalError("invalid_filters", f"case {case_id}: {public_name} and {legacy_name} disagree")
        if public_name in data:
            public_input[public_name] = data[public_name]
        elif legacy_name in data:
            public_input[public_name] = data[legacy_name]
    if "type" in public_input and (not isinstance(public_input["type"], str) or not public_input["type"].strip()):
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: type must be a non-empty string")
    if "path_prefix" in data:
        public_input["path_prefix"] = data["path_prefix"]
    if "pathPrefix" in data:
        public_input["pathPrefix"] = data["pathPrefix"]

    try:
        normalized = normalize_metadata_filters(
            public_input,
            allowed=QUERY_METADATA_FILTERS,
            preserve_path_trailing=True,
        )
    except ValueError as exc:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: {exc}") from exc

    internal: dict[str, Any] = {}
    if project is not None:
        internal["project"] = project
    if "type" in normalized:
        internal["filter_type"] = normalized["type"]
    if "tags" in normalized:
        tags = list(normalized["tags"])
        if not tags:
            raise RetrievalEvalError("invalid_filters", f"case {case_id}: tags must be a non-empty list of strings")
        internal["filter_tags"] = tags
    if "path_prefix" in normalized:
        internal["path_prefix"] = normalized["path_prefix"]
    elif "path_prefix" in public_input or "pathPrefix" in public_input:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: path_prefix must be a non-empty vault-relative string")

    public: dict[str, Any] = {}
    if "type" in normalized:
        public["type"] = normalized["type"]
    if "tags" in normalized:
        public["tags"] = list(normalized["tags"])
    if "path_prefix" in normalized:
        public["path_prefix"] = normalized["path_prefix"]

    matcher = dict(normalized)
    if project is not None:
        matcher["project"] = project
    return EvaluationFilterContract(
        internal=internal,
        public=public,
        query=dict(public),
        matcher=matcher,
    )


def parse_evaluation_filters(raw: object, case_id: str = "<unknown>") -> dict[str, Any]:
    """兼容 facade：返回历史数据集内部过滤器形状。"""

    return dict(normalize_evaluation_filter_contract(raw, case_id).internal)


def _parse_filters(raw: object, case_id: str) -> dict[str, Any]:
    """旧调用方的兼容别名；新代码应调用公开 facade。"""

    return parse_evaluation_filters(raw, case_id)


def _mapping(raw: object, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise RetrievalEvalError(code, message)
    return raw


def _require_schema_version(data: Mapping[str, Any], subject: str) -> None:
    if data.get("schema_version") not in _SUPPORTED_DATASET_SCHEMA_VERSIONS:
        versions = ", ".join(str(value) for value in sorted(_SUPPORTED_DATASET_SCHEMA_VERSIONS))
        raise RetrievalEvalError("unsupported_schema", f"{subject} must use schema_version in {{{versions}}}")


def _nonempty_string(value: object, code: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalEvalError(code, message)
    return value.strip()


def _normalise_relative_path(value: object, case_id: str) -> str:
    path = _nonempty_string(value, "case_invalid", f"case {case_id}: relevant path must be a string")
    try:
        return normalize_vault_relative(path, check_sensitive=False)
    except LocatorError as exc:
        raise RetrievalEvalError("invalid_relevant_path", f"case {case_id}: relevant path must be vault-relative") from exc
