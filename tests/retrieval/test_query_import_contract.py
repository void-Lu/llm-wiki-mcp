import ast
from pathlib import Path


def test_query_pipeline_imports_only_public_execution_context_symbols() -> None:
    """流水线只能依赖执行上下文的公开符号。"""

    source_path = Path(__file__).parents[2] / "src" / "retrieval" / "query_pipeline.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    private_imports = sorted(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "retrieval.query_execution_context"
        for alias in node.names
        if alias.name != "*" and alias.name.startswith("_")
    )

    assert private_imports == []
