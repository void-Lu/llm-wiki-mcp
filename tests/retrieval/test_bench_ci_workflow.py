# tests/retrieval/test_bench_ci_workflow.py
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_bench_ci_workflow_calls_profile_ci() -> None:
    path = REPO / ".github" / "workflows" / "bench-ci.yml"
    assert path.is_file(), "bench-ci.yml must be force-added (git add -f)"
    text = path.read_text(encoding="utf-8")
    assert "--profile ci" in text
    assert "uv run" in text
    assert "llm-wiki-mcp bench" in text


def test_readme_documents_bench_suite() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "bench --profile" in readme
    assert "HeuristicEvidenceJudge" in readme or "弱标签" in readme
    assert "R@10" in readme or "0.75" in readme
