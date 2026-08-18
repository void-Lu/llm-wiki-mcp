from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from retrieval.query_pipeline import DEFAULT_TOP_K, QueryFilters, run_query_v2
from retrieval.query_quality_policy import (
    GATE_REJECT_SCORE_FLOOR,
    GATE_WOULD_SUPPRESS_ALL,
    QualityGateResult,
    evaluate_quality_gate,
)
import retrieval.query_pipeline as query_pipeline_module
from retrieval.vector_index import VectorIndexStore, vector_index_records
from retrieval.vector_provider import DeterministicFakeProvider
from runtime.runtime_config import EmbeddingSettings, QualityGateSettings, TelemetrySettings
from tests.helpers import write_test_page
from wiki.wiki_index import refresh_indexes
from wiki.wiki_paths import create_wiki_root


def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    data = {"title": title, "generated": bool(frontmatter.pop("generated", True)), **frontmatter}
    write_test_page(root, path, data, body)


def _without_quality_gate(result: dict[str, object]) -> dict[str, object]:
    pipeline = result.get("pipeline")
    assert isinstance(pipeline, dict)
    return {
        **result,
        "pipeline": {key: value for key, value in pipeline.items() if key != "quality_gate"},
    }


def _gate_result_with_acceptance(
    result: QualityGateResult,
    accepted_indexes: set[int],
) -> QualityGateResult:
    decisions = tuple(
        replace(
            decision,
            accepted=index in accepted_indexes,
            reason_code=(
                decision.reason_code
                if index in accepted_indexes
                else GATE_REJECT_SCORE_FLOOR
            ),
        )
        for index, decision in enumerate(result.decisions)
    )
    accepted = tuple(decision for decision in decisions if decision.accepted)
    rejected = tuple(decision for decision in decisions if not decision.accepted)
    reason_counts: dict[str, int] = {}
    for decision in decisions:
        reason_counts[decision.reason_code] = reason_counts.get(decision.reason_code, 0) + 1
    summary = replace(
        result.summary,
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        reason_counts=reason_counts,
        fail_open=False,
    )
    return QualityGateResult(decisions, accepted, rejected, summary)


def test_quality_gate_off_is_field_identical_and_shadow_only_extends_pipeline(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice Approval", "invoice approval workflow", type="concept")
    _write(root, "wiki/concepts/noise.md", "Unrelated", "unrelated reference", type="concept")
    refresh_indexes(root)
    telemetry = TelemetrySettings(enabled=False)

    baseline = run_query_v2(root, "invoice approval", retrieval_mode="lexical", telemetry=telemetry)
    off = run_query_v2(
        root,
        "invoice approval",
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="off"),
    )
    shadow = run_query_v2(
        root,
        "invoice approval",
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="shadow"),
    )

    assert off == baseline
    assert _without_quality_gate(shadow) == baseline
    quality_gate = shadow["pipeline"]["quality_gate"]
    assert set(quality_gate) == {
        "policy_version",
        "mode",
        "status",
        "candidate_count",
        "accepted_count",
        "rejected_count",
        "score_family_counts",
        "reason_counts",
        "low_sample_buckets",
        "fail_open",
    }
    assert quality_gate["mode"] == "shadow"
    assert quality_gate["status"] == "gate_shadow"
    assert quality_gate["candidate_count"] == quality_gate["accepted_count"] + quality_gate["rejected_count"]
    assert quality_gate["candidate_count"] == shadow["pipeline"]["counters"]["selected"]
    assert len(quality_gate["score_family_counts"]) <= 5
    assert len(quality_gate["reason_counts"]) <= quality_gate["candidate_count"]
    assert "invoice" not in json.dumps(quality_gate, ensure_ascii=False)
    assert "wiki/" not in json.dumps(quality_gate, ensure_ascii=False)

    enforce = run_query_v2(
        root,
        "invoice approval",
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="enforce"),
    )
    assert _without_quality_gate(enforce) == baseline
    assert enforce["pipeline"]["quality_gate"]["mode"] == "enforce"
    assert enforce["pipeline"]["quality_gate"]["status"] == "gate_enforced"
    assert enforce["pipeline"]["ranking_version"] == baseline["pipeline"]["ranking_version"]


def test_quality_gate_exception_fails_open_without_changing_public_envelope(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice Approval", "invoice approval workflow", type="concept")
    refresh_indexes(root)
    telemetry = TelemetrySettings(enabled=False)
    baseline = run_query_v2(root, "invoice approval", retrieval_mode="lexical", telemetry=telemetry)

    def fail_features(*_args, **_kwargs):
        raise RuntimeError("gate test failure")

    monkeypatch.setattr(query_pipeline_module, "build_candidate_features", fail_features)
    shadow = run_query_v2(
        root,
        "invoice approval",
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="shadow"),
    )

    assert _without_quality_gate(shadow) == baseline
    assert shadow["pipeline"]["quality_gate"]["status"] == "gate_unavailable"
    assert shadow["pipeline"]["quality_gate"]["fail_open"] is True
    assert shadow["pipeline"]["quality_gate"]["reason_counts"] == {"gate_fail_open_error": 1}


def test_quality_gate_enforce_projects_accepted_pages_and_bumps_ranking_version(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(4):
        _write(
            root,
            f"wiki/concepts/gate-{index}.md",
            f"Gate {index}",
            "shared enforce sentinel " * 3,
            type="concept",
        )
    refresh_indexes(root)
    telemetry = TelemetrySettings(enabled=False)
    baseline = run_query_v2(
        root,
        "shared enforce sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
    )
    baseline_paths = [item["path"] for item in baseline["results"] + baseline["additional_results"]]
    assert len(baseline_paths) >= 3

    def reject_second_and_tail(features, *, policy_version):
        result = evaluate_quality_gate(features, policy_version=policy_version)
        return _gate_result_with_acceptance(result, {0, 2})

    monkeypatch.setattr(query_pipeline_module, "evaluate_quality_gate", reject_second_and_tail)
    shadow = run_query_v2(
        root,
        "shared enforce sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="shadow"),
    )
    off = run_query_v2(
        root,
        "shared enforce sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="off"),
    )
    assert _without_quality_gate(shadow) == baseline
    assert off == baseline
    assert shadow["pipeline"]["ranking_version"] == baseline["pipeline"]["ranking_version"]

    enforced = run_query_v2(
        root,
        "shared enforce sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="enforce"),
    )

    assert [item["path"] for item in enforced["results"]] == [baseline_paths[0]]
    assert [item["path"] for item in enforced["additional_results"]] == [baseline_paths[2]]
    assert enforced["pipeline"]["quality_gate"]["status"] == "gate_enforced"
    assert enforced["pipeline"]["quality_gate"]["accepted_count"] == 2
    assert enforced["pipeline"]["quality_gate"]["rejected_count"] >= 1
    assert enforced["pipeline"]["ranking_version"] != baseline["pipeline"]["ranking_version"]
    assert enforced["pipeline"]["fallback"] == baseline["pipeline"]["fallback"]
    for key in ("scope", "corpus", "authority", "coverage"):
        assert enforced["pipeline"][key] == baseline["pipeline"][key]
    assert enforced["pipeline"]["counters"]["selected"] == baseline["pipeline"]["counters"]["selected"]
    assert enforced["pipeline"]["counters"]["graph_hits"] == baseline["pipeline"]["counters"]["graph_hits"]
    assert enforced["results"][0]["citation"] == "[1]"
    assert enforced["additional_results"][0]["citation"] == "[2]"

    enforced_with_context = run_query_v2(
        root,
        "shared enforce sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
        include_context_pack=True,
        quality_gate=QualityGateSettings(mode="enforce"),
    )
    assert [item["path"] for item in enforced_with_context["results"]] == [baseline_paths[0]]
    assert enforced_with_context["results"][0]["content"]


def test_quality_gate_enforce_all_rejected_fails_open_with_stable_observation(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(2):
        _write(
            root,
            f"wiki/concepts/reject-{index}.md",
            f"Reject {index}",
            "all rejected sentinel",
            type="concept",
        )
    refresh_indexes(root)
    telemetry = TelemetrySettings(enabled=False)
    baseline = run_query_v2(
        root,
        "all rejected sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
    )

    def reject_everything(features, *, policy_version):
        result = evaluate_quality_gate(features, policy_version=policy_version)
        return _gate_result_with_acceptance(result, set())

    monkeypatch.setattr(query_pipeline_module, "evaluate_quality_gate", reject_everything)
    enforced = run_query_v2(
        root,
        "all rejected sentinel",
        top_k=1,
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="enforce"),
    )

    assert enforced["results"] == baseline["results"]
    assert enforced["additional_results"] == baseline["additional_results"]
    assert enforced["pipeline"]["fallback"] == baseline["pipeline"]["fallback"]
    assert enforced["pipeline"]["ranking_version"] == baseline["pipeline"]["ranking_version"]
    assert enforced["pipeline"]["quality_gate"]["status"] == "gate_all_rejected"
    assert enforced["pipeline"]["quality_gate"]["fail_open"] is True
    assert enforced["pipeline"]["quality_gate"]["reason_counts"][GATE_WOULD_SUPPRESS_ALL] == 1
    assert "insufficient_evidence" not in json.dumps(enforced, ensure_ascii=False)


def test_quality_gate_enforce_restricts_raw_allowed_source_paths(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw_root = root / "raw/sources/references"
    raw_root.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (raw_root / f"raw-{index}.md").write_text(
            "raw enforce sentinel\n",
            encoding="utf-8",
        )
    refresh_indexes(root)
    telemetry = TelemetrySettings(enabled=False)
    baseline = run_query_v2(
        root,
        "raw enforce sentinel",
        top_k=2,
        retrieval_mode="lexical",
        telemetry=telemetry,
    )
    assert baseline["pipeline"]["fallback"]["level"] == "raw"
    allowed_before = baseline["pipeline"]["fallback"]["allowed_source_paths"]
    assert len(allowed_before) >= 2

    def keep_first_only(features, *, policy_version):
        result = evaluate_quality_gate(features, policy_version=policy_version)
        return _gate_result_with_acceptance(result, {0})

    monkeypatch.setattr(query_pipeline_module, "evaluate_quality_gate", keep_first_only)
    enforced = run_query_v2(
        root,
        "raw enforce sentinel",
        top_k=2,
        retrieval_mode="lexical",
        telemetry=telemetry,
        quality_gate=QualityGateSettings(mode="enforce"),
    )

    accepted_path = enforced["results"][0]["path"]
    assert [item["path"] for item in enforced["additional_results"]] == []
    assert enforced["pipeline"]["fallback"]["level"] == "raw"
    assert enforced["pipeline"]["fallback"]["reasons"] == baseline["pipeline"]["fallback"]["reasons"]
    assert enforced["pipeline"]["fallback"]["allowed_source_paths"] == [accepted_path]


def test_wiki_query_finds_keyword_matches_and_returns_citations(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/projects/alpha/architecture/suitelet.md",
        "Suitelet Entry",
        "This Suitelet handles invoice approval and links to [[specs/invoice-approval.md|spec]].",
        type="architecture",
        tags=["suitelet", "invoice"],
        summary="invoice suitelet",
    )
    _write(
        root,
        "wiki/projects/alpha/specs/invoice-approval.md",
        "Invoice Approval Decision",
        "We chose synchronous invoice approval because finance needs immediate feedback.",
        type="spec",
        generated=False,
        summary="finance decision",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "invoice suitelet", project="alpha", top_k=3, retrieval_mode="lexical")

    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    assert paths[0] == "wiki/projects/alpha/architecture/suitelet.md"
    assert "wiki/projects/alpha/specs/invoice-approval.md" in paths
    assert result["results"][0]["citation"] == "[1]"
    assert result["results"][0]["content"]


def test_wiki_query_uses_existing_retrieval_store_without_reading_corpus(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "invoice approval workflow", type="concept")
    refresh_indexes(root)

    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("corpus read")))
    result = run_query_v2(root, "invoice", retrieval_mode="lexical", include_context_pack=False)

    assert result["results"][0]["path"] == "wiki/concepts/invoice.md"
    assert "content" not in result["results"][0]


def test_wiki_query_project_scope_prioritizes_project_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/architecture/script.md", "Alpha Script", "shared keyword alpha behavior", type="architecture")
    _write(root, "wiki/projects/beta/architecture/script.md", "Beta Script", "shared keyword beta behavior", type="architecture")
    refresh_indexes(root)

    result = run_query_v2(root, "shared keyword", project="beta", top_k=2, retrieval_mode="lexical")

    assert result["results"][0]["path"] == "wiki/projects/beta/architecture/script.md"


def test_wiki_query_project_scope_filters_other_projects(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/architecture/script.md", "Alpha Script", "shared keyword alpha behavior extra extra", type="architecture")
    _write(root, "wiki/projects/beta/architecture/script.md", "Beta Script", "shared keyword beta behavior", type="architecture")
    refresh_indexes(root)

    result = run_query_v2(root, "shared keyword", project="beta", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/projects/beta/architecture/script.md" in paths
    assert "wiki/projects/alpha/architecture/script.md" not in paths


def test_wiki_query_excludes_archives_by_default(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/current/invoice.md", "Current Invoice", "current invoice workflow", type="concept")
    archived = root / "wiki/archives/2026/06/16/concepts/old/invoice.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\ntitle: Old Invoice\ngenerated: true\narchived: true\n---\n\n# Old Invoice\n\narchived invoice workflow",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "invoice workflow", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/current/invoice.md" in paths
    assert all(not path.startswith("wiki/archives/") for path in paths)


def test_wiki_query_uses_frontmatter_tags_and_index(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/suitescript-governance.md",
        "SuiteScript Governance",
        "Usage units matter.",
        type="concept",
        tags=["governance", "netsuite"],
        summary="governance limits",
    )
    _write(root, "wiki/concepts/other.md", "Other", "governance but different tag", type="concept", tags=["other"])
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "governance",
        filters=QueryFilters(tags=("netsuite",)),
        top_k=1,
        retrieval_mode="lexical",
    )

    assert result["ok"] is True
    assert result["results"][0]["path"] == "wiki/concepts/suitescript-governance.md"
    assert "Usage units matter" in result["results"][0]["content"]


def test_wiki_query_includes_project_scoped_raw_sources_when_explicitly_requested(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    alpha = root / "raw/sources/file/alpha/docs"
    beta = root / "raw/sources/file/beta/docs"
    alpha.mkdir(parents=True, exist_ok=True)
    beta.mkdir(parents=True, exist_ok=True)
    (alpha / "alpha.md").write_text("alpha raw invoice", encoding="utf-8")
    (beta / "beta.md").write_text("beta raw invoice", encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "invoice", scope="raw", project="alpha", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert paths == ["raw/sources/file/alpha/docs/alpha.md"]
    assert result["results"][0]["source_kind"] == "raw"


def test_wiki_query_reads_raw_projection_only_for_explicit_raw_scope(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/wiki-only.md", "Wiki Only", "curated material", type="concept")
    raw = root / "raw/sources/file/default/projection.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw projection sentinel", encoding="utf-8")
    refresh_indexes(root)

    default_result = run_query_v2(root, "raw projection sentinel", retrieval_mode="lexical")
    raw_result = run_query_v2(root, "raw projection sentinel", scope="raw", top_k=5, retrieval_mode="lexical")

    assert [item["path"] for item in default_result["results"]] == [
        "raw/sources/file/default/projection.txt"
    ]
    assert default_result["pipeline"]["fallback"]["level"] == "raw"
    assert [item["path"] for item in raw_result["results"]] == ["raw/sources/file/default/projection.txt"]
    assert raw_result["results"][0]["source_kind"] == "raw"


def test_wiki_query_graph_expands_by_sources_and_wikilinks(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle links to [[neighbor.md]].",
        type="concept",
        sources=["raw/sources/a.md"],
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept", sources=["raw/sources/a.md"])
    _write(root, "wiki/concepts/second-hop.md", "Second Hop", "distant content [[neighbor.md]]", type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "needle", top_k=3, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert "wiki/concepts/second-hop.md" in paths
    neighbor = result["results"][paths.index("wiki/concepts/neighbor.md")]
    assert neighbor["scores"]["graph"] > 0


def test_wiki_query_graph_expands_by_full_wiki_reference_target(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/seed.md", "Seed Page", "unique full target needle", type="concept")
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept")
    seed = root / "wiki/concepts/seed.md"
    seed.write_text(seed.read_text(encoding="utf-8").replace("unique full target needle", "unique full target needle [[wiki/concepts/neighbor]]"), encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "unique full target needle", top_k=3, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert result["results"][paths.index("wiki/concepts/neighbor.md")]["scores"]["graph"] > 0


def test_wiki_query_graph_uses_markdown_code_context_semantics(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle links to [[neighbor.md]].\n\n`[[inline-example.md]]`\n\n~~~markdown\n[[fenced-example.md]]\n~~~",
        type="concept",
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor", "ordinary neighbor", type="concept")
    _write(root, "wiki/concepts/inline-example.md", "Inline Example", "not related", type="concept")
    _write(root, "wiki/concepts/fenced-example.md", "Fenced Example", "not related", type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "unique needle", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert "wiki/concepts/inline-example.md" not in paths
    assert "wiki/concepts/fenced-example.md" not in paths


def test_wiki_query_archived_page_is_not_a_graph_bridge(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/seed.md", "Seed Page", "unique needle links to [[bridge]].", type="concept")
    _write(root, "wiki/concepts/distant.md", "Distant", "unrelated content", type="concept")
    archived = root / "wiki/archives/stale/2026/07/27/wiki/concepts/bridge.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\ntitle: Archived Bridge\ntype: concept\ngenerated: true\narchived: true\n---\n\n# Archived Bridge\n\n[[distant]]\n",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "unique needle", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/distant.md" not in paths
    assert all(not path.startswith("wiki/archives/") for path in paths)


def test_wiki_query_returns_v2_budget_and_page_content(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/long.md", "Long Page", "budget " * 2000, type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "budget", top_k=1, retrieval_mode="lexical")

    assert "context_pack" not in result
    assert result["results"][0]["citation"] == "[1]"
    assert result["results"][0]["content"]
    assert result["budget"]["used"] <= result["budget"]["total"]


def test_default_top_k_is_ten_and_explicit_eight_preserves_prefix_scores(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(12):
        _write(root, f"wiki/concepts/result-{index}.md", f"Result {index}", f"shared retrieval term {index}", type="concept")
    refresh_indexes(root)

    default_result = run_query_v2(root, "shared retrieval term", retrieval_mode="lexical")
    explicit_eight = run_query_v2(root, "shared retrieval term", top_k=8, retrieval_mode="lexical")

    assert DEFAULT_TOP_K == 10
    assert len(default_result["results"]) == DEFAULT_TOP_K
    assert [(item["path"], item["score"], item["scores"]) for item in explicit_eight["results"]] == [
        (item["path"], item["score"], item["scores"]) for item in default_result["results"][:8]
    ]


def test_wiki_query_vector_stage_is_optional_warning(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/a.md", "Alpha", "vector keyword", type="concept")
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "vector",
        top_k=1,
        embedding=EmbeddingSettings(enabled=True),
        retrieval_mode="hybrid",
    )

    assert result["results"]
    assert "index_missing" in result["pipeline"]["warnings"]


def test_wiki_query_vector_recall_is_independent(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/lexical.md", "Lexical Match", "expense report keyword", type="concept")
    _write(root, "wiki/concepts/semantic.md", "Semantic Match", "accounts payable operations", type="concept")
    refresh_indexes(root)
    model = tmp_path / "local-bge-m3"
    model.mkdir()
    provider = DeterministicFakeProvider(
        {
            "expense automation": [1, 0, 0, 0],
            "accounts payable operations": [1, 0, 0, 0],
        }
    )
    VectorIndexStore(root).build(vector_index_records(root), provider, include_raw_sources=False)
    monkeypatch.setattr(query_pipeline_module, "LocalBgeM3Provider", lambda *args, **kwargs: provider)

    result = run_query_v2(
        root,
        "expense automation",
        top_k=3,
        include_context_pack=False,
        embedding=EmbeddingSettings(enabled=True, model_path=model),
        retrieval_mode="hybrid",
    )

    semantic = next(item for item in result["results"] if item["path"] == "wiki/concepts/semantic.md")
    assert semantic["scores"]["fts"] == 0
    assert semantic["scores"]["vector"] > 0
    assert result["pipeline"]["counters"]["vector_hits"] > 0


def test_wiki_query_min_vector_score_filters_weak_matches(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/strong.md", "Strong Match", "strong semantic match content", type="concept")
    _write(root, "wiki/concepts/weak.md", "Weak Match", "weak semantic match content", type="concept")
    refresh_indexes(root)
    model = tmp_path / "local-bge-m3"
    model.mkdir()
    provider = DeterministicFakeProvider(
        {
            "test query": [1, 0, 0, 0],
            "strong semantic match": [1, 0, 0, 0],
            "weak semantic match": [3, 95, 0, 0],
        }
    )
    VectorIndexStore(root).build(vector_index_records(root), provider, include_raw_sources=False)
    monkeypatch.setattr(query_pipeline_module, "LocalBgeM3Provider", lambda *args, **kwargs: provider)

    base = dict(enabled=True, model_path=model)
    result_default = run_query_v2(
        root,
        "test query",
        top_k=10,
        include_context_pack=False,
        embedding=EmbeddingSettings(**base),
        retrieval_mode="vector",
    )
    result_open = run_query_v2(
        root,
        "test query",
        top_k=10,
        include_context_pack=False,
        embedding=EmbeddingSettings(**base, min_vector_score=0.0),
        retrieval_mode="vector",
    )

    paths_default = {item["path"] for item in result_default["results"]}
    paths_open = {item["path"] for item in result_open["results"]}
    assert "wiki/concepts/strong.md" in paths_default
    assert "wiki/concepts/weak.md" not in paths_default
    assert "wiki/concepts/strong.md" in paths_open
    assert "wiki/concepts/weak.md" in paths_open


def test_wiki_query_never_builds_missing_vector_index_or_exposes_raw_when_primary_matches(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/visible.md", "Visible", "ordinary content", type="concept")
    raw = root / "raw/sources/private.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw semantic content", encoding="utf-8")
    refresh_indexes(root)
    model = tmp_path / "local-bge-m3"
    model.mkdir()

    result = run_query_v2(
        root,
        "ordinary",
        include_context_pack=False,
        embedding=EmbeddingSettings(enabled=True, model_path=model),
        retrieval_mode="hybrid",
    )

    assert not (root / ".llm-wiki/vector-index").exists()
    assert "index_missing" in result["pipeline"]["warnings"]
    assert all(not item["path"].startswith("raw/") for item in result["results"])


def test_wiki_query_uses_v2_fts_ranking_for_rare_terms(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/common1.md", "Common One", "common word appears here", type="concept")
    _write(root, "wiki/concepts/common2.md", "Common Two", "common word appears here too", type="concept")
    _write(root, "wiki/concepts/common3.md", "Common Three", "common word appears here also", type="concept")
    _write(root, "wiki/concepts/rare.md", "Rare Page", "common word and unique_rare_term here", type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "unique_rare_term", top_k=4, retrieval_mode="lexical")

    assert result["results"][0]["path"] == "wiki/concepts/rare.md"
    assert "keyword" not in result["results"][0]["scores"]
    assert "fts" in result["results"][0]["scores"]


def test_wiki_query_frontmatter_filter_by_type(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/architecture/script.md", "Script", "shared keyword alpha", type="architecture")
    _write(root, "wiki/projects/alpha/specs/choice.md", "Choice", "shared keyword alpha", type="spec", generated=False)
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "shared keyword",
        project="alpha",
        filters=QueryFilters(type="spec"),
        top_k=5,
        retrieval_mode="lexical",
    )

    paths = [item["path"] for item in result["results"]]
    assert "wiki/projects/alpha/specs/choice.md" in paths
    assert "wiki/projects/alpha/architecture/script.md" not in paths


def test_wiki_query_uses_v2_result_schema_without_title_match_or_images(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/diagram.md",
        "Architecture Diagram",
        "The system includes this diagram: ![Suitelet flow](../media/suitelet-flow.png) and another ![Suitelet flow](../media/suitelet-flow.png).",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "architecture", top_k=1, retrieval_mode="lexical")

    item = result["results"][0]
    assert item["path"] == "wiki/concepts/diagram.md"
    assert "title_match" not in item
    assert "images" not in item
    assert item["content"]


def test_wiki_query_v2_returns_ranked_candidates_without_legacy_fields(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice-approval.md", "Invoice Approval", "short body", type="concept")
    _write(root, "wiki/concepts/noisy.md", "Noisy Page", "invoice approval " * 20, type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "invoice approval", top_k=2, retrieval_mode="lexical")

    assert {item["path"] for item in result["results"]} == {
        "wiki/concepts/invoice-approval.md",
        "wiki/concepts/noisy.md",
    }
    assert all(set(item["scores"]) == {"fts", "vector", "rrf", "graph"} for item in result["results"])


def test_wiki_query_length_normalization_keeps_exact_title_above_generated_body(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/3d-secure.md", "3D Secure Payment Authentication", "short reference", type="concept")
    _write(
        root,
        "wiki/concepts/help-noise.md",
        "Commerce Reference",
        "3d secure payment authentication commerce web stores " * 4_000,
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "3D Secure Payment Authentication", top_k=2, include_context_pack=False, retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == [
        "wiki/concepts/3d-secure.md",
        "wiki/concepts/help-noise.md",
    ]


def test_wiki_query_uses_path_tie_break_and_keeps_graph_inside_filters(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/zeta.md", "Zeta", "stable tie needle", type="concept")
    _write(root, "wiki/concepts/alpha.md", "Alpha", "stable tie needle", type="concept")
    _write(root, "wiki/concepts/seed.md", "Seed", "unique bridge needle [[projects/alpha/specs/bridge.md]]", type="concept")
    _write(root, "wiki/projects/alpha/specs/bridge.md", "Bridge", "[[reachable.md]]", type="spec")
    _write(root, "wiki/concepts/reachable.md", "Reachable", "no lexical evidence", type="concept")
    refresh_indexes(root)

    first = run_query_v2(root, "stable tie needle", top_k=2, include_context_pack=False, retrieval_mode="lexical")
    second = run_query_v2(root, "stable tie needle", top_k=2, include_context_pack=False, retrieval_mode="lexical")
    filtered = run_query_v2(
        root,
        "unique bridge needle",
        top_k=5,
        filters=QueryFilters(type="concept"),
        include_context_pack=False,
        retrieval_mode="lexical",
    )

    assert [item["path"] for item in first["results"]] == ["wiki/concepts/alpha.md", "wiki/concepts/zeta.md"]
    assert [item["path"] for item in second["results"]] == [item["path"] for item in first["results"]]
    assert "wiki/projects/alpha/specs/bridge.md" not in [item["path"] for item in filtered["results"]]
    assert "wiki/concepts/reachable.md" not in [item["path"] for item in filtered["results"]]


def test_wiki_query_excludes_structural_pages_from_results(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/needle.md", "Needle", "needle content", type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "needle", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/needle.md" in paths
    assert "wiki/index.md" not in paths
    assert "wiki/overview.md" not in paths


def test_wiki_query_excludes_retired_source_namespace(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = root / "wiki/sources/provider/_entries.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source_index\ngenerated: true\n---\n\n# Source Leaf\n\nsource index needle", encoding="utf-8")

    result = run_query_v2(root, "needle", top_k=5, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/sources/provider/_entries.md" not in paths


def test_wiki_query_graph_expands_escaped_table_wikilinks(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle table link\n\n| Example | Description |\n|---|---|\n| [[neighbor.md\\|Neighbor Page]] | related |",
        type="concept",
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "needle", top_k=2, retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    neighbor = result["results"][paths.index("wiki/concepts/neighbor.md")]
    assert neighbor["scores"]["graph"] > 0
