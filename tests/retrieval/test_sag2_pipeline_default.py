from __future__ import annotations

from pathlib import Path

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

