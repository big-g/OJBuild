"""Bounded structural validation of untrusted MCP tool contracts.

This is not a JSON Schema metaschema validator or an argument validator. No
references are fetched and server annotations never grant local capabilities.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from openjarvis.tools._stubs import ToolSpec

MAX_PAGES = 32
MAX_TOOLS = 1000
MAX_TOOL_BYTES = 262_144
MAX_CATALOG_BYTES = 2_097_152
MAX_JSON_NODES = 20_000
MAX_JSON_DEPTH = 32


def _json_contract(value: Any) -> bytes:
    """Bound decoded structures before copying, encoding or hashing them."""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("MCP tool contract exceeds structural limits")
        if isinstance(item, dict):
            if len(item) > MAX_JSON_NODES - nodes:
                raise ValueError("MCP tool contract exceeds structural limits")
            if any(not isinstance(key, str) for key in item):
                raise ValueError("MCP tool contract must use JSON string keys")
            if any(len(key) > MAX_TOOL_BYTES for key in item):
                raise ValueError("MCP tool contract exceeds size limit")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > MAX_JSON_NODES - nodes:
                raise ValueError("MCP tool contract exceeds structural limits")
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and len(item) > MAX_TOOL_BYTES:
            raise ValueError("MCP tool contract exceeds size limit")
        elif item is None or isinstance(item, (str, bool, int)):
            pass
        elif isinstance(item, float) and math.isfinite(item):
            pass
        else:
            raise ValueError("MCP tool contract contains a non-JSON value")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("MCP tool contract is not valid JSON") from exc
    if len(encoded) > MAX_TOOL_BYTES:
        raise ValueError("MCP tool contract exceeds size limit")
    return encoded


def _schema(schema: Any, *, root: bool = False) -> None:
    if isinstance(schema, bool) and not root:
        return
    if not isinstance(schema, dict):
        raise ValueError("MCP input schema must be an object")
    kind = schema.get("type")
    types = {"object", "array", "string", "number", "integer", "boolean", "null"}
    if root and kind not in (None, "object"):
        raise ValueError("MCP input schema must describe object arguments")
    if kind is not None and not (
        isinstance(kind, str) and kind in types
        or isinstance(kind, list) and kind
        and all(isinstance(t, str) and t in types for t in kind)
        and len(set(kind)) == len(kind)
    ):
        raise ValueError("MCP input schema has an invalid type")
    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        if keyword in schema and (
            not isinstance(schema[keyword], str)
            or not schema[keyword].startswith("#")
        ):
            raise ValueError("MCP input schema references must be local")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(name, str) for name in required
        ) or len(set(required)) != len(required):
            raise ValueError("MCP input schema has invalid required fields")
    for keyword in ("properties", "patternProperties", "$defs", "definitions"):
        if keyword in schema:
            children = schema[keyword]
            if not isinstance(children, dict):
                raise ValueError("MCP input schema has an invalid schema map")
            for child in children.values():
                _schema(child)
    for keyword in (
        "additionalProperties", "unevaluatedProperties", "items", "contains",
        "not", "if", "then", "else", "propertyNames", "unevaluatedItems",
    ):
        if keyword in schema:
            _schema(schema[keyword])
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if keyword in schema:
            children = schema[keyword]
            if not isinstance(children, list):
                raise ValueError("MCP input schema has an invalid schema list")
            for child in children:
                _schema(child)


def parse_tool(tool: Any) -> tuple[ToolSpec, int]:
    """Return an isolated spec and its canonical contract size."""
    if not isinstance(tool, dict):
        raise ValueError("MCP catalog tool must be an object")
    encoded = _json_contract(tool)
    name = tool.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("MCP catalog tool has an invalid name")
    description = tool.get("description", "")
    if not isinstance(description, str):
        raise ValueError("MCP catalog tool has an invalid description")
    # Empty schemas from older OpenJarvis servers remain supported. A missing
    # schema, or an explicit non-object root, is never accepted.
    schema = tool.get("inputSchema")
    _schema(schema, root=True)
    annotations = tool.get("annotations", {})
    if not isinstance(annotations, dict):
        raise ValueError("MCP tool annotations must be an object")
    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        if key in annotations and not isinstance(annotations[key], bool):
            raise ValueError("MCP tool annotation hint must be boolean")
    if "title" in annotations and not isinstance(annotations["title"], str):
        raise ValueError("MCP tool annotation title must be a string")
    return ToolSpec(
        name=name,
        description=description,
        parameters=deepcopy(schema),
        timeout_seconds=600.0,
        metadata={
            "mcp_contract_version": "catalog-v1",
            "mcp_contract_sha256": hashlib.sha256(encoded).hexdigest(),
            "mcp_annotations_untrusted": deepcopy(annotations),
        },
    ), len(encoded)
