"""Calibration artifacts for the Query V2 rule-quality gate.

This module owns the offline artifact contract only.  It does not load a
retrieval store, alter Query V2, or rebuild missing data.  Runtime consumers
can load one immutable view and pass its branch-relative threshold views to the
pure policy owner in :mod:`retrieval.query_quality_policy`.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Literal

from retrieval.query_quality_policy import (
    CandidateFeature,
    GATE_FAIL_OPEN_ERROR,
    GATE_FAIL_OPEN_LOW_SAMPLE,
    GATE_FAIL_OPEN_POLICY_MISSING,
    QUALITY_POLICY_VERSION,
    QualityThresholdView,
    extract_candidate_feature,
    is_score_family,
)


CALIBRATION_ARTIFACT_SCHEMA_VERSION = 1
CALIBRATION_FEATURE_SCHEMA_VERSION = "query-quality-feature-v1"
# This identifies the feature contract, not a score or threshold.  It is
# deliberately stable until the CandidateFeature fields change.
CALIBRATION_FEATURE_SCHEMA_HASH = "query-quality-feature-v1"
DEFAULT_MINIMUM_SAMPLE_COUNT = 10
DEFAULT_CALIBRATION_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "irrelevant_result": 1.0,
        "false_suppression": 1.0,
        "no_answer_false_positive": 5.0,
    }
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_BUCKET_TOKEN = re.compile(r"^[A-Za-z0-9_*][A-Za-z0-9_.*:-]{0,127}$")
_VALID_SCOPES = frozenset({"knowledge", "history", "all", "archive", "raw", "*"})
_VALID_RETRIEVAL_MODES = frozenset({"lexical", "vector", "hybrid", "*"})
_WILDCARD = "*"


class CalibrationArtifactError(ValueError):
    """A schema or generation error at the explicit calibration boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CalibrationGenerationError(CalibrationArtifactError):
    """Raised when the admin generator input cannot form a valid artifact."""


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _mapping(value: object, *, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CalibrationArtifactError(code, message)
    return value


def _nonempty(value: object, *, code: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationArtifactError(code, message)
    return value.strip()


def _safe_number(value: object, *, code: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CalibrationArtifactError(code, f"{field} must be a finite number")
    return float(value)


def _safe_non_negative_int(value: object, *, code: str, field: str) -> int:
    if type(value) is not int or value < 0:
        raise CalibrationArtifactError(code, f"{field} must be a non-negative integer")
    return value


def _safe_token(value: object, *, code: str, field: str) -> str:
    token = _nonempty(value, code=code, message=f"{field} must be a non-empty string")
    if not _SAFE_TOKEN.fullmatch(token):
        raise CalibrationArtifactError(code, f"{field} is not a safe identifier")
    return token


def _validated_weights(value: object) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("weights must be a mapping")
    result: dict[str, float] = {}
    for name, raw_number in value.items():
        key = _safe_token(name, code="artifact_schema_invalid", field="weight name")
        if isinstance(raw_number, bool) or not isinstance(raw_number, (int, float)):
            raise ValueError(f"weights.{key} must be a finite number")
        number = float(raw_number)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"weights.{key} must be non-negative")
        result[key] = number
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class CalibrationIdentity:
    """Identity frozen with an artifact; mappings are recursively immutable."""

    dataset_id: str
    dataset_revision: str
    vault_fingerprint: Mapping[str, Any] | str
    ranking_policy_version: str
    runtime_provenance: Mapping[str, Any]
    feature_schema_hash: str
    config_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vault_fingerprint", _freeze(self.vault_fingerprint))
        object.__setattr__(self, "runtime_provenance", _freeze(self.runtime_provenance))
        for field_name in (
            "dataset_id",
            "dataset_revision",
            "ranking_policy_version",
            "feature_schema_hash",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.vault_fingerprint, (str, Mapping)):
            raise ValueError("vault_fingerprint must be a mapping or string")
        if not isinstance(self.runtime_provenance, Mapping) or not self.runtime_provenance:
            raise ValueError("runtime_provenance must be a non-empty mapping")
        if self.config_hash is not None and (not isinstance(self.config_hash, str) or not self.config_hash.strip()):
            raise ValueError("config_hash must be a non-empty string when supplied")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | "CalibrationIdentity") -> "CalibrationIdentity":
        if isinstance(raw, cls):
            return raw
        data = _mapping(raw, code="artifact_identity_invalid", message="identity must be an object")
        ranking = data.get("ranking_policy_version", data.get("ranking_version"))
        return cls(
            dataset_id=_nonempty(data.get("dataset_id"), code="artifact_identity_invalid", message="identity.dataset_id is required"),
            dataset_revision=_nonempty(
                data.get("dataset_revision", data.get("revision")),
                code="artifact_identity_invalid",
                message="identity.dataset_revision is required",
            ),
            vault_fingerprint=data.get("vault_fingerprint")
            if isinstance(data.get("vault_fingerprint"), (str, Mapping))
            else (_raise_identity("identity.vault_fingerprint is required")),
            ranking_policy_version=_nonempty(
                ranking,
                code="artifact_identity_invalid",
                message="identity.ranking_policy_version is required",
            ),
            runtime_provenance=_mapping(
                data.get("runtime_provenance"),
                code="artifact_identity_invalid",
                message="identity.runtime_provenance is required",
            ),
            feature_schema_hash=_nonempty(
                data.get("feature_schema_hash"),
                code="artifact_identity_invalid",
                message="identity.feature_schema_hash is required",
            ),
            config_hash=data.get("config_hash") if data.get("config_hash") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "dataset_revision": self.dataset_revision,
            "vault_fingerprint": _thaw(self.vault_fingerprint),
            "ranking_policy_version": self.ranking_policy_version,
            "runtime_provenance": _thaw(self.runtime_provenance),
            "feature_schema_hash": self.feature_schema_hash,
        }
        if self.config_hash is not None:
            result["config_hash"] = self.config_hash
        return result


def _raise_identity(message: str) -> str:
    raise CalibrationArtifactError("artifact_identity_invalid", message)


@dataclass(frozen=True, order=True)
class CalibrationBucketKey:
    """A bucket key; source kind is optional for four-dimension compatibility."""

    score_family: str
    effective_scope: str
    language_bucket: str
    retrieval_mode: str
    source_kind: str = _WILDCARD

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (self.score_family, self.effective_scope, self.language_bucket, self.retrieval_mode, self.source_kind)):
            raise ValueError("calibration bucket dimensions must be strings")
        family = self.score_family.casefold()
        scope = self.effective_scope.casefold()
        language = self.language_bucket.casefold()
        mode = self.retrieval_mode.casefold()
        source = self.source_kind.casefold()
        if not is_score_family(family):
            raise ValueError(f"unsupported score family: {self.score_family!r}")
        if scope not in _VALID_SCOPES:
            raise ValueError(f"unsupported effective scope: {self.effective_scope!r}")
        if not _SAFE_BUCKET_TOKEN.fullmatch(language):
            raise ValueError("language_bucket is not safe")
        if mode not in _VALID_RETRIEVAL_MODES:
            raise ValueError(f"unsupported retrieval mode: {self.retrieval_mode!r}")
        if source in {"", "any", "all"}:
            source = _WILDCARD
        if not _SAFE_BUCKET_TOKEN.fullmatch(source):
            raise ValueError("source_kind is not safe")
        object.__setattr__(self, "score_family", family)
        object.__setattr__(self, "effective_scope", scope)
        object.__setattr__(self, "language_bucket", language)
        object.__setattr__(self, "retrieval_mode", mode)
        object.__setattr__(self, "source_kind", source)

    @classmethod
    def from_feature(cls, feature: CandidateFeature) -> "CalibrationBucketKey":
        return cls(
            feature.score_family,
            feature.effective_scope,
            feature.language_bucket or "unknown",
            feature.retrieval_mode or "lexical",
            feature.source_kind or _WILDCARD,
        )

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        """Return the canonical five-field order used by validation."""

        return (
            self.score_family,
            self.effective_scope,
            self.source_kind,
            self.language_bucket,
            self.retrieval_mode,
        )

    def encode(self) -> str:
        # Four-field keys remain readable and compatible with the original PRD
        # when source_kind is the wildcard dimension.
        if self.source_kind == _WILDCARD:
            return "|".join((self.score_family, self.effective_scope, self.language_bucket, self.retrieval_mode))
        return "|".join(self.as_tuple())

    def __str__(self) -> str:
        return self.encode()


def _parse_bucket_parts(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value)
    if not isinstance(value, str):
        raise CalibrationArtifactError("artifact_bucket_invalid", "bucket key must be a string or tuple")
    text = value.strip()
    if text.startswith(("[", "(")) and text.endswith(("]", ")")):
        text = text[1:-1].strip()
    if text.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return tuple(str(item).strip() for item in parsed)
    separator = "|" if "|" in text else ","
    return tuple(part.strip().strip("'\"") for part in text.split(separator))


def parse_bucket_key(value: object) -> CalibrationBucketKey:
    """Decode four- or five-dimensional bucket keys deterministically."""

    parts = _parse_bucket_parts(value)
    try:
        if len(parts) == 4:
            family, scope, language, mode = parts
            return CalibrationBucketKey(family, scope, language, mode)
        if len(parts) == 5:
            family, scope, third, fourth, fifth = parts
            if third.casefold() in {"wiki", "raw", "formal", "project", "raw_chat", "*", "any", "all"}:
                return CalibrationBucketKey(family, scope, fourth, fifth, third)
            # Accept the four-field PRD plus source kind appended by early
            # generators as a compatibility form.
            return CalibrationBucketKey(family, scope, third, fourth, fifth)
    except (TypeError, ValueError) as exc:
        raise CalibrationArtifactError("artifact_bucket_invalid", "invalid calibration bucket key") from exc
    raise CalibrationArtifactError("artifact_bucket_invalid", "bucket key must have four or five fields")


@dataclass(frozen=True, init=False)
class CalibrationThresholds:
    """Branch-relative selected thresholds; absolute score fields are absent."""

    score_ratio: float | None
    margin: float | None
    term_coverage: float | None

    def __init__(
        self,
        score_ratio: float | None = None,
        margin: float | None = None,
        term_coverage: float | None = None,
        *,
        ratio: float | None = None,
        coverage: float | None = None,
    ) -> None:
        if score_ratio is not None and ratio is not None and float(score_ratio) != float(ratio):
            raise ValueError("score_ratio and ratio disagree")
        if term_coverage is not None and coverage is not None and float(term_coverage) != float(coverage):
            raise ValueError("term_coverage and coverage disagree")
        object.__setattr__(self, "score_ratio", score_ratio if score_ratio is not None else ratio)
        object.__setattr__(self, "margin", margin)
        object.__setattr__(self, "term_coverage", term_coverage if term_coverage is not None else coverage)
        self._validate()

    def _validate(self) -> None:
        for field, value, minimum, maximum in (
            ("score_ratio", self.score_ratio, 0.0, 1.0),
            ("term_coverage", self.term_coverage, 0.0, 1.0),
        ):
            if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or not minimum <= float(value) <= maximum):
                raise ValueError(f"{field} must be between {minimum} and {maximum}")
        if self.margin is not None and (
            isinstance(self.margin, bool) or not math.isfinite(float(self.margin)) or float(self.margin) < 0
        ):
            raise ValueError("margin must be a finite non-negative number")

    @property
    def ratio(self) -> float | None:
        return self.score_ratio

    @property
    def coverage(self) -> float | None:
        return self.term_coverage

    def to_dict(self) -> dict[str, float]:
        result: dict[str, float] = {}
        if self.score_ratio is not None:
            result["ratio"] = float(self.score_ratio)
        if self.margin is not None:
            result["margin"] = float(self.margin)
        if self.term_coverage is not None:
            result["coverage"] = float(self.term_coverage)
        return result

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CalibrationThresholds":
        forbidden = {"score", "absolute_score", "score_floor", "absolute_score_floor"}
        if forbidden.intersection(raw):
            raise CalibrationArtifactError(
                "artifact_threshold_invalid",
                "absolute score thresholds are not allowed; use branch-relative ratio, margin, or coverage",
            )
        allowed = {"ratio", "score_ratio", "ratio_grid", "margin", "coverage", "term_coverage"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise CalibrationArtifactError("artifact_threshold_invalid", f"unknown threshold field: {unknown[0]}")

        def selected(*names: str) -> object | None:
            for name in names:
                if name in raw:
                    value = raw[name]
                    if isinstance(value, Mapping):
                        value = value.get("selected", value.get("value"))
                    if isinstance(value, list):
                        raise CalibrationArtifactError("artifact_threshold_invalid", f"{name} must contain a selected value")
                    return value
            return None

        values = {
            "score_ratio": selected("score_ratio", "ratio", "ratio_grid"),
            "margin": selected("margin"),
            "term_coverage": selected("term_coverage", "coverage"),
        }
        try:
            thresholds = cls(**values)
        except (TypeError, ValueError) as exc:
            raise CalibrationArtifactError("artifact_threshold_invalid", "invalid branch-relative threshold") from exc
        if not any(value is not None for value in values.values()):
            raise CalibrationArtifactError("artifact_threshold_invalid", "bucket thresholds cannot be empty")
        return thresholds


@dataclass(frozen=True)
class CalibrationBucket:
    sample_count: int
    thresholds: CalibrationThresholds
    backoff_to: CalibrationBucketKey | None = None
    evidence_status: Literal["proven", "unproven"] = "proven"
    label_count: int | None = None

    def __post_init__(self) -> None:
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("sample_count must be a non-negative integer")
        if self.evidence_status not in {"proven", "unproven"}:
            raise ValueError("evidence_status must be proven or unproven")
        if self.label_count is not None and (type(self.label_count) is not int or self.label_count < 0):
            raise ValueError("label_count must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sample_count": self.sample_count,
            "thresholds": self.thresholds.to_dict(),
            "evidence_status": self.evidence_status,
        }
        if self.backoff_to is not None:
            result["backoff_to"] = self.backoff_to.encode()
        if self.label_count is not None:
            result["label_count"] = self.label_count
        return result


@dataclass(frozen=True)
class CalibrationArtifact:
    schema_version: int
    policy_version: str
    calibration_revision: str
    identity: CalibrationIdentity
    minimum_sample_count: int
    buckets: Mapping[CalibrationBucketKey, CalibrationBucket]
    weights: Mapping[str, float] = DEFAULT_CALIBRATION_WEIGHTS
    status: Literal["proven", "unproven"] = "unproven"
    global_bucket: CalibrationBucketKey | None = None
    backoff_order: tuple[str, ...] = ("language", "source_kind", "scope", "global")

    def __post_init__(self) -> None:
        object.__setattr__(self, "buckets", MappingProxyType(dict(self.buckets)))
        object.__setattr__(self, "weights", _validated_weights(self.weights))
        if self.schema_version != CALIBRATION_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported calibration artifact schema_version")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be a non-empty string")
        if not isinstance(self.calibration_revision, str) or not self.calibration_revision.strip():
            raise ValueError("calibration_revision must be a non-empty string")
        if not isinstance(self.identity, CalibrationIdentity):
            raise ValueError("identity must be a CalibrationIdentity")
        if not isinstance(self.buckets, Mapping) or any(
            not isinstance(key, CalibrationBucketKey) or not isinstance(value, CalibrationBucket)
            for key, value in self.buckets.items()
        ):
            raise ValueError("buckets must map CalibrationBucketKey to CalibrationBucket")
        if type(self.minimum_sample_count) is not int or self.minimum_sample_count <= 0:
            raise ValueError("minimum_sample_count must be a positive integer")
        if self.status not in {"proven", "unproven"}:
            raise ValueError("status must be proven or unproven")
        if self.global_bucket is not None and not isinstance(self.global_bucket, CalibrationBucketKey):
            raise ValueError("global_bucket must be a CalibrationBucketKey")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "calibration_revision": self.calibration_revision,
            "identity": self.identity.to_dict(),
            "minimum_sample_count": self.minimum_sample_count,
            "status": self.status,
            "weights": dict(sorted((str(key), float(value)) for key, value in self.weights.items())),
            "buckets": {key.encode(): bucket.to_dict() for key, bucket in sorted(self.buckets.items())},
            "global_bucket": self.global_bucket.encode() if self.global_bucket is not None else None,
            "backoff_order": list(self.backoff_order),
        }


def _parse_bucket(raw: object, key: CalibrationBucketKey) -> CalibrationBucket:
    data = _mapping(raw, code="artifact_bucket_invalid", message=f"bucket {key.encode()} must be an object")
    sample_count = _safe_non_negative_int(data.get("sample_count"), code="artifact_bucket_invalid", field="sample_count")
    thresholds_raw = _mapping(
        data.get("thresholds"),
        code="artifact_threshold_invalid",
        message=f"bucket {key.encode()} thresholds must be an object",
    )
    try:
        thresholds = CalibrationThresholds.from_mapping(thresholds_raw)
    except CalibrationArtifactError:
        raise
    except ValueError as exc:
        raise CalibrationArtifactError("artifact_threshold_invalid", f"invalid thresholds for {key.encode()}") from exc
    backoff_raw = data.get("backoff_to")
    backoff = parse_bucket_key(backoff_raw) if backoff_raw is not None else None
    evidence_status = data.get("evidence_status", "proven")
    if evidence_status not in {"proven", "unproven"}:
        raise CalibrationArtifactError("artifact_bucket_invalid", "evidence_status must be proven or unproven")
    label_count = data.get("label_count")
    if label_count is not None:
        label_count = _safe_non_negative_int(label_count, code="artifact_bucket_invalid", field="label_count")
    return CalibrationBucket(sample_count, thresholds, backoff, evidence_status, label_count)


def _validate_backoff_graph(
    buckets: Mapping[CalibrationBucketKey, CalibrationBucket],
    global_bucket: CalibrationBucketKey | None,
) -> None:
    keys = set(buckets)
    if global_bucket is not None and global_bucket not in keys:
        raise CalibrationArtifactError("artifact_backoff_invalid", "global_bucket does not exist")
    for key, bucket in buckets.items():
        if bucket.backoff_to is not None and bucket.backoff_to not in keys:
            raise CalibrationArtifactError("artifact_backoff_invalid", f"backoff target missing for {key.encode()}")
        seen: set[CalibrationBucketKey] = set()
        cursor: CalibrationBucketKey | None = key
        while cursor is not None:
            if cursor in seen:
                raise CalibrationArtifactError("artifact_backoff_invalid", "backoff chain contains a cycle")
            seen.add(cursor)
            next_bucket = buckets[cursor]
            cursor = next_bucket.backoff_to


def parse_calibration_artifact(raw: object) -> CalibrationArtifact:
    """Pure schema decoder; callers at unsafe file boundaries catch its error."""

    if isinstance(raw, CalibrationArtifact):
        return raw
    data = _mapping(raw, code="artifact_schema_invalid", message="calibration artifact must be an object")
    if data.get("schema_version") != CALIBRATION_ARTIFACT_SCHEMA_VERSION:
        raise CalibrationArtifactError("artifact_schema_invalid", "unsupported calibration artifact schema_version")
    policy_version = _safe_token(data.get("policy_version"), code="artifact_schema_invalid", field="policy_version")
    revision = _safe_token(data.get("calibration_revision"), code="artifact_schema_invalid", field="calibration_revision")
    try:
        identity = CalibrationIdentity.from_mapping(data.get("identity"))
    except ValueError as exc:
        raise CalibrationArtifactError("artifact_identity_invalid", "invalid calibration identity") from exc
    minimum = data.get("minimum_sample_count", data.get("min_sample_count"))
    minimum_sample_count = _safe_non_negative_int(minimum, code="artifact_schema_invalid", field="minimum_sample_count")
    if minimum_sample_count <= 0:
        raise CalibrationArtifactError("artifact_schema_invalid", "minimum_sample_count must be positive")
    buckets_raw = _mapping(data.get("buckets", {}), code="artifact_schema_invalid", message="buckets must be an object")
    parsed_buckets: dict[CalibrationBucketKey, CalibrationBucket] = {}
    for raw_key, raw_bucket in buckets_raw.items():
        key = parse_bucket_key(raw_key)
        if key in parsed_buckets:
            raise CalibrationArtifactError("artifact_bucket_invalid", "duplicate normalized bucket key")
        parsed_buckets[key] = _parse_bucket(raw_bucket, key)
    weights_raw = data.get("weights", DEFAULT_CALIBRATION_WEIGHTS)
    weights_data = _mapping(weights_raw, code="artifact_schema_invalid", message="weights must be an object")
    weights: dict[str, float] = {}
    for name, value in weights_data.items():
        key = _safe_token(name, code="artifact_schema_invalid", field="weight name")
        number = _safe_number(value, code="artifact_schema_invalid", field=f"weights.{key}")
        if number < 0:
            raise CalibrationArtifactError("artifact_schema_invalid", "weights must be non-negative")
        weights[key] = number
    global_raw = data.get("global_bucket")
    global_bucket = parse_bucket_key(global_raw) if global_raw else None
    order_raw = data.get("backoff_order", ("language", "source_kind", "scope", "global"))
    if not isinstance(order_raw, (list, tuple)) or not all(isinstance(item, str) and item for item in order_raw):
        raise CalibrationArtifactError("artifact_backoff_invalid", "backoff_order must be a list of names")
    status = data.get("status", "unproven")
    if status not in {"proven", "unproven"}:
        raise CalibrationArtifactError("artifact_schema_invalid", "status must be proven or unproven")
    try:
        artifact = CalibrationArtifact(
            schema_version=CALIBRATION_ARTIFACT_SCHEMA_VERSION,
            policy_version=policy_version,
            calibration_revision=revision,
            identity=identity,
            minimum_sample_count=minimum_sample_count,
            buckets=parsed_buckets,
            weights=weights,
            status=status,
            global_bucket=global_bucket,
            backoff_order=tuple(order_raw),
        )
    except ValueError as exc:
        raise CalibrationArtifactError("artifact_schema_invalid", "invalid calibration artifact fields") from exc
    _validate_backoff_graph(artifact.buckets, artifact.global_bucket)
    return artifact


def calibration_artifact_from_dict(raw: object) -> CalibrationArtifact:
    """Compatibility alias for callers that use a model-style decoder name."""

    return parse_calibration_artifact(raw)


def calibration_artifact_to_dict(artifact: CalibrationArtifact) -> dict[str, Any]:
    return parse_calibration_artifact(artifact).to_dict()


def identity_matches(actual: CalibrationIdentity, expected: CalibrationIdentity | Mapping[str, Any]) -> bool:
    """Compare all identity fields supplied by the expected snapshot."""

    if isinstance(expected, CalibrationIdentity):
        return actual.to_dict() == expected.to_dict()
    if not isinstance(expected, Mapping):
        return False
    expected_ranking = expected.get("ranking_policy_version", expected.get("ranking_version"))
    pairs = (
        ("dataset_id", expected.get("dataset_id"), actual.dataset_id),
        ("dataset_revision", expected.get("dataset_revision", expected.get("revision")), actual.dataset_revision),
        ("vault_fingerprint", expected.get("vault_fingerprint"), _thaw(actual.vault_fingerprint)),
        ("ranking_policy_version", expected_ranking, actual.ranking_policy_version),
        ("runtime_provenance", expected.get("runtime_provenance"), _thaw(actual.runtime_provenance)),
        ("feature_schema_hash", expected.get("feature_schema_hash"), actual.feature_schema_hash),
        ("config_hash", expected.get("config_hash"), actual.config_hash),
    )
    for _name, expected_value, actual_value in pairs:
        if expected_value is not None and expected_value != actual_value:
            return False
    return True


@dataclass(frozen=True)
class CalibrationLoadView:
    """One immutable load outcome; a failure never carries a source path."""

    artifact: CalibrationArtifact | None
    status: Literal["loaded", "fail_open"]
    reason_code: str | None = None
    diagnostic_code: str = ""
    fail_open: bool = False

    @property
    def loaded(self) -> bool:
        return self.artifact is not None and self.status == "loaded"

    def for_feature(self, feature: CandidateFeature) -> QualityThresholdView:
        return resolve_threshold_view(self, feature)


CalibrationArtifactView = CalibrationLoadView


class CalibrationArtifactLoader:
    """Read, validate, and identity-check an artifact at most once."""

    def __init__(
        self,
        path: str | Path,
        *,
        expected_identity: CalibrationIdentity | Mapping[str, Any] | None = None,
        expected_policy_version: str | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._expected_identity = expected_identity
        self._expected_policy_version = expected_policy_version
        self._lock = Lock()
        self._view: CalibrationLoadView | None = None

    def load(self) -> CalibrationLoadView:
        if self._view is not None:
            return self._view
        with self._lock:
            if self._view is not None:
                return self._view
            self._view = self._load_once()
            return self._view

    def _load_once(self) -> CalibrationLoadView:
        try:
            if not self._path.is_file():
                return CalibrationLoadView(None, "fail_open", GATE_FAIL_OPEN_POLICY_MISSING, "artifact_missing", True)
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            artifact = parse_calibration_artifact(raw)
            if self._expected_policy_version is not None and artifact.policy_version != self._expected_policy_version:
                return CalibrationLoadView(None, "fail_open", GATE_FAIL_OPEN_POLICY_MISSING, "policy_version_mismatch", True)
            if self._expected_identity is not None and not identity_matches(artifact.identity, self._expected_identity):
                return CalibrationLoadView(None, "fail_open", GATE_FAIL_OPEN_POLICY_MISSING, "identity_mismatch", True)
            return CalibrationLoadView(artifact, "loaded", None, "", False)
        except (OSError, json.JSONDecodeError, CalibrationArtifactError, ValueError, TypeError) as exc:
            diagnostic = getattr(exc, "code", "artifact_invalid")
            return CalibrationLoadView(None, "fail_open", GATE_FAIL_OPEN_POLICY_MISSING, str(diagnostic), True)
        except Exception:
            # A corrupt optional policy must never become a query outage.  Do
            # not expose the exception text or path in the view.
            return CalibrationLoadView(None, "fail_open", GATE_FAIL_OPEN_POLICY_MISSING, "artifact_load_error", True)


def load_calibration_artifact_once(
    path: str | Path,
    *,
    expected_identity: CalibrationIdentity | Mapping[str, Any] | None = None,
    expected_policy_version: str | None = None,
) -> CalibrationLoadView:
    return CalibrationArtifactLoader(
        path,
        expected_identity=expected_identity,
        expected_policy_version=expected_policy_version,
    ).load()


def _feature_from_input(value: CandidateFeature | Mapping[str, Any] | object) -> CandidateFeature:
    if isinstance(value, CandidateFeature):
        return value
    try:
        return extract_candidate_feature(value)
    except (TypeError, ValueError) as exc:
        raise CalibrationGenerationError("observation_invalid", "calibration observation has invalid candidate features") from exc


def _key_candidates(feature: CandidateFeature, artifact: CalibrationArtifact) -> tuple[CalibrationBucketKey, ...]:
    exact = CalibrationBucketKey.from_feature(feature)
    source = exact.source_kind
    language = exact.language_bucket
    mode = exact.retrieval_mode
    scope = exact.effective_scope
    candidates = (
        exact,
        CalibrationBucketKey(exact.score_family, scope, "*", mode, source),
        CalibrationBucketKey(exact.score_family, scope, language, mode, _WILDCARD),
        CalibrationBucketKey(exact.score_family, scope, "*", mode, _WILDCARD),
        CalibrationBucketKey(exact.score_family, "*", language, mode, source),
        CalibrationBucketKey(exact.score_family, "*", "*", mode, source),
        CalibrationBucketKey(exact.score_family, "*", language, mode, _WILDCARD),
        CalibrationBucketKey(exact.score_family, "*", "*", mode, _WILDCARD),
        CalibrationBucketKey(exact.score_family, "*", "*", "*", _WILDCARD),
    )
    result: list[CalibrationBucketKey] = []
    seen: set[CalibrationBucketKey] = set()
    for key in candidates:
        if key not in seen:
            result.append(key)
            seen.add(key)
    if artifact.global_bucket is not None and artifact.global_bucket not in seen:
        result.append(artifact.global_bucket)
    return tuple(result)


def _fail_open_threshold_view(
    *,
    reason: str,
    bucket_key: str = "",
    low_sample_buckets: Iterable[str] = (),
    policy_version: str = QUALITY_POLICY_VERSION,
    calibration_revision: str = "",
) -> QualityThresholdView:
    if reason not in {GATE_FAIL_OPEN_LOW_SAMPLE, GATE_FAIL_OPEN_POLICY_MISSING, GATE_FAIL_OPEN_ERROR}:
        reason = GATE_FAIL_OPEN_ERROR
    return QualityThresholdView(
        bucket_key=bucket_key,
        selection="fail_open",
        fail_open=True,
        fail_open_reason=reason,
        low_sample_buckets=tuple(sorted(set(low_sample_buckets))),
        policy_version=policy_version,
        calibration_revision=calibration_revision,
    )


def resolve_threshold_view(
    artifact_or_view: CalibrationArtifact | CalibrationLoadView | None,
    feature: CandidateFeature,
) -> QualityThresholdView:
    """Resolve exact bucket → declared backoff → structural backoff → fail-open."""

    if isinstance(artifact_or_view, CalibrationLoadView):
        if artifact_or_view.artifact is None:
            return _fail_open_threshold_view(
                reason=artifact_or_view.reason_code or GATE_FAIL_OPEN_POLICY_MISSING,
            )
        artifact = artifact_or_view.artifact
    elif isinstance(artifact_or_view, CalibrationArtifact):
        artifact = artifact_or_view
    else:
        return _fail_open_threshold_view(reason=GATE_FAIL_OPEN_POLICY_MISSING)

    requested = CalibrationBucketKey.from_feature(feature)
    low_sample: list[str] = []
    visited: set[CalibrationBucketKey] = set()
    structural_keys = _key_candidates(feature, artifact)
    structural_index = 0
    candidate_key: CalibrationBucketKey | None = structural_keys[0] if structural_keys else None
    while structural_index < len(structural_keys):
        if candidate_key is None:
            candidate_key = structural_keys[structural_index]
        if candidate_key in visited:
            return _fail_open_threshold_view(
                reason=GATE_FAIL_OPEN_ERROR,
                bucket_key=requested.encode(),
                low_sample_buckets=low_sample,
                policy_version=artifact.policy_version,
                calibration_revision=artifact.calibration_revision,
            )
        visited.add(candidate_key)
        bucket = artifact.buckets.get(candidate_key)
        if bucket is None:
            structural_index += 1
            candidate_key = structural_keys[structural_index] if structural_index < len(structural_keys) else None
            continue
        if bucket.sample_count >= artifact.minimum_sample_count and bucket.evidence_status == "proven":
            selection: Literal["exact", "backoff"] = "exact" if candidate_key == requested else "backoff"
            return QualityThresholdView(
                bucket_key=candidate_key.encode(),
                score_ratio=bucket.thresholds.score_ratio,
                margin=bucket.thresholds.margin,
                term_coverage=bucket.thresholds.term_coverage,
                sample_count=bucket.sample_count,
                backoff_depth=len(low_sample),
                selection=selection,
                low_sample_buckets=tuple(sorted(set(low_sample))),
                policy_version=artifact.policy_version,
                calibration_revision=artifact.calibration_revision,
            )
        low_sample.append(candidate_key.encode())
        if bucket.backoff_to is not None and bucket.backoff_to not in visited:
            candidate_key = bucket.backoff_to
            continue
        structural_index += 1
        candidate_key = structural_keys[structural_index] if structural_index < len(structural_keys) else None
    return _fail_open_threshold_view(
        reason=GATE_FAIL_OPEN_LOW_SAMPLE,
        bucket_key=requested.encode(),
        low_sample_buckets=low_sample or (requested.encode(),),
        policy_version=artifact.policy_version,
        calibration_revision=artifact.calibration_revision,
    )


def resolve_threshold_views(
    artifact_or_view: CalibrationArtifact | CalibrationLoadView | None,
    features: Sequence[CandidateFeature],
) -> tuple[QualityThresholdView, ...]:
    """Resolve one immutable threshold view per candidate in input order."""

    return tuple(resolve_threshold_view(artifact_or_view, feature) for feature in features)


@dataclass(frozen=True)
class CalibrationObservation:
    feature: CandidateFeature
    accepted: bool | None = None
    query_id: str = ""
    answerable: bool | None = None


def parse_calibration_observation(raw: object) -> CalibrationObservation | None:
    """Parse a safe candidate observation; query text is intentionally ignored."""

    if isinstance(raw, CalibrationObservation):
        return raw
    if not isinstance(raw, Mapping):
        return None
    candidate = raw.get("feature", raw.get("candidate", raw))
    try:
        feature = _feature_from_input(candidate)
    except CalibrationGenerationError:
        return None
    accepted_raw = raw.get("accepted", raw.get("relevant", raw.get("label")))
    accepted: bool | None
    if type(accepted_raw) is bool:
        accepted = accepted_raw
    elif isinstance(accepted_raw, (int, float)) and not isinstance(accepted_raw, bool):
        accepted = bool(accepted_raw)
    else:
        accepted = None
    answerable = raw.get("answerable") if type(raw.get("answerable")) is bool else None
    query_id = raw.get("query_id", raw.get("case_id", raw.get("id", "")))
    return CalibrationObservation(feature, accepted, str(query_id) if query_id else "", answerable)


def load_calibration_observations(path: str | Path) -> tuple[CalibrationObservation, ...]:
    """Read JSONL observations at the explicit admin/evaluation boundary."""

    target = Path(path).expanduser()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CalibrationGenerationError("observations_unreadable", "calibration observations could not be read") from exc
    observations: list[CalibrationObservation] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationGenerationError("observations_invalid_json", f"invalid calibration observation at line {line_number}") from exc
        observation = parse_calibration_observation(raw)
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


def _quantile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return round(float(ordered[index]), 12)


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p50": _quantile(values, 0.5),
        "p95": _quantile(values, 0.95),
        "max": max(values) if values else None,
    }


def _safe_report_identity(identity: CalibrationIdentity) -> dict[str, Any]:
    raw = identity.to_dict()
    fingerprint = raw.get("vault_fingerprint")
    safe_fingerprint: object
    if isinstance(fingerprint, Mapping):
        safe_fingerprint = {
            key: value
            for key in ("algorithm", "value", "file_count", "status", "reason")
            if (value := fingerprint.get(key)) is not None and isinstance(value, (str, int, bool))
        }
    else:
        safe_fingerprint = fingerprint if isinstance(fingerprint, str) else None
    runtime = raw.get("runtime_provenance")
    safe_runtime: dict[str, object] = {}
    if isinstance(runtime, Mapping):
        for key in ("package_version", "revision", "dirty", "revision_source", "provenance_incomplete"):
            value = runtime.get(key)
            if isinstance(value, (str, int, bool)):
                safe_runtime[key] = str(value)[:128] if isinstance(value, str) else value
        warnings = runtime.get("warnings")
        if isinstance(warnings, list):
            safe_runtime["warning_count"] = len(warnings)
    return {
        "dataset_id": identity.dataset_id,
        "dataset_revision": identity.dataset_revision,
        "vault_fingerprint": safe_fingerprint,
        "ranking_policy_version": identity.ranking_policy_version,
        "runtime_provenance": safe_runtime,
        "feature_schema_hash": identity.feature_schema_hash,
        **({"config_hash": identity.config_hash} if identity.config_hash is not None else {}),
    }


def _choose_thresholds(
    observations: Sequence[CalibrationObservation],
    ratios: Mapping[int, float],
) -> tuple[CalibrationThresholds | None, str, list[str]]:
    labeled = [item for item in observations if item.accepted is not None]
    if not labeled:
        return None, "unproven", ["labels_missing"]
    positive = [item for item in labeled if item.accepted]
    if not positive:
        return None, "unproven", ["positive_labels_missing"]
    ratio_values = [ratios[index] for index, item in enumerate(observations) if item.accepted and index in ratios]
    margin_values = [item.feature.branch_margin for item in positive if item.feature.branch_margin is not None]
    coverage_values = [item.feature.term_coverage for item in positive]
    values = {
        "score_ratio": min(ratio_values) if ratio_values else None,
        "margin": min(float(value) for value in margin_values) if margin_values else None,
        "term_coverage": min(float(value) for value in coverage_values) if coverage_values else None,
    }
    try:
        thresholds = CalibrationThresholds(**values)
    except ValueError:
        return None, "unproven", ["threshold_selection_failed"]
    return thresholds, "proven", []


@dataclass(frozen=True)
class CalibrationGenerationResult:
    artifact: CalibrationArtifact
    report: Mapping[str, Any]


def generate_calibration_artifact(
    observations: Iterable[CalibrationObservation | Mapping[str, Any]],
    *,
    identity: CalibrationIdentity | Mapping[str, Any],
    calibration_revision: str = "calibration-unproven",
    policy_version: str = QUALITY_POLICY_VERSION,
    minimum_sample_count: int = DEFAULT_MINIMUM_SAMPLE_COUNT,
    weights: Mapping[str, float] = DEFAULT_CALIBRATION_WEIGHTS,
    evidence_status: Literal["proven", "unproven"] = "unproven",
) -> CalibrationGenerationResult:
    """Build a deterministic artifact from frozen candidate observations.

    The generator only selects values derived from branch-local observations.
    It never invents a cross-source absolute score threshold.  Missing labels,
    small buckets, and the 02 holdout's ``unproven`` identity remain visible in
    the report and cause the corresponding bucket to back off at runtime.
    """

    try:
        parsed_identity = CalibrationIdentity.from_mapping(identity)
    except (CalibrationArtifactError, ValueError) as exc:
        raise CalibrationGenerationError("identity_missing", "calibration identity is incomplete") from exc
    policy_version = _safe_token(policy_version, code="generator_invalid", field="policy_version")
    calibration_revision = _safe_token(calibration_revision, code="generator_invalid", field="calibration_revision")
    if type(minimum_sample_count) is not int or minimum_sample_count <= 0:
        raise CalibrationGenerationError("generator_invalid", "minimum_sample_count must be positive")
    if evidence_status not in {"proven", "unproven"}:
        raise CalibrationGenerationError("generator_invalid", "evidence_status must be proven or unproven")
    try:
        normalized_weights = _validated_weights(weights)
    except (CalibrationArtifactError, ValueError) as exc:
        raise CalibrationGenerationError("generator_invalid", "weights must be finite non-negative numbers") from exc

    parsed: list[CalibrationObservation] = []
    skipped = 0
    for raw in observations:
        item = parse_calibration_observation(raw)
        if item is None:
            skipped += 1
        else:
            parsed.append(item)

    grouped: dict[CalibrationBucketKey, list[CalibrationObservation]] = defaultdict(list)
    for item in parsed:
        grouped[CalibrationBucketKey.from_feature(item.feature)].append(item)

    ratios_by_bucket: dict[CalibrationBucketKey, dict[int, float]] = {}
    for key, bucket_items in grouped.items():
        maxima: dict[str, float] = {}
        for index, item in enumerate(bucket_items):
            group_id = item.query_id or f"<observation:{index}>"
            maxima[group_id] = max(maxima.get(group_id, float("-inf")), item.feature.score)
        ratios_by_bucket[key] = {
            index: (
                item.feature.score / maxima[item.query_id or f"<observation:{index}>"]
                if maxima[item.query_id or f"<observation:{index}>"] > 0
                else 1.0
            )
            for index, item in enumerate(bucket_items)
        }

    buckets: dict[CalibrationBucketKey, CalibrationBucket] = {}
    bucket_reports: dict[str, Any] = {}
    unproven_reasons: set[str] = set()
    for key in sorted(grouped):
        bucket_items = grouped[key]
        ratios = ratios_by_bucket[key]
        thresholds, bucket_evidence_status, reasons = _choose_thresholds(bucket_items, ratios)
        if len(bucket_items) < minimum_sample_count:
            bucket_evidence_status = "unproven"
            reasons = [*reasons, "low_sample"]
        if thresholds is None:
            # Keep the artifact structurally inspectable while ensuring the
            # resolver cannot use an evidence-free bucket.
            thresholds = CalibrationThresholds(score_ratio=0.0)
        if bucket_evidence_status == "unproven":
            unproven_reasons.update(reasons)
        label_count = sum(item.accepted is not None for item in bucket_items)
        bucket = CalibrationBucket(
            sample_count=len(bucket_items),
            thresholds=thresholds,
            evidence_status=bucket_evidence_status,
            label_count=label_count,
        )
        buckets[key] = bucket
        bucket_reports[key.encode()] = {
            "sample_count": len(bucket_items),
            "label_count": label_count,
            "evidence_status": bucket_evidence_status,
            "unproven_reasons": sorted(set(reasons)),
            "thresholds": thresholds.to_dict(),
            "distributions": {
                "ratio": _distribution(list(ratios.values())),
                "margin": _distribution([float(item.feature.branch_margin) for item in bucket_items if item.feature.branch_margin is not None]),
                "coverage": _distribution([float(item.feature.term_coverage) for item in bucket_items]),
            },
        }

    # Declare structural backoff targets in the artifact itself.  A target is
    # only linked when the broader bucket was actually generated.
    for key, bucket in list(buckets.items()):
        candidates = (
            CalibrationBucketKey(key.score_family, key.effective_scope, "*", key.retrieval_mode, key.source_kind),
            CalibrationBucketKey(key.score_family, key.effective_scope, key.language_bucket, key.retrieval_mode, "*"),
            CalibrationBucketKey(key.score_family, key.effective_scope, "*", key.retrieval_mode, "*"),
            CalibrationBucketKey(key.score_family, "*", "*", key.retrieval_mode, "*"),
        )
        target = next((candidate for candidate in candidates if candidate in buckets and candidate != key), None)
        if target is not None:
            buckets[key] = CalibrationBucket(
                bucket.sample_count,
                bucket.thresholds,
                target,
                bucket.evidence_status,
                bucket.label_count,
            )
            bucket_reports[key.encode()]["backoff_to"] = target.encode()

    if not parsed:
        unproven_reasons.add("observations_missing")
    if evidence_status == "unproven":
        unproven_reasons.add("evidence_unproven")
    identity_fingerprint = parsed_identity.vault_fingerprint
    identity_is_unproven = isinstance(identity_fingerprint, Mapping) and identity_fingerprint.get("status") == "unproven"
    artifact_status: Literal["proven", "unproven"] = (
        "proven"
        if evidence_status == "proven" and buckets and not unproven_reasons and not identity_is_unproven
        else "unproven"
    )
    artifact = CalibrationArtifact(
        schema_version=CALIBRATION_ARTIFACT_SCHEMA_VERSION,
        policy_version=policy_version,
        calibration_revision=calibration_revision,
        identity=parsed_identity,
        minimum_sample_count=minimum_sample_count,
        buckets=buckets,
        weights=normalized_weights,
        status=artifact_status,
    )
    report: dict[str, Any] = {
        "schema_version": CALIBRATION_ARTIFACT_SCHEMA_VERSION,
        "status": "proven" if artifact_status == "proven" else "unproven",
        "unproven": artifact_status == "unproven",
        "unproven_reasons": sorted(unproven_reasons | ({"identity_unproven"} if identity_is_unproven else set())),
        "policy_version": policy_version,
        "calibration_revision": calibration_revision,
        "minimum_sample_count": minimum_sample_count,
        "identity": _safe_report_identity(parsed_identity),
        "weights": dict(sorted((str(key), float(value)) for key, value in normalized_weights.items())),
        "observation_count": len(parsed),
        "skipped_observation_count": skipped,
        "bucket_count": len(bucket_reports),
        "proven_bucket_count": sum(value["evidence_status"] == "proven" for value in bucket_reports.values()),
        "unproven_bucket_count": sum(value["evidence_status"] != "proven" for value in bucket_reports.values()),
        "buckets": dict(sorted(bucket_reports.items())),
        "holdout_evidence": "proven" if artifact_status == "proven" else "unproven",
        "evidence_status_requested": evidence_status,
    }
    return CalibrationGenerationResult(artifact, MappingProxyType(report))


def write_calibration_outputs(
    result: CalibrationGenerationResult,
    output_dir: str | Path,
) -> dict[str, str]:
    """Write the explicit admin artifact plus JSON/Markdown report."""

    target = Path(output_dir).expanduser().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CalibrationGenerationError("output_unwritable", "calibration outputs could not be written") from exc
    safe_policy = re.sub(r"[^A-Za-z0-9._-]+", "-", result.artifact.policy_version)
    safe_revision = re.sub(r"[^A-Za-z0-9._-]+", "-", result.artifact.calibration_revision)
    stem = f"quality-gate-{safe_policy}-{safe_revision}"
    artifact_path = target / f"{stem}.json"
    report_path = target / f"{stem}.report.json"
    markdown_path = target / f"{stem}.report.md"
    try:
        artifact_path.write_text(
            json.dumps(result.artifact.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report = dict(result.report)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        lines = [
            "# 质量门禁校准报告",
            "",
            f"- 状态：`{report['status']}`",
            f"- policy version：`{report['policy_version']}`",
            f"- calibration revision：`{report['calibration_revision']}`",
            f"- 样本数：`{report['observation_count']}`；桶数：`{report['bucket_count']}`",
            f"- 证据状态：`{report['holdout_evidence']}`",
            "",
            "## 分桶摘要",
            "",
        ]
        for key, bucket in report["buckets"].items():
            lines.append(
                f"- `{key}`：samples={bucket['sample_count']}，labels={bucket['label_count']}，"
                f"status=`{bucket['evidence_status']}`，thresholds={bucket['thresholds']}"
            )
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise CalibrationGenerationError("output_unwritable", "calibration outputs could not be written") from exc
    return {"artifact": str(artifact_path), "report_json": str(report_path), "report_markdown": str(markdown_path)}


def identity_from_manifest(raw: object) -> CalibrationIdentity:
    """Project a frozen retrieval manifest into the calibration identity."""

    data = _mapping(raw, code="identity_missing", message="manifest must be an object")
    nested = data.get("identity")
    identity_data = dict(nested) if isinstance(nested, Mapping) else {}
    for name in (
        "dataset_id",
        "dataset_revision",
        "vault_fingerprint",
        "ranking_policy_version",
        "ranking_version",
        "runtime_provenance",
        "feature_schema_hash",
        "config_hash",
    ):
        if name not in identity_data and data.get(name) is not None:
            identity_data[name] = data[name]
    identity_data.setdefault("dataset_revision", data.get("revision"))
    identity_data.setdefault("ranking_policy_version", identity_data.get("ranking_version"))
    try:
        return CalibrationIdentity.from_mapping(identity_data)
    except (CalibrationArtifactError, ValueError) as exc:
        raise CalibrationGenerationError("identity_missing", "manifest does not contain a complete calibration identity") from exc


def read_json_object(path: str | Path) -> object:
    """Read one admin JSON input without exposing its path in errors."""

    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationGenerationError("input_invalid", "calibration JSON input could not be read") from exc


__all__ = [
    "CALIBRATION_ARTIFACT_SCHEMA_VERSION",
    "CALIBRATION_FEATURE_SCHEMA_HASH",
    "CALIBRATION_FEATURE_SCHEMA_VERSION",
    "CalibrationArtifact",
    "CalibrationArtifactError",
    "CalibrationArtifactLoader",
    "CalibrationArtifactView",
    "CalibrationBucket",
    "CalibrationBucketKey",
    "CalibrationGenerationError",
    "CalibrationGenerationResult",
    "CalibrationIdentity",
    "CalibrationLoadView",
    "CalibrationObservation",
    "CalibrationThresholds",
    "DEFAULT_CALIBRATION_WEIGHTS",
    "DEFAULT_MINIMUM_SAMPLE_COUNT",
    "calibration_artifact_from_dict",
    "calibration_artifact_to_dict",
    "generate_calibration_artifact",
    "identity_from_manifest",
    "identity_matches",
    "load_calibration_artifact_once",
    "load_calibration_observations",
    "parse_bucket_key",
    "parse_calibration_artifact",
    "parse_calibration_observation",
    "read_json_object",
    "resolve_threshold_view",
    "resolve_threshold_views",
    "write_calibration_outputs",
]
