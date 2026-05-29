"""Tests for pipeline_detector module."""

from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.pipeline_detector import (
    PATTERNS,
    Pipeline,
    PipelineDetector,
    _is_project_source,
)


def _make_graph(files, nodes, edges):
    return {"files": files, "nodes": nodes, "edges": edges}


def _node(id_, name, kind, file_path):
    return {
        "id": id_,
        "name": name,
        "kind": kind,
        "filePath": file_path,
        "startLine": 1,
        "endLine": 10,
    }


def _edge(source, target, kind="calls"):
    return {"source": source, "target": target, "kind": kind}


# --- _is_project_source ---


def test_is_project_source_normal():
    assert _is_project_source("src/pkg/main.js") is True


def test_is_project_source_vendor_path():
    assert _is_project_source("node_modules/lib/index.js") is False


def test_is_project_source_vendor_stem():
    assert _is_project_source("src/lodash.js") is False


# --- PLACEHOLDER_MORE_TESTS ---


# --- _build_file_level_graph ---


class TestBuildFileLevelGraph:
    def test_filters_vendor_files(self):
        nodes = [
            _node("f:a", "a", "function", "src/app/main.js"),
            _node("f:b", "b", "function", "node_modules/lib/util.js"),
        ]
        edges = [_edge("f:a", "f:b")]
        detector = PipelineDetector(_make_graph([], nodes, edges))
        file_nodes, file_edges = detector._build_file_level_graph()
        assert len(file_edges) == 0

    def test_cross_file_edges(self):
        nodes = [
            _node("f:a", "a", "function", "src/app/main.js"),
            _node("f:b", "b", "function", "src/app/util.js"),
        ]
        edges = [_edge("f:a", "f:b")]
        detector = PipelineDetector(_make_graph([], nodes, edges))
        file_nodes, file_edges = detector._build_file_level_graph()
        assert ("src/app/main.js", "src/app/util.js") in file_edges
        assert "src/app/main.js" in file_nodes
        assert "src/app/util.js" in file_nodes

    def test_skips_same_file_edges(self):
        nodes = [
            _node("f:a", "a", "function", "src/app/main.js"),
            _node("f:b", "b", "function", "src/app/main.js"),
        ]
        edges = [_edge("f:a", "f:b")]
        detector = PipelineDetector(_make_graph([], nodes, edges))
        _, file_edges = detector._build_file_level_graph()
        assert len(file_edges) == 0

    def test_filters_high_density_vendor(self):
        nodes = [_node(f"f:{i}", f"fn{i}", "function", "src/vendor_bundle.js") for i in range(130)]
        nodes.append(_node("f:caller", "caller", "function", "src/app/main.js"))
        edges = [_edge("f:caller", "f:0")]
        detector = PipelineDetector(_make_graph([], nodes, edges))
        _, file_edges = detector._build_file_level_graph()
        assert len(file_edges) == 0


# --- _extract_text_references ---


class TestExtractTextReferences:
    def test_task_create(self, tmp_path):
        src = tmp_path / "src" / "app"
        src.mkdir(parents=True)
        script = src / "mr_process.js"
        script.write_text(
            "var t = task.create({taskType: task.TaskType.MAP_REDUCE, scriptId: 'customscript_mr_bank'});",
            encoding="utf-8",
        )
        nodes = [_node("f:a", "a", "function", "src/app/mr_process.js")]
        detector = PipelineDetector(_make_graph([], nodes, []), project_path=tmp_path)
        refs = detector._extract_text_references()
        assert any(r[1] == "customscript_mr_bank" and r[2] == "task_create" for r in refs)

    def test_suitelet_client_script_module_path_links_client_script(self, tmp_path):
        src = tmp_path / "src" / "SuiteScripts"
        src.mkdir(parents=True)
        suitelet = src / "sl_order_page.js"
        suitelet.write_text(
            "var form = serverWidget.createForm({title: 'Order'});\n"
            "form.clientScriptModulePath = './cs_order_page.js';\n",
            encoding="utf-8",
        )
        client = src / "cs_order_page.js"
        client.write_text("function pageInit(context) { return true; }", encoding="utf-8")
        files = [
            {"path": "src/SuiteScripts/sl_order_page.js"},
            {"path": "src/SuiteScripts/cs_order_page.js"},
        ]
        nodes = [
            _node("f:sl", "onRequest", "function", "src/SuiteScripts/sl_order_page.js"),
            _node("f:cs", "pageInit", "function", "src/SuiteScripts/cs_order_page.js"),
        ]

        detector = PipelineDetector(_make_graph(files, nodes, []), project_path=tmp_path)

        refs = detector._extract_text_references()
        assert (
            "src/SuiteScripts/sl_order_page.js",
            "src/SuiteScripts/cs_order_page.js",
            "client_script_module_path",
        ) in refs

        pipelines = detector.detect()
        assert len(pipelines) == 1
        assert pipelines[0].files == [
            "src/SuiteScripts/cs_order_page.js",
            "src/SuiteScripts/sl_order_page.js",
        ]
        assert (
            "src/SuiteScripts/sl_order_page.js",
            "src/SuiteScripts/cs_order_page.js",
            "client_script_module_path",
        ) in pipelines[0].implicit_edges

    def test_suitelet_absolute_suite_scripts_client_script_path(self, tmp_path):
        src = tmp_path / "SuiteScripts_GL"
        src.mkdir(parents=True)
        suitelet = src / "sl_hc_claim_invbill_page.js"
        suitelet.write_text(
            "let form = serverWidget.createForm({title: '收付款核销'});\n"
            "form.clientScriptModulePath = '/SuiteScripts/SuiteScripts_GL/cs_hc_claim_invbill_page.js';\n",
            encoding="utf-8",
        )
        client = src / "cs_hc_claim_invbill_page.js"
        client.write_text("function pageInit(context) { return true; }", encoding="utf-8")
        files = [
            {"path": "SuiteScripts_GL/sl_hc_claim_invbill_page.js"},
            {"path": "SuiteScripts_GL/cs_hc_claim_invbill_page.js"},
        ]
        nodes = [
            _node("f:sl", "onRequest", "function", "SuiteScripts_GL/sl_hc_claim_invbill_page.js"),
            _node("f:cs", "pageInit", "function", "SuiteScripts_GL/cs_hc_claim_invbill_page.js"),
        ]

        detector = PipelineDetector(_make_graph(files, nodes, []), project_path=tmp_path)

        refs = detector._extract_text_references()
        assert (
            "SuiteScripts_GL/sl_hc_claim_invbill_page.js",
            "SuiteScripts_GL/cs_hc_claim_invbill_page.js",
            "client_script_module_path",
        ) in refs

    def test_record_type(self, tmp_path):
        src = tmp_path / "src" / "app"
        src.mkdir(parents=True)
        script = src / "ue_handler.js"
        script.write_text(
            "var rec = record.load({type: 'customrecord_bank_detail', id: id});",
            encoding="utf-8",
        )
        nodes = [_node("f:a", "a", "function", "src/app/ue_handler.js")]
        detector = PipelineDetector(_make_graph([], nodes, []), project_path=tmp_path)
        refs = detector._extract_text_references()
        assert any(r[1] == "customrecord_bank_detail" and r[2] == "record_type" for r in refs)

    def test_no_project_path_returns_empty(self):
        nodes = [_node("f:a", "a", "function", "src/app/main.js")]
        detector = PipelineDetector(_make_graph([], nodes, []), project_path=None)
        assert detector._extract_text_references() == []


# --- _merge_graphs ---


class TestMergeGraphs:
    def test_dedup_directed_to_undirected(self):
        nodes = [
            _node("f:a", "a", "function", "src/a.js"),
            _node("f:b", "b", "function", "src/b.js"),
        ]
        edges = [_edge("f:a", "f:b"), _edge("f:b", "f:a")]
        detector = PipelineDetector(_make_graph([], nodes, edges))
        cg_nodes, cg_edges = detector._build_file_level_graph()
        merged_nodes, merged_edges = detector._merge_graphs(cg_nodes, cg_edges, [])
        assert len(merged_edges) == 1


# --- _cluster ---


class TestCluster:
    def test_filters_singletons(self):
        nodes = {f"file{i}.js" for i in range(5)}
        edges = [("file0.js", "file1.js"), ("file2.js", "file3.js")]
        detector = PipelineDetector(_make_graph([], [], []))
        communities = detector._cluster(nodes, edges)
        for c in communities:
            assert len(c) >= 2


# --- _build_pipelines naming ---


class TestBuildPipelinesNaming:
    def test_prefix_removal_and_frequency(self):
        nodes = [
            _node("f:a", "getInputData", "function", "src/mr_hc_vendpay_process.js"),
            _node("f:b", "map", "function", "src/mr_hc_vendpay_export.js"),
            _node("f:c", "reduce", "function", "src/sl_hc_vendpay_view.js"),
        ]
        edges = [_edge("f:a", "f:b"), _edge("f:b", "f:c")]
        files = [
            {"path": "src/mr_hc_vendpay_process.js"},
            {"path": "src/mr_hc_vendpay_export.js"},
            {"path": "src/sl_hc_vendpay_view.js"},
        ]
        detector = PipelineDetector(_make_graph(files, nodes, edges))
        pipelines = detector.detect()
        assert len(pipelines) >= 1
        assert "vendpay" in pipelines[0].name


class TestFindEntryPoints:
    def test_excludes_client_script_lifecycle_functions(self):
        nodes = [
            _node("f:sl", "onRequest", "function", "src/sl_bank_payment_page.js"),
            _node("f:cs1", "pageInit", "function", "src/cs_bank_payment_page.js"),
            _node("f:cs2", "fieldChanged", "function", "src/cs_bank_payment_page.js"),
            _node("f:cs3", "saveRecord", "function", "src/cs_bank_payment_page.js"),
        ]
        detector = PipelineDetector(_make_graph([], nodes, []))

        entries = detector._find_entry_points({
            "src/sl_bank_payment_page.js",
            "src/cs_bank_payment_page.js",
        })

        assert entries == ["onRequest"]

    def test_infers_suitelet_entry_point_from_return_object(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        suitelet = src / "sl_bank_payment_page.js"
        suitelet.write_text(
            "const onRequest = (scriptContext) => { return true; };\n"
            "return {onRequest};\n",
            encoding="utf-8",
        )
        client = src / "cs_bank_payment_page.js"
        client.write_text("return {pageInit, fieldChanged, saveRecord};", encoding="utf-8")
        nodes = [
            _node("file:sl", "sl_bank_payment_page.js", "file", "src/sl_bank_payment_page.js"),
            _node("f:cs1", "pageInit", "function", "src/cs_bank_payment_page.js"),
            _node("f:cs2", "fieldChanged", "function", "src/cs_bank_payment_page.js"),
            _node("f:cs3", "saveRecord", "function", "src/cs_bank_payment_page.js"),
        ]
        detector = PipelineDetector(_make_graph([], nodes, []), project_path=tmp_path)

        entries = detector._find_entry_points({
            "src/sl_bank_payment_page.js",
            "src/cs_bank_payment_page.js",
        })

        assert entries == ["onRequest"]


# --- Integration test ---


class TestDetectIntegration:
    def test_full_detect_with_cross_file_calls(self):
        nodes = [
            _node("f:a1", "getInputData", "function", "src/mr_hc_bank_import.js"),
            _node("f:a2", "map", "function", "src/mr_hc_bank_import.js"),
            _node("f:b1", "processBank", "function", "src/lib_bank_util.js"),
            _node("f:c1", "afterSubmit", "function", "src/ue_hc_bank_validate.js"),
            _node("f:d1", "standalone", "function", "src/sl_report.js"),
        ]
        edges = [
            _edge("f:a1", "f:b1"),
            _edge("f:a2", "f:b1"),
            _edge("f:c1", "f:b1"),
        ]
        files = [
            {"path": "src/mr_hc_bank_import.js"},
            {"path": "src/lib_bank_util.js"},
            {"path": "src/ue_hc_bank_validate.js"},
            {"path": "src/sl_report.js"},
        ]
        detector = PipelineDetector(_make_graph(files, nodes, edges))
        pipelines = detector.detect()
        assert len(pipelines) >= 1
        bank_pipeline = pipelines[0]
        assert "src/mr_hc_bank_import.js" in bank_pipeline.files
        assert "src/lib_bank_util.js" in bank_pipeline.files
        assert "src/ue_hc_bank_validate.js" in bank_pipeline.files
        assert bank_pipeline.confidence > 0

    def test_empty_graph_returns_no_pipelines(self):
        detector = PipelineDetector(_make_graph([], [], []))
        assert detector.detect() == []

    def test_single_file_no_pipeline(self):
        nodes = [_node("f:a", "a", "function", "src/main.js")]
        detector = PipelineDetector(_make_graph([], nodes, []))
        assert detector.detect() == []

