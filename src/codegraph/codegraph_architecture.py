"""Human-readable, evidence-preserving CodeGraph architecture projection."""

from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

EXECUTION_EDGE_KINDS = frozenset({"calls", "instantiates"})
SUPPORTED_LANGUAGES = frozenset({"python", "javascript", "typescript", "js", "ts", "node", "suitescript", "suite_script", "suite-script"})
ENTRYPOINT_NAMES = frozenset({"main", "run", "handler", "execute", "entrypoint"})
SUITESCRIPT_ENTRYPOINT_NAMES = frozenset(
    {
        "get", "post", "put", "delete", "getinputdata", "map", "reduce", "summarize", "onrequest",
        "beforeload", "beforesubmit", "aftersubmit", "pageinit", "fieldchanged", "lineinit",
        "localizationcontextenter", "localizationcontextexit", "postsourcing", "saverecord", "sublistchanged",
        "validatedelete", "validatefield", "validateinsert", "validateline", "execute", "each", "render",
        "onaction", "afterinstall", "afterupdate", "beforeinstall", "beforeuninstall", "beforeupdate", "run",
        "initializespa",
    }
)
KNOWN_TOOL_DECORATORS = frozenset({"tool", "mcp.tool", "app.server.tool", "app.tool", "router.tool"})


@dataclass(frozen=True)
class ControlFact:
    kind: str
    start_line: int
    end_line: int
    label: str
    evidence: str


@dataclass(frozen=True)
class IRNode:
    node_id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    language: str
    start_line: int | None
    end_line: int | None
    signature: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    evidence: tuple[str, ...]
    visibility: str
    is_entrypoint: bool = False
    controls: tuple[ControlFact, ...] = ()
    boundary: bool = False


@dataclass(frozen=True)
class IREdge:
    source: str
    target: str
    kind: str
    line: int | None
    col: int | None
    execution: bool
    evidence: tuple[str, ...]
    status: str = "resolved"
    control: str = ""
    conflict: bool = False
    boundary_label: str = ""


@dataclass(frozen=True)
class Stage:
    stage_id: str
    label: str
    file_path: str | None
    node_ids: tuple[str, ...]


@dataclass(frozen=True)
class PipelineSnapshot:
    pipeline_id: str
    page_path: str
    entrypoint: dict[str, Any]
    node_ids: tuple[str, ...]
    member_files: tuple[str, ...]
    edge_keys: tuple[tuple[str, str, str], ...]
    external_edge_keys: tuple[tuple[str, str, str], ...]
    unresolved_count: int
    pipeline_status: str
    node_order: tuple[str, ...] = ()
    edges: tuple[IREdge, ...] = ()
    stages: tuple[Stage, ...] = ()


@dataclass(frozen=True)
class ArchitectureIR:
    nodes: Mapping[str, IRNode]
    edges: tuple[IREdge, ...]
    pipelines: tuple[PipelineSnapshot, ...]
    file_nodes: Mapping[str, tuple[str, ...]]
    file_roots: Mapping[str, tuple[str, ...]]
    pipeline_by_node: Mapping[str, tuple[str, ...]]
    boundary_labels: Mapping[str, str]


@dataclass
class _AstFact:
    inputs: Mapping[str, Any] | None = None
    output: Mapping[str, Any] | None = None
    controls: list[ControlFact] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)
    decorators: set[str] = field(default_factory=set)


def build_architecture(snapshot: Any, workspace_root: str | Path | None = None) -> ArchitectureIR:
    """Build one normalized IR from a CodeGraph snapshot and ephemeral AST facts."""

    raw_nodes = {str(item.get("id")): dict(item) for item in snapshot.nodes if item.get("id")}
    ast_facts = _python_facts(snapshot.files, raw_nodes, Path(workspace_root) if workspace_root else None)
    overrides = _entrypoint_overrides(Path(workspace_root) if workspace_root else None)
    roots = {node_id for node_id, node in raw_nodes.items() if _trusted_entrypoint(node, ast_facts.get(node_id), overrides)}

    ir_nodes: dict[str, IRNode] = {}
    for node_id, node in raw_nodes.items():
        fact = ast_facts.get(node_id)
        evidence = ["codegraph"]
        if fact and (fact.inputs is not None or fact.output is not None or fact.controls or fact.calls):
            evidence.append("ast")
        ir_nodes[node_id] = IRNode(
            node_id=node_id,
            kind=str(node.get("kind") or "symbol"),
            name=str(node.get("name") or node_id),
            qualified_name=str(node.get("qualified_name") or node.get("name") or node_id),
            file_path=str(node.get("file_path") or ""),
            language=str(node.get("language") or "unknown"),
            start_line=_int_or_none(node.get("start_line")),
            end_line=_int_or_none(node.get("end_line")),
            signature=str(node.get("signature") or ""),
            input_schema=fact.inputs if fact and fact.inputs is not None else _unknown_schema("codegraph"),
            output_schema=fact.output if fact and fact.output is not None else _output_schema(node),
            evidence=tuple(evidence),
            visibility=str(node.get("visibility") or "unknown"),
            is_entrypoint=node_id in roots,
            controls=tuple(fact.controls) if fact else (),
        )

    raw_edges: list[IREdge] = []
    for edge in snapshot.edges:
        raw_edges.append(_edge_from_raw(edge, raw_nodes))
    raw_edges.extend(_supplemental_ast_edges(raw_nodes, ast_facts, raw_edges))
    raw_edges.extend(_unresolved_edges(snapshot.unresolved_refs, raw_nodes))
    edges = tuple(sorted(_deduplicate_edges(raw_edges), key=_edge_sort_key))

    outgoing: dict[str, list[IREdge]] = defaultdict(list)
    for edge in edges:
        if edge.execution:
            outgoing[edge.source].append(edge)
    for values in outgoing.values():
        values.sort(key=_edge_sort_key)

    pipeline_values: list[PipelineSnapshot] = []
    for root_id in sorted(roots, key=lambda value: _node_sort_key(ir_nodes[value])):
        pipeline = _walk_pipeline(snapshot.project, ir_nodes, outgoing, root_id)
        # Skip pipelines with no resolved execution edges — they carry no
        # cross-file or cross-function call chain, only the entrypoint itself.
        if pipeline.edge_keys:
            pipeline_values.append(pipeline)
    slug_counts: dict[str, int] = defaultdict(int)
    for pipeline in pipeline_values:
        slug_counts[_pipeline_slug(pipeline.pipeline_id)] += 1
    fixed_pipelines: list[PipelineSnapshot] = []
    for pipeline in pipeline_values:
        slug = _pipeline_slug(pipeline.pipeline_id)
        if slug_counts[slug] > 1:
            slug = f"{slug}-{hashlib.sha256(pipeline.pipeline_id.encode('utf-8')).hexdigest()[:8]}"
        fixed_pipelines.append(
            PipelineSnapshot(
                **{**pipeline.__dict__, "page_path": f"wiki/projects/{snapshot.project}/architecture/pipelines/{slug}.md"}
            )
        )

    file_nodes: dict[str, list[str]] = defaultdict(list)
    for node in ir_nodes.values():
        if node.file_path:
            file_nodes[node.file_path].append(node.node_id)
    file_roots: dict[str, list[str]] = defaultdict(list)
    for node_id in roots:
        if ir_nodes[node_id].file_path:
            file_roots[ir_nodes[node_id].file_path].append(node_id)
    pipeline_by_node: dict[str, list[str]] = defaultdict(list)
    for pipeline in fixed_pipelines:
        for node_id in pipeline.node_ids:
            if not ir_nodes.get(node_id, IRNode("", "", "", "", "", "", None, None, "", {}, {}, (), "")).boundary:
                pipeline_by_node[node_id].append(pipeline.pipeline_id)
    return ArchitectureIR(
        nodes=ir_nodes,
        edges=edges,
        pipelines=tuple(fixed_pipelines),
        file_nodes={key: tuple(sorted(values, key=lambda value: _node_sort_key(ir_nodes[value]))) for key, values in file_nodes.items()},
        file_roots={key: tuple(sorted(values, key=lambda value: _node_sort_key(ir_nodes[value]))) for key, values in file_roots.items()},
        pipeline_by_node={key: tuple(sorted(values)) for key, values in pipeline_by_node.items()},
        boundary_labels={edge.target: edge.boundary_label for edge in edges if edge.target not in ir_nodes and edge.boundary_label},
    )


def _walk_pipeline(project: str, nodes: Mapping[str, IRNode], outgoing: Mapping[str, list[IREdge]], root_id: str) -> PipelineSnapshot:
    root = nodes[root_id]
    pipeline_id = f"{root.file_path or 'unknown'}::{root.qualified_name or root.name}"
    order: list[str] = []
    visited: set[str] = set()
    active: set[str] = set()
    selected: list[IREdge] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        order.append(node_id)
        active.add(node_id)
        for edge in outgoing.get(node_id, ()):
            target = edge.target
            status = edge.status
            if target in active:
                status = "recursive"
            elif target in visited:
                status = "shared"
            selected.append(
                IREdge(
                    source=edge.source, target=edge.target, kind=edge.kind, line=edge.line, col=edge.col,
                    execution=edge.execution, evidence=edge.evidence, status=status, control=edge.control,
                    conflict=edge.conflict, boundary_label=edge.boundary_label,
                )
            )
            if target in nodes and target not in visited:
                visit(target)
        active.remove(node_id)

    visit(root_id)
    boundary_ids = [edge.target for edge in selected if edge.target not in nodes]
    for boundary_id in boundary_ids:
        if boundary_id not in order:
            order.append(boundary_id)
    member_files = sorted({nodes[node_id].file_path for node_id in order if node_id in nodes and nodes[node_id].file_path})
    stages: list[Stage] = []
    grouped: list[tuple[str | None, list[str]]] = []
    for node_id in order:
        file_path = nodes[node_id].file_path if node_id in nodes else None
        if not grouped or grouped[-1][0] != file_path:
            grouped.append((file_path, []))
        grouped[-1][1].append(node_id)
    for index, (file_path, node_ids) in enumerate(grouped, start=1):
        stages.append(Stage(stage_id=f"stage-{index}", label=file_path or "外部/未解析边界", file_path=file_path, node_ids=tuple(node_ids)))
    edge_keys = tuple(sorted((edge.source, edge.target, edge.kind) for edge in selected if edge.target in nodes))
    external_keys = tuple(sorted((edge.source, edge.target, edge.kind) for edge in selected if edge.target not in nodes))
    status = "partial" if external_keys else "complete"
    entrypoint = {
        "id": root.node_id, "name": root.name, "qualified_name": root.qualified_name,
        "file_path": root.file_path, "language": root.language, "start_line": root.start_line, "end_line": root.end_line,
    }
    return PipelineSnapshot(
        pipeline_id=pipeline_id,
        page_path=f"wiki/projects/{project}/architecture/pipelines/{_pipeline_slug(pipeline_id)}.md",
        entrypoint=entrypoint,
        node_ids=tuple(order),
        member_files=tuple(member_files),
        edge_keys=edge_keys,
        external_edge_keys=external_keys,
        unresolved_count=len(external_keys),
        pipeline_status=status,
        node_order=tuple(order),
        edges=tuple(selected),
        stages=tuple(stages),
    )


def _edge_from_raw(edge: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]) -> IREdge:
    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    kind = str(edge.get("kind") or "reference").casefold()
    if target not in nodes:
        target = _boundary_id(source, target, kind)
    return IREdge(
        source=source, target=target, kind=kind, line=_int_or_none(edge.get("line")), col=_int_or_none(edge.get("col")),
        execution=kind in EXECUTION_EDGE_KINDS, evidence=("codegraph",), conflict=False,
        boundary_label=str(edge.get("target") or "") if target not in nodes else "",
    )


def _unresolved_edges(unresolved: Iterable[Mapping[str, Any]], nodes: Mapping[str, Mapping[str, Any]]) -> list[IREdge]:
    result: list[IREdge] = []
    for item in unresolved:
        source = str(item.get("from_node_id") or "")
        if source not in nodes:
            continue
        kind = str(item.get("reference_kind") or "reference").casefold()
        boundary = _boundary_id(source, str(item.get("reference_name") or "unknown"), kind, "unresolved")
        result.append(
            IREdge(source, boundary, kind, _int_or_none(item.get("line")), _int_or_none(item.get("col")), kind in EXECUTION_EDGE_KINDS, ("codegraph",), "unresolved", boundary_label=str(item.get("reference_name") or "unknown"))
        )
    return result


def _supplemental_ast_edges(nodes: Mapping[str, Mapping[str, Any]], facts: Mapping[str, _AstFact], existing: Iterable[IREdge]) -> list[IREdge]:
    known = {(edge.source, edge.target, edge.kind) for edge in existing}
    by_file_name: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node_id, node in nodes.items():
        by_file_name[(str(node.get("file_path") or ""), str(node.get("name") or ""))].append(node_id)
    additions: list[IREdge] = []
    for source_id, fact in facts.items():
        source = nodes.get(source_id)
        if not source:
            continue
        for name, line in fact.calls:
            candidates = by_file_name.get((str(source.get("file_path") or ""), name), [])
            if len(candidates) != 1:
                continue
            target = candidates[0]
            if (source_id, target, "calls") in known:
                continue
            conflict = any(edge.source == source_id and edge.line == line and edge.execution and edge.target != target for edge in existing)
            additions.append(IREdge(source_id, target, "calls", line, None, True, ("ast",), "supplemental", conflict=conflict))
    return additions


def _python_facts(files: Iterable[Mapping[str, Any]], nodes: Mapping[str, Mapping[str, Any]], workspace: Path | None) -> dict[str, _AstFact]:
    if workspace is None:
        return {}
    by_file: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for node_id, node in nodes.items():
        if str(node.get("language") or "").casefold() == "python":
            by_file[str(node.get("file_path") or "")].append((node_id, node))
    result: dict[str, _AstFact] = {}
    for file_item in files:
        path = str(file_item.get("path") or "")
        if not path or not by_file.get(path):
            continue
        try:
            tree = ast.parse((workspace / Path(path)).read_text(encoding="utf-8"), filename=path)
        except (OSError, SyntaxError, UnicodeError):
            continue
        definitions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for function in definitions:
            candidates = [pair for pair in by_file[path] if pair[1].get("name") == function.name and _line_matches(pair[1], function)]
            if len(candidates) != 1:
                continue
            node_id, _ = candidates[0]
            result[node_id] = _ast_fact(function)
    return result


def _ast_fact(function: ast.FunctionDef | ast.AsyncFunctionDef) -> _AstFact:
    args: dict[str, Any] = {}
    positional = [*function.args.posonlyargs, *function.args.args]
    defaults = [None] * (len(positional) - len(function.args.defaults)) + list(function.args.defaults)
    for arg, default in zip(positional, defaults):
        args[arg.arg] = {"type": _annotation_type(arg.annotation), "optional": default is not None, "evidence": "ast"}
    for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        args[arg.arg] = {"type": _annotation_type(arg.annotation), "optional": default is not None, "evidence": "ast"}
    if function.args.vararg:
        args[function.args.vararg.arg] = {"type": "array", "items": {"type": "unknown"}, "evidence": "ast"}
    if function.args.kwarg:
        args[function.args.kwarg.arg] = {"type": "object", "additionalProperties": {"type": "unknown"}, "evidence": "ast"}
    returns = [item.value for item in ast.walk(function) if isinstance(item, ast.Return) and item.value is not None]
    output = _return_schema(function.returns, returns)
    fact = _AstFact(inputs={"type": "object", "properties": args, "evidence": "ast"}, output=output)
    fact.controls.extend(_control_facts(function))
    fact.calls.extend(_call_facts(function))
    fact.decorators.update(_decorator_name(item) for item in function.decorator_list)
    return fact


class _FunctionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.controls: list[ControlFact] = []
        self.calls: list[tuple[str, int]] = []
        self.root: ast.FunctionDef | ast.AsyncFunctionDef | None = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is not self.root:
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is not self.root:
            return
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self.controls.append(ControlFact("branch", node.lineno, _end_line(node), "分支 if/else", "ast"))
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.controls.append(ControlFact("loop", node.lineno, _end_line(node), "循环 for（不展开迭代）", "ast"))
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.controls.append(ControlFact("loop", node.lineno, _end_line(node), "循环 while（不展开迭代）", "ast"))
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.controls.append(ControlFact("exception", node.lineno, _end_line(node), "异常 try/except/finally", "ast"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name:
            self.calls.append((name.split(".")[-1], node.lineno))
        self.generic_visit(node)


def _control_facts(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ControlFact]:
    visitor = _FunctionVisitor()
    visitor.root = function
    visitor.visit(function)
    return sorted(visitor.controls, key=lambda item: (item.start_line, item.kind, item.end_line))


def _call_facts(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    visitor = _FunctionVisitor()
    visitor.root = function
    visitor.visit(function)
    return visitor.calls


def render_code_fact(
    ir: ArchitectureIR,
    source_path: str,
    file_item: Mapping[str, Any],
    file_page_map: Mapping[str, str],
) -> str:
    lines = [
        "## 文件事实",
        f"- 源码路径：`{source_path}`",
        f"- 语言：`{file_item.get('language') or 'unknown'}`",
        f"- 源码 SHA-256：`{file_item.get('content_hash') or ''}`",
        "- 页面只保存 CodeGraph/AST 派生事实，不包含源码正文。",
        "",
        "## 可信入口链",
    ]
    root_ids = ir.file_roots.get(source_path, ())
    if not root_ids:
        lines.append("- 未识别到可信入口；不会猜测执行链。")
    for root_id in root_ids:
        node = ir.nodes[root_id]
        lines.extend([f"### `{node.qualified_name}`（行 {_span(node)}）", _mermaid(ir, _local_node_ids(ir, source_path, root_id), _local_edges(ir, source_path, root_id)), "", "| 阶段 | 方法/边界 | 证据与定位 |", "|---|---|---|"])
        local_nodes = _local_node_ids(ir, source_path, root_id)
        local_edges = _local_edges(ir, source_path, root_id)
        for node_id in local_nodes:
            item = ir.nodes.get(node_id)
            if item:
                lines.append(f"| 同文件 | `{item.qualified_name}` | {_location(item)}（{_file_link(file_page_map, source_path).strip('（）')}）；{_evidence(item.evidence)} |")
        for edge in local_edges:
            if edge.source in local_nodes:
                target_label = f"`{_node_label(ir, edge.target)}`"
                target_node = ir.nodes.get(edge.target)
                if target_node and target_node.file_path != source_path:
                    target_label += _file_link(file_page_map, target_node.file_path)
                lines.append(f"| 边界/关系 | `{_node_label(ir, edge.source)}` → {target_label} | `{edge.kind}`；{_edge_annotation(edge)} |")
        for node_id in local_nodes:
            item = ir.nodes.get(node_id)
            if item:
                lines.extend(_schema_rows(item))
        if len(local_nodes) == 1 and not local_edges:
            lines.append("- 入口存在，但没有已解析的同文件下游调用。")
        lines.append("")
    lines.extend(["## 未接入入口的可见方法", ""])
    visible = [ir.nodes[node_id] for node_id in ir.file_nodes.get(source_path, ()) if node_id not in root_ids and _is_visible(ir.nodes[node_id])]
    if not visible:
        lines.append("- 无额外可见方法，或 visibility/export metadata 未提供。")
    else:
        lines.append("<details><summary>展开查看</summary>\n")
        for node in visible:
            lines.append(f"- `{node.qualified_name}`（行 {_span(node)}；输入 {_schema_text(node.input_schema)}；输出 {_schema_text(node.output_schema)}）")
        lines.append("\n</details>")
    return "\n".join(lines)


def render_pipeline(
    ir: ArchitectureIR,
    pipeline: PipelineSnapshot,
    file_page_map: Mapping[str, str],
    *,
    node_filter: set[str] | None = None,
) -> str:
    display_order = tuple(node_id for node_id in pipeline.node_order if node_filter is None or node_id in node_filter)
    display_stages = tuple(
        Stage(stage.stage_id, stage.label, stage.file_path, tuple(node_id for node_id in stage.node_ids if node_id in display_order))
        for stage in pipeline.stages
        if any(node_id in display_order for node_id in stage.node_ids)
    )
    display_edges = tuple(
        edge for edge in pipeline.edges
        if node_filter is None or edge.source in node_filter or edge.target in node_filter
    )
    lines = [
        "## Pipeline 状态",
        f"- 状态：`{pipeline.pipeline_status}`",
        f"- 入口：`{pipeline.entrypoint.get('file_path') or ''}::{pipeline.entrypoint.get('qualified_name') or ''}`",
        f"- 节点：`{len(display_order)}`；阶段：`{len(display_stages)}`",
        "- 逻辑链由统一 IR 生成；边界/未知信息不会被静默删除。",
        "",
        "## 阶段概览",
        _stage_mermaid(ir, PipelineSnapshot(**{**pipeline.__dict__, "node_order": display_order, "stages": display_stages, "edges": display_edges})),
    ]
    if len(display_order) <= 20:
        lines.extend(["", "## 完整调用链 Mermaid", _mermaid(ir, display_order, display_edges)])
    else:
        lines.extend(["", "## Mermaid 展示策略", "- 当前 pipeline 超过 20 个节点；Mermaid 仅展示阶段概览，完整节点和边保留在下表，不截断。"])
    lines.extend(["", "## 分阶段完整表格", "| 阶段 | 节点/边 | 类型 | 定位/证据 |", "|---|---|---|---|"])
    for stage in display_stages:
        for node_id in stage.node_ids:
            node = ir.nodes.get(node_id)
            if node:
                link = _file_link(file_page_map, node.file_path)
                lines.append(f"| `{stage.label}` | `{node.qualified_name}`{link} | {node.kind} | {_location(node)}；{_evidence(node.evidence)} |")
            else:
                lines.append(f"| `{stage.label}` | `{_node_label(ir, node_id)}` | boundary | 外部/未解析 |")
    for edge in display_edges:
        lines.append(f"| 边 | `{_node_label(ir, edge.source)}` → `{_node_label(ir, edge.target)}` | `{edge.kind}`/{edge.status} | {_edge_annotation(edge)} |")
    lines.extend(["", "## 入参/出参格式"])
    for node_id in display_order:
        node = ir.nodes.get(node_id)
        if node:
            lines.append(f"- `{node.qualified_name}`：入参 `{_schema_text(node.input_schema)}`；出参 `{_schema_text(node.output_schema)}`。")
            for control in node.controls:
                lines.append(f"  - {control.label}（行 {control.start_line}-{control.end_line}；{control.evidence}）")
    boundaries = [edge for edge in display_edges if edge.target not in ir.nodes]
    if boundaries:
        lines.extend(["", "## 未解析或外部边界", "- 本 pipeline 标记为 `partial`，以下边界未伪造为本地方法："])
        for edge in boundaries:
            lines.append(f"- `{_node_label(ir, edge.source)}` → `{_node_label(ir, edge.target)}`（`{edge.kind}`，{_edge_annotation(edge)}）")
    return "\n".join(lines)


def render_pipeline_index(ir: ArchitectureIR, pipeline: PipelineSnapshot, part_paths: Iterable[str]) -> str:
    lines = [
        "## 大型 Pipeline 导航",
        f"- 状态：`{pipeline.pipeline_status}`",
        f"- 入口：`{pipeline.entrypoint.get('file_path') or ''}::{pipeline.entrypoint.get('qualified_name') or ''}`",
        f"- 完整链路共 `{len(pipeline.node_order)}` 个节点、`{len(pipeline.edges)}` 条执行边；完整表格拆分为以下页面，未静默截断。",
        "",
        "## 阶段概览",
        _stage_mermaid(ir, pipeline),
        "",
        "## 完整表格分片",
    ]
    for index, path in enumerate(part_paths, start=1):
        lines.append(f"- [[{path[:-3]}|第 {index} 片]]")
    return "\n".join(lines)


def render_overview(ir: ArchitectureIR, snapshot: Any, file_page_map: Mapping[str, str]) -> str:
    lines = [
        "## 同步摘要",
        f"- 项目：`{snapshot.project}`",
        f"- CodeGraph 版本：`{snapshot.codegraph_version}`",
        f"- Revision：`{snapshot.revision}`",
        f"- 文件：`{len(snapshot.files)}`；节点：`{len(snapshot.nodes)}`；关系：`{len(snapshot.edges)}`",
        "",
        "## 按入口的技术执行链",
    ]
    if not ir.pipelines:
        lines.append("- 未识别到可信入口；没有生成 pipeline 页面。")
    for pipeline in ir.pipelines:
        lines.append(f"- [[{pipeline.page_path[:-3]}|{pipeline.pipeline_id}]]（`{pipeline.pipeline_status}`；{len(pipeline.node_order)} 个节点）")
    lines.extend(["", "## 文件事实索引"])
    for source_path in sorted(file_page_map):
        lines.append(f"- [[{file_page_map[source_path][:-3]}|{source_path}]]")
    return "\n".join(lines)


def _local_node_ids(ir: ArchitectureIR, source_path: str, root_id: str) -> tuple[str, ...]:
    pipeline = next((item for item in ir.pipelines if str(item.entrypoint.get("id") or "") == root_id), None)
    if pipeline is None:
        return (root_id,)
    return tuple(node_id for node_id in pipeline.node_order if node_id in ir.nodes and ir.nodes[node_id].file_path == source_path)


def _local_edges(ir: ArchitectureIR, source_path: str, root_id: str) -> tuple[IREdge, ...]:
    pipeline = next((item for item in ir.pipelines if str(item.entrypoint.get("id") or "") == root_id), None)
    if pipeline is None:
        return ()
    local_nodes = set(_local_node_ids(ir, source_path, root_id))
    return tuple(edge for edge in pipeline.edges if edge.source in local_nodes and edge.target not in local_nodes)


def _stage_mermaid(ir: ArchitectureIR, pipeline: PipelineSnapshot) -> str:
    lines = ["```mermaid", "flowchart TD"]
    for stage in pipeline.stages:
        node = f"{stage.stage_id}[\"{stage.label}\"]"
        lines.append(f"    {node}")
    for left, right in zip(pipeline.stages, pipeline.stages[1:]):
        lines.append(f"    {left.stage_id} --> {right.stage_id}")
    lines.append("```")
    return "\n".join(lines)


def _mermaid(ir: ArchitectureIR, node_ids: Iterable[str], edges: Iterable[IREdge]) -> str:
    selected = tuple(node_ids)
    lines = ["```mermaid", "flowchart TD"]
    for index, node_id in enumerate(selected):
        safe = f"n{index}"
        label = _node_label(ir, node_id).replace('"', "'")
        lines.append(f'    {safe}["{label}"]')
    aliases = {node_id: f"n{index}" for index, node_id in enumerate(selected)}
    for edge in edges:
        if edge.source not in aliases or edge.target not in aliases:
            continue
        style = "-.->" if edge.status in {"recursive", "shared", "unresolved", "supplemental"} else "-->"
        label = f"|{edge.kind}/{edge.status}|" if edge.status != "resolved" else f"|{edge.kind}|"
        lines.append(f"    {aliases[edge.source]} {style}{label} {aliases[edge.target]}")
    for node_id in selected:
        node = ir.nodes.get(node_id)
        if not node:
            continue
        for index, control in enumerate(node.controls):
            control_id = f"c{len(aliases)}_{index}_{node_id[:8]}"
            lines.append(f'    {control_id}["{control.label} 行 {control.start_line}-{control.end_line}"]')
            if node_id in aliases:
                lines.append(f"    {aliases[node_id]} -.-> {control_id}")
                if control.kind == "loop":
                    lines.append(f"    {control_id} -.回边.-> {aliases[node_id]}")
    lines.append("```")
    return "\n".join(lines)


def _schema_rows(node: IRNode) -> list[str]:
    return [f"| 格式 | `{node.qualified_name}` 入参 | `{_schema_text(node.input_schema)}` |", f"| 格式 | `{node.qualified_name}` 出参 | `{_schema_text(node.output_schema)}` |"]


def _entrypoint_overrides(workspace: Path | None) -> set[str]:
    if workspace is None:
        return set()
    for relative in (Path(".codegraph") / "entrypoints.json", Path("entrypoints.json")):
        path = workspace / relative
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        values = data.get("entrypoints", data) if isinstance(data, Mapping) else data
        if isinstance(values, list):
            return {str(value) for value in values if isinstance(value, str)}
    return set()


def _trusted_entrypoint(node: Mapping[str, Any], fact: _AstFact | None, overrides: set[str]) -> bool:
    node_id = str(node.get("id") or "")
    qualified = str(node.get("qualified_name") or "")
    if node_id in overrides or qualified in overrides or f"{node.get('file_path') or ''}::{qualified}" in overrides:
        return True
    if str(node.get("kind") or "").casefold() not in {"function", "method"}:
        return False
    name = str(node.get("name") or "").casefold()
    language = str(node.get("language") or "").casefold()
    exported = bool(node.get("is_exported"))
    decorators = _decorators(node) | (fact.decorators if fact else set())
    if language == "python":
        return name == "main" or bool(decorators & KNOWN_TOOL_DECORATORS)
    if language in {"suitescript", "suite_script", "suite-script"}:
        return exported and name in SUITESCRIPT_ENTRYPOINT_NAMES
    if language in {"javascript", "typescript", "js", "ts", "node"}:
        return exported and (name in ENTRYPOINT_NAMES or name in SUITESCRIPT_ENTRYPOINT_NAMES or "define" in decorators)
    return False


def _decorators(node: Mapping[str, Any]) -> set[str]:
    value = node.get("decorators")
    if isinstance(value, list):
        return {str(item).strip().lstrip("@").casefold() for item in value}
    text = str(value or "").strip()
    if not text:
        return set()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return {str(item).strip().lstrip("@").casefold() for item in parsed}
    except json.JSONDecodeError:
        pass
    return {item.strip().lstrip("@").casefold() for item in text.replace(",", " ").split() if item.strip()}


def _decorator_name(node: ast.expr) -> str:
    name = _dotted_name(node)
    return name.casefold() if name else ""


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _line_matches(node: Mapping[str, Any], function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    start = _int_or_none(node.get("start_line"))
    return start is None or abs(start - function.lineno) <= 1


def _return_schema(annotation: ast.expr | None, returns: list[ast.expr]) -> Mapping[str, Any]:
    if annotation is not None:
        return {"type": _annotation_type(annotation), "evidence": "ast"}
    if not returns:
        return {"type": "null", "evidence": "ast"}
    shapes = [_expression_schema(item) for item in returns]
    if all(shape == shapes[0] for shape in shapes[1:]):
        return shapes[0]
    return _unknown_schema("ast", conflict=True)


def _expression_schema(value: ast.expr) -> dict[str, Any]:
    if isinstance(value, ast.Constant):
        if value.value is None:
            kind = "null"
        elif isinstance(value.value, bool):
            kind = "boolean"
        elif isinstance(value.value, int) and not isinstance(value.value, bool):
            kind = "integer"
        elif isinstance(value.value, float):
            kind = "number"
        elif isinstance(value.value, str):
            kind = "string"
        else:
            kind = "unknown"
        return {"type": kind, "evidence": "ast"}
    if isinstance(value, ast.Dict):
        properties: dict[str, Any] = {}
        for key, item in zip(value.keys, value.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                properties[key.value] = _expression_schema(item)
        return {"type": "object", "properties": properties, "evidence": "ast"}
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        shapes = [_expression_schema(item) for item in value.elts]
        item_shape = shapes[0] if shapes and all(item == shapes[0] for item in shapes) else {"type": "unknown"}
        return {"type": "array", "items": item_shape, "evidence": "ast"}
    return _unknown_schema("ast")


def _annotation_type(annotation: ast.expr | None) -> str:
    name = _dotted_name(annotation) if annotation else ""
    return {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object", "None": "null"}.get(name, "unknown")


def _output_schema(node: Mapping[str, Any]) -> Mapping[str, Any]:
    value = str(node.get("return_type") or "").casefold()
    mapped = {"str": "string", "string": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object", "none": "null"}.get(value)
    return {"type": mapped or "unknown", "evidence": "codegraph"}


def _unknown_schema(evidence: str, *, conflict: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "unknown", "evidence": evidence}
    if conflict:
        value["conflict"] = True
    return value


def _schema_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_visible(node: IRNode) -> bool:
    return node.visibility.casefold() in {"public", "protected", "exported"} or node.is_entrypoint


def _node_label(ir: ArchitectureIR, node_id: str) -> str:
    node = ir.nodes.get(node_id)
    if node:
        return node.qualified_name
    return ir.boundary_labels.get(node_id, node_id.removeprefix("boundary:") or "unknown")


def _file_link(file_page_map: Mapping[str, str], file_path: str) -> str:
    target = file_page_map.get(file_path)
    return f"（[[{target[:-3]}|文件页]]）" if target else ""


def _location(node: IRNode) -> str:
    return f"{node.file_path}:{_span(node)}"


def _span(node: IRNode) -> str:
    if node.start_line is None:
        return "?"
    return str(node.start_line) if node.end_line in (None, node.start_line) else f"{node.start_line}-{node.end_line}"


def _evidence(values: Iterable[str]) -> str:
    return "/".join(values) or "unknown"


def _edge_annotation(edge: IREdge) -> str:
    details = [f"行 {edge.line or '?'}", _evidence(edge.evidence), edge.status]
    if edge.conflict:
        details.append("conflict")
    return "；".join(details)


def _boundary_id(source: str, target: str, kind: str, prefix: str = "external") -> str:
    digest = hashlib.sha256(f"{source}\0{target}\0{kind}\0{prefix}".encode("utf-8")).hexdigest()[:12]
    return f"boundary:{prefix}:{digest}"


def _deduplicate_edges(edges: Iterable[IREdge]) -> list[IREdge]:
    result: dict[tuple[str, str, str, int | None, str], IREdge] = {}
    for edge in edges:
        key = (edge.source, edge.target, edge.kind, edge.line, edge.status)
        result[key] = edge
    return list(result.values())


def _edge_sort_key(edge: IREdge) -> tuple[Any, ...]:
    return (edge.source, edge.line or 0, edge.col or 0, edge.target, edge.kind, edge.status)


def _node_sort_key(node: IRNode) -> tuple[Any, ...]:
    return (node.file_path, node.start_line or 0, node.qualified_name, node.node_id)


def _pipeline_slug(value: str) -> str:
    slug = "".join(char if char.isalnum() or char in ".-_" else "-" for char in value.replace("/", "-"))
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-._").lower()[:120] or "pipeline"


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _end_line(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", getattr(node, "lineno", 0)))
