"""校准 artifact、纯解析视图与 admin 生成边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval.query_quality_calibration import (
    CALIBRATION_FEATURE_SCHEMA_HASH,
    CalibrationArtifactLoader,
    CalibrationBucketKey,
    CalibrationIdentity,
    CalibrationLoadView,
    CalibrationObservation,
    CalibrationThresholds,
    generate_calibration_artifact,
    identity_matches,
    load_calibration_artifact_once,
    parse_calibration_artifact,
    resolve_threshold_view,
    resolve_threshold_views,
    write_calibration_outputs,
)
from retrieval.query_quality_policy import (
    GATE_FAIL_OPEN_LOW_SAMPLE,
    GATE_FAIL_OPEN_POLICY_MISSING,
    GATE_REJECT_SCORE_FLOOR,
    CandidateFeature,
    evaluate_quality_policy,
)


def _identity(*, revision: str = "dataset-rev") -> CalibrationIdentity:
    return CalibrationIdentity(
        dataset_id="query-quality-holdout",
        dataset_revision=revision,
        vault_fingerprint={"algorithm": "sha256", "value": "vault-digest", "status": "complete"},
        ranking_policy_version="query-v2-passage-rrf-10",
        runtime_provenance={"package_version": "test", "revision": "runtime-rev", "revision_source": "fixture"},
        feature_schema_hash=CALIBRATION_FEATURE_SCHEMA_HASH,
    )


def _feature(score: float, *, language: str = "latin") -> CandidateFeature:
    return CandidateFeature(
        page_path=f"wiki/{score}.md",
        score_family="main_rrf",
        score=score,
        source_kind="wiki",
        effective_scope="knowledge",
        language_bucket=language,
        retrieval_mode="lexical",
    )


def _artifact_data(*, minimum: int = 2) -> tuple[dict[str, object], CalibrationBucketKey, CalibrationBucketKey]:
    exact = CalibrationBucketKey("main_rrf", "knowledge", "latin", "lexical", "wiki")
    language_backoff = CalibrationBucketKey("main_rrf", "knowledge", "*", "lexical", "wiki")
    global_bucket = CalibrationBucketKey("main_rrf", "*", "*", "*", "*")
    return (
        {
            "schema_version": 1,
            "policy_version": "query-quality-policy-v0",
            "calibration_revision": "cal-1",
            "identity": _identity().to_dict(),
            "minimum_sample_count": minimum,
            "status": "unproven",
            "weights": {"irrelevant_result": 1.0, "false_suppression": 1.0},
            "buckets": {
                exact.encode(): {
                    "sample_count": 1,
                    "thresholds": {"ratio": 0.8, "margin": 0.1, "coverage": 0.2},
                    "backoff_to": language_backoff.encode(),
                },
                language_backoff.encode(): {
                    "sample_count": minimum,
                    "thresholds": {"ratio": 0.7, "margin": 0.05, "coverage": 0.0},
                },
                global_bucket.encode(): {
                    "sample_count": minimum,
                    "thresholds": {"ratio": 0.6},
                },
            },
            "global_bucket": global_bucket.encode(),
        },
        exact,
        language_backoff,
    )


def test_artifact_schema_is_immutable_and_rejects_absolute_score() -> None:
    raw, exact, _language_backoff = _artifact_data()
    artifact = parse_calibration_artifact(raw)

    assert artifact.buckets[exact].thresholds == CalibrationThresholds(ratio=0.8, margin=0.1, coverage=0.2)
    with pytest.raises(TypeError):
        artifact.buckets[exact] = artifact.buckets[exact]  # type: ignore[index]
    with pytest.raises(TypeError):
        artifact.identity.runtime_provenance["secret"] = "no"  # type: ignore[index]

    bad = json.loads(json.dumps(raw))
    bad["buckets"][exact.encode()]["thresholds"]["score"] = 0.5
    with pytest.raises(ValueError, match="absolute score"):
        parse_calibration_artifact(bad)


def test_resolver_uses_exact_then_declared_backoff_then_fail_open() -> None:
    raw, exact, language_backoff = _artifact_data()
    artifact = parse_calibration_artifact(raw)

    view = resolve_threshold_view(artifact, _feature(0.5))
    assert view.selection == "backoff"
    assert view.bucket_key == language_backoff.encode()
    assert view.score_ratio == 0.7
    assert view.low_sample_buckets == (exact.encode(),)

    low_raw = json.loads(json.dumps(raw))
    low_raw["buckets"].pop(language_backoff.encode())
    global_key = CalibrationBucketKey("main_rrf", "*", "*", "*", "*").encode()
    low_raw["buckets"][global_key]["sample_count"] = 1
    low_raw["buckets"][exact.encode()]["backoff_to"] = None
    low_raw["buckets"][exact.encode()]["sample_count"] = 1
    low_artifact = parse_calibration_artifact(low_raw)
    low_view = resolve_threshold_view(low_artifact, _feature(0.5))
    assert low_view.fail_open is True
    assert low_view.fail_open_reason == GATE_FAIL_OPEN_LOW_SAMPLE
    assert set(low_view.low_sample_buckets) == {
        exact.encode(),
        CalibrationBucketKey("main_rrf", "*", "*", "*", "*").encode(),
    }


def test_loader_missing_corrupt_identity_and_one_time_snapshot_fail_open(tmp_path: Path) -> None:
    raw, _exact, _language_backoff = _artifact_data()
    artifact_path = tmp_path / "calibration.json"
    artifact_path.write_text(json.dumps(raw), encoding="utf-8")

    expected = _identity()
    loader = CalibrationArtifactLoader(artifact_path, expected_identity=expected)
    loaded = loader.load()
    assert loaded.loaded is True
    artifact_path.write_text("{broken", encoding="utf-8")
    assert loader.load() is loaded
    assert identity_matches(loaded.artifact.identity, expected)  # type: ignore[union-attr]

    missing = load_calibration_artifact_once(tmp_path / "missing.json", expected_identity=expected)
    assert missing.loaded is False
    assert missing.reason_code == GATE_FAIL_OPEN_POLICY_MISSING

    artifact_path.write_text(json.dumps(raw), encoding="utf-8")
    mismatch = load_calibration_artifact_once(artifact_path, expected_identity=_identity(revision="other"))
    assert mismatch.loaded is False
    assert mismatch.reason_code == GATE_FAIL_OPEN_POLICY_MISSING


def test_policy_threshold_view_is_optional_and_fail_open_keeps_original_candidates(tmp_path: Path) -> None:
    raw, _exact, _language_backoff = _artifact_data()
    artifact = parse_calibration_artifact(raw)
    features = (_feature(1.0), _feature(0.5))

    legacy = evaluate_quality_policy(features)
    assert legacy.accepted_features == features
    views = resolve_threshold_views(artifact, features)
    calibrated = evaluate_quality_policy(
        features,
        threshold_view=CalibrationLoadView(artifact, "loaded"),
    )
    assert len(calibrated.accepted) == 1
    assert calibrated.rejected[0].reason_code == GATE_REJECT_SCORE_FLOOR
    assert calibrated.summary.threshold_selection_counts["backoff"] == 2

    missing = load_calibration_artifact_once(tmp_path / "not-there.json")
    fail_open = evaluate_quality_policy(features, threshold_view=missing)
    assert fail_open.accepted_features == features
    assert fail_open.summary.fail_open is True
    assert fail_open.summary.reason_counts[GATE_FAIL_OPEN_POLICY_MISSING] == 2
    assert fail_open.summary.low_sample_buckets == ("artifact_missing",)


def test_generator_records_branch_relative_thresholds_backoff_weights_and_unproven_status(tmp_path: Path) -> None:
    narrow = (_feature(1.0), _feature(0.8))
    broad = (_feature(0.9, language="*"), _feature(0.7, language="*"))
    observations = [
        *(CalibrationObservation(feature, True, query_id="q-narrow") for feature in narrow),
        *(CalibrationObservation(feature, True, query_id="q-broad") for feature in broad),
    ]
    result = generate_calibration_artifact(
        observations,
        identity=_identity(),
        calibration_revision="cal-synthetic",
        minimum_sample_count=2,
    )

    narrow_key = CalibrationBucketKey("main_rrf", "knowledge", "latin", "lexical", "wiki")
    broad_key = CalibrationBucketKey("main_rrf", "knowledge", "*", "lexical", "wiki")
    assert result.artifact.status == "unproven"
    assert result.report["holdout_evidence"] == "unproven"
    assert result.artifact.buckets[narrow_key].backoff_to == broad_key
    assert result.artifact.buckets[narrow_key].thresholds.score_ratio == pytest.approx(0.8)
    assert result.artifact.weights["false_suppression"] == 1.0
    assert "score" not in result.artifact.buckets[narrow_key].thresholds.to_dict()

    outputs = write_calibration_outputs(result, tmp_path / "reports")
    assert {Path(path).suffix for path in outputs.values()} == {".json", ".md"}
    assert all(Path(path).is_file() for path in outputs.values())
