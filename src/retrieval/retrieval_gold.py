"""Read-only telemetry sampling and human-gold materialization for retrieval evals."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from retrieval.query_telemetry import redact_query
from retrieval.retrieval_eval import RetrievalEvalError, _parse_filters, vault_fingerprint
from wiki.knowledge_compiler import filesystem_path


GOLD_SCHEMA_VERSION = 2
DEFAULT_GOLD_COUNT = 50
_SCOPE_VALUES = {"auto", "knowledge", "history", "all", "archive", "raw"}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ENTITY_RE = re.compile(r"(?:\bN[/ ._-][\w/.-]+\b|\b(?:api|module|record|search|suitelet)\b|模块|接口)", re.I)
_FILTER_RE = re.compile(r"(?:project|type|tag|path|过滤|项目|标签|目录)", re.I)


class RetrievalGoldError(ValueError):
    """A user-correctable sampling or annotation error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TelemetryCandidate:
    query_hash: str
    query: str
    scope: str
    project: str
    has_passages: bool
    occurred_at: str
    candidate_tags: tuple[str, ...]
    language: str

    @property
    def key(self) -> str:
        return f"{self.query_hash}|{self.scope}|{self.project}"


def sample_retrieval_gold(
    vault_root: str | Path,
    output_dir: str | Path,
    *,
    count: int = DEFAULT_GOLD_COUNT,
    seed: str = "wiki-query-real-vault-v1",
    dataset_id: str = "codingwork-wiki-query-gold",
) -> dict[str, Any]:
    """Create a deterministic, redacted annotation template from telemetry.

    The source database is opened with SQLite's read-only URI mode. This function
    never instantiates :class:`QueryTelemetry`, because its constructor creates the
    telemetry directory/table when absent.
    """

    if count <= 0:
        raise RetrievalGoldError("invalid_sample_count", "sample count must be greater than zero")
    root = filesystem_path(vault_root)
    candidates = _read_candidates(root)
    if len(candidates) < count:
        raise RetrievalGoldError(
            "insufficient_candidates",
            f"telemetry has only {len(candidates)} deduplicated candidates; {count} required",
        )
    fingerprint = vault_fingerprint(root)
    selected, coverage = _select_candidates(candidates, count=count, seed=seed)
    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    template_path = target / "gold-template.jsonl"
    manifest_path = target / "gold-template.manifest.json"
    records = [_template_record(candidate, index) for index, candidate in enumerate(selected, 1)]
    _write_jsonl(template_path, records)
    manifest = {
        "schema_version": GOLD_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "revision": f"draft-{fingerprint['value'][:12]}-{_stable_seed(seed)[:12]}",
        "abstention_threshold": 0.0,
        "status": "draft",
        "vault": {"logical_name": "codingwork", "fingerprint": fingerprint},
        "sampling": {
            "count": count,
            "seed": seed,
            "candidate_count": len(candidates),
            "dedup_key": "query_hash|scope|project",
            "coverage_targets": _coverage_targets(),
            "coverage_actual": coverage["actual"],
            "coverage_gaps": coverage["gaps"],
        },
        "annotation": {
            "status": "pending",
            "instructions": "填写 answerable、relevant、grade、needs_review 和必要备注；不要依据当前排名标注。",
        },
        "provenance": {
            "source": "query_telemetry_read_only",
            "passage_ids_exported": False,
            "historical_paths_exported": False,
            "raw_body_exported": False,
        },
    }
    _write_json(manifest_path, manifest)
    return {
        "ok": True,
        "template": str(template_path),
        "manifest": str(manifest_path),
        "count": count,
        "coverage": coverage,
        "revision": manifest["revision"],
        "vault_fingerprint": fingerprint,
    }


def finalize_retrieval_gold(
    template_path: str | Path,
    manifest_path: str | Path,
    vault_root: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Validate human annotations and materialize evaluator-compatible gold files."""

    template_file = Path(template_path).expanduser().resolve()
    source_manifest_file = Path(manifest_path).expanduser().resolve()
    template = _read_jsonl(template_file)
    source_manifest = _read_json(source_manifest_file)
    _validate_draft_manifest(source_manifest)
    root = filesystem_path(vault_root)
    if not root.is_dir():
        raise RetrievalGoldError("vault_missing", "vault root is not a directory")

    final_records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(template, 1):
        record = _materialize_record(raw, line_number, root)
        final_records.append(record)
    if not final_records:
        raise RetrievalGoldError("dataset_empty", "annotation template is empty")

    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    cases_path = target / "gold.jsonl"
    final_manifest_path = target / "gold.manifest.json"
    _write_jsonl(cases_path, final_records)
    encoded = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in final_records)
    fingerprint = vault_fingerprint(root)
    revision = f"gold-{fingerprint['value'][:12]}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:12]}"
    final_manifest = dict(source_manifest)
    final_manifest.update(
        {
            "schema_version": GOLD_SCHEMA_VERSION,
            "revision": revision,
            "status": "reviewed",
            "vault": {"logical_name": "codingwork", "fingerprint": fingerprint},
            "annotation": {"status": "reviewed", "case_count": len(final_records), "needs_review": False},
            "provenance": {
                **dict(source_manifest.get("provenance") or {}),
                "materialized_from": template_file.name,
                "raw_body_exported": False,
            },
        }
    )
    _write_json(final_manifest_path, final_manifest)
    return {
        "ok": True,
        "dataset": str(cases_path),
        "manifest": str(final_manifest_path),
        "count": len(final_records),
        "revision": revision,
        "vault_fingerprint": fingerprint,
    }


def _read_candidates(root: Path) -> list[TelemetryCandidate]:
    database = root / ".llm-wiki" / "state.sqlite3"
    if not database.is_file():
        raise RetrievalGoldError("telemetry_missing", "query telemetry database does not exist")
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RetrievalGoldError("telemetry_unreadable", "query telemetry database could not be opened read-only") from exc
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(query_telemetry)")}
        required = {"query_hash", "normalized_query_redacted", "scope", "project", "passage_ids"}
        if not required.issubset(columns):
            raise RetrievalGoldError("telemetry_schema_invalid", "query telemetry schema is missing required fields")
        outcome_expression = "outcome" if "outcome" in columns else "'completed'"
        rows = connection.execute(
            "SELECT query_hash, normalized_query_redacted, scope, project, passage_ids, at "
            f"FROM query_telemetry WHERE {outcome_expression} = 'completed' "
            "AND normalized_query_redacted IS NOT NULL AND normalized_query_redacted <> ''"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RetrievalGoldError("telemetry_unreadable", "query telemetry could not be read") from exc
    finally:
        connection.close()

    candidates: dict[str, TelemetryCandidate] = {}
    for row in rows:
        query_hash = str(row[0] or "").strip()
        query = redact_query(str(row[1] or "")).strip()
        scope = str(row[2] or "auto").strip() or "auto"
        project = str(row[3] or "").strip()
        if not query or scope not in _SCOPE_VALUES:
            continue
        candidate = TelemetryCandidate(
            query_hash=query_hash or hashlib.sha256(query.encode("utf-8")).hexdigest(),
            query=query,
            scope=scope,
            project=project,
            has_passages=bool(str(row[4] or "").strip()),
            occurred_at=str(row[5] or ""),
            candidate_tags=_candidate_tags(query, scope, project, bool(str(row[4] or "").strip())),
            language=_language(query),
        )
        # Keep one record per query hash/scope/project. Prefer the most recent
        # representative only for deterministic provenance; no result paths are read.
        previous = candidates.get(candidate.key)
        if previous is None or candidate.occurred_at > previous.occurred_at:
            candidates[candidate.key] = candidate
    return list(candidates.values())


def _select_candidates(
    candidates: Iterable[TelemetryCandidate],
    *,
    count: int,
    seed: str,
) -> tuple[list[TelemetryCandidate], dict[str, Any]]:
    ordered = sorted(candidates, key=lambda item: _sort_key(item, seed))
    targets = _coverage_targets()
    selected: list[TelemetryCandidate] = []
    selected_keys: set[str] = set()
    for tag, minimum in targets.items():
        pool = [item for item in ordered if tag in item.candidate_tags and item.key not in selected_keys]
        for item in pool[:minimum]:
            selected.append(item)
            selected_keys.add(item.key)
    for item in ordered:
        if len(selected) >= count:
            break
        if item.key not in selected_keys:
            selected.append(item)
            selected_keys.add(item.key)
    selected = sorted(selected[:count], key=lambda item: _sort_key(item, seed))
    actual = {tag: sum(tag in item.candidate_tags for item in selected) for tag in targets}
    gaps = {tag: minimum - actual[tag] for tag, minimum in targets.items() if actual[tag] < minimum}
    return selected, {"actual": actual, "gaps": gaps}


def _coverage_targets() -> dict[str, int]:
    return {"knowledge_auto": 20, "no_answer_candidate": 10, "project_filter": 10, "chinese": 5, "entity_api": 5}


def _template_record(candidate: TelemetryCandidate, index: int) -> dict[str, Any]:
    filters: dict[str, str] = {"project": candidate.project} if candidate.project else {}
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "id": f"codingwork-{index:04d}",
        "query": candidate.query,
        "scope": candidate.scope,
        "filters": filters,
        "language": candidate.language,
        "candidate_tags": list(candidate.candidate_tags),
        "answerable": None,
        "relevant": None,
        "needs_review": True,
        "annotator_note": "",
    }


def _materialize_record(raw: Mapping[str, Any], line_number: int, root: Path) -> dict[str, Any]:
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise RetrievalGoldError("case_invalid", f"line {line_number}: id must be a non-empty string")
    if raw.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise RetrievalGoldError("unsupported_schema", f"case {case_id}: schema_version must be {GOLD_SCHEMA_VERSION}")
    query = raw.get("query")
    if not isinstance(query, str) or not query.strip():
        raise RetrievalGoldError("case_invalid", f"case {case_id}: query is required")
    answerable = raw.get("answerable")
    if type(answerable) is not bool:
        raise RetrievalGoldError("annotation_incomplete", f"case {case_id}: answerable must be true or false")
    if raw.get("needs_review") is not False:
        raise RetrievalGoldError("annotation_needs_review", f"case {case_id}: needs_review must be false")
    scope = raw.get("scope", "knowledge")
    if scope not in _SCOPE_VALUES:
        raise RetrievalGoldError("invalid_scope", f"case {case_id}: unsupported scope")
    filters = raw.get("filters", {})
    if not isinstance(filters, dict):
        raise RetrievalGoldError("invalid_filters", f"case {case_id}: filters must be an object")
    try:
        normalized_filters = _parse_filters(filters, case_id)
    except RetrievalEvalError as exc:
        raise RetrievalGoldError("invalid_filters", str(exc)) from exc
    relevant_raw = raw.get("relevant")
    if not isinstance(relevant_raw, list):
        raise RetrievalGoldError("annotation_incomplete", f"case {case_id}: relevant must be a list")
    relevant: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in relevant_raw:
        if not isinstance(item, dict):
            raise RetrievalGoldError("case_invalid", f"case {case_id}: relevant entries must be objects")
        path = _safe_relative_path(item.get("path"), case_id)
        grade = item.get("grade")
        if type(grade) is not int or not 1 <= grade <= 3:
            raise RetrievalGoldError("invalid_grade", f"case {case_id}: grade must be an integer from 1 to 3")
        if path in seen_paths:
            raise RetrievalGoldError("duplicate_relevant_path", f"case {case_id}: duplicate path {path}")
        target = (root / Path(*path.split("/"))).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise RetrievalGoldError("relevant_path_missing", f"case {case_id}: path does not exist in the vault")
        seen_paths.add(path)
        relevant.append({"path": path, "grade": grade})
    if answerable and not relevant:
        raise RetrievalGoldError("missing_relevant_path", f"case {case_id}: answerable case needs a relevant path")
    if not answerable and relevant:
        raise RetrievalGoldError("invalid_no_answer_case", f"case {case_id}: answerable=false requires relevant=[]")
    language = raw.get("language", "unknown")
    if not isinstance(language, str) or not language.strip():
        raise RetrievalGoldError("case_invalid", f"case {case_id}: language must be a string")
    tags = raw.get("tags", raw.get("candidate_tags", []))
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise RetrievalGoldError("case_invalid", f"case {case_id}: tags must be a list of strings")
    notes = raw.get("annotator_note", raw.get("notes", ""))
    if not isinstance(notes, str):
        raise RetrievalGoldError("case_invalid", f"case {case_id}: annotator_note must be a string")
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "id": case_id.strip(),
        "query": redact_query(query.strip()),
        "scope": scope,
        "filters": normalized_filters,
        "answerable": answerable,
        "relevant": relevant,
        "language": language.strip(),
        "tags": list(dict.fromkeys(tag.strip() for tag in tags)),
        "notes": notes,
    }


def _validate_draft_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise RetrievalGoldError("unsupported_schema", f"manifest must use schema_version={GOLD_SCHEMA_VERSION}")
    if not isinstance(manifest.get("dataset_id"), str) or not manifest["dataset_id"].strip():
        raise RetrievalGoldError("manifest_invalid", "manifest.dataset_id is required")
    if manifest.get("status") != "draft":
        raise RetrievalGoldError("manifest_invalid", "annotation manifest must have status=draft")


def _safe_relative_path(value: object, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalGoldError("invalid_relevant_path", f"case {case_id}: path must be a string")
    path = value.replace("\\", "/").strip()
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise RetrievalGoldError("invalid_relevant_path", f"case {case_id}: path must be vault-relative")
    return pure.as_posix()


def _candidate_tags(query: str, scope: str, project: str, has_passages: bool) -> tuple[str, ...]:
    tags: list[str] = []
    if scope in {"knowledge", "auto"}:
        tags.append("knowledge_auto")
    if not has_passages:
        tags.append("no_answer_candidate")
    if project:
        tags.append("project_filter")
    if _CJK_RE.search(query):
        tags.append("chinese")
    if _ENTITY_RE.search(query):
        tags.append("entity_api")
    if _FILTER_RE.search(query):
        tags.append("filter_query")
    return tuple(tags)


def _language(query: str) -> str:
    has_cjk = bool(_CJK_RE.search(query))
    has_latin = bool(re.search(r"[A-Za-z]", query))
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "zh"
    if has_latin:
        return "en"
    return "unknown"


def _sort_key(candidate: TelemetryCandidate, seed: str) -> str:
    return hashlib.sha256(f"{seed}\0{candidate.key}".encode("utf-8")).hexdigest()


def _stable_seed(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RetrievalGoldError("template_missing", f"annotation file does not exist: {path.name}")
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RetrievalGoldError("template_unreadable", "annotation file could not be read") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetrievalGoldError("case_invalid_json", f"line {line_number}: invalid JSON") from exc
        if not isinstance(raw, dict):
            raise RetrievalGoldError("case_invalid", f"line {line_number}: case must be an object")
        records.append(raw)
    return records


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalGoldError("manifest_invalid", "annotation manifest could not be read") from exc
    if not isinstance(raw, dict):
        raise RetrievalGoldError("manifest_invalid", "annotation manifest must be an object")
    return raw


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    text = "\n".join(json.dumps(dict(record), ensure_ascii=False, sort_keys=True) for record in records) + "\n"
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "DEFAULT_GOLD_COUNT",
    "GOLD_SCHEMA_VERSION",
    "RetrievalGoldError",
    "finalize_retrieval_gold",
    "sample_retrieval_gold",
]
