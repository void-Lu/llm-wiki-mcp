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

    def forced_score(self, question, passage_text, *, context=(), title="", page_path=""):
        lowered = passage_text.casefold()
        if "calibration procedure" in lowered:
            return 0.95
        if lowered.count("needle") >= 3:
            return 0.05
        return real_score(
            self, question, passage_text, context=context, title=title, page_path=page_path
        )

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

    def forced_score(self, question, passage_text, *, context=(), title="", page_path=""):
        lowered = passage_text.casefold()
        if "calibration procedure" in lowered:
            return 0.95
        if lowered.count("needle") >= 3:
            return 0.05
        return real_score(
            self, question, passage_text, context=context, title=title, page_path=page_path
        )

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
        "raw_fallback_mode",
        "recovery_reasons",
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


def test_raw_empty_pool_still_replace(tmp_path: Path) -> None:
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
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_single_shot"] is True
    assert sag2["raw_fallback_mode"] == "raw_replace"


def test_raw_weak_pool_merges_and_keeps_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Weak wiki evidence must survive raw single-shot (merge, not replace)."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Wiki page: only weak overlap with the question (one shared token).
    write_test_page(
        root,
        "wiki/concepts/report-stub.md",
        {"title": "Stub Notes", "type": "concept"},
        "access reports overview without depth.",
    )
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    # Task 1 title/CJK bonuses can push natural overlap ≥0.15; force weak pool.
    # Patch the names bound in query_execution_context (direct imports).
    import retrieval.query_execution_context as qec

    monkeypatch.setattr(qec, "wiki_hop_is_weak", lambda selected, evidence: bool(selected))
    monkeypatch.setattr(
        qec,
        "wiki_hop_pool_kind",
        lambda selected, evidence: "weak" if selected else "empty",
    )

    result = run_query_v2(root, "access reports", retrieval_mode="lexical", top_k=5)
    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    kinds = [item.get("source_kind") for item in result["results"]]
    # Wiki stub must remain somewhere in the returned / additional pool or results.
    all_paths = paths + [item["path"] for item in result.get("additional_results", [])]
    assert "wiki/concepts/report-stub.md" in all_paths
    assert any(k == "raw" or str(p).startswith("raw/") for p, k in zip(paths, kinds)) or any(
        str(p).startswith("raw/") for p in all_paths
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_single_shot"] is True
    assert sag2["raw_fallback_mode"] == "raw_merge"
    # Must not look like a ladder branch.
    assert result["pipeline"]["coverage"]["triggered"] is False


def test_pipeline_sag2_telemetry_includes_raw_fallback_mode(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/email-capture.md",
        {"title": "Email Capture", "type": "concept"},
        "Email capture plug-in implementation uses the Email Capture Plug-in interface.",
    )
    refresh_indexes(root)
    result = run_query_v2(root, "email capture", top_k=3)
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_fallback_mode"] in {"none", "raw_merge", "raw_replace"}


def test_coverage_raw_merge_keeps_wiki_and_adds_raw(tmp_path: Path) -> None:
    """Uncovered Latin on a strong-ish wiki pool must merge raw, not replace."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/invoice.md",
        {"title": "Invoice", "type": "concept"},
        "invoice approval is documented here",
    )
    raw = root / "raw/sources/references/rag.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: RAG Reference\n---\n\nRAG evidence complements invoice retrieval.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "invoice RAG", retrieval_mode="lexical", top_k=5
    )
    assert result["ok"] is True
    all_paths = [item["path"] for item in result["results"]] + [
        item["path"] for item in result.get("additional_results", [])
    ]
    assert "wiki/concepts/invoice.md" in all_paths
    assert any("rag" in str(p).casefold() or str(p).startswith("raw/") for p in all_paths)
    sag2 = result["pipeline"]["sag2_hop"]
    assert "coverage_raw_merge" in sag2["recovery_reasons"]
    assert sag2["raw_single_shot"] is True
    assert sag2["raw_fallback_mode"] == "raw_merge"
    assert result["pipeline"]["coverage"]["triggered"] is True
    assert "rag" in [
        t.casefold() for t in result["pipeline"]["coverage"]["uncovered_latin_terms"]
    ] or result["pipeline"]["coverage"]["triggered"] is True
    # Must not look like full P2 ladder (no wiki_relaxed / raw_zero lexical mode).
    assert result["pipeline"]["lexical"]["mode"] not in {
        "wiki_relaxed",
        "raw_zero",
        "coverage",
    }


def test_coverage_and_weak_share_one_raw_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage raw must not be followed by a second weak raw FTS."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/invoice.md",
        {"title": "Invoice", "type": "concept"},
        "invoice approval is documented here",
    )
    raw = root / "raw/sources/references/rag.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: RAG Reference\n---\n\nRAG evidence complements invoice retrieval.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    import retrieval.query_execution_context as qec

    calls = {"n": 0}
    real_get = qec.QueryExecutionContext.get_raw_store

    def wrapped(self):  # noqa: ANN001
        store = real_get(self)
        if not getattr(store, "_knife_counted", False):
            orig = store.search_fts

            def counted(query, **kwargs):  # noqa: ANN001
                calls["n"] += 1
                return orig(query, **kwargs)

            store.search_fts = counted  # type: ignore[method-assign]
            store._knife_counted = True  # type: ignore[attr-defined]
        return store

    monkeypatch.setattr(qec.QueryExecutionContext, "get_raw_store", wrapped)
    # Force eager weak/empty so a missing orchestrator guard
    # (`if not self.sag2_raw_single_shot`) would admit a second raw FTS.
    # Without this, a strong-ish post-coverage pool can make plan_raw_single_shot
    # stay false and the call-count assert would pass vacuously.
    monkeypatch.setattr(qec, "wiki_hop_is_weak", lambda selected, evidence: True)
    monkeypatch.setattr(
        qec,
        "wiki_hop_pool_kind",
        lambda selected, evidence: "weak" if selected else "empty",
    )
    monkeypatch.setattr(qec, "plan_raw_single_shot", lambda **kwargs: True)

    result = None
    err: BaseException | None = None
    try:
        result = run_query_v2(root, "invoice RAG", retrieval_mode="lexical", top_k=5)
    except BaseException as exc:  # noqa: BLE001 — assert call count even if merge crashes
        err = exc
    assert calls["n"] <= 1, (
        f"expected at most one raw FTS after coverage; got {calls['n']}"
        + (f" (pipeline error: {err!r})" if err is not None else "")
    )
    assert result is not None, f"pipeline crashed before assertions: {err!r}"
    assert "coverage_raw_merge" in result["pipeline"]["sag2_hop"]["recovery_reasons"]


def test_cjk_empty_pool_fires_one_wiki_relaxed(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Strict AND may miss; relaxed OR / CJK bigrams should hit.
    write_test_page(
        root,
        "wiki/concepts/timebill.md",
        {"title": "工时填写", "type": "concept"},
        "本文说明如何配置工时填写流程与保存步骤。",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "如何配置工时填写", retrieval_mode="lexical", top_k=5
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert "wiki_relaxed_empty" in sag2["recovery_reasons"] or (
        "wiki_relaxed_cjk" in sag2["recovery_reasons"]
    )
    # Strong relaxed fill must skip raw and keep relaxed lexical mode.
    assert sag2["raw_fallback_mode"] == "none"
    assert sag2["raw_single_shot"] is False
    assert result["pipeline"]["lexical"]["mode"] in {"relaxed", "wiki_relaxed"}
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] >= 1
    assert result["results"]
    assert result["results"][0]["path"].startswith("wiki/")


def test_cjk_relaxed_still_empty_falls_back_to_raw_replace(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # No wiki pages that can answer; raw holds the CJK-adjacent Latin help text.
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: 工时帮助\n---\n\n如何配置工时填写：打开菜单后保存。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "如何配置工时填写", retrieval_mode="lexical", top_k=5
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert "wiki_relaxed_empty" in sag2["recovery_reasons"]
    # After failed/empty relaxed, empty→raw replace.
    assert sag2["raw_fallback_mode"] == "raw_replace"
    assert sag2["raw_single_shot"] is True
    assert result["results"]
    assert str(result["results"][0]["path"]).startswith("raw/")


def test_default_path_never_runs_full_p2_ladder_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when coverage + CJK empty signals exist, do not call plan_fallback chain."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/invoice.md",
        {"title": "Invoice", "type": "concept"},
        "invoice approval",
    )
    refresh_indexes(root)

    import retrieval.query_execution_context as qec
    import retrieval.query_recovery as recovery

    calls = {"plan_fallback": 0}
    real_plan = recovery.plan_fallback

    def counted(state):  # noqa: ANN001
        calls["plan_fallback"] += 1
        return real_plan(state)

    monkeypatch.setattr(qec, "plan_fallback", counted)
    monkeypatch.setattr(recovery, "plan_fallback", counted)

    run_query_v2(root, "invoice RAG 如何配置", retrieval_mode="lexical", top_k=5)
    assert calls["plan_fallback"] == 0


def test_cjk_empty_pool_strong_relaxed_skips_raw(tmp_path: Path) -> None:
    """Strong wiki_relaxed fill must look strong to raw (evidence stamped)."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/timebill.md",
        {"title": "工时填写", "type": "concept"},
        "本文说明如何配置工时填写流程与保存步骤。",
    )
    # Raw competitor that would win/merge if knife-2 strength were invisible.
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: 工时帮助\n---\n\n如何配置工时填写：打开菜单后保存。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "如何配置工时填写", retrieval_mode="lexical", top_k=5
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert "wiki_relaxed_empty" in sag2["recovery_reasons"] or (
        "wiki_relaxed_cjk" in sag2["recovery_reasons"]
    )
    assert sag2["raw_fallback_mode"] == "none"
    assert sag2["raw_single_shot"] is False
    assert result["pipeline"]["lexical"]["mode"] in {"relaxed", "wiki_relaxed"}
    assert result["results"]
    assert str(result["results"][0]["path"]).startswith("wiki/")
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] >= 1


def test_wiki_relaxed_cjk_weak_pool_skips_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Weak+CJK reason telemetry; strong relaxed fill must skip raw."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Pre-existing weak wiki row so pool_kind is weak (not empty).
    write_test_page(
        root,
        "wiki/concepts/stub.md",
        {"title": "笔记", "type": "concept"},
        "配置说明",
    )
    write_test_page(
        root,
        "wiki/concepts/timebill.md",
        {"title": "工时填写", "type": "concept"},
        "本文说明如何配置工时填写流程与保存步骤。",
    )
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: 工时帮助\n---\n\n如何配置工时填写：打开菜单后保存。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    import retrieval.query_execution_context as qec
    from retrieval.sag2_evidence import HeuristicEvidenceJudge

    # Force pre-relaxed pool to look weak so knife-2 plans wiki_relaxed_cjk.
    # After strong fill, real wiki_hop_* must see stamped evidence (≥0.15).
    real_kind = qec.wiki_hop_pool_kind
    real_weak = qec.wiki_hop_is_weak
    state = {"relaxed_done": False}

    def kind_after(selected, evidence):  # noqa: ANN001
        if not state["relaxed_done"]:
            return "weak" if selected else "empty"
        return real_kind(selected, evidence)

    def weak_after(selected, evidence):  # noqa: ANN001
        if not state["relaxed_done"]:
            return True
        return real_weak(selected, evidence)

    real_relaxed = qec.QueryExecutionContext._run_wiki_relaxed_single_shot

    def wrapped_relaxed(self, request_view, reason):  # noqa: ANN001
        real_relaxed(self, request_view, reason)
        state["relaxed_done"] = True

    monkeypatch.setattr(qec, "wiki_hop_pool_kind", kind_after)
    monkeypatch.setattr(qec, "wiki_hop_is_weak", weak_after)
    monkeypatch.setattr(
        qec.QueryExecutionContext, "_run_wiki_relaxed_single_shot", wrapped_relaxed
    )
    # Keep judge real so stamping uses hop/judge scores.
    assert HeuristicEvidenceJudge is not None

    result = run_query_v2(
        root, "如何配置工时填写", retrieval_mode="lexical", top_k=5
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert "wiki_relaxed_cjk" in sag2["recovery_reasons"]
    assert sag2["raw_fallback_mode"] == "none"
    assert sag2["raw_single_shot"] is False
    assert result["pipeline"]["lexical"]["mode"] in {"relaxed", "wiki_relaxed"}
    assert result["results"]
    assert str(result["results"][0]["path"]).startswith("wiki/")
