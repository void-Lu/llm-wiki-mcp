from __future__ import annotations

from pathlib import Path
import pytest

from retrieval.query_pipeline import run_query_v2
from tests.helpers import write_test_page
from wiki.wiki_index import refresh_indexes
from wiki.wiki_paths import create_wiki_root


def _seed_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/email-capture.md",
        {"title": "Email Capture", "type": "concept"},
        "Email capture plug-in implementation uses the Email Capture Plug-in interface.",
    )
    write_test_page(
        root,
        "wiki/concepts/email-capture-interface.md",
        {"title": "Email Capture Interface", "type": "concept"},
        "The Email Capture Plug-in Interface Description covers event callbacks.",
    )
    refresh_indexes(root)
    return root


def test_default_query_runs_without_p2_ladder_branch_names(tmp_path: Path) -> None:
    root = _seed_vault(tmp_path)
    result = run_query_v2(root, "email capture plug-in implementation", top_k=5)
    assert result["ok"] is True
    pipeline = result["pipeline"]
    # Deprecated ladder signals must not show as actively triggered on default path.
    assert pipeline.get("coverage", {}).get("triggered") in {False, None}
    lexical_mode = pipeline.get("lexical", {}).get("mode")
    assert lexical_mode not in {"wiki_relaxed", "raw_zero", "coverage"}
    sag2 = pipeline.get("sag2_hop")
    assert isinstance(sag2, dict)
    assert "hops_run" in sag2


def test_legacy_p2_ladder_opt_in_still_reachable(tmp_path: Path) -> None:
    """Contrast path: explicit flag may still schedule ladder branches."""
    root = _seed_vault(tmp_path)
    result = run_query_v2(
        root,
        "email capture plug-in implementation",
        top_k=5,
        enable_legacy_p2_ladder=True,
    )
    assert result["ok"] is True
    # Opt-in must still expose sag2_hop telemetry (hop remains default main path).
    assert isinstance(result["pipeline"].get("sag2_hop"), dict)

def test_pack_path_prefers_evidence_score_over_stage_one_bm25_order(
    tmp_path: Path, monkeypatch
) -> None:
    """Hop evidence must outrank stage-one BM25 / path order on the pack path."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Both pages match query "needle". Path/BM25 would favor zzz (higher TF /
    # later path must not win once evidence prefers aaa).
    write_test_page(
        root,
        "wiki/concepts/zzz-weak.md",
        {"title": "Weak Evidence", "type": "concept"},
        "needle needle needle needle needle",
    )
    write_test_page(
        root,
        "wiki/concepts/aaa-strong.md",
        {"title": "Preferred Evidence", "type": "concept"},
        "needle calibration procedure for sensor array",
    )
    refresh_indexes(root)

    from retrieval.sag2_evidence import HeuristicEvidenceJudge

    real_score = HeuristicEvidenceJudge.score

    def forced_score(self, question, passage_text, *, context=()):
        lowered = passage_text.casefold()
        if "calibration procedure" in lowered:
            return 0.95
        if lowered.count("needle") >= 3:
            return 0.05
        return real_score(self, question, passage_text, context=context)

    monkeypatch.setattr(HeuristicEvidenceJudge, "score", forced_score)

    result = run_query_v2(
        root,
        "needle",
        top_k=2,
        retrieval_mode="lexical",
        include_context_pack=False,
    )
    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    assert paths[:2] == [
        "wiki/concepts/aaa-strong.md",
        "wiki/concepts/zzz-weak.md",
    ]



def test_graph_expansion_happens_after_hop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed_vault(tmp_path)
    order: list[str] = []

    import retrieval.sag2_hop as sag2_hop_module
    import retrieval.query_pipeline as qp

    original_run = sag2_hop_module.Sag2HopController.run

    def wrapped(self, question, initial_hits):  # noqa: ANN001
        order.append("hop")
        return original_run(self, question, initial_hits)

    monkeypatch.setattr(sag2_hop_module.Sag2HopController, "run", wrapped)

    original_expand = qp.apply_graph_expansion

    def expand_probe(*args, **kwargs):  # noqa: ANN001
        order.append("graph")
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(qp, "apply_graph_expansion", expand_probe)
    run_query_v2(root, "email capture plug-in", top_k=5, graph_expansion=True)
    assert "hop" in order and "graph" in order
    assert order.index("hop") < order.index("graph")
    assert order.count("graph") == 1


def test_graph_seeds_reflect_evidence_before_expand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-hop graph must seed from evidence-blended scores, not raw fusion."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/zzz-weak.md",
        {"title": "Weak Evidence", "type": "concept"},
        "needle needle needle needle needle links to [[aaa-strong]].",
    )
    write_test_page(
        root,
        "wiki/concepts/aaa-strong.md",
        {"title": "Preferred Evidence", "type": "concept"},
        "needle calibration procedure for sensor array",
    )
    refresh_indexes(root)

    from retrieval.sag2_evidence import HeuristicEvidenceJudge
    import retrieval.query_pipeline as qp

    real_score = HeuristicEvidenceJudge.score

    def forced_score(self, question, passage_text, *, context=()):
        lowered = passage_text.casefold()
        if "calibration procedure" in lowered:
            return 0.95
        if lowered.count("needle") >= 3:
            return 0.05
        return real_score(self, question, passage_text, context=context)

    monkeypatch.setattr(HeuristicEvidenceJudge, "score", forced_score)
    # Huge blend weight: if expand runs before blend, seed scores ignore it.
    monkeypatch.setattr(qp, "SAG2_EVIDENCE_BLEND_WEIGHT", 100.0)

    seed_fusion: dict[str, float] = {}
    original_expand = qp.apply_graph_expansion

    def expand_probe(scored, candidates, graph, max_graph_hops=2, collect_reasons=False):  # noqa: ANN001
        for path, candidate in scored.items():
            seed_fusion[path] = float(candidate.fusion_score)
        return original_expand(
            scored, candidates, graph, max_graph_hops=max_graph_hops, collect_reasons=collect_reasons
        )

    monkeypatch.setattr(qp, "apply_graph_expansion", expand_probe)

    result = run_query_v2(
        root,
        "needle",
        top_k=2,
        retrieval_mode="lexical",
        graph_expansion=True,
        include_context_pack=False,
    )
    assert result["ok"] is True
    assert "wiki/concepts/aaa-strong.md" in seed_fusion
    assert "wiki/concepts/zzz-weak.md" in seed_fusion
    # Blend must already be applied to graph seeds (0.95*100 vs 0.05*100).
    assert seed_fusion["wiki/concepts/aaa-strong.md"] - seed_fusion["wiki/concepts/zzz-weak.md"] > 50.0


def test_graph_ablation_zeroes_graph_score(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/seed.md",
        {"title": "Seed Page", "type": "concept"},
        "unique needle links to [[neighbor.md]].",
    )
    write_test_page(
        root,
        "wiki/concepts/neighbor.md",
        {"title": "Neighbor Page", "type": "concept"},
        "related content without the unique token.",
    )
    refresh_indexes(root)

    disabled = run_query_v2(
        root, "needle", top_k=5, retrieval_mode="lexical", graph_expansion=False
    )
    assert disabled["ok"] is True
    assert disabled["pipeline"]["counters"]["graph_hits"] == 0
    assert all(item["scores"]["graph"] == 0 for item in disabled["results"])


def test_raw_single_shot_when_wiki_hop_empty(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "access reports", retrieval_mode="lexical")

    assert result["results"]
    assert result["results"][0]["path"] == "raw/sources/file/default/help.md"
    assert result["results"][0]["source_kind"] == "raw"
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert "wiki_hop_empty" in result["pipeline"]["fallback"]["reasons"] or (
        "wiki_hop_weak" in result["pipeline"]["fallback"]["reasons"]
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_single_shot"] is True


def test_no_ladder_branch_when_raw_single_shot(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "access reports", retrieval_mode="lexical")
    pipeline = result["pipeline"]
    assert pipeline["coverage"]["triggered"] is False
    assert pipeline["lexical"]["mode"] not in {
        "wiki_relaxed",
        "coverage",
        "raw_zero",
        "raw_prefix",  # single-shot uses strict once; prefix ladder must stay off
    }
    assert pipeline["counters"]["relaxed_fts_hits"] == 0


def test_pipeline_includes_sag2_hop_telemetry(tmp_path: Path) -> None:
    root = _seed_vault(tmp_path)
    result = run_query_v2(root, "email capture", top_k=3)
    sag2 = result["pipeline"]["sag2_hop"]
    assert set(sag2) >= {
        "hops_run",
        "judge_calls",
        "expansion_term_count",
        "stop_reason",
        "truncated",
        "raw_single_shot",
    }
    assert isinstance(sag2["hops_run"], int)
    assert isinstance(sag2["judge_calls"], int)
    assert isinstance(sag2["expansion_term_count"], int)
    assert isinstance(sag2["stop_reason"], str)
    assert isinstance(sag2["truncated"], bool)
    assert isinstance(sag2["raw_single_shot"], bool)
    assert "results" in result and "budget" in result
    # Deprecated ladder keys remain readable but inert on default path.
    assert result["pipeline"]["coverage"]["triggered"] is False
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] == 0
