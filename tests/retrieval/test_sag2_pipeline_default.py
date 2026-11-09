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
