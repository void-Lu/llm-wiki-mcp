"""Parse NetSuite customization XML and JSON config files into SourceDocument objects."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.models import SourceDocument

# NetSuite customization element tags that carry semantic meaning
NETSUITE_CUSTOMIZATION_TAGS = frozenset({
    "customrecord",
    "customsegment",
    "workflow",
    "savedsearch",
    "script",
    "scriptdeployment",
    "clientscript",
    "mapreducescript",
    "scheduledscript",
    "usereventscript",
    "customfield",
    "customlist",
    "customtab",
    "customform",
    "suitelet",
    "restlet",
    "userevent",
    "mapreduce",
    "scheduled",
    "portlet",
    "bundleinstallationscript",
    "massupdate",
    "csvimport",
})

# Attributes that NetSuite customization XMLs use for identification
SCRIPT_ID_ATTRS = ("scriptid", "script_id")
DEPLOYMENT_ID_ATTRS = ("deploymentid", "deployment_id")


def parse_xml_file(
    path: Path,
    source_name: str = "",
    repo_root: Path | None = None,
) -> SourceDocument | None:
    """Parse a NetSuite customization XML file into a SourceDocument."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    frontmatter: dict[str, Any] = {"language": "xml"}
    file_hash = hashlib.sha256(raw).hexdigest()
    frontmatter["file_hash"] = file_hash

    # Extract NetSuite-specific metadata
    _extract_xml_metadata(root, frontmatter)

    # Build paths
    effective_root = repo_root or path.parent
    relative_path = path.resolve().relative_to(effective_root.resolve()).as_posix()
    doc_id = hashlib.sha1(f"{source_name}:{relative_path}".lower().encode("utf-8")).hexdigest()
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    return SourceDocument(
        doc_id=doc_id,
        source_path=relative_path,
        absolute_path=path,
        frontmatter=frontmatter,
        body=text,
        updated_at=updated_at,
        source_kind="code",
        source_name=source_name,
        file_hash=file_hash,
        repo_root=str(effective_root),
        repo_relative_path=relative_path,
        language="xml",
    )


def _extract_xml_metadata(element: ET.Element, frontmatter: dict[str, Any]) -> None:
    """Extract NetSuite-specific metadata from XML element tree."""
    # Extract script_id — search the root and its first-level children
    script_id = _find_attr_recursive(element, SCRIPT_ID_ATTRS)
    if script_id:
        frontmatter["script_id"] = script_id

    # Extract deployment_id — search the root and its first-level children
    deployment_id = _find_attr_recursive(element, DEPLOYMENT_ID_ATTRS)
    if deployment_id:
        frontmatter["deployment_id"] = deployment_id

    script_file = _extract_script_file(element)
    if script_file:
        frontmatter["script_file"] = script_file

    deployment_ids = _extract_child_script_ids(element, "scriptdeployment")
    if deployment_ids:
        frontmatter["deployment_ids"] = deployment_ids
        frontmatter.setdefault("deployment_id", deployment_ids[0])

    script_parameters = _extract_child_script_ids(element, "scriptcustomfield")
    if script_parameters:
        frontmatter["script_parameters"] = script_parameters

    # Identify the record type from container element tags
    record_type = _identify_record_type(element)
    if record_type:
        frontmatter["record_type"] = record_type

    # Extract name and description from direct children
    name_el = element.find("name")
    if name_el is not None and name_el.text:
        frontmatter["name"] = name_el.text.strip()

    desc_el = element.find("description")
    if desc_el is not None and desc_el.text:
        frontmatter["description"] = desc_el.text.strip()

    # Also check first-level children for name/description if root doesn't have them
    for child in element:
        tag_lower = child.tag.lower()
        if tag_lower in NETSUITE_CUSTOMIZATION_TAGS:
            if "name" not in frontmatter:
                child_name = child.find("name")
                if child_name is not None and child_name.text:
                    frontmatter["name"] = child_name.text.strip()
            if "description" not in frontmatter:
                child_desc = child.find("description")
                if child_desc is not None and child_desc.text:
                    frontmatter["description"] = child_desc.text.strip()
            if "script_id" not in frontmatter:
                for attr in SCRIPT_ID_ATTRS:
                    val = child.get(attr)
                    if val:
                        frontmatter["script_id"] = val
                        break
            if "deployment_id" not in frontmatter:
                for attr in DEPLOYMENT_ID_ATTRS:
                    val = child.get(attr)
                    if val:
                        frontmatter["deployment_id"] = val
                        break


def _find_attr_recursive(element: ET.Element, attr_names: tuple[str, ...], max_depth: int = 2) -> str | None:
    """Find an attribute value by searching the element and its children up to max_depth."""
    for attr in attr_names:
        val = element.get(attr)
        if val:
            return val

    if max_depth > 0:
        for child in element:
            result = _find_attr_recursive(child, attr_names, max_depth - 1)
            if result:
                return result

    return None


def _identify_record_type(element: ET.Element) -> str | None:
    """Identify the NetSuite record type from element tags.

    Checks the root element first, then its direct children.
    Returns the lowercase tag if it matches a known NetSuite customization type.
    """
    # Check root element
    tag_lower = element.tag.lower()
    if tag_lower in NETSUITE_CUSTOMIZATION_TAGS:
        return tag_lower

    # Check direct children
    for child in element:
        child_tag = child.tag.lower()
        if child_tag in NETSUITE_CUSTOMIZATION_TAGS:
            return child_tag

    return None


def _extract_script_file(element: ET.Element) -> str | None:
    script_file = element.findtext("scriptfile")
    if not script_file:
        return None
    normalized = script_file.strip().strip("[]").strip().replace("\\", "/")
    normalized = normalized.lstrip("/")
    if normalized.startswith("SuiteScripts/"):
        return f"src/FileCabinet/{normalized}"
    return normalized or None


def _extract_child_script_ids(element: ET.Element, tag: str) -> list[str]:
    values: list[str] = []
    for child in element.iter(tag):
        value = child.get("scriptid") or child.get("script_id")
        if value and value not in values:
            values.append(value)
    return values


def parse_json_config(
    path: Path,
    source_name: str = "",
    repo_root: Path | None = None,
) -> SourceDocument | None:
    """Parse a JSON config/manifest file into a SourceDocument."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        # Wrap non-dict values
        data = {"_value": data}

    frontmatter: dict[str, Any] = {"language": "json"}
    file_hash = hashlib.sha256(raw).hexdigest()
    frontmatter["file_hash"] = file_hash

    # Extract top-level keys
    top_keys = list(data.keys())
    if top_keys:
        frontmatter["keys"] = top_keys

    # Extract script_id
    if "script_id" in data and isinstance(data["script_id"], str):
        frontmatter["script_id"] = data["script_id"]

    # Extract name (prefer 'name', fall back to 'label')
    if "name" in data and isinstance(data["name"], str):
        frontmatter["name"] = data["name"]
    elif "label" in data and isinstance(data["label"], str):
        frontmatter["name"] = data["label"]

    # Extract record_type (prefer 'record_type', fall back to 'type')
    if "record_type" in data and isinstance(data["record_type"], str):
        frontmatter["record_type"] = data["record_type"]
    elif "type" in data and isinstance(data["type"], str):
        frontmatter["record_type"] = data["type"]

    # Build paths
    effective_root = repo_root or path.parent
    relative_path = path.resolve().relative_to(effective_root.resolve()).as_posix()
    doc_id = hashlib.sha1(f"{source_name}:{relative_path}".lower().encode("utf-8")).hexdigest()
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    return SourceDocument(
        doc_id=doc_id,
        source_path=relative_path,
        absolute_path=path,
        frontmatter=frontmatter,
        body=text,
        updated_at=updated_at,
        source_kind="code",
        source_name=source_name,
        file_hash=file_hash,
        repo_root=str(effective_root),
        repo_relative_path=relative_path,
        language="json",
    )