import ast
import importlib
from pathlib import Path


MIGRATED_RECALL_NAMES = frozenset(
    {
        "ADAPTIVE_EXPAND_MAX",
        "ADAPTIVE_SCORE_RATIO",
        "DEFAULT_TOP_K",
        "IDENTIFIER_PHRASE_BONUS",
        "IDENTIFIER_PHRASE_CANDIDATES",
        "MAX_ADAPTIVE_SCORE_RATIO",
        "PASSAGE_PROBE_LIMIT",
        "PASSAGE_SCAN_LIMIT",
        "RANKING_POLICY_VERSION",
        "RAW_FALLBACK_CANDIDATE_LIMIT",
        "RAW_FALLBACK_LIMIT",
        "RRF_K",
        "adaptive_expand",
        "classify_intent",
        "merge_coverage_items",
        "query_expansion",
        "raw_recovery_candidates",
        "relaxed_recovery_items",
        "step_counts_for_pages",
        "uncovered_latin_terms",
    }
)


def test_query_pipeline_imports_only_public_execution_context_symbols() -> None:
    """流水线只能依赖执行上下文的公开符号。"""

    source_path = Path(__file__).parents[2] / "src" / "retrieval" / "query_pipeline.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    private_imports = sorted(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "retrieval.query_execution_context"
        for alias in node.names
        if alias.name != "*" and alias.name.startswith("_")
    )

    assert private_imports == []


def test_query_recall_policy_public_interface_is_explicit() -> None:
    policy = importlib.import_module("retrieval.query_recall_policy")

    assert set(policy.__all__) == MIGRATED_RECALL_NAMES
    assert all(not name.startswith("_") for name in policy.__all__)


def test_source_never_imports_migrated_recall_names_from_execution_context() -> None:
    source_root = Path(__file__).parents[2] / "src"
    violations: list[str] = []
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.ImportFrom)
                or node.module != "retrieval.query_execution_context"
            ):
                continue
            for alias in node.names:
                if alias.name in MIGRATED_RECALL_NAMES:
                    violations.append(f"{source_path}:{alias.name}")

    assert violations == []


def test_execution_context_does_not_reexport_recall_policy_names() -> None:
    context = importlib.import_module("retrieval.query_execution_context")

    assert all(not hasattr(context, name) for name in MIGRATED_RECALL_NAMES)
