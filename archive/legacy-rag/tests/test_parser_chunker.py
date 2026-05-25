from pathlib import Path

from netsuite_rag_mcp.chunker import chunk_document, chunk_code_document
from netsuite_rag_mcp.chunker_xml_json import chunk_xml_document, chunk_json_config
from netsuite_rag_mcp.models import SourceDocument
from netsuite_rag_mcp.parser import parse_file, parse_markdown_file
from netsuite_rag_mcp.parser_xml_json import parse_xml_file, parse_json_config


def test_parse_markdown_frontmatter_and_body(tmp_path: Path):
    vault = tmp_path / "vault"
    note_dir = vault / "projects" / "project-a" / "scripts" / "restlet"
    note_dir.mkdir(parents=True)
    note = note_dir / "order-sync.md"
    note.write_text(
        """---
type: script
project: project-a
author: alice
script_type: restlet
script_id: customscript_order_sync_restlet
related_objects: [salesorder, itemfulfillment]
related_scripts: [customscript_order_sync_mr]
status: active
tags: [netsuite, suitescript, restlet]
---

# RESTlet - 订单同步接口

## 用途
同步订单。
""",
        encoding="utf-8",
    )

    document = parse_markdown_file(note, vault)

    assert document.source_path == "projects/project-a/scripts/restlet/order-sync.md"
    assert document.frontmatter["type"] == "script"
    assert document.frontmatter["related_objects"] == ["salesorder", "itemfulfillment"]
    assert "同步订单" in document.body
    assert len(document.doc_id) == 40


def test_chunk_document_splits_h2_and_preserves_code_fence(tmp_path: Path):
    vault = tmp_path / "vault"
    note = vault / "script.md"
    vault.mkdir()
    note.write_text(
        """---
type: script
project: project-a
script_type: restlet
script_id: customscript_order_sync_restlet
related_objects: [salesorder]
related_scripts: []
status: active
---

# RESTlet - 订单同步接口

## 核心逻辑
调用 Map/Reduce。

## 代码片段
```javascript
var url = 'https://example.com/api';
```
""",
        encoding="utf-8",
    )

    document = parse_markdown_file(note, vault)
    chunks = chunk_document(document)

    assert len(chunks) == 2
    assert chunks[0].heading == "核心逻辑"
    assert chunks[1].heading == "代码片段"
    assert "Map/Reduce" in chunks[0].text


# ── parse_code_file: SuiteScript Restlet with @NScriptType and define() ──


def test_parse_code_file_restlet(tmp_path: Path):
    vault = tmp_path / "vault"
    scripts = vault / "scripts"
    scripts.mkdir(parents=True)
    code_file = scripts / "order_sync.js"
    code_file.write_text(
        """/**
 * @NScriptType Restlet
 * @NApiVersion 2.1
 * @NModuleScope SameAccount
 */

define(
  ["N/record", "N/search", "./lib/helper"],
  function (record, search, helper) {
    function doGet(context) {
      var result = record.load({ type: context.request.params.type, id: context.request.params.id });
      return JSON.stringify(result);
    }

    function doPost(context) {
      var body = JSON.parse(context.request.body);
      var id = record.create({ type: body.type }).save();
      return JSON.stringify({ id: id });
    }

    return {
      get: doGet,
      post: doPost,
    };
  }
);
""",
        encoding="utf-8",
    )

    doc = parse_file(code_file, vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "javascript"
    assert doc.frontmatter["script_type"] == "Restlet"
    assert doc.frontmatter["api_version"] == "2.1"
    assert doc.frontmatter["module_scope"] == "SameAccount"
    assert doc.frontmatter["dependencies"] == ["N/record", "N/search", "./lib/helper"]
    assert "get: doGet" in doc.body
    assert doc.file_hash != ""
    assert doc.source_name == ""


# ── parse_code_file: UserEvent script ──


def test_parse_code_file_user_event(tmp_path: Path):
    vault = tmp_path / "vault"
    scripts = vault / "scripts"
    scripts.mkdir(parents=True)
    code_file = scripts / "ue_before_submit.ts"
    code_file.write_text(
        """/**
 * @NScriptType UserEvent
 * @NApiVersion 2.1
 */

import { EntryPoints } from "N/types";
import record from "N/record";

export function beforeLoad(context: EntryPoints.UserEventContext): void {
  // nothing
}

export function beforeSubmit(context: EntryPoints.UserEventContext): void {
  if (context.type !== context.UserEventType.CREATE) return;
  const newRecord = context.newRecord;
  newRecord.setValue({ fieldId: "custbody_status", value: "pending" });
}

export function afterSubmit(context: EntryPoints.UserEventContext): void {
  const id = context.newRecord.id;
  log.debug("afterSubmit", `Record ${id} created`);
}
""",
        encoding="utf-8",
    )

    doc = parse_file(code_file, vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "typescript"
    assert doc.frontmatter["script_type"] == "UserEvent"
    assert doc.frontmatter["api_version"] == "2.1"
    functions = doc.frontmatter["functions"]
    fn_names = [f["name"] for f in functions]
    assert "afterSubmit" in fn_names

    after_submit_fn = next(f for f in functions if f["name"] == "afterSubmit")
    assert after_submit_fn["entry_point"] is True


# ── extract function boundaries ──


def test_parse_code_file_function_boundaries(tmp_path: Path):
    vault = tmp_path / "vault"
    scripts = vault / "scripts"
    scripts.mkdir(parents=True)
    code_file = scripts / "restlet.js"
    code_file.write_text(
        """/**
 * @NScriptType Restlet
 * @NApiVersion 2.1
 */

define(["N/record"], function (record) {
  function doGet(context) {
    return "hello";
  }

  function doPost(context) {
    return "created";
  }

  return {
    get: doGet,
    post: doPost,
  };
});
""",
        encoding="utf-8",
    )

    doc = parse_file(code_file, vault)

    functions = doc.frontmatter["functions"]
    fn_map = {f["name"]: f for f in functions}

    assert "doGet" in fn_map
    assert fn_map["doGet"]["start_line"] > 0
    assert fn_map["doGet"]["end_line"] >= fn_map["doGet"]["start_line"]
    assert fn_map["doGet"]["entry_point"] is True

    assert "doPost" in fn_map
    assert fn_map["doPost"]["entry_point"] is True


# ── fallback: plain JavaScript without NetSuite annotations ──


def test_parse_code_file_fallback_plain_js(tmp_path: Path):
    vault = tmp_path / "vault"
    libs = vault / "libs"
    libs.mkdir(parents=True)
    code_file = libs / "utils.js"
    code_file.write_text(
        """// A simple utility library

function add(a, b) {
  return a + b;
}

const multiply = (a, b) => {
  return a * b;
};

var obj = {
  greet: function(name) {
    return "Hello, " + name;
  }
};
""",
        encoding="utf-8",
    )

    doc = parse_file(code_file, vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "javascript"
    # No NetSuite annotations, so these should not be set
    assert "script_type" not in doc.frontmatter
    assert doc.frontmatter.get("dependencies", []) if "dependencies" in doc.frontmatter else True
    # Functions should still be detected
    functions = doc.frontmatter.get("functions", [])
    fn_names = [f["name"] for f in functions]
    assert "add" in fn_names
    assert "multiply" in fn_names
    assert "greet" in fn_names
    assert doc.file_hash != ""


# ── parse_file dispatcher routing ──


def test_parse_file_dispatches_md(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    md_file = vault / "note.md"
    md_file.write_text(
        """---
type: note
tags: [test]
---

# Hello

Body text.
""",
        encoding="utf-8",
    )

    doc = parse_file(md_file, vault)

    assert doc is not None
    assert doc.source_kind == "note"


def test_parse_file_dispatches_js(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    js_file = vault / "script.js"
    js_file.write_text(
        """/**
 * @NScriptType Suitelet
 * @NApiVersion 2.1
 */
define([], function() { return { onRequest: function(ctx) {} }; });
""",
        encoding="utf-8",
    )

    doc = parse_file(js_file, vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "javascript"


def test_parse_file_returns_none_for_unsupported(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    py_file = vault / "module.py"
    py_file.write_text("print('hello')", encoding="utf-8")

    result = parse_file(py_file, vault)

    assert result is None


# ── description extraction from JSDoc ──


def test_parse_code_file_extracts_description(tmp_path: Path):
    vault = tmp_path / "vault"
    scripts = vault / "scripts"
    scripts.mkdir(parents=True)
    code_file = scripts / "saved_search.js"
    code_file.write_text(
        """/**
 * Saved Search RESTlet - provides search functionality
 * for external integrations.
 *
 * @NScriptType Restlet
 * @NApiVersion 2.1
 */

define(["N/search"], function (search) {
  function get(context) {
    return JSON.stringify(search.load({ id: context.request.params.id }).run().getRange({ start: 0, end: 1000 }));
  }

  return { get: get };
});
""",
        encoding="utf-8",
    )

    doc = parse_file(code_file, vault)

    assert doc is not None
    description = doc.frontmatter.get("description", "")
    assert "Saved Search RESTlet" in description


# ── parse_xml_file: NetSuite customization XML with script_id ──


def test_parse_xml_file_netsuite_customization(tmp_path: Path):
    vault = tmp_path / "vault"
    customization = vault / "customizations"
    customization.mkdir(parents=True)
    xml_file = customization / "custom_record.xml"
    xml_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<customrecord scriptid="customrecord_sample">
  <name>Sample Custom Record</name>
  <description>A sample custom record type</description>
  <field scriptid="custrecord_field1" type="TEXT">
    <label>Field 1</label>
  </field>
</customrecord>
""",
        encoding="utf-8",
    )

    doc = parse_xml_file(xml_file, source_name="test-repo", repo_root=vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "xml"
    assert doc.frontmatter.get("script_id") == "customrecord_sample"
    assert doc.frontmatter.get("name") == "Sample Custom Record"
    assert doc.frontmatter.get("description") == "A sample custom record type"
    assert doc.file_hash != ""
    assert doc.source_name == "test-repo"
    assert "<customrecord" in doc.body


def test_parse_xml_file_workflow_with_deployment(tmp_path: Path):
    vault = tmp_path / "vault"
    scripts = vault / "scripts"
    scripts.mkdir(parents=True)
    xml_file = scripts / "workflow.xml"
    xml_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<workflow scriptid="customworkflow_approval">
  <name>Approval Workflow</name>
  <description>Multi-step approval</description>
</workflow>
""",
        encoding="utf-8",
    )

    doc = parse_xml_file(xml_file, source_name="repo", repo_root=vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "xml"
    assert doc.frontmatter.get("script_id") == "customworkflow_approval"
    assert doc.frontmatter.get("record_type") == "workflow"


def test_parse_xml_file_malformed_returns_none(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    bad_file = vault / "bad.xml"
    bad_file.write_text("this is not xml <<>>", encoding="utf-8")

    result = parse_xml_file(bad_file, source_name="repo", repo_root=vault)

    # Malformed XML should return None gracefully
    assert result is None


def test_parse_xml_file_nonexistent_returns_none(tmp_path: Path):
    nonexistent = tmp_path / "does_not_exist.xml"

    result = parse_xml_file(nonexistent, source_name="repo", repo_root=tmp_path)

    assert result is None


def test_parse_xml_file_savedsearch(tmp_path: Path):
    vault = tmp_path / "vault"
    searches = vault / "searches"
    searches.mkdir(parents=True)
    xml_file = searches / "saved_search.xml"
    xml_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<savedsearch scriptid="custsearch_orders">
  <name>Orders Search</name>
  <description>Search for recent orders</description>
</savedsearch>
""",
        encoding="utf-8",
    )

    doc = parse_xml_file(xml_file, source_name="repo", repo_root=vault)

    assert doc is not None
    assert doc.frontmatter.get("script_id") == "custsearch_orders"
    assert doc.frontmatter.get("record_type") == "savedsearch"


# ── parse_json_config: manifest/config JSON ──


def test_parse_json_config_manifest(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "manifest.json"
    json_file.write_text(
        """{
  "script_id": "customscript_my_restlet",
  "name": "My RESTlet",
  "type": "restlet",
  "version": "2.1",
  "dependencies": ["N/record", "N/search"]
}
""",
        encoding="utf-8",
    )

    doc = parse_json_config(json_file, source_name="repo", repo_root=vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "json"
    assert doc.frontmatter.get("script_id") == "customscript_my_restlet"
    assert doc.frontmatter.get("name") == "My RESTlet"
    assert doc.frontmatter.get("record_type") == "restlet"
    assert doc.frontmatter.get("keys") == ["script_id", "name", "type", "version", "dependencies"]
    assert doc.file_hash != ""
    assert "customscript_my_restlet" in doc.body


def test_parse_json_config_with_label_instead_of_name(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "config.json"
    json_file.write_text(
        """{
  "script_id": "customscript_config",
  "label": "Config Script",
  "record_type": "suitelet"
}
""",
        encoding="utf-8",
    )

    doc = parse_json_config(json_file, source_name="repo", repo_root=vault)

    assert doc is not None
    assert doc.frontmatter.get("name") == "Config Script"
    assert doc.frontmatter.get("record_type") == "suitelet"


def test_parse_json_config_simple_object(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "simple.json"
    json_file.write_text(
        """{"version": "1.0", "name": "Simple Config"}
""",
        encoding="utf-8",
    )

    doc = parse_json_config(json_file, source_name="repo", repo_root=vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "json"
    assert doc.frontmatter.get("name") == "Simple Config"
    assert doc.frontmatter.get("keys") == ["version", "name"]


def test_parse_json_config_malformed_returns_none(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    bad_file = vault / "bad.json"
    bad_file.write_text("{invalid json", encoding="utf-8")

    result = parse_json_config(bad_file, source_name="repo", repo_root=vault)

    assert result is None


def test_parse_json_config_nonexistent_returns_none(tmp_path: Path):
    nonexistent = tmp_path / "does_not_exist.json"

    result = parse_json_config(nonexistent, source_name="repo", repo_root=tmp_path)

    assert result is None


# ── parse_file dispatcher for .xml and .json ──


def test_parse_file_dispatches_xml(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    xml_file = vault / "customization.xml"
    xml_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<customsegment scriptid="customsegment_status">
  <name>Status Segment</name>
</customsegment>
""",
        encoding="utf-8",
    )

    doc = parse_file(xml_file, vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "xml"
    assert doc.frontmatter.get("script_id") == "customsegment_status"


def test_parse_file_dispatches_json(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "config.json"
    json_file.write_text(
        """{"script_id": "customscript_test", "name": "Test Script", "type": "scheduled"}
""",
        encoding="utf-8",
    )

    doc = parse_file(json_file, vault)

    assert doc is not None
    assert doc.source_kind == "code"
    assert doc.language == "json"
    assert doc.frontmatter.get("script_id") == "customscript_test"


# ── chunk_xml_document ──


def test_chunk_xml_document_by_customization_elements(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    xml_file = vault / "multi_customization.xml"
    xml_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<customizations>
  <customrecord scriptid="customrecord_a">
    <name>Record A</name>
  </customrecord>
  <workflow scriptid="customworkflow_b">
    <name>Workflow B</name>
  </workflow>
  <savedsearch scriptid="custsearch_c">
    <name>Search C</name>
  </savedsearch>
</customizations>
""",
        encoding="utf-8",
    )

    doc = parse_xml_file(xml_file, source_name="repo", repo_root=vault)
    assert doc is not None

    chunks = chunk_xml_document(doc)

    assert len(chunks) >= 3
    # Check that chunks have script_id metadata
    chunk_with_script_id = [c for c in chunks if c.metadata.get("script_id")]
    assert len(chunk_with_script_id) >= 1
    # All chunks should have source_kind='code'
    for chunk in chunks:
        assert chunk.source_kind == "code"
        assert chunk.source_name == "repo"


def test_chunk_xml_document_single_root_element(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    xml_file = vault / "single.xml"
    xml_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<customrecord scriptid="customrecord_single">
  <name>Single Record</name>
  <description>Just one record</description>
</customrecord>
""",
        encoding="utf-8",
    )

    doc = parse_xml_file(xml_file, source_name="repo", repo_root=vault)
    assert doc is not None

    chunks = chunk_xml_document(doc)

    assert len(chunks) >= 1
    assert chunks[0].source_kind == "code"
    assert "customrecord" in chunks[0].heading.lower()


def test_chunk_xml_document_fallback_for_unclean_content(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    # Use SourceDocument directly with non-XML body to test fallback
    from netsuite_rag_mcp.models import SourceDocument

    doc = SourceDocument(
        doc_id="test-xml-fallback",
        source_path="fallback.xml",
        absolute_path=vault / "fallback.xml",
        frontmatter={"language": "xml"},
        body="Line 1\nLine 2\nLine 3\nLine 4\nLine 5",
        updated_at="2026-01-01T00:00:00+00:00",
        source_kind="code",
        source_name="repo",
        file_hash="abc123",
        repo_root=str(vault),
        repo_relative_path="fallback.xml",
        language="xml",
    )

    chunks = chunk_xml_document(doc)

    # Should fall back to line-based chunking
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.source_kind == "code"
        assert chunk.source_name == "repo"


# ── chunk_json_config ──


def test_chunk_json_config_by_top_level_keys(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "config.json"
    json_file.write_text(
        """{
  "serverConfig": {
    "host": "localhost",
    "port": 8080
  },
  "clientConfig": {
    "timeout": 30,
    "retries": 3
  },
  "script_id": "customscript_config"
}
""",
        encoding="utf-8",
    )

    doc = parse_json_config(json_file, source_name="repo", repo_root=vault)
    assert doc is not None

    chunks = chunk_json_config(doc)

    assert len(chunks) >= 2
    headings = [c.heading for c in chunks]
    assert "serverConfig" in headings
    assert "clientConfig" in headings
    for chunk in chunks:
        assert chunk.source_kind == "code"
        assert chunk.source_name == "repo"


def test_chunk_json_config_array_elements(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "deployments.json"
    json_file.write_text(
        """{
  "deployments": [
    {"script_id": "customscript_a", "name": "Script A"},
    {"script_id": "customscript_b", "name": "Script B"}
  ]
}
""",
        encoding="utf-8",
    )

    doc = parse_json_config(json_file, source_name="repo", repo_root=vault)
    assert doc is not None

    chunks = chunk_json_config(doc)

    # Should chunk the array elements inside "deployments"
    assert len(chunks) >= 2
    # Check that script_id metadata is preserved
    chunks_with_script_id = [c for c in chunks if c.metadata.get("script_id")]
    assert len(chunks_with_script_id) >= 1


def test_chunk_json_config_simple_value_single_chunk(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    json_file = vault / "simple.json"
    json_file.write_text(
        """{"name": "Simple", "version": "1.0"}
""",
        encoding="utf-8",
    )

    doc = parse_json_config(json_file, source_name="repo", repo_root=vault)
    assert doc is not None

    chunks = chunk_json_config(doc)

    # Simple small value should create a single chunk
    assert len(chunks) == 1
    assert chunks[0].source_kind == "code"


# ── chunk_code_document: SuiteScript code chunking ──


def _make_code_document(
    body: str,
    frontmatter: dict | None = None,
    doc_id: str = "test_doc",
    source_path: str = "scripts/test.js",
    source_name: str = "test-repo",
    file_hash: str = "abc123",
) -> SourceDocument:
    """Helper to create a code SourceDocument for testing chunk_code_document."""
    fm = frontmatter or {}
    return SourceDocument(
        doc_id=doc_id,
        source_path=source_path,
        absolute_path=Path("/fake/path/test.js"),
        frontmatter=fm,
        body=body,
        updated_at="2026-01-01T00:00:00+00:00",
        source_kind="code",
        source_name=source_name,
        file_hash=file_hash,
    )


def test_chunk_code_document_creates_header_and_function_chunks():
    """Test that chunk_code_document creates a header chunk + function chunks."""
    code = """/**
 * @NScriptType Restlet
 * @NApiVersion 2.1
 * @NModuleScope SameAccount
 */

define(
  ["N/record", "N/search"],
  function (record, search) {
    function doGet(context) {
      var result = record.load({ type: context.request.params.type, id: context.request.params.id });
      return JSON.stringify(result);
    }

    function doPost(context) {
      var body = JSON.parse(context.request.body);
      var id = record.create({ type: body.type }).save();
      return JSON.stringify({ id: id });
    }

    return {
      get: doGet,
      post: doPost,
    };
  }
);
"""
    functions = [
        {"name": "doGet", "start_line": 10, "end_line": 12, "entry_point": True},
        {"name": "doPost", "start_line": 14, "end_line": 17, "entry_point": True},
    ]
    frontmatter = {
        "language": "javascript",
        "file_hash": "abc123",
        "script_type": "Restlet",
        "api_version": "2.1",
        "module_scope": "SameAccount",
        "dependencies": ["N/record", "N/search"],
        "functions": functions,
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    # Should have: 1 header chunk + 2 function chunks = 3 total
    assert len(chunks) == 3

    # Header chunk
    header = chunks[0]
    assert header.function_name == "(header)"
    assert header.heading == "Restlet"
    assert header.line_start == 1
    assert header.line_end == 9  # lines before first function
    assert header.source_kind == "code"
    assert header.source_name == "test-repo"
    assert header.file_hash == "abc123"
    assert header.metadata.get("entry_point") is False
    assert "@NScriptType Restlet" in header.text
    assert "define" in header.text

    # doGet entry point chunk
    get_chunk = chunks[1]
    assert get_chunk.function_name == "doGet"
    assert get_chunk.heading == "doGet"
    assert get_chunk.line_start == 10
    assert get_chunk.line_end == 12
    assert get_chunk.source_kind == "code"
    assert get_chunk.metadata.get("entry_point") is True

    # doPost entry point chunk
    post_chunk = chunks[2]
    assert post_chunk.function_name == "doPost"
    assert post_chunk.heading == "doPost"
    assert post_chunk.line_start == 14
    assert post_chunk.line_end == 17
    assert post_chunk.source_kind == "code"
    assert post_chunk.metadata.get("entry_point") is True


def test_chunk_code_document_entry_point_vs_helper():
    """Test that entry_point flag is correctly propagated in metadata."""
    code = """/**
 * @NScriptType UserEvent
 * @NApiVersion 2.1
 */

define(["N/record"], function (record) {
  function beforeLoad(context) {
    // entry point
  }

  function beforeSubmit(context) {
    // entry point
  }

  function afterSubmit(context) {
    // entry point
  }

  function _helper(context) {
    // not an entry point
  }

  return { beforeLoad: beforeLoad, beforeSubmit: beforeSubmit, afterSubmit: afterSubmit };
});
"""
    functions = [
        {"name": "beforeLoad", "start_line": 6, "end_line": 8, "entry_point": True},
        {"name": "beforeSubmit", "start_line": 10, "end_line": 12, "entry_point": True},
        {"name": "afterSubmit", "start_line": 14, "end_line": 16, "entry_point": True},
        {"name": "_helper", "start_line": 18, "end_line": 20, "entry_point": False},
    ]
    frontmatter = {
        "language": "javascript",
        "script_type": "UserEvent",
        "functions": functions,
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    # 1 header + 3 entry points + 1 helper = 5
    assert len(chunks) == 5

    entry_point_chunks = [c for c in chunks if c.metadata.get("entry_point") is True]
    helper_chunks = [c for c in chunks if c.metadata.get("entry_point") is False and c.function_name != "(header)"]
    header_chunks = [c for c in chunks if c.function_name == "(header)"]

    assert len(entry_point_chunks) == 3
    assert len(helper_chunks) == 1
    assert len(header_chunks) == 1

    # Helper chunk should have entry_point=False
    helper = helper_chunks[0]
    assert helper.function_name == "_helper"


def test_chunk_code_document_line_start_end_correctness():
    """Test that line_start/line_end correspond to actual text content."""
    code_lines = [
        "/**",
        " * @NScriptType Restlet",
        " * @NApiVersion 2.1",
        " */",
        "",
        "define(['N/record'], function (record) {",
        "  function get(context) {",
        "    return 'hello';",
        "  }",
        "  return { get: get };",
        "});",
    ]
    code = "\n".join(code_lines)

    functions = [
        {"name": "get", "start_line": 7, "end_line": 9, "entry_point": True},
    ]
    frontmatter = {
        "language": "javascript",
        "script_type": "Restlet",
        "functions": functions,
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    assert len(chunks) == 2

    # Header: lines 1 through 6 (before function at line 7)
    header = chunks[0]
    assert header.line_start == 1
    assert header.line_end == 6

    # Function chunk: lines 7 through 9
    func_chunk = chunks[1]
    assert func_chunk.line_start == 7
    assert func_chunk.line_end == 9

    # Verify the text extracted matches the expected lines
    header_lines = code.splitlines()
    header_text = "\n".join(header_lines[0:6])  # lines 1-6 (0-indexed: 0-5)
    assert header.text.strip() == header_text.strip()

    func_text = "\n".join(header_lines[6:9])  # lines 7-9 (0-indexed: 6-8)
    assert func_chunk.text.strip() == func_text.strip()


def test_chunk_code_document_metadata_propagation():
    """Test that frontmatter metadata is propagated to all chunks."""
    code_lines = [
        "/**",
        " * @NScriptType Restlet",
        " * @NApiVersion 2.1",
        " */",
        "",
        "define([], function() { function get(ctx) {} return { get: get }; });",
        "function get(context) { return 1; }",
    ]
    code = "\n".join(code_lines)
    functions = [
        {"name": "get", "start_line": 7, "end_line": 7, "entry_point": True},
    ]
    frontmatter = {
        "language": "javascript",
        "script_type": "Restlet",
        "api_version": "2.1",
        "dependencies": ["N/record"],
        "project": "my-project",
        "related_objects": ["salesorder"],
        "related_scripts": ["customscript_foo"],
        "functions": functions,
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    # 1 header + 1 function = 2
    assert len(chunks) == 2

    # All chunks should have these metadata fields
    for chunk in chunks:
        assert chunk.metadata.get("script_type") == "Restlet"
        assert chunk.metadata.get("api_version") == "2.1"
        assert chunk.source_kind == "code"
        assert chunk.source_name == "test-repo"
        assert chunk.file_hash == "abc123"


def test_chunk_code_document_fallback_line_chunking():
    """Test fallback chunking when frontmatter has no functions."""
    # Create a document without functions metadata
    lines = [f"// line {i}" for i in range(1, 121)]
    code = "\n".join(lines)

    frontmatter = {
        "language": "javascript",
        "file_hash": "abc123",
        # No 'functions' key
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    # Fallback should produce line-based chunks
    assert len(chunks) >= 2  # 120 lines / 50 per chunk = 3 chunks

    # Each fallback chunk should have function_name like (chunk_0), (chunk_1), etc.
    for i, chunk in enumerate(chunks):
        assert chunk.function_name == f"(chunk_{i})"
        assert chunk.line_start > 0
        assert chunk.line_end >= chunk.line_start
        assert chunk.source_kind == "code"

    # Verify total lines covered
    total_lines_covered = chunks[-1].line_end
    assert total_lines_covered == len(lines)


def test_chunk_code_document_fallback_empty_functions():
    """Test fallback chunking when frontmatter has empty functions list."""
    lines = [f"var x = {i};" for i in range(1, 101)]
    code = "\n".join(lines)

    frontmatter = {
        "language": "javascript",
        "functions": [],  # Empty functions list triggers fallback
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.source_kind == "code"


def test_chunk_code_document_chunk_id_pattern():
    """Test that chunk IDs follow the {doc_id}_code_{chunk_index} pattern."""
    code = "// header\nfunction foo() { return 1; }\nfunction bar() { return 2; }"
    functions = [
        {"name": "foo", "start_line": 2, "end_line": 2, "entry_point": False},
        {"name": "bar", "start_line": 3, "end_line": 3, "entry_point": False},
    ]
    frontmatter = {"language": "javascript", "functions": functions}

    doc = _make_code_document(body=code, frontmatter=frontmatter, doc_id="abc123")
    chunks = chunk_code_document(doc)

    for i, chunk in enumerate(chunks):
        assert chunk.id == f"abc123_code_{i}"


def test_chunk_code_document_only_processes_code_source_kind():
    """Test that chunk_code_document raises or returns empty for non-code documents."""
    from netsuite_rag_mcp.models import SourceDocument

    note_doc = SourceDocument(
        doc_id="note1",
        source_path="notes/test.md",
        absolute_path=Path("/fake/test.md"),
        frontmatter={"type": "note"},
        body="# Hello\n\nWorld",
        updated_at="2026-01-01T00:00:00+00:00",
        source_kind="note",  # NOT 'code'
    )

    chunks = chunk_code_document(note_doc)
    # Should return empty list for non-code documents
    assert chunks == []


def test_chunk_code_document_handles_single_function_file():
    """Test a file with only one function (header + one function)."""
    code = """/**
 * @NScriptType Scheduled
 * @NApiVersion 2.1
 */

define([], function () {
  function execute(context) {
    log.debug('running scheduled script');
  }

  return { execute: execute };
});
"""
    functions = [
        {"name": "execute", "start_line": 7, "end_line": 9, "entry_point": True},
    ]
    frontmatter = {
        "language": "javascript",
        "script_type": "Scheduled",
        "functions": functions,
    }

    doc = _make_code_document(body=code, frontmatter=frontmatter)
    chunks = chunk_code_document(doc)

    assert len(chunks) == 2  # header + 1 function

    header = chunks[0]
    assert header.function_name == "(header)"
    assert header.heading == "Scheduled"

    func = chunks[1]
    assert func.function_name == "execute"
    assert func.heading == "execute"
    assert func.metadata.get("entry_point") is True