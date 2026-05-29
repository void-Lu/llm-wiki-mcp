"""Pipeline detection via Louvain clustering on file-level call graphs.

Combines CodeGraph edges with text-extracted implicit references (N/task,
N/record, N/url, etc.) to identify business pipelines in SuiteScript projects.
"""

from __future__ import annotations

import re
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.louvain import louvain


@dataclass
class Pipeline:
    name: str
    files: list[str]
    entry_points: list[str] = field(default_factory=list)
    shared_records: list[str] = field(default_factory=list)
    implicit_edges: list[tuple[str, str, str]] = field(default_factory=list)
    confidence: float = 0.0


_SUITESCRIPT_ENTRY_NAMES = frozenset({
    "getInputData", "map", "reduce", "summarize",
    "onRequest", "onAction", "beforeLoad", "beforeSubmit", "afterSubmit",
    "execute", "pageInit", "fieldChanged", "postSourcing",
    "sublistChanged", "lineInit", "validateField", "validateLine",
    "validateInsert", "validateDelete", "saveRecord",
})

_FILE_PREFIX_RE = re.compile(
    r"^(mr_hc_|sl_hc_|ue_hc_|cs_hc_|cl_hc_|mr_|sl_|ue_|cs_|cl_|ss_|rl_|wf_)"
)

# --- PLACEHOLDER_PATTERNS ---

PATTERNS: dict[str, re.Pattern[str]] = {
    "task_create": re.compile(
        r"""task\.create\s*\(\s*\{[^}]*scriptId\s*:\s*['"](\w+)['"]""", re.DOTALL
    ),
    "record_type": re.compile(
        r"""record\.(load|create|submitFields)\s*\(\s*\{[^}]*type\s*:\s*['"](\w+)['"]""", re.DOTALL
    ),
    "search_id": re.compile(
        r"""search\.(load|create)\s*\(\s*\{[^}]*id\s*:\s*['"](\w+)['"]""", re.DOTALL
    ),
    "url_script": re.compile(
        r"""url\.resolveScript\s*\(\s*\{[^}]*scriptId\s*:\s*['"](\w+)['"]""", re.DOTALL
    ),
    "url_record": re.compile(
        r"""url\.resolveRecord\s*\(\s*\{[^}]*recordType\s*:\s*['"](\w+)['"]""", re.DOTALL
    ),
    "client_script_module_path": re.compile(
        r"""\.clientScriptModulePath\s*=\s*['"]([^'"]+)['"]"""
    ),
    "exec_step": re.compile(
        r"""execution_step['"]\s*\]\s*=\s*['"]?(\w+)"""
    ),
    "custscript": re.compile(
        r"""['"]custscript_(\w+)['"]"""
    ),
}

_RETURN_OBJECT_RE = re.compile(r"""return\s*\{(?P<body>[^}]+)\}""", re.DOTALL)
_IDENTIFIER_RE = re.compile(r"""[A-Za-z_$][\w$]*""")

_VENDOR_PATH_PARTS = {"node_modules", "vendor", ".venv", "venv", "dist", "build", "__pycache__"}
_VENDOR_FILE_STEMS = {
    "papaparse", "lodash", "underscore", "jquery", "moment", "dayjs",
    "axios", "rxjs", "d3", "chart", "three", "pixi", "phaser",
}
_VENDOR_NODE_DENSITY_THRESHOLD = 120


def _is_project_source(file_path: str) -> bool:
    if not file_path:
        return False
    parts = file_path.replace("\\", "/").split("/")
    if any(part in _VENDOR_PATH_PARTS for part in parts):
        return False
    stem = Path(parts[-1]).stem.lower() if parts else ""
    stem_base = stem.removesuffix(".min")
    return stem_base not in _VENDOR_FILE_STEMS


def _normalize_source_path(file_path: str) -> str:
    normalized = posixpath.normpath(file_path.replace("\\", "/").lstrip("/"))
    return "" if normalized == "." else normalized


def _is_client_script_file(file_path: str) -> bool:
    stem = Path(file_path.replace("\\", "/")).stem.lower()
    return stem.startswith(("cs_", "cl_", "cs_hc_", "cl_hc_"))


# --- PLACEHOLDER_CLASS ---


class PipelineDetector:
    def __init__(self, graph_data: dict[str, Any], project_path: Path | None = None):
        self.nodes: list[dict[str, Any]] = graph_data.get("nodes") or []
        self.edges: list[dict[str, Any]] = graph_data.get("edges") or []
        self.files: list[dict[str, Any]] = graph_data.get("files") or []
        self.project_path = project_path
        self._node_by_id: dict[str, dict[str, Any]] = {
            str(n.get("id")): n for n in self.nodes if n.get("id")
        }
        self._nodes_by_file: dict[str, list[dict[str, Any]]] = {}
        for node in self.nodes:
            fp = str(node.get("filePath") or node.get("file") or "")
            if fp:
                self._nodes_by_file.setdefault(fp, []).append(node)

    def detect(self) -> list[Pipeline]:
        cg_nodes, cg_edges = self._build_file_level_graph()
        text_refs = self._extract_text_references()
        merged_nodes, merged_edges = self._merge_graphs(cg_nodes, cg_edges, text_refs)
        if len(merged_nodes) < 2:
            return []
        communities = self._cluster(merged_nodes, merged_edges)
        return self._build_pipelines(communities, merged_nodes)

    def _build_file_level_graph(self) -> tuple[set[str], list[tuple[str, str]]]:
        file_nodes: set[str] = set()
        file_edges: list[tuple[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.get("kind") not in {"calls", "imports"}:
                continue
            src_node = self._node_by_id.get(str(edge.get("source") or ""), {})
            tgt_node = self._node_by_id.get(str(edge.get("target") or ""), {})
            src_file = str(src_node.get("filePath") or "")
            tgt_file = str(tgt_node.get("filePath") or "")
            if not src_file or not tgt_file or src_file == tgt_file:
                continue
            if not _is_project_source(src_file) or not _is_project_source(tgt_file):
                continue
            if self._is_vendored(src_file) or self._is_vendored(tgt_file):
                continue
            pair = (src_file, tgt_file)
            if pair not in seen_edges:
                seen_edges.add(pair)
                file_edges.append(pair)
            file_nodes.add(src_file)
            file_nodes.add(tgt_file)
        return file_nodes, file_edges

    def _is_vendored(self, file_path: str) -> bool:
        return len(self._nodes_by_file.get(file_path, [])) >= _VENDOR_NODE_DENSITY_THRESHOLD

    def _extract_text_references(self) -> list[tuple[str, str, str]]:
        if not self.project_path:
            return []
        refs: list[tuple[str, str, str]] = []
        source_files = {
            str(n.get("filePath") or n.get("file") or "")
            for n in self.nodes if n.get("filePath") or n.get("file")
        }
        source_files.update(str(f.get("path") or "") for f in self.files if f.get("path"))
        for file_path in source_files:
            if not _is_project_source(file_path):
                continue
            full_path = self.project_path / file_path
            if not full_path.is_file():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            refs.extend(self._extract_refs_from_content(file_path, content))
        return refs

    def _extract_refs_from_content(self, file_path: str, content: str) -> list[tuple[str, str, str]]:
        refs: list[tuple[str, str, str]] = []
        for ref_type, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                target_id = match.group(match.lastindex or 1)
                if ref_type == "client_script_module_path":
                    resolved = self._resolve_module_path(file_path, target_id)
                    if not resolved:
                        continue
                    target_id = resolved
                refs.append((file_path, target_id, ref_type))
        return refs

    def _resolve_module_path(self, source_file: str, module_path: str) -> str | None:
        known_files = self._known_file_map()
        source_dir = posixpath.dirname(_normalize_source_path(source_file))
        raw_module_path = module_path.replace("\\", "/").lstrip("/")
        candidates: list[str] = []
        if raw_module_path.startswith(("./", "../")):
            candidates.append(_normalize_source_path(posixpath.join(source_dir, raw_module_path)))
        else:
            candidates.append(_normalize_source_path(raw_module_path))
            if raw_module_path.lower().startswith("suitescripts/"):
                candidates.append(_normalize_source_path(raw_module_path.split("/", 1)[1]))
            candidates.append(_normalize_source_path(posixpath.join(source_dir, raw_module_path)))

        for candidate in self._module_path_variants(candidates):
            lowered = candidate.lower()
            if lowered in known_files:
                return known_files[lowered]
            if (self.project_path / candidate).is_file():
                return candidate
            suffix = f"/{lowered}"
            matches = [original for key, original in known_files.items() if key.endswith(suffix)]
            if len(matches) == 1:
                return matches[0]
        return None

    def _known_file_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for f in self.files:
            path = _normalize_source_path(str(f.get("path") or ""))
            if path:
                mapping[path.lower()] = path
        for node in self.nodes:
            path = _normalize_source_path(str(node.get("filePath") or node.get("file") or ""))
            if path:
                mapping.setdefault(path.lower(), path)
        return mapping

    def _module_path_variants(self, candidates: list[str]) -> list[str]:
        variants: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            for value in (candidate, f"{candidate}.js" if not Path(candidate).suffix else ""):
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    variants.append(value)
        return variants

    def _merge_graphs(
        self,
        cg_nodes: set[str],
        cg_edges: list[tuple[str, str]],
        text_refs: list[tuple[str, str, str]],
    ) -> tuple[set[str], list[tuple[str, str]]]:
        all_nodes = set(cg_nodes)
        seen: set[tuple[str, str]] = set()
        edges: list[tuple[str, str]] = []
        for src, tgt in cg_edges:
            key = (min(src, tgt), max(src, tgt))
            if key not in seen:
                seen.add(key)
                edges.append(key)
        # Build scriptId -> file mapping for implicit edge resolution
        script_to_file = self._build_script_to_file_map()
        for src_file, target_id, ref_type in text_refs:
            tgt_file = target_id if ref_type == "client_script_module_path" else script_to_file.get(target_id)
            if tgt_file and tgt_file != src_file:
                all_nodes.add(src_file)
                all_nodes.add(tgt_file)
                key = (min(src_file, tgt_file), max(src_file, tgt_file))
                if key not in seen:
                    seen.add(key)
                    edges.append(key)
        return all_nodes, edges

    def _build_script_to_file_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for f in self.files:
            path = str(f.get("path") or "")
            if not path:
                continue
            stem = Path(path).stem.lower()
            mapping[stem] = path
            cleaned = _FILE_PREFIX_RE.sub("", stem)
            if cleaned != stem:
                mapping[cleaned] = path
        return mapping

    def _cluster(self, nodes: set[str], edges: list[tuple[str, str]]) -> list[set[str]]:
        result = louvain(nodes, edges)
        return [c for c in result.communities if len(c) >= 2]

    def _build_pipelines(self, communities: list[set[str]], all_nodes: set[str]) -> list[Pipeline]:
        total_files = max(1, len(all_nodes))
        pipelines: list[Pipeline] = []
        for idx, community in enumerate(communities):
            name = self._name_community(community, idx)
            entry_points = self._find_entry_points(community)
            shared_records = self._find_shared_records(community)
            implicit = self._find_implicit_edges(community)
            confidence = len(community) / total_files
            pipelines.append(Pipeline(
                name=name,
                files=sorted(community),
                entry_points=entry_points,
                shared_records=shared_records,
                implicit_edges=implicit,
                confidence=round(confidence, 3),
            ))
        return sorted(pipelines, key=lambda p: (-len(p.files), p.name))

    def _name_community(self, community: set[str], index: int) -> str:
        words: list[str] = []
        for file_path in community:
            stem = Path(file_path).stem.lower()
            cleaned = _FILE_PREFIX_RE.sub("", stem)
            parts = [p for p in cleaned.split("_") if len(p) > 1]
            words.extend(parts)
        counts = Counter(words)
        frequent = [w for w, c in counts.most_common(5) if c >= 2]
        if frequent:
            return "-".join(frequent[:3])
        if words:
            return "-".join(sorted(set(words))[:3])
        return f"pipeline-{index}"

    def _find_entry_points(self, community: set[str]) -> list[str]:
        entries: list[str] = []
        for file_path in community:
            if _is_client_script_file(file_path):
                continue
            for node in self._nodes_by_file.get(file_path, []):
                name = str(node.get("name") or "")
                if name in _SUITESCRIPT_ENTRY_NAMES:
                    qualified = str(node.get("qualifiedName") or name)
                    entries.append(qualified)
            entries.extend(self._infer_entry_points_from_source(file_path))
        return sorted(set(entries))

    def _infer_entry_points_from_source(self, file_path: str) -> list[str]:
        full_path = self.project_path / file_path if self.project_path else None
        if not full_path or not full_path.is_file():
            return []
        try:
            content = full_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        entries: list[str] = []
        for match in _RETURN_OBJECT_RE.finditer(content):
            body = match.group("body")
            for identifier in _IDENTIFIER_RE.findall(body):
                if identifier in _SUITESCRIPT_ENTRY_NAMES:
                    entries.append(identifier)
        return entries

    def _find_shared_records(self, community: set[str]) -> list[str]:
        records: set[str] = set()
        for file_path in community:
            full_path = self.project_path / file_path if self.project_path else None
            if not full_path or not full_path.is_file():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in PATTERNS["record_type"].finditer(content):
                record_type = match.group(2)
                if record_type.startswith("customrecord") or record_type.startswith("customlist"):
                    records.add(record_type)
        return sorted(records)

    def _find_implicit_edges(self, community: set[str]) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        for file_path in community:
            full_path = self.project_path / file_path if self.project_path else None
            if not full_path or not full_path.is_file():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for src_file, target_id, ref_type in self._extract_refs_from_content(file_path, content):
                if ref_type == "record_type":
                    continue
                result.append((src_file, target_id, ref_type))
        return result


