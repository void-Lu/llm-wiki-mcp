"""Chunk XML and JSON SourceDocument objects into Chunk objects for RAG indexing."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from netsuite_rag_mcp.models import Chunk, SourceDocument

# NetSuite customization element tags used for top-level chunking
NETSUITE_CUSTOMIZATION_TAGS = frozenset({
    "customrecord",
    "customsegment",
    "workflow",
    "savedsearch",
    "script",
    "scriptdeployment",
    "customfield",
    "customlist",
    "customtab",
    "customform",
    "suitelet",
    "restlet",
    "userevent",
    "mapreduce",
    "scheduled",
    "clientscript",
    "portlet",
    "bundleinstallationscript",
    "massupdate",
    "csvimport",
})

SCRIPT_ID_ATTRS = ("scriptid", "script_id")
DEPLOYMENT_ID_ATTRS = ("deploymentid", "deployment_id")

# Minimum number of lines for a fallback line-based chunk
FALLBACK_CHUNK_LINES = 80


def chunk_xml_document(document: SourceDocument) -> list[Chunk]:
    """Chunk an XML SourceDocument by major customization elements.

    For NetSuite customization XMLs, each major element (customrecord, workflow, etc.)
    becomes a chunk.  For generic XML, chunk by top-level children of the root.
    Falls back to line-based chunking if XML parsing fails.
    """
    try:
        root = ET.fromstring(document.body)
    except ET.ParseError:
        return _fallback_line_chunks(document, "xml")

    chunks: list[Chunk] = []

    # Check if root itself is a customization element
    root_is_customization = root.tag.lower() in NETSUITE_CUSTOMIZATION_TAGS

    # Check if any direct children are customization elements
    customization_children = [
        child for child in root
        if child.tag.lower() in NETSUITE_CUSTOMIZATION_TAGS
    ]

    if root_is_customization and not customization_children:
        # Single root customization: one chunk for the whole document
        chunk = _make_xml_chunk(document, root, 0)
        chunks.append(chunk)
    elif customization_children:
        # Multiple customization elements: one chunk per customization
        for idx, child in enumerate(customization_children):
            chunk = _make_xml_chunk(document, child, idx, parent_tag=root.tag)
            chunks.append(chunk)
    else:
        # Generic XML: chunk by top-level children of root
        top_children = list(root)
        if top_children:
            for idx, child in enumerate(top_children):
                chunk = _make_xml_chunk(document, child, idx, parent_tag=root.tag)
                chunks.append(chunk)
        else:
            # Root has no children; make one chunk from the whole document
            chunk = _make_xml_chunk(document, root, 0)
            chunks.append(chunk)

    # If no chunks were produced, fall back to line-based
    if not chunks:
        return _fallback_line_chunks(document, "xml")

    return chunks


def _make_xml_chunk(
    document: SourceDocument,
    element: ET.Element,
    index: int,
    parent_tag: str | None = None,
) -> Chunk:
    """Create a Chunk from an XML element."""
    tag = element.tag

    # Build heading: tag name + script_id if available
    script_id = None
    for attr in SCRIPT_ID_ATTRS:
        val = element.get(attr)
        if val:
            script_id = val
            break

    heading = f"{tag}/{script_id}" if script_id else tag

    # Build metadata
    metadata = dict(document.frontmatter)
    metadata.update({
        "doc_id": document.doc_id,
        "chunk_index": index,
        "source_path": document.source_path,
        "heading": heading,
        "updated_at": document.updated_at,
    })

    if script_id:
        metadata["script_id"] = script_id

    deployment_id = None
    for attr in DEPLOYMENT_ID_ATTRS:
        val = element.get(attr)
        if val:
            deployment_id = val
            break
    if deployment_id:
        metadata["deployment_id"] = deployment_id

    record_type = tag.lower()
    if record_type in NETSUITE_CUSTOMIZATION_TAGS:
        metadata["record_type"] = record_type

    if parent_tag:
        metadata["parent_tag"] = parent_tag

    # Element text content (pretty-printed XML)
    text = _element_to_string(element)

    return Chunk(
        id=f"{document.doc_id}:{index}",
        doc_id=document.doc_id,
        chunk_index=index,
        source_path=document.source_path,
        heading=heading,
        text=text,
        metadata=metadata,
        source_kind="code",
        source_name=document.source_name,
        file_hash=document.file_hash,
    )


def _element_to_string(element: ET.Element) -> str:
    """Pretty-print an XML element to a string."""
    ET.indent(element)
    return ET.tostring(element, encoding="unicode")


def chunk_json_config(document: SourceDocument) -> list[Chunk]:
    """Chunk a JSON config SourceDocument by top-level keys or array elements.

    For object JSON: each top-level key with a dict or list value becomes a chunk.
    For array-of-objects JSON: each object is a chunk.
    Small/simple values produce a single chunk.
    """
    try:
        data = json.loads(document.body)
    except (json.JSONDecodeError, ValueError):
        return _fallback_line_chunks(document, "json")

    if isinstance(data, dict):
        return _chunk_json_object(document, data)
    elif isinstance(data, list):
        return _chunk_json_array(document, data)
    else:
        # Simple value: single chunk
        return [_make_json_chunk(document, document.body, 0, heading=document.source_path)]


def _chunk_json_object(document: SourceDocument, data: dict) -> list[Chunk]:
    """Chunk a JSON object by top-level keys."""
    chunks: list[Chunk] = []
    index = 0

    # Separate complex values (dicts and lists) from simple values
    complex_keys: list[str] = []
    simple_data: dict[str, Any] = {}

    for key, value in data.items():
        if isinstance(value, (dict, list)):
            complex_keys.append(key)
        else:
            simple_data[key] = value

    # Create chunks for complex values
    for key in complex_keys:
        value = data[key]
        if isinstance(value, list):
            # Array value: chunk each object element separately
            obj_items = [item for item in value if isinstance(item, dict)]
            if obj_items:
                for i, item in enumerate(obj_items):
                    item_text = json.dumps(item, indent=2, ensure_ascii=False)
                    metadata_script_id = item.get("script_id", document.frontmatter.get("script_id", ""))
                    heading = f"{key}[{i}]"
                    if metadata_script_id:
                        heading = f"{key}[{i}]/{metadata_script_id}"
                    chunk = _make_json_chunk(
                        document, item_text, index,
                        heading=heading,
                        extra_metadata={"script_id": metadata_script_id} if metadata_script_id else {},
                    )
                    chunks.append(chunk)
                    index += 1
            else:
                # Array of non-objects
                item_text = json.dumps({key: value}, indent=2, ensure_ascii=False)
                chunk = _make_json_chunk(document, item_text, index, heading=key)
                chunks.append(chunk)
                index += 1
        elif isinstance(value, dict):
            item_text = json.dumps({key: value}, indent=2, ensure_ascii=False)
            chunk = _make_json_chunk(document, item_text, index, heading=key)
            chunks.append(chunk)
            index += 1

    # Remaining simple values: if there are any, create one chunk for them
    if simple_data:
        simple_text = json.dumps(simple_data, indent=2, ensure_ascii=False)
        # But if there were no complex chunks, include everything as one chunk
        if not chunks:
            full_text = json.dumps(data, indent=2, ensure_ascii=False)
            chunk = _make_json_chunk(document, full_text, 0, heading=document.source_path)
            return [chunk]
        else:
            chunk = _make_json_chunk(document, simple_text, index, heading="_metadata")
            chunks.append(chunk)

    # If no chunks were produced, fall back
    if not chunks:
        return _fallback_line_chunks(document, "json")

    return chunks


def _chunk_json_array(document: SourceDocument, data: list) -> list[Chunk]:
    """Chunk a JSON array: each dict element becomes a chunk."""
    chunks: list[Chunk] = []

    for i, item in enumerate(data):
        if isinstance(item, dict):
            item_text = json.dumps(item, indent=2, ensure_ascii=False)
            script_id = item.get("script_id", "")
            heading = f"[{i}]"
            if script_id:
                heading = f"[{i}]/{script_id}"
            extra = {"script_id": script_id} if script_id else {}
            chunk = _make_json_chunk(
                document, item_text, i, heading=heading,
                extra_metadata=extra,
            )
            chunks.append(chunk)

    if not chunks:
        # Empty or non-dict array: single chunk
        text = json.dumps(data, indent=2, ensure_ascii=False)
        chunk = _make_json_chunk(document, text, 0, heading=document.source_path)
        return [chunk]

    return chunks


def _make_json_chunk(
    document: SourceDocument,
    text: str,
    index: int,
    heading: str,
    extra_metadata: dict[str, Any] | None = None,
) -> Chunk:
    """Create a Chunk from JSON content."""
    metadata = dict(document.frontmatter)
    metadata.update({
        "doc_id": document.doc_id,
        "chunk_index": index,
        "source_path": document.source_path,
        "heading": heading,
        "updated_at": document.updated_at,
    })
    if extra_metadata:
        metadata.update(extra_metadata)

    return Chunk(
        id=f"{document.doc_id}:{index}",
        doc_id=document.doc_id,
        chunk_index=index,
        source_path=document.source_path,
        heading=heading,
        text=text,
        metadata=metadata,
        source_kind="code",
        source_name=document.source_name,
        file_hash=document.file_hash,
    )


def _fallback_line_chunks(document: SourceDocument, language: str) -> list[Chunk]:
    """Fall back to line-based chunking when structured parsing fails."""
    lines = document.body.splitlines()
    total_lines = len(lines)

    if total_lines == 0:
        return [_make_line_chunk(document, 0, 0, total_lines, language)]

    # Use large chunks for fallback; at least one chunk
    chunk_size = FALLBACK_CHUNK_LINES
    chunks: list[Chunk] = []
    idx = 0
    start = 0

    while start < total_lines:
        end = min(start + chunk_size, total_lines)
        chunk = _make_line_chunk(document, idx, start, end, language)
        chunks.append(chunk)
        idx += 1
        start = end

    return chunks


def _make_line_chunk(
    document: SourceDocument,
    index: int,
    line_start: int,
    line_end: int,
    language: str,
) -> Chunk:
    """Create a Chunk from a line range."""
    lines = document.body.splitlines()
    text = "\n".join(lines[line_start:line_end])
    heading = f"L{line_start + 1}-{line_end}" if line_end > line_start + 1 else f"L{line_start + 1}"

    metadata = dict(document.frontmatter)
    metadata.update({
        "doc_id": document.doc_id,
        "chunk_index": index,
        "source_path": document.source_path,
        "heading": heading,
        "updated_at": document.updated_at,
        "language": language,
    })

    return Chunk(
        id=f"{document.doc_id}:{index}",
        doc_id=document.doc_id,
        chunk_index=index,
        source_path=document.source_path,
        heading=heading,
        text=text,
        metadata=metadata,
        source_kind="code",
        source_name=document.source_name,
        file_hash=document.file_hash,
        line_start=line_start + 1,  # 1-based
        line_end=line_end,  # 1-based, inclusive
    )