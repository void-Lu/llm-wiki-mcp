from __future__ import annotations

import hashlib
from pathlib import Path

from codegraph.codegraph_architecture import build_architecture, render_code_fact, render_pipeline
from codegraph.codegraph_sync import GraphSnapshot


def _snapshot(tmp_path: Path, nodes: list[dict], edges: list[dict], *, source_files: dict[str, str] | None = None) -> GraphSnapshot:
    source_files = source_files or {}
    files = []
    for path, content in source_files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        files.append({"path": path, "language": "python", "content_hash": hashlib.sha256(content.encode()).hexdigest(), "size": len(content.encode()), "node_count": 0})
    return GraphSnapshot("demo", "test", "test", 6, tuple(files), tuple(nodes), tuple(edges), (), "graph", "revision")


def _node(node_id: str, name: str, file_path: str, line: int, *, language: str = "python", kind: str = "function", exported: int = 0, visibility: str = "") -> dict:
    return {
        "id": node_id, "kind": kind, "name": name, "qualified_name": name, "file_path": file_path,
        "language": language, "start_line": line, "end_line": line + 3, "is_exported": exported,
        "visibility": visibility, "signature": "(value)",
    }


def test_python_control_flow_recursion_and_schema_share_one_ir(tmp_path: Path) -> None:
    source = """def main(value: str) -> dict:
    if value:
        for item in value:
            try:
                return helper(item)
            except Exception:
                return {}
    return {}

def helper(value: str) -> dict:
    if value:
        return helper(value)
    return {\"ok\": True}
"""
    nodes = [_node("main", "main", "a.py", 1), _node("helper", "helper", "a.py", 10)]
    edges = [
        {"source": "main", "target": "helper", "kind": "calls", "line": 5},
        {"source": "helper", "target": "helper", "kind": "calls", "line": 12},
    ]
    ir = build_architecture(_snapshot(tmp_path, nodes, edges, source_files={"a.py": source}), tmp_path)
    pipeline = ir.pipelines[0]
    body = render_pipeline(ir, pipeline, {"a.py": "wiki/projects/demo/architecture/code-facts/a.py.md"})

    assert pipeline.node_order.count("helper") == 1
    assert any(edge.status == "recursive" for edge in pipeline.edges)
    assert {item.kind for item in ir.nodes["main"].controls} == {"branch", "loop", "exception"}
    assert '"type":"object"' in body
    assert "分支" in body and "循环" in body and "异常" in body and "回边" in body
    assert "def main" not in body and "```python" not in body


def test_cross_file_pipeline_orders_shared_helper_and_excludes_support_edges(tmp_path: Path) -> None:
    nodes = [
        _node("a", "main", "a.py", 1), _node("b", "b", "b.py", 1),
        _node("c", "c", "c.py", 1), _node("shared", "shared", "c.py", 10),
    ]
    edges = [
        {"source": "a", "target": "b", "kind": "calls", "line": 2},
        {"source": "b", "target": "c", "kind": "calls", "line": 3},
        {"source": "b", "target": "shared", "kind": "instantiates", "line": 4},
        {"source": "c", "target": "shared", "kind": "calls", "line": 5},
        {"source": "a", "target": "b", "kind": "imports", "line": 1},
        {"source": "a", "target": "c", "kind": "contains", "line": 1},
        {"source": "b", "target": "c", "kind": "references", "line": 1},
    ]
    ir = build_architecture(_snapshot(tmp_path, nodes, edges), tmp_path)
    pipeline = ir.pipelines[0]
    body = render_pipeline(ir, pipeline, {f"{name}.py": f"wiki/projects/demo/architecture/code-facts/{name}.py.md" for name in "abc"})

    assert pipeline.node_order[:4] == ("a", "b", "c", "shared")
    assert sum(edge.target == "shared" for edge in pipeline.edges) == 2
    assert any(edge.status == "shared" for edge in pipeline.edges)
    assert "a::a" in "a::a"
    assert body.index("`main`") < body.index("`b`") < body.index("`c`")
    assert "`imports`" not in body and "`contains`" not in body and "`references`" not in body
    assert "[[wiki/projects/demo/architecture/code-facts/b.py|文件页]]" in body


def test_language_entrypoint_adapters_and_unknown_schema(tmp_path: Path) -> None:
    nodes = [
        _node("js", "main", "index.js", 1, language="javascript", exported=1),
        _node("ts", "handler", "handler.ts", 1, language="typescript", exported=1),
        _node("suite", "afterSubmit", "ue.js", 1, language="suitescript", exported=1),
        _node("ordinary", "helper", "ue.js", 10, language="suitescript", exported=1),
    ]
    ir = build_architecture(_snapshot(tmp_path, nodes, []), tmp_path)

    # Entrypoints are recognised but no pipelines are generated without resolved calls.
    assert {node_id for node_id in ir.nodes if ir.nodes[node_id].is_entrypoint} == {"js", "ts", "suite"}
    assert not ir.pipelines
    assert ir.nodes["js"].input_schema["type"] == "unknown"
    assert ir.nodes["js"].output_schema["type"] == "unknown"


def test_code_fact_without_root_and_public_method_is_explicit(tmp_path: Path) -> None:
    nodes = [_node("public", "public_api", "no_root.py", 1, visibility="public"), _node("private", "private", "no_root.py", 10)]
    ir = build_architecture(_snapshot(tmp_path, nodes, [], source_files={"no_root.py": "def public_api():\n    return 1\n"}), tmp_path)
    body = render_code_fact(ir, "no_root.py", {"path": "no_root.py", "language": "python", "content_hash": "x"}, {"no_root.py": "wiki/projects/demo/architecture/code-facts/no_root.py.md"})

    assert "未识别到可信入口" in body
    assert "public_api" in body
    assert "不会猜测执行链" in body


def test_large_pipeline_keeps_full_table_and_uses_stage_mermaid(tmp_path: Path) -> None:
    nodes = [_node("n0", "main", "large.py", 1)] + [_node(f"n{i}", f"step{i}", "large.py", i + 1) for i in range(1, 22)]
    edges = [{"source": f"n{i}", "target": f"n{i + 1}", "kind": "calls", "line": i + 1} for i in range(22 - 1)]
    ir = build_architecture(_snapshot(tmp_path, nodes, edges), tmp_path)
    body = render_pipeline(ir, ir.pipelines[0], {"large.py": "wiki/projects/demo/architecture/code-facts/large.py.md"})

    assert "## 阶段概览" in body
    assert "## Mermaid 展示策略" in body
    assert "step21" in body
    assert "完整节点和边保留在下表" in body
